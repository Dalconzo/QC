#!/usr/bin/env python3
"""
camera-daemon.py

Always-on supervisor for the Hamilton camera recorder.

This daemon is the workstation-local automation layer for camera capture. It
waits for the configured Hamilton Run Manager process to appear, launches one
continuous recorder for that run, waits for the recorder to finish when the
process exits, then returns to idle so the next run is captured automatically.

The daemon does not capture video itself. It owns lifecycle management,
singleton protection, status reporting, and auto-start friendliness, while
`camera-recorder.py` remains the component that talks to ffmpeg/OpenCV and
pairs each recording with its Hamilton trace file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, get_profile, load_effective_config, validate_config
from local_retention import cleanup_runs
from stage_central_replay import stage_runs
from upload_central_replay import upload_staged_runs
from workstation_release import build_contract_status, get_deployment_status

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUTO_STAGE_RECENT_DAYS = 2.0
RECORDER_REARM_EXIT_CODE = 20


def runtime_base_dir() -> Path:
    """Return the directory that contains packaged sibling tools when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_recorder_entry() -> Path:
    """Pick the recorder executable/script that matches the current runtime."""
    base_dir = runtime_base_dir()
    if getattr(sys, "frozen", False):
        packaged = base_dir / "camera-recorder.exe"
        if packaged.exists():
            return packaged
    return base_dir / "camera-recorder.py"


EXIT_CONTRACT_FINALIZED_RUN = 0
EXIT_CONTRACT_REARM_SEGMENT = 20
EXIT_CONTRACT_DISCARD_SPECULATIVE_SEGMENT = 21
REARM_EXIT_CODES = {
    EXIT_CONTRACT_REARM_SEGMENT,
    EXIT_CONTRACT_DISCARD_SPECULATIVE_SEGMENT,
}

INGEST_IDLE = "idle"
INGEST_QUEUED = "queued"
INGEST_RUNNING = "running"
INGEST_COMPLETED = "completed"
INGEST_FAILED = "failed"


def classify_exit_contract(return_code: int) -> str:
    """Map recorder exit codes onto the daemon's session-management contract."""
    if return_code == EXIT_CONTRACT_FINALIZED_RUN:
        return "finalized_run"
    if return_code == EXIT_CONTRACT_REARM_SEGMENT:
        return "rearm_segment"
    if return_code == EXIT_CONTRACT_DISCARD_SPECULATIVE_SEGMENT:
        return "discard_speculative_segment"
    return "error"


def emit_log(message: str, *, log_path: Path | None = None, is_error: bool = False) -> None:
    """Mirror daemon diagnostics to the console and an optional log file."""
    stream = sys.stderr if is_error else sys.stdout
    print(message, file=stream)
    if not log_path:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{dt.datetime.now().isoformat()} {message}\n"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def write_status(status_path: Path, payload: dict) -> None:
    """Persist the latest daemon state for local troubleshooting scripts."""
    status_path.parent.mkdir(parents=True, exist_ok=True)
    enriched = dict(payload)
    enriched["updated_at"] = dt.datetime.now().isoformat()
    with status_path.open("w", encoding="utf-8") as handle:
        json.dump(enriched, handle, indent=2)
        handle.write("\n")


def guess_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0] or "")
    except Exception:
        return ""


def post_status_json(base_url: str, route: str, payload: dict, *, timeout_sec: float) -> None:
    if not base_url:
        return
    url = f"{base_url.rstrip('/')}{route}"
    body = json.dumps(payload).encode("utf-8")
    request = urlrequest.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlrequest.urlopen(request, timeout=max(0.5, timeout_sec)) as response:
        if response.status >= 400:
            raise RuntimeError(f"Status push failed: {response.status}")


def iter_run_manifests(runs_root: Path) -> list[Path]:
    return sorted(runs_root.rglob("*.run.json"), key=lambda path: path.stat().st_mtime, reverse=True)


def find_recent_run_payload(runs_root: Path, *, not_before: dt.datetime | None = None) -> dict | None:
    for manifest_path in iter_run_manifests(runs_root):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        stopped_at_local = str(payload.get("stopped_at_local") or "").strip()
        if not_before and stopped_at_local:
            try:
                stopped_at = dt.datetime.fromisoformat(stopped_at_local)
            except ValueError:
                stopped_at = None
            if stopped_at and stopped_at < (not_before - dt.timedelta(seconds=5)):
                continue
        payload.setdefault("manifest_path", str(manifest_path.resolve()))
        return payload
    return None


