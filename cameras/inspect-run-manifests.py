#!/usr/bin/env python3
"""
inspect-run-manifests.py

Diagnose replay manifests under one runs root and optionally quarantine or
delete stale manifest files whose paired video/trace paths are no longer valid.

This only touches `.run.json` files. It does not delete videos or traces.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, load_effective_config


def compute_run_id(manifest_path: Path, payload: dict) -> str:
    """Mirror the replay app's stable run id derivation."""
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


def load_run_manifest(manifest_path: Path) -> dict:
    """Read one run manifest and normalize the main absolute paths."""
    with manifest_path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)

    payload["manifest_path"] = str(manifest_path.resolve())
    payload["video_path"] = str(Path(payload.get("video_path", "")).resolve()) if payload.get("video_path") else ""
    payload["trace_path"] = str(Path(payload.get("trace_path", "")).resolve()) if payload.get("trace_path") else ""
    payload["run_id"] = compute_run_id(manifest_path, payload)
    return payload


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


def iter_manifest_paths(runs_root: Path) -> list[Path]:
    """Return replay manifests in newest-first order."""
    return sorted(runs_root.rglob("*.run.json"), key=lambda path: path.stat().st_mtime, reverse=True)


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
        "run_id": payload.get("run_id") or compute_run_id(manifest_path, payload),
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
    manifests = iter_manifest_paths(runs_root)
    items = [describe_manifest(path) for path in manifests]

    summary = {
        "runs_root": str(runs_root),
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
