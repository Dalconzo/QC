#!/usr/bin/env python3
"""
camera-recorder.py

Continuous camera recorder for Hamilton runs.

The recorder now creates one finalized MP4 per HxRun session instead of fixed
time segments. Once the recording stops, it looks in the configured Hamilton
log directory for the trace file whose last-write time is closest to the stop
time and writes a manifest that pairs the video with that trace file.

This script still prefers ffmpeg for capture and falls back to OpenCV if
ffmpeg is unavailable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, get_profile, load_effective_config, validate_config
from camera_source import is_numeric_source, to_ffmpeg_input
from local_compaction import apply_compaction_metadata_to_segments, generate_local_compaction
from local_retention import build_local_retention_payload
from replay_manifest import (
    DEFAULT_FULL_DETAIL_RETENTION,
    DEFAULT_LOCAL_COMPACTION,
    DEFAULT_REPLAY_MODE,
    DEFAULT_STORAGE_TIER,
    REPLAY_MANIFEST_CAPABILITIES,
    REPLAY_MANIFEST_VERSION,
    normalize_replay_manifest_payload,
)
from replay_tags import derive_run_tags
from trace_replay import build_trace_replay_summary

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDER_REARM_EXIT_CODE = 20


@dataclass(frozen=True)
class TraceMatch:
    """Describe the trace file chosen for one recorded segment."""

    path: Path
    delta_sec: float
    modified_at: dt.datetime


@dataclass(frozen=True)
class RunWindow:
    """Track raw capture timing separately from the logical assay window."""

    capture_started_at: dt.datetime
    capture_stopped_at: dt.datetime
    logical_started_at: dt.datetime
    logical_stopped_at: dt.datetime
    logical_stop_reason: str
    logical_stop_details: dict | None = None

    def capture_duration_sec(self) -> float:
        return round((self.capture_stopped_at - self.capture_started_at).total_seconds(), 3)

    def logical_duration_sec(self) -> float:
        return round((self.logical_stopped_at - self.logical_started_at).total_seconds(), 3)

    def logical_start_offset_sec(self) -> float:
        return round((self.logical_started_at - self.capture_started_at).total_seconds(), 3)

    def logical_stop_offset_sec(self) -> float:
        return round((self.logical_stopped_at - self.capture_started_at).total_seconds(), 3)


def find_ffmpeg(explicit: str | None = None) -> tuple[bool, str | None]:
    """Locate ffmpeg, preferring an explicit or repo-local copy."""
    if explicit:
        explicit_path = Path(explicit)
        if explicit_path.exists():
            return True, str(explicit_path)

    here = Path(__file__).parent
    for candidate in (
        here / "ffmpeg.exe",
        here / "dist" / "ffmpeg.exe",
        Path.cwd() / "cameras" / "ffmpeg.exe",
        Path.cwd() / "cameras" / "dist" / "ffmpeg.exe",
    ):
        if candidate.exists():
            return True, str(candidate)

    exe = shutil.which("ffmpeg")
    return exe is not None, exe


def list_dshow_devices(ffmpeg_bin: str) -> list[dict]:
    """Return DirectShow video devices using ffmpeg probe output."""
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
            capture_output=True,
            text=True,
            check=False,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        lines = out.splitlines()
        devices: list[dict] = []
        pending: dict | None = None
        import re

        re_name = re.compile(r'\] "([^"]+)" \(video\)')
        re_alt = re.compile(r'Alternative name "([^"]+)"')
        for line in lines:
            match_name = re_name.search(line)
            if match_name:
                if pending:
                    devices.append(pending)
                pending = {"name": match_name.group(1), "alt": None}
                continue

            if pending:
                match_alt = re_alt.search(line)
                if match_alt:
                    pending["alt"] = match_alt.group(1)

        if pending:
            devices.append(pending)

        for index, device in enumerate(devices):
            device["index"] = index
        return devices
    except Exception:
        return []


def ts_now() -> dt.datetime:
    return dt.datetime.now()


def ts_label(timestamp: dt.datetime) -> str:
    return timestamp.strftime("%Y%m%d_%H%M%S")


def emit_log(message: str, *, log_path: Path | None = None, is_error: bool = False) -> None:
    """Write recorder diagnostics to stdout/stderr and an optional log file.

    The live recorder is often run from an operator shell. Mirroring the same
    messages into a log file gives us a durable audit trail for startup gating,
    backend selection, stop reasons, and trace pairing without requiring the
    operator to keep the original terminal open.
    """
    stream = sys.stderr if is_error else sys.stdout
    print(message, file=stream)
    if not log_path:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{dt.datetime.now().isoformat()} {message}\n"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _normalize_proc_names(proc_names: str | list[str] | tuple[str, ...]) -> list[str]:
    """Normalize process selectors into executable names."""
    if isinstance(proc_names, (list, tuple)):
        items = [str(item).strip() for item in proc_names]
    else:
        text = str(proc_names or "").replace(";", ",")
        items = [item.strip() for item in text.split(",")]
    return [item for item in items if item]


def _is_any_proc_running(proc_names: str | list[str] | tuple[str, ...]) -> bool:
    """Return True when any named executable is running."""
    normalized = _normalize_proc_names(proc_names)
    if not normalized:
        return True
    try:
        import psutil  # type: ignore

        for proc in psutil.process_iter(attrs=["name"]):
            try:
                proc_name = (proc.info.get("name") or "").lower()
                if any(proc_name == expected.lower() for expected in normalized):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        try:
            result = subprocess.run(["tasklist"], capture_output=True, text=True, check=False)
            output = (result.stdout or "").lower()
            return any(expected.lower() in output for expected in normalized)
        except Exception:
            return True


def wait_for_process_start(
    proc_names: str | list[str] | tuple[str, ...],
    *,
    poll_sec: float,
    timeout_sec: int,
    verbose: bool = False,
    log_path: Path | None = None,
) -> bool:
    """Block until one of the target processes appears."""
    normalized = _normalize_proc_names(proc_names)
    if not normalized:
        return True

    started = time.monotonic()
    last_status = 0.0
    while True:
        if _is_any_proc_running(normalized):
            if verbose:
                emit_log(f"[recorder] Startup gate satisfied by process: {', '.join(normalized)}", log_path=log_path)
            return True

        if timeout_sec > 0 and (time.monotonic() - started) >= timeout_sec:
            if verbose:
                emit_log(f"[recorder] Startup gate timed out waiting for: {', '.join(normalized)}", log_path=log_path)
            return False

        now = time.monotonic()
        if verbose and (now - last_status) >= max(1.0, poll_sec):
            emit_log(f"[recorder] Waiting for process start: {', '.join(normalized)}", log_path=log_path)
            last_status = now
        time.sleep(max(0.25, poll_sec))


def choose_trace_file(
    log_dir: Path,
    log_glob: str,
    stop_time: dt.datetime,
) -> TraceMatch | None:
    """Pick the trace file whose last-write time is closest to recorder stop.

    Hamilton only flushes the `.trc` to disk when the method completes, so the
    most reliable pairing signal available right after HxRun exits is the trace
    file modification time near recorder shutdown.
    """
    if not log_dir.exists():
        return None

    patterns = [pattern.strip() for pattern in str(log_glob or "*.trc").split(";") if pattern.strip()]
    candidates: list[tuple[float, Path, dt.datetime]] = []
    for pattern in patterns:
        for path in log_dir.glob(pattern):
            if not path.is_file():
                continue
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime)
            delta = abs((modified - stop_time).total_seconds())
            candidates.append((delta, path, modified))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1].name.lower()))
    nearest_delta, nearest_path, nearest_modified = candidates[0]
    return TraceMatch(path=nearest_path, delta_sec=nearest_delta, modified_at=nearest_modified)


def snapshot_log_files(log_dir: Path, log_glob: str) -> dict[str, float]:
    """Capture the current mtime view of matching Hamilton log files."""
    if not log_dir.exists():
        return {}
    snapshot: dict[str, float] = {}
    patterns = [pattern.strip() for pattern in str(log_glob or "*.trc").split(";") if pattern.strip()]
    for pattern in patterns:
        for path in log_dir.glob(pattern):
            if not path.is_file():
                continue
            try:
                snapshot[str(path.resolve())] = path.stat().st_mtime
            except Exception:
                continue
    return snapshot


def detect_log_activity(previous: dict[str, float], current: dict[str, float]) -> list[dict]:
    """Return files that are new or whose mtime advanced since the prior snapshot."""
    changes: list[dict] = []
    for path, mtime in current.items():
        prior = previous.get(path)
        if prior is None or mtime > prior:
            changes.append(
                {
                    "path": path,
                    "modified_at_local": dt.datetime.fromtimestamp(mtime).isoformat(),
                    "change_type": "created" if prior is None else "updated",
                }
            )
    changes.sort(key=lambda item: (item["modified_at_local"], item["path"]))
    return changes


@dataclass(frozen=True)
class CaptureResult:
    """Describe one completed recorder segment and any mid-session trigger details."""

    video_path: Path
    capture_started_at: dt.datetime
    capture_stopped_at: dt.datetime
    stop_reason: str
    logical_stopped_at: dt.datetime | None = None
    logical_stop_reason: str | None = None
    logical_stop_details: dict | None = None
    trace_activity_seen: bool = False


def build_run_window(
    *,
    capture_started_at: dt.datetime,
    capture_stopped_at: dt.datetime,
    stop_reason: str,
    logical_started_at: dt.datetime | None = None,
    logical_stopped_at: dt.datetime | None = None,
    logical_stop_reason: str | None = None,
    logical_stop_details: dict | None = None,
) -> RunWindow:
    """Normalize recorder timing into capture and logical assay windows."""
    return RunWindow(
        capture_started_at=capture_started_at,
        capture_stopped_at=capture_stopped_at,
        logical_started_at=logical_started_at or capture_started_at,
        logical_stopped_at=logical_stopped_at or capture_stopped_at,
        logical_stop_reason=logical_stop_reason or stop_reason,
        logical_stop_details=logical_stop_details or {},
    )


def build_run_manifest_payload(
    *,
    label: str,
    source: str,
    video_path: Path,
    run_window: RunWindow,
    stop_reason: str,
    process_gate: str,
    log_dir: Path,
    log_glob: str,
    trace_match: TraceMatch | None,
    storage_tier: str = DEFAULT_STORAGE_TIER,
    segments_override: list[dict] | None = None,
    local_compaction: dict | None = None,
    local_retention: dict | None = None,
) -> dict:
    """Build the replay manifest payload for one recorded run."""
    trace_path = trace_match.path if trace_match else None
    trace_delta_sec = trace_match.delta_sec if trace_match else None
    trace_modified_at = trace_match.modified_at if trace_match else None
    trace_summary = build_trace_replay_summary(trace_path) if trace_path and trace_path.exists() else {}
    source_segments = segments_override if segments_override is not None else trace_summary.get("segments", [])
    segments: list[dict] = []
    for item in source_segments:
        normalized = dict(item)
        normalized["video_path"] = str(video_path.resolve())
        segments.append(normalized)
    payload = {
        "label": label,
        "source": source,
        "video_path": str(video_path.resolve()),
        "video_filename": video_path.name,
        "started_at_local": run_window.capture_started_at.isoformat(),
        "stopped_at_local": run_window.capture_stopped_at.isoformat(),
        "duration_sec": run_window.capture_duration_sec(),
        "stop_reason": stop_reason,
        "process_gate": process_gate,
        "hamilton_log_dir": str(log_dir.resolve()),
        "hamilton_log_glob": log_glob,
        "trace_path": str(trace_path.resolve()) if trace_path else "",
        "trace_filename": trace_path.name if trace_path else "",
        "trace_mtime_delta_sec": round(trace_delta_sec, 3) if trace_delta_sec is not None else None,
        "capture_started_at_local": run_window.capture_started_at.isoformat(),
        "capture_stopped_at_local": run_window.capture_stopped_at.isoformat(),
        "capture_duration_sec": run_window.capture_duration_sec(),
        "logical_run_started_at_local": run_window.logical_started_at.isoformat(),
        "logical_run_stopped_at_local": run_window.logical_stopped_at.isoformat(),
        "logical_run_duration_sec": run_window.logical_duration_sec(),
        "logical_run_start_offset_sec": run_window.logical_start_offset_sec(),
        "logical_run_stop_offset_sec": run_window.logical_stop_offset_sec(),
        "logical_stop_reason": run_window.logical_stop_reason,
        "logical_stop_details": dict(run_window.logical_stop_details or {}),
        "trace_modified_at_local": trace_modified_at.isoformat() if trace_modified_at else "",
        "trace_match_mode": "nearest_mtime_to_reference_time",
        "trace_finalized_after_logical_stop_sec": (
            round((trace_modified_at - run_window.logical_stopped_at).total_seconds(), 3)
            if trace_modified_at
            else None
        ),
        "trace_finalized_after_capture_stop_sec": (
            round((trace_modified_at - run_window.capture_stopped_at).total_seconds(), 3)
            if trace_modified_at
            else None
        ),
        "replay_manifest_version": REPLAY_MANIFEST_VERSION,
        "replay_capabilities": list(REPLAY_MANIFEST_CAPABILITIES),
        "storage_tier": storage_tier,
        "replay_default_mode": DEFAULT_REPLAY_MODE,
        "full_detail_retained_until_local": str(
            (local_retention or {}).get("retain_until_local") or DEFAULT_FULL_DETAIL_RETENTION
        ),
        "local_compaction": dict(local_compaction or DEFAULT_LOCAL_COMPACTION),
        "local_retention": dict(local_retention or {}),
        "trace_event_count": trace_summary.get("trace_event_count", 0),
        "trace_started_at_local": trace_summary.get("trace_started_at_local", ""),
        "trace_stopped_at_local": trace_summary.get("trace_stopped_at_local", ""),
        "trace_duration_sec": trace_summary.get("trace_duration_sec"),
        "chapters": trace_summary.get("chapters", []),
        "segments": segments,
        "idle_segment_count": sum(1 for item in segments if item.get("kind") == "idle"),
        "active_segment_count": sum(1 for item in segments if item.get("kind") == "active"),
    }
    return payload


def write_run_manifest(
    manifest_path: Path,
    *,
    label: str,
    source: str,
    video_path: Path,
    run_window: RunWindow,
    stop_reason: str,
    process_gate: str,
    log_dir: Path,
    log_glob: str,
    trace_match: TraceMatch | None,
    storage_tier: str = DEFAULT_STORAGE_TIER,
    segments_override: list[dict] | None = None,
    local_compaction: dict | None = None,
    local_retention: dict | None = None,
) -> None:
    """Persist the pairing artifact the replay UI will load later."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_run_manifest_payload(
        label=label,
        source=source,
        video_path=video_path,
        run_window=run_window,
        stop_reason=stop_reason,
        process_gate=process_gate,
        log_dir=log_dir,
        log_glob=log_glob,
        trace_match=trace_match,
        storage_tier=storage_tier,
        segments_override=segments_override,
        local_compaction=local_compaction,
        local_retention=local_retention,
    )
    payload = normalize_replay_manifest_payload(payload, manifest_path=manifest_path)
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def request_ffmpeg_stop(proc: subprocess.Popen, verbose: bool = False, log_path: Path | None = None) -> None:
    """Ask ffmpeg to exit cleanly so MP4 metadata is finalized."""
    if proc.poll() is not None:
        return

    try:
        if proc.stdin:
            if verbose:
                emit_log("[recorder] Requesting graceful ffmpeg shutdown.", log_path=log_path)
            proc.stdin.write("q\n")
            proc.stdin.flush()
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def build_output_path(out_dir: Path, start_time: dt.datetime, label: str) -> Path:
    """Create a stable filename for one continuous run recording."""
    safe_label = label.strip() or "cam"
    return out_dir / f"{ts_label(start_time)}_{safe_label}.mp4"


