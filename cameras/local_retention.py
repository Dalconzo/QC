#!/usr/bin/env python3
"""
local_retention.py

Manage workstation-local retention metadata and safe cleanup of source videos.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, load_effective_config
from replay_manifest import DEFAULT_LOCAL_RETENTION, normalize_replay_manifest_payload


def local_now(now: dt.datetime | None = None) -> dt.datetime:
    current = now or dt.datetime.now().astimezone()
    if current.tzinfo is not None:
        return current.astimezone().replace(tzinfo=None)
    return current


def local_now_text(now: dt.datetime | None = None) -> str:
    return local_now(now).isoformat(timespec="seconds")


def load_manifest(manifest_path: Path) -> dict:
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    return normalize_replay_manifest_payload(payload, manifest_path=manifest_path)


def write_manifest(manifest_path: Path, payload: dict) -> dict:
    normalized = normalize_replay_manifest_payload(payload, manifest_path=manifest_path)
    manifest_path.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    return normalized


def build_local_retention_payload(
    *,
    manifest_payload: dict,
    enabled: bool,
    retention_days: int,
    require_upload_ack: bool,
    require_local_compaction: bool,
) -> dict:
    base = dict(manifest_payload.get("local_retention") or DEFAULT_LOCAL_RETENTION)
    base.update(
        {
            "enabled": bool(enabled),
            "retention_days": int(retention_days),
            "require_upload_ack": bool(require_upload_ack),
            "require_local_compaction": bool(require_local_compaction),
        }
    )
    normalized = normalize_replay_manifest_payload(
        {
            **manifest_payload,
            "local_retention": base,
        }
    )
    return dict(normalized["local_retention"])


def initialize_local_retention(
    manifest_payload: dict,
    *,
    enabled: bool,
    retention_days: int,
    require_upload_ack: bool,
    require_local_compaction: bool,
) -> dict:
    retention = build_local_retention_payload(
        manifest_payload=manifest_payload,
        enabled=enabled,
        retention_days=retention_days,
        require_upload_ack=require_upload_ack,
        require_local_compaction=require_local_compaction,
    )
    manifest_payload["local_retention"] = retention
    manifest_payload["full_detail_retained_until_local"] = retention.get("retain_until_local") or ""
    return manifest_payload


def record_upload_ack(
    manifest_path: Path,
    *,
    central_run_id: str,
    acknowledged_at_utc: str,
    ack_path: str,
) -> dict:
    payload = load_manifest(manifest_path)
    retention = dict(payload.get("local_retention") or DEFAULT_LOCAL_RETENTION)
    retention.update(
        {
            "upload_status": "acknowledged",
            "upload_completed_at_utc": acknowledged_at_utc,
            "upload_error": "",
            "ack_path": ack_path,
            "central_run_id": central_run_id,
            "lan_available": True,
        }
    )
    payload["local_retention"] = retention
    return write_manifest(manifest_path, payload)


def record_upload_failure(manifest_path: Path, *, error_text: str) -> dict:
    payload = load_manifest(manifest_path)
    retention = dict(payload.get("local_retention") or DEFAULT_LOCAL_RETENTION)
    retention.update(
        {
            "upload_status": "upload_failed",
            "upload_error": str(error_text or ""),
        }
    )
    payload["local_retention"] = retention
    return write_manifest(manifest_path, payload)


def _parse_local_timestamp(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def evaluate_cleanup(manifest_payload: dict, *, now_local: dt.datetime | None = None) -> tuple[str, str]:
    retention = manifest_payload.get("local_retention") or {}
    if not bool(retention.get("enabled", False)):
        return ("disabled", "retention_disabled")
    if retention.get("original_deleted_at_local"):
        return ("already_deleted", "original_already_deleted")

    video_path = Path(str(manifest_payload.get("video_path") or ""))
    if not video_path.exists():
        return ("missing_original", "original_missing")

    if bool(retention.get("require_upload_ack", True)) and retention.get("upload_status") != "acknowledged":
        return ("blocked", "upload_not_acknowledged")

    if bool(retention.get("require_local_compaction", False)):
        compaction_status = str((manifest_payload.get("local_compaction") or {}).get("status") or "")
        if compaction_status != "succeeded":
            return ("blocked", "local_compaction_not_ready")

    eligible_text = str(retention.get("original_delete_eligible_at_local") or "")
    eligible_at = _parse_local_timestamp(eligible_text)
    if eligible_at is None:
        return ("blocked", "delete_window_not_ready")

    if local_now(now_local) < eligible_at:
        return ("blocked", "retention_window_not_elapsed")
    return ("eligible", "eligible_for_deletion")


def cleanup_one_manifest(manifest_path: Path, *, now_local: dt.datetime | None = None, delete: bool = True) -> dict:
    payload = load_manifest(manifest_path)
    retention = dict(payload.get("local_retention") or DEFAULT_LOCAL_RETENTION)
    video_path = Path(str(payload.get("video_path") or ""))
    action, reason = evaluate_cleanup(payload, now_local=now_local)
    result = {
        "manifest_path": str(manifest_path.resolve()),
        "video_path": str(video_path.resolve()) if str(video_path) else "",
        "action": action,
        "reason": reason,
        "deleted_bytes": 0,
    }

    retention["last_cleanup_at_local"] = local_now_text(now_local)
    retention["last_cleanup_action"] = action
    retention["last_cleanup_reason"] = reason

    if action == "eligible" and delete:
        deleted_bytes = 0
        try:
            deleted_bytes = video_path.stat().st_size
        except Exception:
            deleted_bytes = 0
        video_path.unlink(missing_ok=True)
        retention["original_deleted_at_local"] = local_now_text(now_local)
        retention["last_cleanup_action"] = "deleted"
        retention["last_cleanup_reason"] = "eligible_for_deletion"
        result["action"] = "deleted"
        result["reason"] = "eligible_for_deletion"
        result["deleted_bytes"] = deleted_bytes

    payload["local_retention"] = retention
    write_manifest(manifest_path, payload)
    return result


def collect_usage(payload: dict) -> dict:
    retention = payload.get("local_retention") or {}
    return {
        "original_bytes": int(retention.get("original_video_size_bytes") or 0),
        "derived_bytes": int(retention.get("derived_total_size_bytes") or 0),
    }


def cleanup_runs(
    *,
    runs_root: Path,
    now_local: dt.datetime | None = None,
    delete: bool = True,
    limit: int = 0,
) -> dict:
    manifests = sorted(runs_root.rglob("*.run.json"))
    if limit > 0:
        manifests = manifests[:limit]

    items: list[dict] = []
    scanned_count = 0
    deleted_count = 0
    eligible_count = 0
    blocked_count = 0
    missing_count = 0
    deleted_bytes = 0
    original_bytes = 0
    derived_bytes = 0

    for manifest_path in manifests:
        payload = load_manifest(manifest_path)
        usage = collect_usage(payload)
        original_bytes += usage["original_bytes"]
        derived_bytes += usage["derived_bytes"]
        result = cleanup_one_manifest(manifest_path, now_local=now_local, delete=delete)
        items.append(result)
        scanned_count += 1
        if result["action"] == "deleted":
            deleted_count += 1
            deleted_bytes += int(result["deleted_bytes"] or 0)
        elif result["action"] == "eligible":
            eligible_count += 1
        elif result["action"] == "missing_original":
            missing_count += 1
        elif result["action"] == "blocked":
            blocked_count += 1

    return {
        "scanned_run_count": scanned_count,
        "deleted_run_count": deleted_count,
        "eligible_run_count": eligible_count,
        "blocked_run_count": blocked_count,
        "missing_original_run_count": missing_count,
        "deleted_bytes": deleted_bytes,
        "original_bytes": original_bytes,
        "derived_bytes": derived_bytes,
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean up workstation-local replay originals after retention gates pass")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the base camera config JSON")
    parser.add_argument("--local-config", default=str(DEFAULT_LOCAL_OVERRIDE_PATH), help="Path to the optional workstation-local override JSON")
    parser.add_argument("--runs-root", default="", help="Override the configured runs root")
    parser.add_argument("--limit", type=int, default=0, help="Optional maximum number of manifests to inspect")
    parser.add_argument("--dry-run", action="store_true", help="Report cleanup eligibility without deleting files")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    config = load_effective_config(
        config_path=Path(args.config).resolve(),
        local_override_path=Path(args.local_config).resolve(),
    )
    runs_root = Path(args.runs_root).resolve() if args.runs_root else Path(config["storage"]["runs_root"]).resolve()
    payload = cleanup_runs(runs_root=runs_root, delete=not args.dry_run, limit=args.limit)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Scanned: {payload['scanned_run_count']}")
        print(f"Deleted: {payload['deleted_run_count']}")
        print(f"Eligible: {payload['eligible_run_count']}")
        print(f"Deleted bytes: {payload['deleted_bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
