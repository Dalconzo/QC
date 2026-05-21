#!/usr/bin/env python3
"""
inspect-run-manifests.py

Diagnose replay manifests under one runs root and optionally quarantine or
delete stale manifest files whose paired video/trace paths are no longer valid.

This only touches `.run.json` files. It does not delete videos or traces.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import shutil
import sys
from pathlib import Path

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, load_effective_config
from replay_manifest import normalize_replay_manifest_payload
from replay_tags import derive_run_tags


def emit_log(message: str) -> None:
    """Mirror manifest inspection diagnostics to the operator shell."""
    logging.getLogger("camera.inspect_run_manifests").info(message)
    print(message, file=sys.stdout)


def load_run_manifest(manifest_path: Path, *, log_fn=emit_log) -> dict:
    """Read one run manifest and normalize the main absolute paths."""
    with manifest_path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    normalized = normalize_replay_manifest_payload(payload, manifest_path=manifest_path)
    trace_path = Path(normalized["trace_path"]) if normalized.get("trace_path") else None
    tag_payload = derive_run_tags(trace_path, log_fn=log_fn)
    normalized["run_tags_version"] = tag_payload["version"]
    normalized["run_tags"] = tag_payload["tags"]
    normalized["run_tag_summary"] = tag_payload["summary"]
    normalized["run_tag_search_text"] = tag_payload["search_text"]
    if log_fn:
        summary = tag_payload["summary"]
        log_fn(
            "[inspect-run-manifests] "
            f"manifest={manifest_path.resolve()} "
            f"trace={trace_path.resolve() if trace_path else ''} "
            f"outcome={summary.get('outcome') or 'unknown'} "
            f"primary_barcode={summary.get('primary_barcode') or '-'} "
            f"replay_tag_count={summary.get('tag_count') or 0}"
        )
    return normalized


def determine_replay_status(payload: dict) -> str:
    """Classify whether the manifest is immediately replayable."""
    has_video = bool(payload.get("video_path") and Path(payload["video_path"]).exists())
    has_trace = bool(payload.get("trace_path") and Path(payload["trace_path"]).exists())
    if has_video and has_trace:
        return "ready"
    if has_video:
        return "missing_trace"
    if has_trace:
        return "missing_video"
    return "missing_video_and_trace"


def iter_manifest_paths(runs_root: Path, *, recent_days: float = 0) -> list[Path]:
    """Return replay manifests in newest-first order, optionally filtered by recency."""
    cutoff_timestamp = None
    if recent_days > 0:
        cutoff_timestamp = (dt.datetime.now() - dt.timedelta(days=recent_days)).timestamp()

    manifests: list[Path] = []
    for path in runs_root.rglob("*.run.json"):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if cutoff_timestamp is not None and stat_result.st_mtime < cutoff_timestamp:
            continue
        manifests.append(path)
    manifests.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return manifests


def describe_manifest(manifest_path: Path) -> dict:
    """Summarize one manifest and why it is or is not replayable."""
    payload = load_run_manifest(manifest_path)
    video_path = Path(payload["video_path"]) if payload.get("video_path") else None
    trace_path = Path(payload["trace_path"]) if payload.get("trace_path") else None
    has_video = bool(video_path and video_path.exists())
    has_trace = bool(trace_path and trace_path.exists())
    status = determine_replay_status(payload)
    problems: list[str] = []

    if not payload.get("video_path"):
        problems.append("Manifest does not declare a video_path.")
    elif not has_video:
        problems.append(f"Video path is missing on disk: {payload['video_path']}")

    if not payload.get("trace_path"):
        problems.append("Manifest does not declare a trace_path.")
    elif not has_trace:
        problems.append(f"Trace path is missing on disk: {payload['trace_path']}")

    return {
        "run_id": payload.get("run_id") or "",
        "manifest_path": str(manifest_path.resolve()),
        "label": payload.get("label") or "run",
        "started_at_local": payload.get("started_at_local"),
        "video_path": payload.get("video_path") or "",
        "trace_path": payload.get("trace_path") or "",
        "has_video": has_video,
        "has_trace": has_trace,
        "replay_status": status,
        "problems": problems,
    }


def load_runs_root(config_path: Path, local_config_path: Path, explicit_runs_root: str) -> Path:
    """Resolve the runs root from CLI or merged camera config."""
    if explicit_runs_root:
        return Path(explicit_runs_root).resolve()
    config = load_effective_config(config_path=config_path, local_override_path=local_config_path)
    return Path(config["storage"]["runs_root"]).resolve()


def should_clean(item: dict, *, mode: str) -> bool:
    """Decide whether the manifest matches the requested cleanup mode."""
    status = item["replay_status"]
    if mode == "all-stale":
        return status != "ready"
    if mode == "missing-video":
        return status in {"missing_video", "missing_video_and_trace"}
    if mode == "missing-trace":
        return status in {"missing_trace", "missing_video_and_trace"}
    raise ValueError(f"Unsupported cleanup mode: {mode}")


def quarantine_manifest(item: dict, quarantine_root: Path, runs_root: Path) -> str:
    """Move one stale manifest into a quarantine tree under the runs root."""
    manifest_path = Path(item["manifest_path"])
    try:
        relative_path = manifest_path.relative_to(runs_root)
    except ValueError:
        relative_path = Path(manifest_path.name)

    target_path = quarantine_root / relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(manifest_path), str(target_path))
    return str(target_path.resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or clean replay run manifests")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the base camera config JSON")
    parser.add_argument("--local-config", default=str(DEFAULT_LOCAL_OVERRIDE_PATH), help="Path to the optional workstation-local override JSON")
    parser.add_argument("--runs-root", default="", help="Directory that contains .run.json replay manifests")
    parser.add_argument("--recent-days", type=float, default=0, help="Only inspect manifests modified within this many days. 0 scans all history.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--cleanup",
        choices=("none", "all-stale", "missing-video", "missing-trace"),
        default="none",
        help="Optionally remove or quarantine stale manifests after inspection",
    )
    parser.add_argument("--delete", action="store_true", help="Delete matching stale manifests instead of quarantining them")
    parser.add_argument("--quarantine-dir", default="", help="Directory where stale manifests should be moved")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    local_config_path = Path(args.local_config).resolve()
    runs_root = load_runs_root(config_path, local_config_path, args.runs_root)
    manifests = iter_manifest_paths(runs_root, recent_days=max(0.0, args.recent_days))
    items = [describe_manifest(path) for path in manifests]

    summary = {
        "runs_root": str(runs_root),
        "recent_days": max(0.0, args.recent_days),
        "manifest_count": len(items),
        "ready_count": sum(1 for item in items if item["replay_status"] == "ready"),
        "stale_count": sum(1 for item in items if item["replay_status"] != "ready"),
    }

    cleanup_results: list[dict] = []
    if args.cleanup != "none":
        quarantine_dir = Path(args.quarantine_dir).resolve() if args.quarantine_dir else (runs_root / "_quarantine")
        for item in items:
            if not should_clean(item, mode=args.cleanup):
                continue
            manifest_path = Path(item["manifest_path"])
            if args.delete:
                manifest_path.unlink(missing_ok=False)
                cleanup_results.append({"manifest_path": item["manifest_path"], "action": "deleted"})
            else:
                target_path = quarantine_manifest(item, quarantine_dir, runs_root)
                cleanup_results.append(
                    {
                        "manifest_path": item["manifest_path"],
                        "action": "quarantined",
                        "target_path": target_path,
                    }
                )

    payload = {
        "summary": summary,
        "items": items,
        "cleanup": cleanup_results,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Runs root: {summary['runs_root']}")
        print(f"Manifests: {summary['manifest_count']}")
        print(f"Ready: {summary['ready_count']}")
        print(f"Stale: {summary['stale_count']}")
        for item in items:
            print(f"- {item['label']} [{item['replay_status']}]")
            print(f"  manifest: {item['manifest_path']}")
            if item["problems"]:
                for problem in item["problems"]:
                    print(f"  problem: {problem}")
        if cleanup_results:
            print("Cleanup:")
            for result in cleanup_results:
                target = result.get("target_path")
                if target:
                    print(f"  - {result['action']}: {result['manifest_path']} -> {target}")
                else:
                    print(f"  - {result['action']}: {result['manifest_path']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