def _is_valid_recording(video_path: Path, *, stop_reason: str) -> bool:
    """Reject recorder outputs that never produced a usable video artifact.

    When ffmpeg fails during startup it can still leave behind an empty file
    path, and earlier code treated that as a completed run. The replay catalog
    then filled with junk manifests. We only keep runs that produced a real
    file with content.
    """
    try:
        if not video_path.exists():
            return False
        size = video_path.stat().st_size
    except Exception:
        return False

    if size <= 0:
        return False

    # A backend exit with a trivially small file is still a failed launch.
    if stop_reason == "backend_exit" and size < 1024:
        return False
    return True


def should_split_on_log_activity(
    *,
    enable_midrun_split: bool,
    changed_files: list[dict],
    stop_when_exe: str,
) -> bool:
    """Return True when mid-session log activity should finalize the current segment."""
    if not enable_midrun_split or not changed_files:
        return False
    if stop_when_exe and not _is_any_proc_running(stop_when_exe):
        return False
    return True


def build_ffmpeg_command(
    ffmpeg_bin: str,
    source: str,
    out_path: Path,
    *,
    framerate: int | None,
    video_size: str | None,
    dshow_rtbufsize: str | None,
) -> list[str]:
    """Construct the ffmpeg command for one long-running recording."""
    src = source.strip()
    source_kind, normalized_source = to_ffmpeg_input(src)

    if source_kind == "rtsp":
        input_args = ["-rtsp_transport", "tcp", "-i", normalized_source]
        codec_args = ["-c", "copy", "-an"]
    elif source_kind == "dshow":
        input_args = ["-f", "dshow"]
        if dshow_rtbufsize:
            # DirectShow's default real-time buffer is small enough to drop
            # frames on slower workstations or higher-resolution webcams.
            input_args += ["-rtbufsize", str(dshow_rtbufsize)]
        if framerate:
            input_args += ["-framerate", str(framerate)]
        if video_size:
            input_args += ["-video_size", str(video_size)]
        input_args += ["-i", normalized_source]
        codec_args = [
            "-vcodec",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-an",
        ]
    else:
        input_args = ["-i", normalized_source]
        codec_args = ["-c", "copy", "-an"]

    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        *input_args,
        *codec_args,
        "-movflags",
        "+faststart",
        str(out_path),
    ]