def build_status_envelope(*, config: dict, profile: dict, source: str, state: str, extra: dict | None = None, run_payload: dict | None = None) -> dict:
    central_ingest = config.get("central_ingest", {})
    daemon_status = config.get("daemon", {})
    return {
        "workstation": {
            "workstation_id": socket.gethostname().lower(),
            "hostname": socket.gethostname(),
            "machine_alias": socket.gethostname(),
            "repo_root": str(REPO_ROOT),
            "local_ip": guess_local_ip(),
            "software_version": "camera-daemon.jdp.v1",
        },
        "camera_profile": {
            "profile_id": str(profile.get("id") or "default"),
            "profile_key": str(profile.get("id") or "default"),
            "profile_label": str(profile.get("label") or profile.get("id") or "default"),
            "source_name": source,
        },
        "status": {
            "state": state,
            "upload_phase": str((extra or {}).get("upload_phase") or ""),
            "last_error": str((extra or {}).get("last_error") or (extra or {}).get("last_upload_error") or ""),
            "current_local_run_id": str((run_payload or {}).get("local_run_id") or ""),
            "current_label": str((run_payload or {}).get("label") or ""),
            "current_started_at_local": str((run_payload or {}).get("started_at_local") or ""),
            "daemon_status_path": str(daemon_status.get("status_path") or ""),
            "staging_root": str(central_ingest.get("staging_root") or ""),
            "upload_root": str(central_ingest.get("upload_root") or ""),
        },
        "run": run_payload or {},
    }


def read_pid_file(pid_path: Path) -> int | None:
    """Return the recorded PID if the file exists and is readable."""
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def is_pid_running(pid: int | None) -> bool:
    """Check whether a process id is still alive without requiring psutil."""
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
            output = (result.stdout or "").lower()
            return str(pid) in output and "no tasks are running" not in output
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def claim_singleton(pid_path: Path) -> None:
    """Prevent multiple daemon instances from supervising the same workstation."""
    existing_pid = read_pid_file(pid_path)
    if existing_pid and is_pid_running(existing_pid):
        raise RuntimeError(f"Camera daemon is already running with PID {existing_pid}")

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")


def release_singleton(pid_path: Path) -> None:
    """Remove the pid file when the daemon exits cleanly."""
    try:
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass


def _normalize_proc_names(proc_names: str | list[str] | tuple[str, ...]) -> list[str]:
    """Normalize configured process selectors into executable names."""
    if isinstance(proc_names, (list, tuple)):
        items = [str(item).strip() for item in proc_names]
    else:
        text = str(proc_names or "").replace(";", ",")
        items = [item.strip() for item in text.split(",")]
    return [item for item in items if item]


def is_any_proc_running(proc_names: str | list[str] | tuple[str, ...]) -> bool:
    """Return True when any named executable is present on the workstation."""
    normalized = _normalize_proc_names(proc_names)
    if not normalized:
        return False

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
            return False


def build_recorder_command(
    *,
    config_path: Path,
    local_config_path: Path,
    recorder_script: Path,
    recorder_log_path: Path | None,
    process_name: str,
    profile_id: str,
    source: str,
    out_dir: str,
    label: str,
    enable_midrun_split: bool,
    discard_without_trace: bool,
) -> list[str]:
    """Construct one child recorder invocation for a single Hamilton run."""
    if recorder_script.suffix.lower() == ".exe":
        command = [
            str(recorder_script),
            "--config",
            str(config_path),
            "--local-config",
            str(local_config_path),
            "--profile",
            profile_id,
            "--start-when-exe",
            process_name,
            "--stop-when-exe",
            process_name,
        ]
    else:
        command = [
            sys.executable,
            str(recorder_script),
            "--config",
            str(config_path),
            "--local-config",
            str(local_config_path),
            "--profile",
            profile_id,
            "--start-when-exe",
            process_name,
            "--stop-when-exe",
            process_name,
        ]

    if source:
        command += ["--source", source]
    if out_dir:
        command += ["--out-dir", out_dir]
    if label:
        command += ["--label", label]
    if recorder_log_path:
        command += ["--recorder-log", str(recorder_log_path)]
    if enable_midrun_split:
        command.append("--enable-midrun-split")
    if discard_without_trace:
        command.append("--discard-without-trace")
    return command


