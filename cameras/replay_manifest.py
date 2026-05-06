#!/usr/bin/env python3
"""
replay_manifest.py

Shared helpers for versioned run-manifest normalization across recorder,
local replay, and central staging/upload paths.
"""
from __future__ import annotations

import datetime as dt
import hashlib
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
DEFAULT_LOCAL_COMPACTION = {
    "status": "not_requested",
    "artifacts_root": "",
    "generated_at_local": "",
    "failure": "",
    "source_video_path": "",
    "source_video_size_bytes": 0,
    "total_derived_size_bytes": 0,
    "segment_derivatives": [],
}
DEFAULT_LOCAL_RETENTION = {
    "enabled": True,
    "retention_days": 7,
    "derived_retention_days": 30,
    "require_upload_ack": True,
    "require_local_compaction": False,
    "upload_status": "pending",
    "upload_completed_at_utc": "",
    "upload_error": "",
    "ack_path": "",
    "central_run_id": "",
    "lan_available": False,
    "original_video_path": "",
    "original_video_size_bytes": 0,
    "derived_total_size_bytes": 0,
    "retain_until_local": "",
    "original_delete_eligible_at_local": "",
    "original_deleted_at_local": "",
    "derived_retain_until_local": "",
    "derived_delete_eligible_at_local": "",
    "derived_deleted_at_local": "",
    "last_cleanup_at_local": "",
    "last_cleanup_action": "",
    "last_cleanup_mode": "",
    "last_cleanup_reason": "",
}


def build_run_identity(payload: dict) -> dict:
    """Build a path-independent identity for one logical recorded run."""
    chapters = [
        {
            "chapter_id": str(item.get("chapter_id") or ""),
            "start_offset_sec": _normalize_offset(item.get("start_offset_sec")),
            "label": str(item.get("label") or ""),
            "kind": str(item.get("kind") or ""),
            "phase_source": str(item.get("phase_source") or ""),
            "is_idle": bool(item.get("is_idle")),
        }
        for item in (payload.get("chapters") or [])
        if isinstance(item, dict)
    ]
    segments = [
        {
            "segment_id": str(item.get("segment_id") or ""),
            "kind": str(item.get("kind") or ""),
            "start_offset_sec": _normalize_offset(item.get("start_offset_sec")),
            "stop_offset_sec": _normalize_offset(item.get("stop_offset_sec")),
            "duration_sec": _normalize_offset(item.get("duration_sec")),
            "phase_label": str(item.get("phase_label") or ""),
            "phase_source": str(item.get("phase_source") or ""),
            "source_line_index": item.get("source_line_index"),
            "video_encoding_profile": str(item.get("video_encoding_profile") or ""),
            "is_skipped_by_default": bool(item.get("is_skipped_by_default")),
            "derived_video_encoding_profile": str(item.get("derived_video_encoding_profile") or ""),
        }
        for item in (payload.get("segments") or [])
        if isinstance(item, dict)
    ]
    return {
        "identity_version": "run-identity.v2",
        "label": str(payload.get("label") or ""),
        "source": str(payload.get("source") or ""),
        "process_gate": str(payload.get("process_gate") or ""),
        "stop_reason": str(payload.get("stop_reason") or ""),
        "started_at_local": str(payload.get("started_at_local") or ""),
        "stopped_at_local": str(payload.get("stopped_at_local") or ""),
        "duration_sec": _normalize_optional_float(payload.get("duration_sec"), default=0.0) or 0.0,
        "trace_mtime_delta_sec": _normalize_optional_float(payload.get("trace_mtime_delta_sec"), default=0.0) or 0.0,
        "trace_started_at_local": str(payload.get("trace_started_at_local") or ""),
        "trace_stopped_at_local": str(payload.get("trace_stopped_at_local") or ""),
        "trace_duration_sec": _normalize_optional_float(payload.get("trace_duration_sec"), default=0.0) or 0.0,
        "trace_event_count": int(payload.get("trace_event_count") or 0),
        "replay_manifest_version": str(payload.get("replay_manifest_version") or ""),
        "replay_capabilities": [str(item) for item in (payload.get("replay_capabilities") or [])],
        "storage_tier": str(payload.get("storage_tier") or ""),
        "replay_default_mode": str(payload.get("replay_default_mode") or ""),
        "chapters": chapters,
        "segments": segments,
    }


