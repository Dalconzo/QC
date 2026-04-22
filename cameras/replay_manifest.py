#!/usr/bin/env python3
"""
replay_manifest.py

Shared helpers for versioned run-manifest normalization across recorder,
local replay, and central staging/upload paths.
"""
from __future__ import annotations

import json
from pathlib import Path

from trace_replay import build_trace_replay_summary


REPLAY_MANIFEST_VERSION = "hybrid-replay.v1"
REPLAY_MANIFEST_CAPABILITIES = [
    "trace_chapters",
    "trace_segments",
    "idle_skip_default",
]
DEFAULT_STORAGE_TIER = "full_run_source"
DEFAULT_REPLAY_MODE = "skip_idle"
DEFAULT_FULL_DETAIL_RETENTION = ""


def compute_run_id(manifest_path: Path, payload: dict) -> str:
    """Build a stable run identifier from the manifest identity and timing."""
    import hashlib

    identity = {
        "manifest_path": str(manifest_path.resolve()),
        "video_path": str(Path(payload.get("video_path", "")).resolve()) if payload.get("video_path") else "",
        "trace_path": str(Path(payload.get("trace_path", "")).resolve()) if payload.get("trace_path") else "",
        "started_at_local": payload.get("started_at_local") or "",
        "stopped_at_local": payload.get("stopped_at_local") or "",
        "label": payload.get("label") or "",
    }
    return hashlib.sha1(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def normalize_replay_manifest_payload(payload: dict, *, manifest_path: Path | None = None) -> dict:
    """Normalize one run manifest to the current hybrid replay contract."""
    enriched = dict(payload)
    if manifest_path is not None:
        enriched["manifest_path"] = str(manifest_path.resolve())
    enriched["video_path"] = str(Path(enriched.get("video_path", "")).resolve()) if enriched.get("video_path") else ""
    enriched["trace_path"] = str(Path(enriched.get("trace_path", "")).resolve()) if enriched.get("trace_path") else ""

    trace_path = Path(enriched["trace_path"]) if enriched.get("trace_path") else None
    trace_summary: dict = {}
    if trace_path and trace_path.exists():
        trace_summary = build_trace_replay_summary(trace_path)

    enriched["replay_manifest_version"] = REPLAY_MANIFEST_VERSION
    enriched["replay_capabilities"] = list(REPLAY_MANIFEST_CAPABILITIES)
    enriched["storage_tier"] = str(enriched.get("storage_tier") or DEFAULT_STORAGE_TIER)
    enriched["replay_default_mode"] = str(enriched.get("replay_default_mode") or DEFAULT_REPLAY_MODE)
    enriched["full_detail_retained_until_local"] = str(
        enriched.get("full_detail_retained_until_local") or DEFAULT_FULL_DETAIL_RETENTION
    )
    enriched["trace_event_count"] = int(enriched.get("trace_event_count") or trace_summary.get("trace_event_count") or 0)
    enriched["trace_started_at_local"] = (
        enriched.get("trace_started_at_local") or trace_summary.get("trace_started_at_local") or ""
    )
    enriched["trace_stopped_at_local"] = (
        enriched.get("trace_stopped_at_local") or trace_summary.get("trace_stopped_at_local") or ""
    )
    enriched["trace_duration_sec"] = _normalize_optional_float(
        enriched.get("trace_duration_sec"),
        default=trace_summary.get("trace_duration_sec"),
    )

    existing_chapters = enriched.get("chapters")
    existing_segments = enriched.get("segments")
    needs_trace_rebuild = (
        payload.get("replay_manifest_version") != REPLAY_MANIFEST_VERSION
        or not _are_valid_chapters(existing_chapters)
        or not _are_valid_segments(existing_segments)
    )

    chapters = trace_summary.get("chapters", []) if (needs_trace_rebuild and trace_summary) else existing_chapters
    segments = trace_summary.get("segments", []) if (needs_trace_rebuild and trace_summary) else existing_segments
    normalized_segments = _normalize_segments(segments, video_path=enriched.get("video_path") or "")
    normalized_chapters = _normalize_chapters(chapters)

    enriched["chapters"] = normalized_chapters
    enriched["segments"] = normalized_segments
    enriched["idle_segment_count"] = sum(1 for item in normalized_segments if item.get("kind") == "idle")
    enriched["active_segment_count"] = sum(1 for item in normalized_segments if item.get("kind") == "active")

    if manifest_path is not None:
        enriched["run_id"] = compute_run_id(manifest_path, enriched)
    return enriched


def _normalize_optional_float(value, *, default=None) -> float | None:
    candidate = default if value in (None, "") else value
    if candidate in (None, ""):
        return None
    try:
        return float(candidate)
    except (TypeError, ValueError):
        return None


def _normalize_offset(value) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0


def _are_valid_chapters(chapters) -> bool:
    if not isinstance(chapters, list):
        return False
    for item in chapters:
        if not isinstance(item, dict):
            return False
        if "start_offset_sec" not in item or "label" not in item:
            return False
    return True


def _are_valid_segments(segments) -> bool:
    if not isinstance(segments, list):
        return False
    for item in segments:
        if not isinstance(item, dict):
            return False
        required = {
            "segment_id",
            "kind",
            "start_offset_sec",
            "stop_offset_sec",
            "duration_sec",
            "phase_label",
            "phase_source",
            "video_path",
            "video_encoding_profile",
            "is_skipped_by_default",
        }
        if not required.issubset(item.keys()):
            return False
    return True


def _normalize_chapters(chapters) -> list[dict]:
    if not isinstance(chapters, list):
        return []
    normalized: list[dict] = []
    for index, item in enumerate(chapters, start=1):
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "chapter_id": str(item.get("chapter_id") or f"chapter-{index:03d}"),
                "start_offset_sec": _normalize_offset(item.get("start_offset_sec")),
                "label": str(item.get("label") or "Run activity"),
                "kind": str(item.get("kind") or "marker"),
                "phase_source": str(item.get("phase_source") or "trace"),
                "is_idle": bool(item.get("is_idle")),
            }
        )
    return normalized


def _normalize_segments(segments, *, video_path: str) -> list[dict]:
    if not isinstance(segments, list):
        return []
    normalized: list[dict] = []
    for index, item in enumerate(segments, start=1):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "active")
        start_offset_sec = _normalize_offset(item.get("start_offset_sec"))
        stop_offset_sec = _normalize_offset(item.get("stop_offset_sec"))
        duration_value = item.get("duration_sec")
        duration_sec = _normalize_optional_float(duration_value, default=stop_offset_sec - start_offset_sec)
        duration_sec = round(max(0.0, duration_sec or 0.0), 3)
        normalized.append(
            {
                "segment_id": str(item.get("segment_id") or f"{kind}-{index:03d}"),
                "kind": kind,
                "start_offset_sec": start_offset_sec,
                "stop_offset_sec": stop_offset_sec,
                "duration_sec": duration_sec,
                "phase_label": str(item.get("phase_label") or "Run activity"),
                "phase_source": str(item.get("phase_source") or "trace"),
                "source_line_index": item.get("source_line_index"),
                "video_path": str(item.get("video_path") or video_path or ""),
                "video_encoding_profile": str(item.get("video_encoding_profile") or "source_full_run"),
                "is_skipped_by_default": bool(
                    item.get("is_skipped_by_default") if "is_skipped_by_default" in item else kind == "idle"
                ),
            }
        )
    return normalized