def run_post_run_central_ingest(
    *,
    config_path: Path,
    local_config_path: Path,
    daemon_log_path: Path | None,
    update_status_fn=None,
) -> dict | None:
    """Stage and upload newly completed runs when auto-upload is enabled."""
    config = load_effective_config(config_path=config_path, local_override_path=local_config_path)
    central_ingest = config.get("central_ingest", {})
    if not bool(central_ingest.get("auto_upload_on_run_complete", False)):
        return None
    auto_stage_recent_days = central_ingest.get("auto_stage_recent_days", DEFAULT_AUTO_STAGE_RECENT_DAYS)
    try:
        recent_days = max(0.0, float(auto_stage_recent_days))
    except (TypeError, ValueError):
        recent_days = DEFAULT_AUTO_STAGE_RECENT_DAYS

    if update_status_fn is not None:
        update_status_fn("uploading", upload_phase="staging", auto_stage_recent_days=recent_days)

    emit_log(
        f"[daemon] Auto-staging completed runs for central replay ingest (recent_days={recent_days:g})",
        log_path=daemon_log_path,
    )
    stage_payload = stage_runs(
        config_path=config_path,
        local_config_path=local_config_path,
        runs_root=None,
        staging_root=None,
        limit=0,
        restage=False,
        recent_days=recent_days,
    )
    emit_log(
        f"[daemon] Auto-stage complete: staged={stage_payload['staged_run_count']} skipped={stage_payload['skipped_run_count']}",
        log_path=daemon_log_path,
    )

    if update_status_fn is not None:
        update_status_fn(
            "uploading",
            upload_phase="uploading",
            auto_stage_recent_days=recent_days,
            last_stage_batch_id=stage_payload["batch_id"],
            last_stage_staged_run_count=stage_payload["staged_run_count"],
            last_stage_skipped_run_count=stage_payload["skipped_run_count"],
        )

    upload_payload = upload_staged_runs(
        config_path=config_path,
        local_config_path=local_config_path,
        staging_root=None,
        upload_root=None,
        limit=0,
        batch_id="",
    )
    emit_log(
        f"[daemon] Auto-upload complete: uploaded={upload_payload['uploaded_run_count']} failed={upload_payload['failed_run_count']}",
        log_path=daemon_log_path,
        is_error=(upload_payload["failed_run_count"] > 0),
    )
    return {
        "stage": stage_payload,
        "upload": upload_payload,
        "cleanup": None,
    }


def run_post_run_local_cleanup(
    *,
    config_path: Path,
    local_config_path: Path,
    daemon_log_path: Path | None,
) -> dict | None:
    """Apply workstation-local retention cleanup after a completed recorder session."""
    config = load_effective_config(config_path=config_path, local_override_path=local_config_path)
    retention = config.get("storage", {}).get("retention", {})
    if not bool(retention.get("cleanup_on_run_complete", False)):
        return None

    runs_root = Path(config["storage"]["runs_root"]).resolve()
    emit_log("[daemon] Running local retention cleanup", log_path=daemon_log_path)
    cleanup_payload = cleanup_runs(
        runs_root=runs_root,
        delete=True,
        limit=0,
        emergency_config=retention.get("emergency") or {},
    )
    emit_log(
        "[daemon] Local cleanup complete: "
        f"deleted={cleanup_payload['deleted_run_count']} "
        f"eligible={cleanup_payload['eligible_run_count']} "
        f"emergency_deleted={cleanup_payload['emergency_deleted_run_count']} "
        f"critical={cleanup_payload['critical_pressure_remaining']}",
        log_path=daemon_log_path,
    )
    return cleanup_payload