def resolve_segment_playback(segment: dict, *, manifest_payload: dict) -> dict:
    """Return the preferred playback source for one segment."""
    derived_path_text = str(segment.get("derived_video_path") or "")
    original_path_text = str(segment.get("video_path") or manifest_payload.get("video_path") or "")
    derived_path = Path(derived_path_text) if derived_path_text else None
    original_path = Path(original_path_text) if original_path_text else None
    retention = manifest_payload.get("local_retention") or {}

    if derived_path is not None and derived_path.exists():
        return {
            "source_kind": "local_derived",
            "video_path": str(derived_path.resolve()),
            "video_filename": derived_path.name,
            "video_encoding_profile": str(
                segment.get("derived_video_encoding_profile") or segment.get("video_encoding_profile") or ""
            ),
            "is_local": True,
            "is_available": True,
        }

    if original_path is not None and original_path.exists():
        return {
            "source_kind": "local_original",
            "video_path": str(original_path.resolve()),
            "video_filename": original_path.name,
            "video_encoding_profile": str(segment.get("video_encoding_profile") or "source_full_run"),
            "is_local": True,
            "is_available": True,
        }

    if bool(retention.get("lan_available")) and str(retention.get("central_run_id") or ""):
        return {
            "source_kind": "lan_artifact",
            "video_path": "",
            "video_filename": "",
            "video_encoding_profile": str(segment.get("derived_video_encoding_profile") or segment.get("video_encoding_profile") or ""),
            "is_local": False,
            "is_available": True,
        }

    return {
        "source_kind": "missing",
        "video_path": "",
        "video_filename": "",
        "video_encoding_profile": "",
        "is_local": False,
        "is_available": False,
    }


def summarize_playback_availability(manifest_payload: dict) -> dict:
    """Describe which local or LAN-backed playback paths still exist."""
    trace_path = Path(str(manifest_payload.get("trace_path") or ""))
    original_path = Path(str(manifest_payload.get("video_path") or ""))
    retention = manifest_payload.get("local_retention") or {}
    segments = manifest_payload.get("segments") or []

    trace_available = bool(str(trace_path) and trace_path.exists())
    original_available = bool(str(original_path) and original_path.exists())
    lan_available = bool(retention.get("lan_available")) and bool(str(retention.get("central_run_id") or ""))

    local_segment_count = 0
    local_derived_segment_count = 0
    for segment in segments:
        resolved = resolve_segment_playback(segment, manifest_payload=manifest_payload)
        if resolved["is_local"] and resolved["is_available"]:
            local_segment_count += 1
        if resolved["source_kind"] == "local_derived":
            local_derived_segment_count += 1

    if not trace_available:
        status = "missing_trace"
    elif original_available:
        status = "ready_full_run"
    elif local_segment_count > 0:
        status = "ready_segments_only"
    elif lan_available:
        status = "lan_only"
    else:
        status = "missing_video"

    return {
        "status": status,
        "trace_available": trace_available,
        "full_source_available": original_available,
        "local_segment_count": local_segment_count,
        "local_derived_segment_count": local_derived_segment_count,
        "lan_available": lan_available,
        "central_run_id": str(retention.get("central_run_id") or ""),
        "original_deleted_at_local": str(retention.get("original_deleted_at_local") or ""),
        "derived_deleted_at_local": str(retention.get("derived_deleted_at_local") or ""),
    }


def determine_storage_tier(manifest_payload: dict) -> str:
    """Describe the currently available local/LAN replay tier for the run."""
    playback = summarize_playback_availability(manifest_payload)
    if playback["full_source_available"] and playback["local_derived_segment_count"] > 0:
        return "full_run_plus_local_derivatives"
    if playback["full_source_available"]:
        return "full_run_source"
    if playback["local_derived_segment_count"] > 0:
        return "local_derived_hot"
    if playback["lan_available"]:
        return "lan_archive_only"
    return "metadata_only"


