#!/usr/bin/env python3
"""
camera_live.py

Shared live-camera helpers for the Hamilton workstation tools.

The replay UI, source probe, and workstation preflight all need the same
low-level behavior: find ffmpeg, normalize camera source strings, and grab a
single JPEG frame without standing up a long-lived capture session.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from camera_config import get_profile
from camera_source import to_ffmpeg_input


def find_ffmpeg(explicit: str | None = None) -> str | None:
    """Locate ffmpeg using the same search order as the recorder workflow."""
    if explicit:
        explicit_path = Path(explicit)
        if explicit_path.exists():
            return str(explicit_path)

    here = Path(__file__).parent
    for candidate in (
        here / "ffmpeg.exe",
        here / "dist" / "ffmpeg.exe",
        Path.cwd() / "cameras" / "ffmpeg.exe",
        Path.cwd() / "cameras" / "dist" / "ffmpeg.exe",
    ):
        if candidate.exists():
            return str(candidate)

    return shutil.which("ffmpeg")


def build_live_frame_command(
    ffmpeg_bin: str,
    source: str,
    *,
    framerate: int | None,
    video_size: str | None,
    jpeg_quality: int,
) -> list[str]:
    """Build a one-frame ffmpeg capture command for live preview/probing."""
    src = (source or "").strip()
    if not src:
        raise ValueError("Camera profile source is empty.")

    source_kind, normalized_source = to_ffmpeg_input(src)

    if source_kind == "rtsp":
        input_args = ["-rtsp_transport", "tcp", "-i", normalized_source]
    elif source_kind == "dshow":
        input_args = ["-f", "dshow"]
        if framerate:
            input_args += ["-framerate", str(framerate)]
        if video_size:
            input_args += ["-video_size", str(video_size)]
        input_args += ["-i", normalized_source]
    else:
        input_args = ["-i", normalized_source]

    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        *input_args,
        "-frames:v",
        "1",
        "-q:v",
        str(jpeg_quality),
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]


def summarize_profile(profile: dict) -> dict:
    """Return lightweight camera profile metadata for UI and diagnostics."""
    return {
        "id": profile.get("id") or "",
        "label": profile.get("label") or profile.get("id") or "Camera",
        "source": profile.get("source") or "",
        "framerate": profile.get("framerate"),
        "video_size": profile.get("video_size"),
    }


def capture_live_frame(config: dict, profile_id: str | None = None) -> tuple[bytes, dict, str]:
    """Capture one JPEG frame from the requested camera profile.

    This intentionally keeps the operation stateless so rollout diagnostics can
    verify a camera source without leaving background processes behind.
    """
    live_config = config.get("live", {})
    profile = get_profile(config, profile_id or live_config.get("default_profile") or None)
    ffmpeg_bin = find_ffmpeg(profile.get("ffmpeg_path") or config.get("recorder", {}).get("ffmpeg_path") or "")
    if not ffmpeg_bin:
        raise FileNotFoundError("ffmpeg was not found for live preview capture.")

    cmd = build_live_frame_command(
        ffmpeg_bin,
        str(profile.get("source") or ""),
        framerate=profile.get("framerate"),
        video_size=profile.get("video_size"),
        jpeg_quality=int(live_config.get("jpeg_quality") or 4),
    )

    timeout_sec = int(live_config.get("frame_timeout_sec") or 8)
    completed = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout_sec)
    if completed.returncode != 0:
        error_text = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        if not error_text:
            error_text = f"ffmpeg exited with code {completed.returncode}"
        raise RuntimeError(error_text)
    if not completed.stdout:
        raise RuntimeError("ffmpeg returned no image data for live preview.")

    return completed.stdout, summarize_profile(profile), ffmpeg_bin