class AsyncIngestManager:
    """Run post-recording staging/upload work off the supervisor hot path."""

    def __init__(
        self,
        *,
        config_path: Path,
        local_config_path: Path,
        daemon_log_path: Path | None,
        post_run_ingest_fn,
    ) -> None:
        self._config_path = config_path
        self._local_config_path = local_config_path
        self._daemon_log_path = daemon_log_path
        self._post_run_ingest_fn = post_run_ingest_fn
        self._jobs: queue.Queue[dict | None] = queue.Queue()
        self._completions: queue.Queue[dict] = queue.Queue()
        self._lock = threading.Lock()
        self._stop_requested = False
        self._pending_count = 0
        self._active_job: dict | None = None
        self._active_phase = ""
        self._thread: threading.Thread | None = None

    def enabled(self) -> bool:
        return self._post_run_ingest_fn is not None

    def pending_count(self) -> int:
        with self._lock:
            return self._pending_count

    def active_job(self) -> dict | None:
        with self._lock:
            return dict(self._active_job) if self._active_job else None

    def active_phase(self) -> str:
        with self._lock:
            return self._active_phase

    def has_pending_work(self) -> bool:
        with self._lock:
            return self._pending_count > 0 or self._active_job is not None

    def enqueue(self, job: dict) -> int:
        with self._lock:
            self._pending_count += 1
        self._ensure_started()
        self._jobs.put(dict(job))
        return self.pending_count()

    def poll_completions(self) -> list[dict]:
        items: list[dict] = []
        while True:
            try:
                items.append(self._completions.get_nowait())
            except queue.Empty:
                return items

    def shutdown(self, *, drain_timeout_sec: float = 0.0) -> None:
        with self._lock:
            self._stop_requested = True
        if self._thread is None:
            return
        self._jobs.put(None)
        if drain_timeout_sec > 0:
            self._thread.join(timeout=max(0.0, drain_timeout_sec))

    def _ensure_started(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._worker_main,
            name="camera-daemon-ingest",
            daemon=True,
        )
        self._thread.start()

    def _worker_main(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return

            run_payload = dict(job.get("run_payload") or {})
            with self._lock:
                self._active_job = dict(job)
                self._active_phase = "staging"
            phase_updates: list[dict] = []

            def capture_phase(_state: str, **extra: object) -> None:
                phase = str(extra.get("upload_phase") or "")
                with self._lock:
                    self._active_phase = phase or self._active_phase
                phase_updates.append(dict(extra))

            try:
                ingest_payload = self._post_run_ingest_fn(
                    config_path=self._config_path,
                    local_config_path=self._local_config_path,
                    daemon_log_path=self._daemon_log_path,
                    update_status_fn=capture_phase,
                )
            except Exception as exc:
                result = {
                    "ok": False,
                    "error": str(exc),
                    "run_payload": run_payload,
                    "completed_at": dt.datetime.now().isoformat(),
                    "phase_updates": phase_updates,
                }
            else:
                result = {
                    "ok": True,
                    "payload": ingest_payload,
                    "run_payload": run_payload,
                    "completed_at": dt.datetime.now().isoformat(),
                    "phase_updates": phase_updates,
                }

            with self._lock:
                self._pending_count = max(0, self._pending_count - 1)
                self._active_job = None
            self._active_phase = ""
        self._completions.put(result)


def run_supervisor(
    *,
    config_path: Path,
    local_config_path: Path,
    profile_id: str,
    source_override: str,
    out_dir_override: str,
    label_override: str,
    stop_file: Path,
    pid_file: Path,
    status_path: Path,
    daemon_log_path: Path | None,
    recorder_log_path: Path | None,
    idle_poll_sec: float,
    heartbeat_sec: float,
    relaunch_delay_sec: float,
    idle_timeout_sec: int,
    run_once: bool,
    max_cycles: int,
    recorder_script: Path,
    is_process_running_fn=is_any_proc_running,
    post_run_ingest_fn=run_post_run_central_ingest,
    post_run_cleanup_fn=run_post_run_local_cleanup,
) -> int:
    """Run the idle->record->idle loop until explicitly stopped."""
    config = load_effective_config(config_path=config_path, local_override_path=local_config_path)
    validation = validate_config(config, require_hamilton_log_dir=True)
    if validation["errors"]:
        for item in validation["errors"]:
            emit_log(item, log_path=daemon_log_path, is_error=True)
        return 1

    try:
        profile = get_profile(config, profile_id or None)
    except KeyError:
        emit_log(f"Unknown camera profile: {profile_id}", log_path=daemon_log_path, is_error=True)
        return 1

    process_name = str(config["hamilton"]["process_name"])
    source = source_override or str(profile.get("source") or "")
    out_dir = out_dir_override or str(config["storage"]["runs_root"])
    label = label_override or str(profile.get("label") or profile["id"])
    recorder_stop_file = Path(str(config["recorder"]["stop_file"]))
    status_server_url = str(config.get("central_ingest", {}).get("status_server_url") or "").strip()
    status_timeout_sec = float(config.get("central_ingest", {}).get("status_timeout_sec", 5) or 5)
    deployment_status = get_deployment_status(REPO_ROOT)
    contract_status = build_contract_status(config)
    enable_midrun_split = bool(config.get("daemon", {}).get("enable_midrun_split", False))
    runs_root = Path(str(config["storage"]["runs_root"])).resolve()
    auto_upload_enabled = bool(config.get("central_ingest", {}).get("auto_upload_on_run_complete", False))

    try:
        claim_singleton(pid_file)
    except RuntimeError as exc:
        emit_log(str(exc), log_path=daemon_log_path, is_error=True)
        return 2

    started_at = dt.datetime.now().isoformat()
    cycle_count = 0
    child_proc: subprocess.Popen | None = None
    child_started_at = 0.0
    loop_started_at = time.monotonic()
    last_heartbeat = 0.0
    run_session_active = False
    sticky_status_fields: dict[str, object] = {}
    next_launch_speculative = False
    current_run_payload: dict | None = None
    current_state = "starting"
    ingest_manager = AsyncIngestManager(
        config_path=config_path,
        local_config_path=local_config_path,
        daemon_log_path=daemon_log_path,
        post_run_ingest_fn=(post_run_ingest_fn if auto_upload_enabled else None),
    )

    def publish_workstation_status(state: str, *, extra: dict | None = None) -> None:
        if not status_server_url:
            return
        payload = build_status_envelope(
            config=config,
            profile=profile,
            source=source,
            state=state,
            extra=extra,
            run_payload=current_run_payload,
        )
        try:
            post_status_json(
                status_server_url,
                "/api/workstations/heartbeat",
                payload,
                timeout_sec=status_timeout_sec,
            )
        except (OSError, RuntimeError, urlerror.URLError) as exc:
            emit_log(f"[daemon] Status heartbeat failed: {exc}", log_path=daemon_log_path, is_error=True)

    def publish_run_status(state: str, run_payload: dict | None, *, extra: dict | None = None) -> None:
        if not status_server_url or not run_payload or not run_payload.get("local_run_id"):
            return
        run_update = dict(run_payload)
        run_update["replay_status"] = state
        if extra:
            run_update.update(extra)
        payload = build_status_envelope(
            config=config,
            profile=profile,
            source=source,
            state=state,
            extra=extra,
            run_payload=run_update,
        )
        try:
            post_status_json(
                status_server_url,
                "/api/runs/status",
                payload,
                timeout_sec=status_timeout_sec,
            )
        except (OSError, RuntimeError, urlerror.URLError) as exc:
            emit_log(f"[daemon] Run status push failed: {exc}", log_path=daemon_log_path, is_error=True)

    def update_status(state: str, **extra: object) -> None:
        nonlocal current_state
        payload = {
            "state": state,
            "daemon_pid": os.getpid(),
            "profile_id": profile["id"],
            "profile_label": profile.get("label"),
            "source": source,
            "out_dir": out_dir,
            "process_name": process_name,
            "started_at": started_at,
            "cycle_count": cycle_count,
            "child_pid": child_proc.pid if child_proc else None,
            "stop_file": str(stop_file),
            "pid_file": str(pid_file),
            "status_path": str(status_path),
            "daemon_log_path": str(daemon_log_path) if daemon_log_path else "",
            "recorder_log_path": str(recorder_log_path) if recorder_log_path else "",
            "deployment": deployment_status,
            "contract_status": contract_status,
        }
        sticky_status_fields.update(extra)
        payload.update(sticky_status_fields)
        payload.update(extra)
        current_state = state
        write_status(status_path, payload)
        publish_workstation_status(state, extra=payload)

    def update_ingest_status(ingest_state: str, *, state: str | None = None, **extra: object) -> None:
        active_job = ingest_manager.active_job()
        payload = {
            "ingest_state": ingest_state,
            "pending_ingest_count": ingest_manager.pending_count(),
            "active_ingest_run_id": str(((active_job or {}).get("run_payload") or {}).get("local_run_id") or ""),
            "active_ingest_manifest_path": str(((active_job or {}).get("run_payload") or {}).get("manifest_path") or ""),
            "active_ingest_phase": ingest_manager.active_phase(),
        }
        payload.update(extra)
        effective_state = state or current_state
        update_status(effective_state, **payload)

    def flush_ingest_completions() -> None:
        for item in ingest_manager.poll_completions():
            run_payload = item.get("run_payload") or None
            base_payload = {
                "pending_ingest_count": ingest_manager.pending_count(),
                "active_ingest_run_id": "",
                "active_ingest_manifest_path": "",
                "active_ingest_phase": "",
                "last_ingest_completed_at": item.get("completed_at", ""),
                "last_ingest_run_id": str((run_payload or {}).get("local_run_id") or ""),
                "last_ingest_manifest_path": str((run_payload or {}).get("manifest_path") or ""),
            }
            if not item.get("ok"):
                error_text = str(item.get("error") or "unknown ingest failure")
                emit_log(
                    f"[daemon] Background auto-upload failed: {error_text}",
                    log_path=daemon_log_path,
                    is_error=True,
                )
                update_ingest_status(
                    INGEST_FAILED,
                    **base_payload,
                    last_upload_error=error_text,
                    last_ingest_error=error_text,
                )
                if run_payload:
                    publish_run_status("failed", run_payload, extra={"last_error": error_text})
                continue

            ingest_payload = item.get("payload") or None
            if ingest_payload:
                update_ingest_status(
                    INGEST_COMPLETED,
                    **base_payload,
                    last_upload_error="",
                    last_ingest_error="",
                    last_stage_batch_id=ingest_payload["stage"]["batch_id"],
                    last_stage_staged_run_count=ingest_payload["stage"]["staged_run_count"],
                    last_stage_skipped_run_count=ingest_payload["stage"]["skipped_run_count"],
                    last_upload_batch_id=ingest_payload["upload"]["ingest_batch_id"],
                    last_uploaded_run_count=ingest_payload["upload"]["uploaded_run_count"],
                    last_failed_upload_run_count=ingest_payload["upload"]["failed_run_count"],
                )
                if run_payload:
                    matching_item = next(
                        (
                            entry
                            for entry in ingest_payload["upload"].get("items", [])
                            if entry.get("local_run_id") == run_payload.get("local_run_id") and entry.get("action") == "acknowledged"
                        ),
                        None,
                    )
                    if matching_item:
                        publish_run_status(
                            "available",
                            run_payload,
                            extra={"central_run_id": matching_item.get("central_run_id", "")},
                        )
                    elif ingest_payload["upload"].get("failed_run_count", 0) > 0:
                        publish_run_status("failed", run_payload, extra={"last_error": "auto_upload_failed"})
            else:
                update_ingest_status(
                    INGEST_COMPLETED,
                    **base_payload,
                    last_upload_error="",
                    last_ingest_error="",
                )

    emit_log(
        f"[daemon] Starting supervisor for profile '{profile['id']}' gated on {process_name}",
        log_path=daemon_log_path,
    )
    update_status(
        "starting",
        ingest_state=INGEST_IDLE,
        pending_ingest_count=0,
        active_ingest_run_id="",
        active_ingest_manifest_path="",
        active_ingest_phase="",
        last_ingest_run_id="",
        last_ingest_manifest_path="",
        last_ingest_completed_at="",
        last_ingest_error="",
    )

    try:
        while True:
            flush_ingest_completions()
            if stop_file.exists():
                emit_log("[daemon] Stop file detected. Exiting idle loop.", log_path=daemon_log_path)
                update_status("stopping", reason="daemon_stop_file")
                break

            if child_proc is not None:
                return_code = child_proc.poll()
                if return_code is None:
                    now = time.monotonic()
                    if (now - last_heartbeat) >= heartbeat_sec:
                        duration = round(now - child_started_at, 1)
                        emit_log(
                            f"[daemon] Recorder child {child_proc.pid} active for {duration}s",
                            log_path=daemon_log_path,
                        )
                        update_status("recording", child_runtime_sec=duration)
                        last_heartbeat = now

                    if stop_file.exists():
                        emit_log("[daemon] Stop requested while recording. Signaling recorder stop file.", log_path=daemon_log_path)
                        recorder_stop_file.parent.mkdir(parents=True, exist_ok=True)
                        recorder_stop_file.write_text("stop", encoding="utf-8")
                        update_status("stopping", reason="daemon_stop_file", child_runtime_sec=round(now - child_started_at, 1))
                    time.sleep(max(0.25, idle_poll_sec))
                    continue

                recorder_finished_at = dt.datetime.now()
                exit_contract = classify_exit_contract(return_code)
                emit_log(
                    f"[daemon] Recorder child exited with code {return_code} ({exit_contract})",
                    log_path=daemon_log_path,
                    is_error=(return_code not in (EXIT_CONTRACT_FINALIZED_RUN, *REARM_EXIT_CODES)),
                )
                if return_code == EXIT_CONTRACT_FINALIZED_RUN:
                    current_run_payload = find_recent_run_payload(runs_root, not_before=recorder_finished_at - dt.timedelta(minutes=30))
                    if current_run_payload:
                        publish_run_status("pending_upload", current_run_payload)
                update_status(
                    "idle",
                    last_exit_code=return_code,
                    last_exit_contract=exit_contract,
                    last_cycle_completed_at=dt.datetime.now().isoformat(),
                    waiting_for_process_exit=False,
                )
                if return_code == EXIT_CONTRACT_FINALIZED_RUN and auto_upload_enabled and ingest_manager.enabled():
                    if current_run_payload:
                        publish_run_status("uploading", current_run_payload, extra={"upload_phase": "queued"})
                    pending_count = ingest_manager.enqueue(
                        {
                            "run_payload": current_run_payload or {},
                            "enqueued_at": dt.datetime.now().isoformat(),
                        }
                    )
                    update_ingest_status(
                        INGEST_QUEUED,
                        state="idle",
                        pending_ingest_count=pending_count,
                        last_ingest_enqueued_at=dt.datetime.now().isoformat(),
                    )
                else:
                    update_ingest_status(INGEST_IDLE, state="idle")
                cleanup_payload = None
                if return_code == EXIT_CONTRACT_FINALIZED_RUN and post_run_cleanup_fn is not None:
                    try:
                        cleanup_payload = post_run_cleanup_fn(
                            config_path=config_path,
                            local_config_path=local_config_path,
                            daemon_log_path=daemon_log_path,
                        )
                    except Exception as exc:
                        emit_log(
                            f"[daemon] Local cleanup failed: {exc}",
                            log_path=daemon_log_path,
                            is_error=True,
                        )
                        update_status("idle", last_cleanup_error=str(exc))
                    else:
                        if cleanup_payload:
                            update_status(
                                "idle",
                                last_cleanup_deleted_run_count=int(cleanup_payload.get("deleted_run_count", 0) or 0),
                                last_cleanup_deleted_bytes=int(cleanup_payload.get("deleted_bytes", 0) or 0),
                                last_cleanup_eligible_run_count=int(cleanup_payload.get("eligible_run_count", 0) or 0),
                            )
                child_proc = None
                child_started_at = 0.0
                last_heartbeat = 0.0
                if return_code == RECORDER_REARM_EXIT_CODE:
                    emit_log("[daemon] Recorder requested immediate rearm inside the same HxRun session.", log_path=daemon_log_path)
                    run_session_active = False
                    next_launch_speculative = True
                else:
                    cycle_count += 1
                    run_session_active = True
                    next_launch_speculative = False
                current_run_payload = None

                if return_code != RECORDER_REARM_EXIT_CODE and (run_once or (max_cycles > 0 and cycle_count >= max_cycles)):
                    emit_log("[daemon] Run limit reached. Exiting.", log_path=daemon_log_path)
                    update_status(
                        "stopped",
                        reason="run_limit",
                        last_exit_code=return_code,
                        last_exit_contract=exit_contract,
                    )
                    break

                if relaunch_delay_sec > 0:
                    time.sleep(relaunch_delay_sec)
                continue

            if idle_timeout_sec > 0 and (time.monotonic() - loop_started_at) >= idle_timeout_sec:
                emit_log("[daemon] Idle timeout reached before the next Hamilton run.", log_path=daemon_log_path)
                update_status("stopped", reason="idle_timeout")
                break

            if not is_process_running_fn(process_name):
                run_session_active = False
                now = time.monotonic()
                if (now - last_heartbeat) >= heartbeat_sec:
                    emit_log(f"[daemon] Waiting for process start: {process_name}", log_path=daemon_log_path)
                    update_status("idle", waiting_for_process_exit=False)
                    last_heartbeat = now
                time.sleep(max(0.25, idle_poll_sec))
                continue

            if run_session_active:
                now = time.monotonic()
                if (now - last_heartbeat) >= heartbeat_sec:
                    emit_log(
                        f"[daemon] Waiting for {process_name} to exit before arming the next recording session",
                        log_path=daemon_log_path,
                    )
                    update_status("idle", waiting_for_process_exit=True)
                    last_heartbeat = now
                time.sleep(max(0.25, idle_poll_sec))
                continue

            retention_config = config.get("storage", {}).get("retention", {})
            emergency_config = retention_config.get("emergency") or {}
            if bool(emergency_config.get("enabled", False)):
                headroom_payload = cleanup_runs(
                    runs_root=Path(config["storage"]["runs_root"]).resolve(),
                    delete=True,
                    limit=0,
                    emergency_config=emergency_config,
                    run_normal_cleanup=False,
                )
                update_status(
                    "idle",
                    low_disk_emergency_active=bool(headroom_payload.get("emergency_active")),
                    low_disk_free_bytes=int(headroom_payload.get("disk_free_bytes_after", 0) or 0),
                    low_disk_emergency_deleted_run_count=int(headroom_payload.get("emergency_deleted_run_count", 0) or 0),
                    low_disk_critical=bool(headroom_payload.get("critical_pressure_remaining")),
                )
                if bool(headroom_payload.get("critical_pressure_remaining")):
                    emit_log(
                        "[daemon] Critical low disk remains after emergency cleanup. Blocking new recording launch.",
                        log_path=daemon_log_path,
                        is_error=True,
                    )
                    update_status(
                        "blocked_low_disk",
                        low_disk_emergency_active=bool(headroom_payload.get("emergency_active")),
                        low_disk_free_bytes=int(headroom_payload.get("disk_free_bytes_after", 0) or 0),
                        low_disk_emergency_deleted_run_count=int(
                            headroom_payload.get("emergency_deleted_run_count", 0) or 0
                        ),
                        low_disk_critical=True,
                    )
                    time.sleep(max(0.25, idle_poll_sec))
                    continue

            child_command = build_recorder_command(
                config_path=config_path,
                local_config_path=local_config_path,
                recorder_script=recorder_script,
                recorder_log_path=recorder_log_path,
                process_name=process_name,
                profile_id=profile["id"],
                source=source,
                out_dir=out_dir,
                label=label,
                enable_midrun_split=enable_midrun_split,
                discard_without_trace=next_launch_speculative,
            )
            emit_log(f"[daemon] Launching recorder child for active {process_name} session", log_path=daemon_log_path)
            emit_log(f"[daemon] Recorder cmd: {' '.join(child_command)}", log_path=daemon_log_path)
            child_proc = subprocess.Popen(child_command)
            child_started_at = time.monotonic()
            last_heartbeat = 0.0
            current_run_payload = None
            update_status("recording", child_started_at=dt.datetime.now().isoformat())
            time.sleep(max(0.25, idle_poll_sec))

        return 0
    finally:
        flush_ingest_completions()
        ingest_manager.shutdown(drain_timeout_sec=15.0)
        flush_ingest_completions()
        if child_proc is not None and child_proc.poll() is None:
            try:
                recorder_stop_file.parent.mkdir(parents=True, exist_ok=True)
                recorder_stop_file.write_text("stop", encoding="utf-8")
                child_proc.wait(timeout=15)
            except Exception:
                try:
                    child_proc.send_signal(signal.SIGTERM)
                except Exception:
                    pass
        release_singleton(pid_file)
        try:
            stop_file.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Always-on supervisor for Hamilton camera capture")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the base camera config JSON")
    parser.add_argument("--local-config", default=str(DEFAULT_LOCAL_OVERRIDE_PATH), help="Path to the optional workstation-local override JSON")
    parser.add_argument("--profile", default="", help="Camera profile id from the shared config")
    parser.add_argument("--source", default="", help="Override the selected profile source")
    parser.add_argument("--out-dir", default="", help="Override the configured runs root")
    parser.add_argument("--label", default="", help="Override the recording label")
    parser.add_argument("--daemon-log", default="", help="Persistent log path for daemon diagnostics")
    parser.add_argument("--recorder-log", default="", help="Persistent log path for child recorder diagnostics")
    parser.add_argument("--status-path", default="", help="Status JSON written by the daemon")
    parser.add_argument("--pid-file", default="", help="PID file used for singleton protection")
    parser.add_argument("--stop-file", default="", help="Stop-file sentinel for shutting down the daemon")
    parser.add_argument("--idle-poll-sec", type=float, default=None, help="Polling interval while waiting for HxRun.exe")
    parser.add_argument("--heartbeat-sec", type=float, default=None, help="How often to log idle/recording heartbeats")
    parser.add_argument("--relaunch-delay-sec", type=float, default=None, help="Delay before the daemon returns to idle after one run finishes")
    parser.add_argument("--idle-timeout-sec", type=int, default=0, help="Exit if no run starts within this many seconds. 0 waits indefinitely.")
    parser.add_argument("--once", action="store_true", help="Exit after the first recorder session completes")
    parser.add_argument("--max-cycles", type=int, default=0, help="Exit after this many completed recorder sessions. 0 means unlimited.")
    parser.add_argument("--recorder-script", default="", help="Override recorder script path. Mainly useful for tests.")
    args = parser.parse_args()

    config = load_effective_config(
        config_path=Path(args.config),
        local_override_path=Path(args.local_config),
    )
    validation = validate_config(config, require_hamilton_log_dir=True)
    daemon_config = config.get("daemon", {})

    if validation["errors"]:
        for item in validation["errors"]:
            print(item, file=sys.stderr)
        return 1

    daemon_log_path = Path(args.daemon_log or daemon_config.get("log_path") or "") if (args.daemon_log or daemon_config.get("log_path")) else None
    status_path = Path(args.status_path or daemon_config.get("status_path") or (REPO_ROOT / "logs" / "camera-daemon-status.json"))
    pid_file = Path(args.pid_file or daemon_config.get("pid_file") or (REPO_ROOT / "logs" / "camera-daemon.pid"))
    stop_file = Path(args.stop_file or daemon_config.get("stop_file") or (REPO_ROOT / "cameras" / "camera-daemon.stop"))
    recorder_log_path = None
    recorder_log_value = args.recorder_log or ""
    if recorder_log_value:
        recorder_log_path = Path(recorder_log_value)
    else:
        recorder_log_dir = str(config.get("storage", {}).get("recorder_log_dir") or "")
        if recorder_log_dir:
            recorder_log_path = Path(recorder_log_dir) / "camera-recorder-daemon.log"
    recorder_script = Path(args.recorder_script or default_recorder_entry())
    idle_poll_sec = args.idle_poll_sec if args.idle_poll_sec is not None else float(daemon_config.get("idle_poll_sec", 1.0))
    heartbeat_sec = args.heartbeat_sec if args.heartbeat_sec is not None else float(daemon_config.get("heartbeat_sec", 10.0))
    relaunch_delay_sec = args.relaunch_delay_sec if args.relaunch_delay_sec is not None else float(daemon_config.get("relaunch_delay_sec", 2.0))

    for item in validation["warnings"]:
        emit_log(item, log_path=daemon_log_path)

    return run_supervisor(
        config_path=Path(args.config),
        local_config_path=Path(args.local_config),
        profile_id=args.profile,
        source_override=args.source,
        out_dir_override=args.out_dir,
        label_override=args.label,
        stop_file=stop_file,
        pid_file=pid_file,
        status_path=status_path,
        daemon_log_path=daemon_log_path,
        recorder_log_path=recorder_log_path,
        idle_poll_sec=idle_poll_sec,
        heartbeat_sec=heartbeat_sec,
        relaunch_delay_sec=relaunch_delay_sec,
        idle_timeout_sec=args.idle_timeout_sec,
        run_once=args.once,
        max_cycles=args.max_cycles,
        recorder_script=recorder_script,
    )


if __name__ == "__main__":
    raise SystemExit(main())