def compute_run_id(manifest_path: Path, payload: dict) -> str:
    """Build a stable run identifier from the manifest identity and timing."""
    del manifest_path
    identity = build_run_identity(payload)
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
    enriched["local_compaction"] = _normalize_local_compaction(
        enriched.get("local_compaction"),
        source_video_path=enriched.get("video_path") or "",
    )
    enriched["local_retention"] = _normalize_local_retention(
        enriched.get("local_retention"),
        payload=enriched,
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
    enriched["storage_tier"] = determine_storage_tier(enriched)

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
                "derived_video_path": str(item.get("derived_video_path") or ""),
                "derived_video_filename": str(item.get("derived_video_filename") or ""),
                "derived_video_encoding_profile": str(item.get("derived_video_encoding_profile") or ""),
                "derived_size_bytes": int(item.get("derived_size_bytes") or 0),
            }
        )
    return normalized


def _normalize_local_compaction(raw_value, *, source_video_path: str) -> dict:
    if not isinstance(raw_value, dict):
        raw_value = {}

    derivatives = raw_value.get("segment_derivatives")
    normalized_derivatives: list[dict] = []
    if isinstance(derivatives, list):
        for item in derivatives:
            if not isinstance(item, dict):
                continue
            normalized_derivatives.append(
                {
                    "segment_id": str(item.get("segment_id") or ""),
                    "kind": str(item.get("kind") or ""),
                    "video_path": str(item.get("video_path") or ""),
                    "video_filename": str(item.get("video_filename") or ""),
                    "video_encoding_profile": str(item.get("video_encoding_profile") or ""),
                    "size_bytes": int(item.get("size_bytes") or 0),
                }
            )

    return {
        "status": str(raw_value.get("status") or DEFAULT_LOCAL_COMPACTION["status"]),
        "artifacts_root": str(raw_value.get("artifacts_root") or ""),
        "generated_at_local": str(raw_value.get("generated_at_local") or ""),
        "failure": str(raw_value.get("failure") or ""),
        "source_video_path": str(raw_value.get("source_video_path") or source_video_path or ""),
        "source_video_size_bytes": int(raw_value.get("source_video_size_bytes") or 0),
        "total_derived_size_bytes": int(raw_value.get("total_derived_size_bytes") or 0),
        "segment_derivatives": normalized_derivatives,
    }


def _parse_local_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _parse_utc_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone().replace(tzinfo=None)


