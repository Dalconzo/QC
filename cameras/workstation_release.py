#!/usr/bin/env python3
"""
workstation_release.py

Small helpers for exposing deployed workstation version and recorder contract
status through the operator-facing tooling.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from replay_manifest import REPLAY_MANIFEST_CAPABILITIES, REPLAY_MANIFEST_VERSION


def _run_git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def get_deployment_status(repo_root: Path) -> dict:
    """Return best-effort git/deployment metadata for one workstation checkout."""
    commit = _run_git(repo_root, "rev-parse", "HEAD")
    branch = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    is_dirty = bool(_run_git(repo_root, "status", "--short"))
    return {
        "repo_root": str(repo_root.resolve()),
        "git_commit": commit,
        "git_commit_short": commit[:7] if commit else "",
        "git_branch": branch,
        "git_is_dirty": is_dirty,
        "available": bool(commit),
    }


def build_contract_status(config: dict) -> dict:
    """Summarize the recorder/storage contract that a workstation will follow."""
    storage = config.get("storage", {}) or {}
    retention = storage.get("retention", {}) or {}
    emergency = retention.get("emergency", {}) or {}
    compaction = storage.get("compaction", {}) or {}
    central_ingest = config.get("central_ingest", {}) or {}
    staging_cleanup = central_ingest.get("staging_cleanup", {}) or {}
    return {
        "replay_manifest_version": REPLAY_MANIFEST_VERSION,
        "replay_capabilities": list(REPLAY_MANIFEST_CAPABILITIES),
        "local_compaction_enabled": bool(compaction.get("enabled", False)),
        "retention_enabled": bool(retention.get("enabled", False)),
        "original_retention_days": int(retention.get("original_retention_days", 0) or 0),
        "require_upload_ack": bool(retention.get("require_upload_ack", True)),
        "require_local_compaction": bool(retention.get("require_local_compaction", False)),
        "cleanup_on_run_complete": bool(retention.get("cleanup_on_run_complete", False)),
        "emergency_cleanup_enabled": bool(emergency.get("enabled", False)),
        "central_ingest_transport": str(central_ingest.get("transport") or ""),
        "auto_upload_on_run_complete": bool(central_ingest.get("auto_upload_on_run_complete", False)),
        "staging_cleanup_enabled": bool(staging_cleanup.get("enabled", False)),
        "prune_after_ack": bool(staging_cleanup.get("prune_after_ack", False)),
    }
