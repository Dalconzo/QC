#!/usr/bin/env python3
"""
camera_source.py

Shared camera-source normalization helpers.

The workstation config should be easy for operators to edit, so we accept a
plain camera name like `Arducam USB Camera` in config and only expand it into
ffmpeg's DirectShow syntax when we actually build a capture command.
"""
from __future__ import annotations

import re


_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_WINDOWS_PATH_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def is_numeric_source(source: str) -> bool:
    """Return True for OpenCV-style camera indexes such as `0` or `1`."""
    return bool(str(source or "").strip().isdigit())


def is_explicit_dshow_source(source: str) -> bool:
    """Return True when the source already declares DirectShow syntax."""
    value = str(source or "").strip().lower()
    return value.startswith("dshow:") or value.startswith("video=") or value.startswith("audio=")


def looks_like_url_or_path(source: str) -> bool:
    """Preserve obvious non-camera-name inputs as-is."""
    value = str(source or "").strip()
    lower = value.lower()
    if not value:
        return False
    if lower.startswith("rtsp"):
        return True
    if _URL_RE.match(value):
        return True
    if _WINDOWS_PATH_RE.match(value):
        return True
    if value.startswith("\\\\") or value.startswith("/") or value.startswith("./") or value.startswith("../"):
        return True
    return False


def normalize_camera_name(source: str) -> str:
    """Collapse common DirectShow spellings into the friendly device name.

    Examples:
    - `dshow:video="Arducam USB Camera"` -> `Arducam USB Camera`
    - `video="Arducam USB Camera"` -> `Arducam USB Camera`
    - `Arducam USB Camera` -> `Arducam USB Camera`
    """
    value = str(source or "").strip()
    if not value:
        return ""

    if value.lower().startswith("dshow:"):
        value = value.split(":", 1)[1].strip()

    if value.lower().startswith("video=") or value.lower().startswith("audio="):
        _key, raw_name = value.split("=", 1)
        value = raw_name.strip()

    value = value.strip()
    # Shell handoffs on operator workstations occasionally preserve a whole
    # source token inside single quotes, for example `'0'` or
    # `'Arducam USB Camera'`. Strip one outer quote layer so the downstream
    # ffmpeg/OpenCV selection logic can still recover.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()

    return value.strip().strip('"')


def to_ffmpeg_input(source: str) -> tuple[str, str]:
    """Return the ffmpeg input kind and normalized input string.

    Plain camera names become DirectShow video devices automatically so the
    workstation config only needs the friendly device name.
    """
    value = str(source or "").strip()
    if not value:
        return "empty", ""
    if is_numeric_source(value):
        return "numeric", value
    normalized_name = normalize_camera_name(value)
    if is_numeric_source(normalized_name):
        return "numeric", normalized_name
    if looks_like_url_or_path(value):
        if value.lower().startswith("rtsp"):
            return "rtsp", value
        return "generic", value
    if is_explicit_dshow_source(value):
        return "dshow", f'video="{normalized_name}"'
    return "dshow", f'video="{normalized_name}"'
