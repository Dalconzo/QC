#!/usr/bin/env python3
"""
local_compaction.py

Post-run local storage helpers for generating compact per-segment replay
artifacts from one finalized full-run source recording.
"""
from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path


ACTIVE_PROFILE_NAME = "derived_active_h264"
IDLE_PROFILE_NAME = "derived_idle_h264_2fps"


def build_segment_artifacts_root(video_path: Path, configured_root: str = "") -> Path:
    """Return the deterministic folder used for per-run compacted artifacts."""
    if configured_root:
        return Path(configured_root) / video_path.stem
    return video_path.parent / f"{video_path.stem}.derived"


def build_segment_output_path(artifacts_root: Path, segment: dict) -> Path:
    """Return the compacted segment filename for one replay segment."""
    segment_id = str(segment.get("segment_id") or "segment").replace("/", "-").replace("\\", "-")
    kind = str(segment.get("kind") or "active")
    return artifacts_root / f"{segment_id}_{kind}.mp4"


def build_transcode_command(
    ffmpeg_bin: str,
    source_video_path: Path,
    output_path: Path,
    *,
    start_offset_sec: float,
    stop_offset_sec: float,
    kind: str,
    active_crf: int,
    active_preset: str,
    idle_crf: int,
    idle_preset: str,
    idle_fps: int,
) -> list[str]:
    """Build one ffmpeg command for an active or idle segment derivative."""
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{float(start_offset_sec):.3f}",
        "-to",
        f"{float(stop_offset_sec):.3f}",
        "-i",
        str(source_video_path),
    ]
    if kind == "idle" and int(idle_fps) > 0:
        command += ["-vf", f"fps={int(idle_fps)}"]
    command += [
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        idle_preset if kind == "idle" else active_preset,
        "-crf",
        str(idle_crf if kind == "idle" else active_crf),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    return command


def generate_local_compaction(
    *,
    ffmpeg_bin: str | None,
    source_video_path: Path,
    segments: list[dict],
    configured_root: str = "",
    min_segment_duration_sec: float = 5.0,
    active_crf: int = 30,
    active_preset: str = "veryfast",
    idle_crf: int = 36,
    idle_preset: str = "veryfast",
    idle_fps: int = 2,
) -> dict:
    """Generate compact per-segment derivatives and return manifest metadata."""
    source_video_path = source_video_path.resolve()
    artifacts_root = build_segment_artifacts_root(source_video_path, configured_root)
    result = {
        "status": "not_requested",
        "artifacts_root": str(artifacts_root.resolve()),
        "generated_at_local": "",
        "failure": "",
        "source_video_path": str(source_video_path),
        "source_video_size_bytes": source_video_path.stat().st_size if source_video_path.exists() else 0,
        "total_derived_size_bytes": 0,
        "segment_derivatives": [],
    }

    if not ffmpeg_bin:
        result["status"] = "ffmpeg_unavailable"
        return result

    if not isinstance(segments, list) or not segments:
        result["status"] = "no_segments"
        return result

    artifacts_root.mkdir(parents=True, exist_ok=True)
    total_size = 0
    try:
        for segment in segments:
            duration_sec = float(segment.get("duration_sec") or 0)
            if duration_sec < float(min_segment_duration_sec):
                continue

            output_path = build_segment_output_path(artifacts_root, segment)
            command = build_transcode_command(
                ffmpeg_bin,
                source_video_path,
                output_path,
                start_offset_sec=float(segment.get("start_offset_sec") or 0.0),
                stop_offset_sec=float(segment.get("stop_offset_sec") or 0.0),
                kind=str(segment.get("kind") or "active"),
                active_crf=active_crf,
                active_preset=active_preset,
                idle_crf=idle_crf,
                idle_preset=idle_preset,
                idle_fps=idle_fps,
            )
            subprocess.run(command, check=True, capture_output=True, text=True)
            size_bytes = output_path.stat().st_size
            total_size += size_bytes
            result["segment_derivatives"].append(
                {
                    "segment_id": str(segment.get("segment_id") or ""),
                    "kind": str(segment.get("kind") or ""),
                    "video_path": str(output_path.resolve()),
                    "video_filename": output_path.name,
                    "video_encoding_profile": IDLE_PROFILE_NAME if segment.get("kind") == "idle" else ACTIVE_PROFILE_NAME,
                    "size_bytes": size_bytes,
                }
            )
    except Exception as exc:
        result["status"] = "failed"
        result["failure"] = str(exc)
        return result

    if not result["segment_derivatives"]:
        result["status"] = "no_segments"
        return result

    result["status"] = "succeeded"
    result["generated_at_local"] = dt.datetime.now().isoformat(timespec="seconds")
    result["total_derived_size_bytes"] = total_size
    return result


def apply_compaction_metadata_to_segments(segments: list[dict], compaction_result: dict) -> list[dict]:
    """Attach derivative paths to segment metadata without changing primary replay paths."""
    derivative_by_segment = {
        item.get("segment_id"): item
        for item in (compaction_result.get("segment_derivatives") or [])
        if isinstance(item, dict) and item.get("segment_id")
    }

    enriched: list[dict] = []
    for item in segments:
        segment = dict(item)
        derivative = derivative_by_segment.get(segment.get("segment_id"))
        if derivative:
            segment["derived_video_path"] = derivative.get("video_path") or ""
            segment["derived_video_filename"] = derivative.get("video_filename") or ""
            segment["derived_video_encoding_profile"] = derivative.get("video_encoding_profile") or ""
            segment["derived_size_bytes"] = int(derivative.get("size_bytes") or 0)
        enriched.append(segment)
    return enriched