def run_ffmpeg(
    ffmpeg_bin: str,
    source: str,
    out_dir: Path,
    label: str,
    stop_file: Path,
    *,
    stop_when_exe: str = "",
    verbose: bool = False,
    poll_sec: float = 1.0,
    framerate: int | None = None,
    video_size: str | None = None,
    dshow_rtbufsize: str | None = None,
    max_record_sec: int = 0,
    log_dir: Path | None = None,
    log_glob: str = "*.trc",
    enable_midrun_split: bool = False,
    log_path: Path | None = None,
) -> CaptureResult:
    """Record a single MP4 until the process gate exits, timeout, or stop file appears."""
    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = ts_now()
    out_path = build_output_path(out_dir, started_at, label)
    cmd = build_ffmpeg_command(
        ffmpeg_bin,
        source,
        out_path,
        framerate=framerate,
        video_size=video_size,
        dshow_rtbufsize=dshow_rtbufsize,
    )

    if verbose:
        emit_log("[recorder] ffmpeg cmd:", log_path=log_path)
        emit_log(f"  {' '.join(cmd)}", log_path=log_path)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
    )
    stop_reason = "backend_exit"
    monotonic_started = time.monotonic()
    last_heartbeat = monotonic_started
    log_snapshot = snapshot_log_files(log_dir, log_glob) if log_dir else {}
    split_details: dict | None = None
    trace_activity_seen = False

    try:
        while True:
            if stop_file.exists():
                stop_reason = "stop_file"
                request_ffmpeg_stop(proc, verbose, log_path)
                break

            if stop_when_exe and not _is_any_proc_running(stop_when_exe):
                stop_reason = "process_exit"
                request_ffmpeg_stop(proc, verbose, log_path)
                break

            if max_record_sec > 0 and (time.monotonic() - monotonic_started) >= max_record_sec:
                stop_reason = "max_record_sec"
                request_ffmpeg_stop(proc, verbose, log_path)
                break

            if log_dir:
                current_snapshot = snapshot_log_files(log_dir, log_glob)
                changed_files = detect_log_activity(log_snapshot, current_snapshot)
                if changed_files:
                    trace_activity_seen = True
                if should_split_on_log_activity(
                    enable_midrun_split=enable_midrun_split,
                    changed_files=changed_files,
                    stop_when_exe=stop_when_exe,
                ):
                    stop_reason = "trace_log_activity"
                    split_details = {"changed_files": changed_files}
                    request_ffmpeg_stop(proc, verbose, log_path)
                    break
                log_snapshot = current_snapshot

            ret = proc.poll()
            if ret is not None:
                if ret != 0:
                    err = ""
                    try:
                        err = proc.stderr.read() if proc.stderr else ""
                    except Exception:
                        err = ""
                    emit_log(f"ffmpeg exited early with code {ret}", log_path=log_path, is_error=True)
                    if err:
                        emit_log(err.strip(), log_path=log_path, is_error=True)
                break

            now = time.monotonic()
            if verbose and (now - last_heartbeat) >= 5.0:
                elapsed_sec = now - monotonic_started
                emit_log(
                    f"[recorder] Capture heartbeat: {elapsed_sec:.1f}s elapsed, writing to {out_path.name}",
                    log_path=log_path,
                )
                last_heartbeat = now

            time.sleep(max(0.25, poll_sec))

        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)
    finally:
        try:
            stop_file.unlink(missing_ok=True)
        except Exception:
            pass

    stopped_at = ts_now()
    return CaptureResult(
        video_path=out_path,
        capture_started_at=started_at,
        capture_stopped_at=stopped_at,
        stop_reason=stop_reason,
        logical_stopped_at=stopped_at,
        logical_stop_reason=stop_reason,
        logical_stop_details=split_details or {},
        trace_activity_seen=trace_activity_seen,
    )


