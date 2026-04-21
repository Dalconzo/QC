#!/usr/bin/env python3
"""
camera_config.py

Shared config loader for the Hamilton camera tooling.

The recorder, replay app, and future daemon all need to agree on the same
workstation-local settings: where Hamilton writes traces, which process gates a
run, where video artifacts live, and how cameras are identified. Centralizing
that logic here keeps the PowerShell wrappers thin and gives us one place to
add local override support for each workstation.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "camera-recorder.json"
DEFAULT_LOCAL_OVERRIDE_PATH = REPO_ROOT / "config" / "camera-recorder.local.json"

DEFAULT_CONFIG = {
    "hamilton": {
        "log_dir": r"C:\Program Files (x86)\HAMILTON\LogFiles",
        "log_glob": "*.trc",
        "process_name": "HxRun.exe",
    },
    "storage": {
        "runs_root": str(REPO_ROOT / "cameras" / "video_clips"),
        "manifest_dir": "",
        "recorder_log_dir": str(REPO_ROOT / "logs"),
        "compaction": {
            "enabled": False,
            "artifacts_root": "",
            "min_segment_duration_sec": 5.0,
            "active_crf": 30,
            "active_preset": "veryfast",
            "idle_crf": 36,
            "idle_preset": "veryfast",
            "idle_fps": 2,
        },
        "retention": {
            "enabled": True,
            "original_retention_days": 7,
            "require_upload_ack": True,
            "require_local_compaction": False,
            "cleanup_on_run_complete": True,
        },
    },
    "recorder": {
        "default_profile": "default",
        "poll_sec": 1.0,
        "max_record_sec": 0,
        "startup_timeout_sec": 0,
        "dshow_rtbufsize": "256M",
        "ffmpeg_path": "",
        "stop_file": str(REPO_ROOT / "cameras" / "cameras.recorder.stop"),
    },
    "replay": {
        "host": "127.0.0.1",
        "port": 5050,
        "log_path": str(REPO_ROOT / "logs" / "camera-replay.log"),
    },
    "live": {
        "default_profile": "default",
        "frame_timeout_sec": 8,
        "refresh_ms": 1000,
        "jpeg_quality": 4,
    },
    "central_ingest": {
        "staging_root": str(REPO_ROOT / "cameras" / "central_staging"),
        "upload_root": str(REPO_ROOT / "cameras" / "central_replay_root"),
        "transport": "filesystem",
        "auto_upload_on_run_complete": False,
    },
    "daemon": {
        "task_name": "HamiltonCameraRecorderDaemon",
        "stop_file": str(REPO_ROOT / "cameras" / "camera-daemon.stop"),
        "pid_file": str(REPO_ROOT / "logs" / "camera-daemon.pid"),
        "status_path": str(REPO_ROOT / "logs" / "camera-daemon-status.json"),
        "log_path": str(REPO_ROOT / "logs" / "camera-daemon.log"),
        "idle_poll_sec": 1.0,
        "heartbeat_sec": 10.0,
        "relaunch_delay_sec": 2.0,
    },
    "profiles": [
        {
            "id": "default",
            "label": "Default Camera",
            "source": "0",
            "framerate": None,
            "video_size": None,
            "ffmpeg_path": "",
        }
    ],
}


def read_json_file(path: Path) -> dict:
    """Read a JSON file if it exists; missing files are treated as empty."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Camera config must be a JSON object: {path}")
    return payload


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Merge nested dictionaries while treating lists as whole replacements."""
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _legacy_to_nested(raw: dict) -> dict:
    """Translate the older flat config keys into the newer nested schema."""
    partial: dict = {}

    if raw.get("hamilton_log_dir") or raw.get("hamilton_log_glob"):
        partial.setdefault("hamilton", {})
        if raw.get("hamilton_log_dir"):
            partial["hamilton"]["log_dir"] = raw["hamilton_log_dir"]
        if raw.get("hamilton_log_glob"):
            partial["hamilton"]["log_glob"] = raw["hamilton_log_glob"]

    if (
        raw.get("default_poll_sec") is not None
        or raw.get("default_max_record_sec") is not None
        or raw.get("dshow_rtbufsize") is not None
    ):
        partial.setdefault("recorder", {})
        if raw.get("default_poll_sec") is not None:
            partial["recorder"]["poll_sec"] = raw["default_poll_sec"]
        if raw.get("default_max_record_sec") is not None:
            partial["recorder"]["max_record_sec"] = raw["default_max_record_sec"]
        if raw.get("dshow_rtbufsize") is not None:
            partial["recorder"]["dshow_rtbufsize"] = raw["dshow_rtbufsize"]

    if raw.get("manifest_dir"):
        partial.setdefault("storage", {})
        partial["storage"]["manifest_dir"] = raw["manifest_dir"]

    if raw.get("default_source") is not None:
        partial["profiles"] = [
            {
                "id": "default",
                "label": "Default Camera",
                "source": str(raw["default_source"]),
            }
        ]

    return partial


def _extract_nested(raw: dict) -> dict:
    """Keep only the top-level keys used by the modern camera config schema."""
    partial: dict = {}
    for key in ("hamilton", "storage", "recorder", "replay", "live", "central_ingest", "daemon", "profiles"):
        value = raw.get(key)
        if value is not None:
            partial[key] = copy.deepcopy(value)
    return partial


def normalize_config(raw: dict) -> dict:
    """Normalize one config payload into the shared nested schema."""
    nested = _extract_nested(raw)
    nested = _deep_merge(nested, _legacy_to_nested(raw))
    return nested


def _normalize_profiles(config: dict) -> dict:
    """Ensure every profile has a stable id and expected optional fields."""
    normalized = copy.deepcopy(config)
    raw_profiles = normalized.get("profiles") or []
    profiles: list[dict] = []
    for index, raw_profile in enumerate(raw_profiles):
        if not isinstance(raw_profile, dict):
            continue
        profile = {
            "id": str(raw_profile.get("id") or f"profile-{index + 1}"),
            "label": str(raw_profile.get("label") or raw_profile.get("id") or f"Camera {index + 1}"),
            "source": str(raw_profile.get("source") or ""),
            "framerate": raw_profile.get("framerate"),
            "video_size": raw_profile.get("video_size"),
            "ffmpeg_path": str(raw_profile.get("ffmpeg_path") or ""),
        }
        profiles.append(profile)

    if not profiles:
        profiles = copy.deepcopy(DEFAULT_CONFIG["profiles"])

    normalized["profiles"] = profiles
    return normalized


def load_effective_config(
    *,
    config_path: Path | None = None,
    local_override_path: Path | None = None,
) -> dict:
    """Load the merged base + local workstation override configuration."""
    base_path = config_path or DEFAULT_CONFIG_PATH
    local_path = local_override_path or DEFAULT_LOCAL_OVERRIDE_PATH

    base_partial = normalize_config(read_json_file(base_path))
    local_partial = normalize_config(read_json_file(local_path))

    effective = _deep_merge(DEFAULT_CONFIG, base_partial)
    effective = _deep_merge(effective, local_partial)
    effective = _normalize_profiles(effective)

    effective["config_path"] = str(base_path.resolve())
    effective["local_override_path"] = str(local_path.resolve())
    effective["local_override_exists"] = local_path.exists()
    return effective


def get_profile(config: dict, profile_id: str | None = None) -> dict:
    """Return the requested camera profile or the configured default."""
    desired = str(profile_id or config.get("recorder", {}).get("default_profile") or "default")
    for profile in config.get("profiles", []):
        if profile.get("id") == desired:
            return profile
    raise KeyError(desired)


def validate_config(config: dict, *, require_hamilton_log_dir: bool = True) -> dict:
    """Return validation errors and warnings for operator-facing config checks."""
    errors: list[str] = []
    warnings: list[str] = []

    hamilton = config.get("hamilton", {})
    storage = config.get("storage", {})
    recorder = config.get("recorder", {})
    replay = config.get("replay", {})
    live = config.get("live", {})
    central_ingest = config.get("central_ingest", {})
    daemon = config.get("daemon", {})
    profiles = config.get("profiles", [])

    if require_hamilton_log_dir:
        if not hamilton.get("log_dir"):
            errors.append("hamilton.log_dir is required.")
        elif not Path(str(hamilton["log_dir"])).exists():
            errors.append(f"Hamilton log directory does not exist: {hamilton['log_dir']}")
    elif hamilton.get("log_dir") and not Path(str(hamilton["log_dir"])).exists():
        warnings.append(f"Hamilton log directory is missing on this machine: {hamilton['log_dir']}")

    if not hamilton.get("process_name"):
        errors.append("hamilton.process_name is required.")

    if recorder.get("poll_sec") is None or float(recorder.get("poll_sec", 0)) <= 0:
        errors.append("recorder.poll_sec must be greater than 0.")

    if int(recorder.get("max_record_sec", 0)) < 0:
        errors.append("recorder.max_record_sec cannot be negative.")

    if int(recorder.get("startup_timeout_sec", 0)) < 0:
        errors.append("recorder.startup_timeout_sec cannot be negative.")

    dshow_rtbufsize = str(recorder.get("dshow_rtbufsize") or "").strip()
    if not dshow_rtbufsize:
        errors.append("recorder.dshow_rtbufsize is required.")

    replay_port = int(replay.get("port", 0))
    if replay_port <= 0 or replay_port > 65535:
        errors.append("replay.port must be between 1 and 65535.")

    replay_log_path = str(replay.get("log_path") or "")
    if not replay_log_path.strip():
        errors.append("replay.log_path is required.")

    if int(live.get("frame_timeout_sec", 0)) <= 0:
        errors.append("live.frame_timeout_sec must be greater than 0.")

    if int(live.get("refresh_ms", 0)) <= 0:
        errors.append("live.refresh_ms must be greater than 0.")

    jpeg_quality = int(live.get("jpeg_quality", -1))
    if jpeg_quality < 2 or jpeg_quality > 31:
        errors.append("live.jpeg_quality must be between 2 and 31.")

    staging_root = str(central_ingest.get("staging_root") or "").strip()
    if not staging_root:
        errors.append("central_ingest.staging_root is required.")
    elif not Path(staging_root).exists():
        warnings.append(f"Central ingest staging root does not exist yet and will be created on first use: {staging_root}")

    upload_root = str(central_ingest.get("upload_root") or "").strip()
    if not upload_root:
        errors.append("central_ingest.upload_root is required.")
    elif not Path(upload_root).exists():
        warnings.append(f"Central ingest upload root does not exist yet and will be created on first use: {upload_root}")

    transport = str(central_ingest.get("transport") or "").strip().lower()
    if transport not in {"filesystem"}:
        errors.append("central_ingest.transport must currently be 'filesystem'.")

    default_live_profile = str(live.get("default_profile") or "").strip()
    if default_live_profile and default_live_profile not in {str(profile.get("id")) for profile in profiles}:
        errors.append(f"live.default_profile does not match a configured profile: {default_live_profile}")

    if float(daemon.get("idle_poll_sec", 0)) <= 0:
        errors.append("daemon.idle_poll_sec must be greater than 0.")

    if float(daemon.get("heartbeat_sec", 0)) <= 0:
        errors.append("daemon.heartbeat_sec must be greater than 0.")

    if float(daemon.get("relaunch_delay_sec", 0)) < 0:
        errors.append("daemon.relaunch_delay_sec cannot be negative.")

    if not str(daemon.get("task_name") or "").strip():
        errors.append("daemon.task_name is required.")

    for field_name in ("stop_file", "pid_file", "status_path", "log_path"):
        if not str(daemon.get(field_name) or "").strip():
            errors.append(f"daemon.{field_name} is required.")

    if not profiles:
        errors.append("At least one camera profile is required.")

    seen_ids: set[str] = set()
    for profile in profiles:
        profile_id = str(profile.get("id") or "")
        if not profile_id:
            errors.append("Every camera profile needs a non-empty id.")
            continue
        if profile_id in seen_ids:
            errors.append(f"Duplicate camera profile id: {profile_id}")
        seen_ids.add(profile_id)
        if not str(profile.get("source") or "").strip():
            errors.append(f"Camera profile '{profile_id}' is missing a source.")
        ffmpeg_path = str(profile.get("ffmpeg_path") or "")
        if ffmpeg_path and not Path(ffmpeg_path).exists():
            errors.append(f"Camera profile '{profile_id}' points to a missing ffmpeg path: {ffmpeg_path}")

    default_profile = str(recorder.get("default_profile") or "default")
    if default_profile not in seen_ids:
        errors.append(f"recorder.default_profile does not match any configured profile: {default_profile}")

    configured_ffmpeg = str(recorder.get("ffmpeg_path") or "")
    if configured_ffmpeg and not Path(configured_ffmpeg).exists():
        errors.append(f"Configured recorder.ffmpeg_path does not exist: {configured_ffmpeg}")

    runs_root = Path(str(storage.get("runs_root") or ""))
    if not str(runs_root):
        errors.append("storage.runs_root is required.")
    elif not runs_root.exists():
        warnings.append(f"Runs root does not exist yet and will be created on first use: {runs_root}")

    recorder_log_dir = str(storage.get("recorder_log_dir") or "")
    if recorder_log_dir and not Path(recorder_log_dir).exists():
        warnings.append(f"Recorder log dir does not exist yet and will be created on first use: {recorder_log_dir}")

    compaction = storage.get("compaction") or {}
    if not isinstance(compaction, dict):
        errors.append("storage.compaction must be an object when provided.")
    else:
        min_segment_duration_sec = float(compaction.get("min_segment_duration_sec", 0) or 0)
        if min_segment_duration_sec < 0:
            errors.append("storage.compaction.min_segment_duration_sec cannot be negative.")

        idle_fps = int(compaction.get("idle_fps", 0) or 0)
        if idle_fps < 0:
            errors.append("storage.compaction.idle_fps cannot be negative.")

        for field_name in ("active_crf", "idle_crf"):
            value = int(compaction.get(field_name, -1) or -1)
            if value < 0 or value > 51:
                errors.append(f"storage.compaction.{field_name} must be between 0 and 51.")

        for field_name in ("active_preset", "idle_preset"):
            if not str(compaction.get(field_name) or "").strip():
                errors.append(f"storage.compaction.{field_name} is required.")

    retention = storage.get("retention") or {}
    if not isinstance(retention, dict):
        errors.append("storage.retention must be an object when provided.")
    else:
        if int(retention.get("original_retention_days", 0) or 0) < 0:
            errors.append("storage.retention.original_retention_days cannot be negative.")

    return {"errors": errors, "warnings": warnings}