def _normalize_local_retention(raw_value, *, payload: dict) -> dict:
    if not isinstance(raw_value, dict):
        raw_value = {}

    video_path = str(payload.get("video_path") or "")
    video_size_bytes = 0
    if video_path:
        try:
            video_size_bytes = Path(video_path).stat().st_size
        except Exception:
            video_size_bytes = 0

    local_compaction = payload.get("local_compaction") or {}
    derived_total_size_bytes = int(
        raw_value.get("derived_total_size_bytes")
        or local_compaction.get("total_derived_size_bytes")
        or 0
    )
    retention_days = int(raw_value.get("retention_days") or DEFAULT_LOCAL_RETENTION["retention_days"])
    derived_retention_days = int(raw_value.get("derived_retention_days") or DEFAULT_LOCAL_RETENTION["derived_retention_days"])
    require_upload_ack = bool(
        raw_value.get("require_upload_ack")
        if "require_upload_ack" in raw_value
        else DEFAULT_LOCAL_RETENTION["require_upload_ack"]
    )
    require_local_compaction = bool(
        raw_value.get("require_local_compaction")
        if "require_local_compaction" in raw_value
        else DEFAULT_LOCAL_RETENTION["require_local_compaction"]
    )

    stopped_at_local = _parse_local_datetime(str(payload.get("stopped_at_local") or ""))
    retain_until_local = ""
    if stopped_at_local is not None:
        retain_until_local = (stopped_at_local + dt.timedelta(days=retention_days)).isoformat(timespec="seconds")
    derived_retain_until_local = ""
    if stopped_at_local is not None:
        derived_retain_until_local = (stopped_at_local + dt.timedelta(days=derived_retention_days)).isoformat(timespec="seconds")

    upload_completed_at_utc = str(raw_value.get("upload_completed_at_utc") or "")
    upload_completed_local = _parse_utc_datetime(upload_completed_at_utc)
    compaction_status = str(local_compaction.get("status") or "")
    upload_status = str(raw_value.get("upload_status") or DEFAULT_LOCAL_RETENTION["upload_status"])
    lan_available = bool(raw_value.get("lan_available")) or upload_status == "acknowledged"

    eligible_at_local = ""
    eligibility_candidates: list[dt.datetime] = []
    if retain_until_local:
        retain_until_dt = _parse_local_datetime(retain_until_local)
        if retain_until_dt is not None:
            eligibility_candidates.append(retain_until_dt)
    if require_upload_ack:
        if upload_status == "acknowledged" and upload_completed_local is not None:
            eligibility_candidates.append(upload_completed_local)
        else:
            eligibility_candidates = []
    if require_local_compaction:
        if compaction_status == "succeeded":
            generated_at = _parse_local_datetime(str(local_compaction.get("generated_at_local") or ""))
            if generated_at is not None:
                eligibility_candidates.append(generated_at)
        else:
            eligibility_candidates = []
    if eligibility_candidates:
        eligible_at_local = max(eligibility_candidates).isoformat(timespec="seconds")

    derived_eligible_at_local = ""
    derived_eligibility_candidates: list[dt.datetime] = []
    if derived_retain_until_local and derived_total_size_bytes > 0:
        derived_retain_until_dt = _parse_local_datetime(derived_retain_until_local)
        if derived_retain_until_dt is not None:
            derived_eligibility_candidates.append(derived_retain_until_dt)
    if require_upload_ack:
        if upload_status == "acknowledged" and upload_completed_local is not None:
            derived_eligibility_candidates.append(upload_completed_local)
        else:
            derived_eligibility_candidates = []
    if require_local_compaction:
        if compaction_status == "succeeded":
            generated_at = _parse_local_datetime(str(local_compaction.get("generated_at_local") or ""))
            if generated_at is not None:
                derived_eligibility_candidates.append(generated_at)
        else:
            derived_eligibility_candidates = []
    if derived_eligibility_candidates:
        derived_eligible_at_local = max(derived_eligibility_candidates).isoformat(timespec="seconds")

    return {
        "enabled": bool(raw_value.get("enabled") if "enabled" in raw_value else DEFAULT_LOCAL_RETENTION["enabled"]),
        "retention_days": retention_days,
        "derived_retention_days": derived_retention_days,
        "require_upload_ack": require_upload_ack,
        "require_local_compaction": require_local_compaction,
        "upload_status": upload_status,
        "upload_completed_at_utc": upload_completed_at_utc,
        "upload_error": str(raw_value.get("upload_error") or ""),
        "ack_path": str(raw_value.get("ack_path") or ""),
        "central_run_id": str(raw_value.get("central_run_id") or ""),
        "lan_available": lan_available,
        "original_video_path": str(raw_value.get("original_video_path") or video_path or ""),
        "original_video_size_bytes": int(raw_value.get("original_video_size_bytes") or video_size_bytes),
        "derived_total_size_bytes": derived_total_size_bytes,
        "retain_until_local": str(raw_value.get("retain_until_local") or retain_until_local),
        "original_delete_eligible_at_local": str(
            raw_value.get("original_delete_eligible_at_local") or eligible_at_local
        ),
        "original_deleted_at_local": str(raw_value.get("original_deleted_at_local") or ""),
        "derived_retain_until_local": str(raw_value.get("derived_retain_until_local") or derived_retain_until_local),
        "derived_delete_eligible_at_local": str(
            raw_value.get("derived_delete_eligible_at_local") or derived_eligible_at_local
        ),
        "derived_deleted_at_local": str(raw_value.get("derived_deleted_at_local") or ""),
        "last_cleanup_at_local": str(raw_value.get("last_cleanup_at_local") or ""),
        "last_cleanup_action": str(raw_value.get("last_cleanup_action") or ""),
        "last_cleanup_mode": str(raw_value.get("last_cleanup_mode") or ""),
        "last_cleanup_reason": str(raw_value.get("last_cleanup_reason") or ""),
    }