def run_opencv(
    source: str,
    out_dir: Path,
    label: str,
    stop_file: Path,
    *,
    stop_when_exe: str = "",
    fps: int = 20,
    fourcc: str = "mp4v",
    poll_sec: float = 1.0,
    max_record_sec: int = 0,
    verbose: bool = False,
    log_dir: Path | None = None,
    log_glob: str = "*.trc",
    enable_midrun_split: bool = False,
    log_path: Path | None = None,
) -> CaptureResult:
    """Fallback continuous recorder when ffmpeg is unavailable."""
    import cv2  # type: ignore

    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(0 if source.isdigit() else source)
    if not cap.isOpened():
        raise RuntimeError("Failed to open camera source")

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
    code = cv2.VideoWriter_fourcc(*fourcc)
    started_at = ts_now()
    out_path = build_output_path(out_dir, started_at, label)
    writer = cv2.VideoWriter(str(out_path), code, fps, (width, height))
    monotonic_started = time.monotonic()
    last_heartbeat = monotonic_started
    stop_reason = "stop_file"
    log_snapshot = snapshot_log_files(log_dir, log_glob) if log_dir else {}
    split_details: dict | None = None
    trace_activity_seen = False

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            writer.write(frame)

            if stop_file.exists():
                stop_reason = "stop_file"
                break
            if stop_when_exe and not _is_any_proc_running(stop_when_exe):
                stop_reason = "process_exit"
                break
            if max_record_sec > 0 and (time.monotonic() - monotonic_started) >= max_record_sec:
                stop_reason = "max_record_sec"
                break

            if log_dir:
                current_snapshot = snapshot_log_files(log_dir, log_glob)
                changed_files = detect_log_activity(log_snapshot, current_snapshot)
                if changed_files:
                    trace_activity_seen = True
                if should_split_on_log_activity(
                    enable_midrun_split=enable_midrun_split,
                    changed_files=changed_files,
                    stop_when_exe=stop_when_exe,
                ):
                    stop_reason = "trace_log_activity"
                    split_details = {"changed_files": changed_files}
                    break
                log_snapshot = current_snapshot

            now = time.monotonic()
            if (verbose or log_path) and (now - last_heartbeat) >= 5.0:
                elapsed_sec = now - monotonic_started
                emit_log(
                    f"[recorder] Capture heartbeat: {elapsed_sec:.1f}s elapsed, writing to {out_path.name}",
                    log_path=log_path,
                )
                last_heartbeat = now

            time.sleep(max(0.01, min(0.25, poll_sec)))
    finally:
        writer.release()
        cap.release()
        try:
            stop_file.unlink(missing_ok=True)
        except Exception:
            pass

    stopped_at = ts_now()
    return CaptureResult(
        video_path=out_path,
        capture_started_at=started_at,
        capture_stopped_at=stopped_at,
        stop_reason=stop_reason,
        logical_stopped_at=stopped_at,
        logical_stop_reason=stop_reason,
        logical_stop_details=split_details or {},
        trace_activity_seen=trace_activity_seen,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Continuous camera recorder for Hamilton runs")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the base camera config JSON")
    ap.add_argument("--local-config", default=str(DEFAULT_LOCAL_OVERRIDE_PATH), help="Path to the optional workstation-local override JSON")
    ap.add_argument("--profile", default="", help="Camera profile id from the shared config")
    ap.add_argument("--source", default="", help="Camera source (index or URL). Overrides the selected profile source.")
    ap.add_argument("--out-dir", default="", help="Directory for run recordings")
    ap.add_argument("--label", default="", help="Label to include in filenames and manifests")
    ap.add_argument("--stop-file", default="", help="Path to stop file to end recording")
    ap.add_argument("--start-when-exe", default="", help="Wait to begin recording until one of these process names is running (comma/semicolon-separated)")
    ap.add_argument("--stop-when-exe", default="", help="Stop when the given process name is no longer running")
    ap.add_argument("--startup-timeout-sec", type=int, default=None, help="How long to wait for --start-when-exe before exiting. 0 waits indefinitely.")
    ap.add_argument("--poll-sec", type=float, default=None, help="Polling interval for process-gate checks")
    ap.add_argument("--max-record-sec", type=int, default=None, help="Safety cap for total recording time. 0 disables the cap.")
    ap.add_argument("--ffmpeg", default="", help="Optional path to ffmpeg.exe; if provided, recorder uses ffmpeg backend")
    ap.add_argument("--verbose", action="store_true", help="Print backend selection and underlying ffmpeg command")
    ap.add_argument("--list-devices", action="store_true", help="List DirectShow cameras and exit (requires ffmpeg)")
    ap.add_argument("--select-device", action="store_true", help="Interactively select a DirectShow camera (requires ffmpeg)")
    ap.add_argument("--framerate", type=int, default=None, help="Desired framerate for dshow webcams")
    ap.add_argument("--video-size", default="", help="Desired resolution WxH for dshow webcams")
    ap.add_argument("--dshow-rtbufsize", default="", help="DirectShow real-time input buffer size such as 256M")
    ap.add_argument("--log-dir", default="", help="Hamilton trace directory used for post-run pairing")
    ap.add_argument("--log-glob", default="", help="Semicolon-separated glob(s) used to find Hamilton traces")
    ap.add_argument("--manifest-dir", default="", help="Optional directory for run manifests; defaults next to the video")
    ap.add_argument("--recorder-log", default="", help="Optional path for a persistent recorder diagnostic log")
    ap.add_argument("--enable-midrun-split", action="store_true", help="Cut the current segment when matching log activity appears while the process gate stays open")
    ap.add_argument("--discard-without-trace", action="store_true", help="If this speculative segment ends without any matched trace, discard it instead of writing a bogus manifest")
    ap.add_argument("--dump-config", action="store_true", help="Print the effective camera config and exit")
    ap.add_argument("--validate-config", action="store_true", help="Validate the effective camera config and exit")
    ap.add_argument("--list-profiles", action="store_true", help="List configured camera profiles and exit")
    args = ap.parse_args()

    config = load_effective_config(
        config_path=Path(args.config),
        local_override_path=Path(args.local_config),
    )
    validation = validate_config(config, require_hamilton_log_dir=not (args.list_devices or args.select_device))
    if args.dump_config:
        print(json.dumps(config, indent=2))
        return 0
    if args.list_profiles:
        print(json.dumps(config["profiles"], indent=2))
        return 0
    if args.validate_config:
        if validation["errors"]:
            for item in validation["errors"]:
                print(item, file=sys.stderr)
            return 1
        for item in validation["warnings"]:
            print(item)
        print("Camera config validation passed.")
        return 0
    if validation["errors"]:
        for item in validation["errors"]:
            print(item, file=sys.stderr)
        return 1

    try:
        profile = get_profile(config, args.profile or None)
    except KeyError:
        print(f"Unknown camera profile: {args.profile}", file=sys.stderr)
        return 1

    hamilton = config["hamilton"]
    storage = config["storage"]
    recorder = config["recorder"]
    compaction_config = storage.get("compaction") or {}
    retention_config = storage.get("retention") or {}

    source = args.source or profile.get("source") or "0"
    out_dir = Path(args.out_dir or storage.get("runs_root") or (REPO_ROOT / "cameras" / "video_clips"))
    stop_file = Path(args.stop_file or recorder.get("stop_file") or "cameras.recorder.stop")
    log_dir = Path(args.log_dir or hamilton.get("log_dir") or Path.cwd())
    log_glob = args.log_glob or hamilton.get("log_glob") or "*.trc"
    start_when_exe = args.start_when_exe or hamilton.get("process_name") or ""
    effective_stop_when_exe = args.stop_when_exe or start_when_exe
    label = args.label or profile.get("label") or "cam"
    manifest_dir = args.manifest_dir or storage.get("manifest_dir") or ""
    recorder_log = Path(args.recorder_log) if args.recorder_log else None
    poll_sec = args.poll_sec if args.poll_sec is not None else float(recorder.get("poll_sec", 1.0))
    startup_timeout_sec = (
        args.startup_timeout_sec if args.startup_timeout_sec is not None else int(recorder.get("startup_timeout_sec", 0))
    )
    max_record_sec = args.max_record_sec if args.max_record_sec is not None else int(recorder.get("max_record_sec", 0))
    framerate = args.framerate if args.framerate is not None else profile.get("framerate")
    video_size = args.video_size or profile.get("video_size") or None
    dshow_rtbufsize = args.dshow_rtbufsize or recorder.get("dshow_rtbufsize") or None
    ffmpeg_override = args.ffmpeg or profile.get("ffmpeg_path") or recorder.get("ffmpeg_path") or ""

    if start_when_exe and not wait_for_process_start(
        start_when_exe,
        poll_sec=poll_sec,
        timeout_sec=startup_timeout_sec,
        verbose=args.verbose,
        log_path=recorder_log,
    ):
        emit_log("Recorder exited before capture because the startup gate was never satisfied.", log_path=recorder_log, is_error=True)
        return 3

    ok, ffmpeg_bin = find_ffmpeg(ffmpeg_override or None)

    if ok and ffmpeg_bin and (args.list_devices or args.select_device):
        devices = list_dshow_devices(ffmpeg_bin)
        if not devices:
            print("No DirectShow devices found or probe failed.")
            return 2
        print("DirectShow video devices:")
        for device in devices:
            suffix = f"  (alt: {device['alt']})" if device.get("alt") else ""
            print(f"  [{device['index']}] {device['name']}{suffix}")
        if args.list_devices:
            return 0
        try:
            choice = int(input("Select device index: ").strip())
        except Exception:
            print("Invalid selection input")
            return 2
        selected = next((device for device in devices if device["index"] == choice), None)
        if not selected:
            print("Invalid selection")
            return 2
        name = selected.get("name") or selected.get("alt")
        if not name:
            print("Selected device has no usable name")
            return 2
        source = name

    emit_log(
        f"Recording run video to {out_dir}. "
        f"Create '{stop_file}' to stop. "
        f"Trace pairing dir: '{log_dir}'.",
        log_path=recorder_log,
    )
    if effective_stop_when_exe:
        emit_log(f"Run lifecycle gate: {effective_stop_when_exe}", log_path=recorder_log)
    if max_record_sec > 0:
        emit_log(f"Safety timeout: {max_record_sec} seconds", log_path=recorder_log)

    if ok and ffmpeg_bin and not is_numeric_source(source):
        if args.verbose:
            emit_log(f"Using ffmpeg backend: {ffmpeg_bin}", log_path=recorder_log)
        try:
            capture = run_ffmpeg(
                ffmpeg_bin,
                source,
                out_dir,
                label,
                stop_file,
                stop_when_exe=effective_stop_when_exe,
                verbose=args.verbose,
                poll_sec=poll_sec,
                framerate=framerate,
                video_size=video_size,
                dshow_rtbufsize=dshow_rtbufsize,
                max_record_sec=max_record_sec,
                log_dir=log_dir,
                log_glob=log_glob,
                enable_midrun_split=args.enable_midrun_split,
                log_path=recorder_log,
            )
        except Exception as exc:
            emit_log(f"ERROR: ffmpeg backend failed: {exc}", log_path=recorder_log, is_error=True)
            return 2
    else:
        if args.verbose:
            if is_numeric_source(source):
                emit_log("Using OpenCV backend for numeric camera source", log_path=recorder_log)
            else:
                emit_log("Using OpenCV backend (ffmpeg not found)", log_path=recorder_log)
        try:
            capture = run_opencv(
                source,
                out_dir,
                label,
                stop_file,
                stop_when_exe=effective_stop_when_exe,
                poll_sec=poll_sec,
                max_record_sec=max_record_sec,
                verbose=args.verbose,
                log_dir=log_dir,
                log_glob=log_glob,
                enable_midrun_split=args.enable_midrun_split,
                log_path=recorder_log,
            )
        except Exception as exc:
            emit_log("ERROR: OpenCV backend failed and ffmpeg not found.", log_path=recorder_log, is_error=True)
            emit_log("Install OpenCV: python -m pip install opencv-python", log_path=recorder_log, is_error=True)
            emit_log("Or specify ffmpeg: --ffmpeg C:\\QC\\cameras\\ffmpeg.exe", log_path=recorder_log, is_error=True)
            emit_log(f"Details: {exc}", log_path=recorder_log, is_error=True)
            return 2

    video_path = capture.video_path
    started_at = capture.capture_started_at
    stopped_at = capture.capture_stopped_at
    stop_reason = capture.stop_reason

    if not _is_valid_recording(video_path, stop_reason=stop_reason):
        emit_log(
            f"Discarding failed recording attempt: {video_path}",
            log_path=recorder_log,
            is_error=True,
        )
        try:
            video_path.unlink(missing_ok=True)
        except Exception:
            pass
        return 4

    pair_time = capture.logical_stopped_at or stopped_at
    trace_match = choose_trace_file(log_dir, log_glob, pair_time)
    if args.discard_without_trace and trace_match is None:
        emit_log(
            f"Discarding speculative segment without matched trace: {video_path}",
            log_path=recorder_log,
        )
        try:
            video_path.unlink(missing_ok=True)
        except Exception:
            pass
        return 0

    if trace_match:
        tag_payload = derive_run_tags(trace_match.path)
        tag_summary = tag_payload["summary"]
        emit_log(
            "[recorder] "
            f"Trace tags outcome={tag_summary.get('outcome') or 'unknown'} "
            f"primary_barcode={tag_summary.get('primary_barcode') or '-'} "
            f"errors={tag_summary.get('error_count') or 0} aborts={tag_summary.get('abort_count') or 0}",
            log_path=recorder_log,
        )

    run_window = build_run_window(
        capture_started_at=started_at,
        capture_stopped_at=stopped_at,
        stop_reason=stop_reason,
        logical_stopped_at=capture.logical_stopped_at,
        logical_stop_reason=capture.logical_stop_reason,
        logical_stop_details=capture.logical_stop_details,
    )
    manifest_root = Path(manifest_dir) if manifest_dir else video_path.parent
    manifest_path = manifest_root / f"{video_path.stem}.run.json"
    trace_summary = build_trace_replay_summary(trace_match.path) if trace_match and trace_match.path.exists() else {}
    trace_segments = trace_summary.get("segments", [])
    local_compaction = dict(DEFAULT_LOCAL_COMPACTION)
    segments_override = None
    storage_tier = DEFAULT_STORAGE_TIER
    if bool(compaction_config.get("enabled")):
        emit_log(f"Generating local compaction artifacts for {video_path.name}", log_path=recorder_log)
        local_compaction = generate_local_compaction(
            ffmpeg_bin=ffmpeg_bin,
            source_video_path=video_path,
            segments=trace_segments,
            configured_root=str(compaction_config.get("artifacts_root") or ""),
            min_segment_duration_sec=float(compaction_config.get("min_segment_duration_sec", 5.0) or 5.0),
            active_crf=int(compaction_config.get("active_crf", 30) or 30),
            active_preset=str(compaction_config.get("active_preset") or "veryfast"),
            idle_crf=int(compaction_config.get("idle_crf", 36) or 36),
            idle_preset=str(compaction_config.get("idle_preset") or "veryfast"),
            idle_fps=int(compaction_config.get("idle_fps", 2) or 2),
        )
        segments_override = apply_compaction_metadata_to_segments(trace_segments, local_compaction)
        if local_compaction.get("status") == "succeeded":
            storage_tier = "full_run_plus_local_derivatives"
            emit_log(
                f"Local compaction wrote {len(local_compaction.get('segment_derivatives') or [])} derived segment files.",
                log_path=recorder_log,
            )
        else:
            emit_log(
                f"Local compaction did not produce derivatives: {local_compaction.get('status')}",
                log_path=recorder_log,
            )
    local_retention = build_local_retention_payload(
        manifest_payload={
            "video_path": str(video_path.resolve()),
            "stopped_at_local": run_window.capture_stopped_at.isoformat(),
            "local_compaction": local_compaction,
        },
        enabled=bool(retention_config.get("enabled", True)),
        retention_days=int(retention_config.get("original_retention_days", 7) or 7),
        derived_retention_days=int(retention_config.get("derived_retention_days", 30) or 30),
        require_upload_ack=bool(retention_config.get("require_upload_ack", True)),
        require_local_compaction=bool(retention_config.get("require_local_compaction", False)),
    )
    write_run_manifest(
        manifest_path,
        label=label,
        source=source,
        video_path=video_path,
        run_window=run_window,
        stop_reason=stop_reason,
        process_gate=effective_stop_when_exe,
        log_dir=log_dir,
        log_glob=log_glob,
        trace_match=trace_match,
        storage_tier=storage_tier,
        segments_override=segments_override,
        local_compaction=local_compaction,
        local_retention=local_retention,
    )

    emit_log(f"Wrote video: {video_path}", log_path=recorder_log)
    emit_log(f"Wrote run manifest: {manifest_path}", log_path=recorder_log)
    if trace_match:
        emit_log(f"Paired trace: {trace_match.path} (mtime delta {trace_match.delta_sec:.2f}s)", log_path=recorder_log)
    else:
        emit_log("No matching trace file found.", log_path=recorder_log)

    if stop_reason == "trace_log_activity":
        emit_log("Mid-session trace activity detected; signaling daemon to rearm recorder.", log_path=recorder_log)
        return RECORDER_REARM_EXIT_CODE

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
