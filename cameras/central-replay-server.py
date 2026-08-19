#!/usr/bin/env python3
"""
central-replay-server.py

Serve the first LAN browse/API layer for centrally ingested replay runs.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import logging
import mimetypes
import re
import sqlite3
import socket
from contextlib import closing
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, load_effective_config
from replay_tags import derive_run_tags, serialize_summary, serialize_tags
from upload_central_replay import CENTRAL_CATALOG_FILENAME, init_central_db


STATIC_DIR = Path(__file__).resolve().parent / "central_replay_static"
REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL_PATH = Path(__file__).resolve().parent / "sql" / "central-replay-schema.sql"
DEFAULT_SERVER_CONFIG_PATH = REPO_ROOT / "config" / "central-replay-server.json"
DEFAULT_SERVER_LOCAL_CONFIG_PATH = REPO_ROOT / "config" / "central-replay-server.local.json"
TRACE_LINE_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})> ?(?P<body>.*)$")
PENDING_RUN_PREFIX = "pending-run:"
FILE_STREAM_CHUNK_SIZE = 1024 * 1024
REQUIRED_ARTIFACT_TYPES = (
    ("run_manifest_json", "application/json"),
    ("video_mp4", "video/mp4"),
    ("trace_trc", "text/plain"),
)
STATUS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workstation_runtime_status (
    workstation_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL DEFAULT '',
    machine_alias TEXT NOT NULL DEFAULT '',
    repo_root TEXT NOT NULL DEFAULT '',
    profile_key TEXT NOT NULL DEFAULT '',
    profile_label TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    local_ip TEXT NOT NULL DEFAULT '',
    software_version TEXT NOT NULL DEFAULT '',
    current_state TEXT NOT NULL DEFAULT 'idle',
    upload_phase TEXT NOT NULL DEFAULT '',
    current_local_run_id TEXT NOT NULL DEFAULT '',
    current_label TEXT NOT NULL DEFAULT '',
    current_started_at_local TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    last_event_kind TEXT NOT NULL DEFAULT '',
    last_event_utc TEXT NOT NULL DEFAULT '',
    last_heartbeat_utc TEXT NOT NULL DEFAULT '',
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_runs (
    pending_run_id TEXT PRIMARY KEY,
    workstation_id TEXT NOT NULL,
    local_run_id TEXT NOT NULL,
    camera_profile_id TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    process_gate TEXT NOT NULL DEFAULT '',
    stop_reason TEXT NOT NULL DEFAULT '',
    started_at_local TEXT NOT NULL DEFAULT '',
    stopped_at_local TEXT NOT NULL DEFAULT '',
    duration_sec REAL NOT NULL DEFAULT 0,
    hamilton_log_dir TEXT NOT NULL DEFAULT '',
    hamilton_log_glob TEXT NOT NULL DEFAULT '',
    trace_pairing_delta_sec REAL,
    replay_status TEXT NOT NULL DEFAULT 'pending_upload',
    upload_phase TEXT NOT NULL DEFAULT '',
    local_manifest_path TEXT NOT NULL DEFAULT '',
    local_video_path TEXT NOT NULL DEFAULT '',
    local_trace_path TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    first_reported_utc TEXT NOT NULL,
    last_reported_utc TEXT NOT NULL,
    promoted_central_run_id TEXT NOT NULL DEFAULT '',
    UNIQUE (workstation_id, local_run_id),
    FOREIGN KEY (workstation_id) REFERENCES workstations(workstation_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_runs_started
    ON pending_runs(started_at_local DESC);

CREATE INDEX IF NOT EXISTS idx_pending_runs_workstation_started
    ON pending_runs(workstation_id, started_at_local DESC);
"""

DEFAULT_SERVER_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 5080,
        "site_name": "Central Replay",
        "log_path": str(REPO_ROOT / "logs" / "central-replay-server.log"),
        "healthcheck_path": "/api/healthz",
        "workstation_heartbeat_timeout_sec": 30.0,
    },
    "storage": {
        "upload_root": str(REPO_ROOT / "cameras" / "central_replay_root"),
        "catalog_path": "",
    },
}


@dataclass
class TraceEvent:
    index: int
    elapsed_sec: float
    stamp_local: str
    line: str


@dataclass
class RuntimeSettings:
    server_config_path: str
    server_local_config_path: str
    server_local_override_exists: bool
    camera_config_path: str
    camera_local_config_path: str
    camera_config_fallback_used: bool
    upload_root: str
    catalog_path: str
    host: str
    port: int
    site_name: str
    log_path: str
    healthcheck_path: str
    workstation_heartbeat_timeout_sec: float


def read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Central replay server config must be a JSON object: {path}")
    return payload


def utc_now_text() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def heartbeat_cutoff_text(timeout_sec: float) -> str:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=timeout_sec)
    return cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")


def pending_run_id_for(workstation_id: str, local_run_id: str) -> str:
    return f"{PENDING_RUN_PREFIX}{workstation_id}:{local_run_id}"


def is_pending_run_id(value: str) -> bool:
    return value.startswith(PENDING_RUN_PREFIX)


def deep_merge(base: dict, overlay: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def normalize_server_config(raw: dict) -> dict:
    partial: dict = {}
    for key in ("server", "storage"):
        value = raw.get(key)
        if isinstance(value, dict):
            partial[key] = copy.deepcopy(value)
    return partial


def load_server_config(
    *,
    config_path: Path | None = None,
    local_override_path: Path | None = None,
) -> dict:
    base_path = config_path or DEFAULT_SERVER_CONFIG_PATH
    local_path = local_override_path or DEFAULT_SERVER_LOCAL_CONFIG_PATH
    base_partial = normalize_server_config(read_json_file(base_path))
    local_partial = normalize_server_config(read_json_file(local_path))
    effective = deep_merge(DEFAULT_SERVER_CONFIG, base_partial)
    effective = deep_merge(effective, local_partial)
    effective["config_path"] = str(base_path.resolve())
    effective["local_override_path"] = str(local_path.resolve())
    effective["local_override_exists"] = local_path.exists()
    return effective


def validate_runtime_settings(runtime: RuntimeSettings) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if not runtime.host.strip():
        errors.append("server.host is required.")
    if runtime.port <= 0 or runtime.port > 65535:
        errors.append("server.port must be between 1 and 65535.")
    if not runtime.log_path.strip():
        errors.append("server.log_path is required.")
    if not runtime.site_name.strip():
        errors.append("server.site_name is required.")
    if not runtime.healthcheck_path.startswith("/"):
        errors.append("server.healthcheck_path must start with '/'.")
    if runtime.workstation_heartbeat_timeout_sec <= 0:
        errors.append("server.workstation_heartbeat_timeout_sec must be greater than 0.")
    if not runtime.upload_root.strip():
        errors.append("storage.upload_root is required.")
    elif not Path(runtime.upload_root).exists():
        warnings.append(f"Central upload root does not exist yet: {runtime.upload_root}")

    catalog_path = Path(runtime.catalog_path)
    if runtime.catalog_path.strip() and not catalog_path.exists():
        warnings.append(f"Central replay catalog does not exist yet: {runtime.catalog_path}")

    return {"errors": errors, "warnings": warnings}


def resolve_runtime_settings(args: argparse.Namespace) -> RuntimeSettings:
    server_config = load_server_config(
        config_path=Path(args.server_config),
        local_override_path=Path(args.server_local_config),
    )

    upload_root = str(args.upload_root or server_config["storage"].get("upload_root") or "").strip()
    camera_config_fallback_used = False
    if not upload_root:
        camera_config = load_effective_config(
            config_path=Path(args.config),
            local_override_path=Path(args.local_config),
        )
        upload_root = str(camera_config.get("central_ingest", {}).get("upload_root") or "").strip()
        camera_config_fallback_used = True

    upload_root_path = Path(upload_root).resolve() if upload_root else Path()
    explicit_catalog_path = str(args.catalog_path or server_config["storage"].get("catalog_path") or "").strip()
    if explicit_catalog_path:
        catalog_path = Path(explicit_catalog_path).resolve()
    elif upload_root:
        catalog_path = (upload_root_path / CENTRAL_CATALOG_FILENAME).resolve()
    else:
        catalog_path = Path()

    return RuntimeSettings(
        server_config_path=str(Path(args.server_config).resolve()),
        server_local_config_path=str(Path(args.server_local_config).resolve()),
        server_local_override_exists=Path(args.server_local_config).exists(),
        camera_config_path=str(Path(args.config).resolve()),
        camera_local_config_path=str(Path(args.local_config).resolve()),
        camera_config_fallback_used=camera_config_fallback_used,
        upload_root=str(upload_root_path) if upload_root else "",
        catalog_path=str(catalog_path) if str(catalog_path) else "",
        host=str(args.host or server_config["server"].get("host") or "").strip(),
        port=int(args.port if args.port is not None else server_config["server"].get("port", 5080)),
        site_name=str(args.site_name or server_config["server"].get("site_name") or "").strip(),
        log_path=str(Path(args.log_path).resolve()) if args.log_path else str(server_config["server"].get("log_path") or "").strip(),
        healthcheck_path=str(args.health_path or server_config["server"].get("healthcheck_path") or "/api/healthz").strip(),
        workstation_heartbeat_timeout_sec=float(
            args.workstation_heartbeat_timeout_sec
            if args.workstation_heartbeat_timeout_sec is not None
            else server_config["server"].get("workstation_heartbeat_timeout_sec", 30.0)
        ),
    )


def parse_trace_events(trace_path: Path) -> list[TraceEvent]:
    events: list[TraceEvent] = []
    first_stamp: dt.datetime | None = None
    with trace_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            if not line:
                continue
            match = TRACE_LINE_RE.match(line)
            if not match:
                continue
            stamp = dt.datetime.strptime(match.group("stamp"), "%Y-%m-%d %H:%M:%S")
            if first_stamp is None:
                first_stamp = stamp
            elapsed_sec = max(0.0, (stamp - first_stamp).total_seconds())
            events.append(
                TraceEvent(
                    index=len(events),
                    elapsed_sec=elapsed_sec,
                    stamp_local=stamp.isoformat(sep=" "),
                    line=line,
                )
            )
    return events


def stream_file_handle(
    handle,
    writer,
    *,
    start: int = 0,
    byte_count: int | None = None,
    chunk_size: int = FILE_STREAM_CHUNK_SIZE,
) -> None:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")

    if start:
        handle.seek(start)

    remaining = byte_count
    while remaining is None or remaining > 0:
        read_size = chunk_size if remaining is None else min(chunk_size, remaining)
        chunk = handle.read(read_size)
        if not chunk:
            break
        writer.write(chunk)
        if remaining is not None:
            remaining -= len(chunk)


def get_db_connection(catalog_path: Path) -> sqlite3.Connection:
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(catalog_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL_PATH.read_text(encoding="utf-8"))
    conn.executescript(STATUS_SCHEMA_SQL)
    conn.commit()
    return conn


def resolve_storage_path(upload_root: Path, storage_relpath: str) -> Path:
    candidate = (upload_root / storage_relpath).resolve()
    try:
        candidate.relative_to(upload_root.resolve())
    except ValueError as exc:
        raise ValueError("Artifact path escapes upload root") from exc
    return candidate


def encode_media_relpath(storage_relpath: str) -> str:
    return "/".join(quote(part) for part in storage_relpath.split("/") if part)


def artifact_row_to_summary(artifact: sqlite3.Row) -> dict:
    storage_relpath = str(artifact["storage_relpath"] or "").replace("\\", "/")
    return {
        "artifact_id": artifact["artifact_id"],
        "artifact_type": artifact["artifact_type"],
        "original_filename": artifact["original_filename"],
        "storage_relpath": storage_relpath,
        "mime_type": artifact["mime_type"],
        "compression_kind": artifact["compression_kind"],
        "content_sha256": artifact["content_sha256"],
        "size_bytes": artifact["size_bytes"],
        "stored_at_utc": artifact["stored_at_utc"],
        "is_required": bool(artifact["is_required"]),
        "is_ready": bool(artifact["is_ready"]),
        "media_url": f"/media/{encode_media_relpath(storage_relpath)}" if storage_relpath else "",
    }


def append_missing_required_artifacts(artifacts: list[dict], central_run_id: str) -> list[dict]:
    items = [dict(item) for item in artifacts]
    by_type = {str(item["artifact_type"]): item for item in items}
    for artifact_type, mime_type in REQUIRED_ARTIFACT_TYPES:
        if artifact_type in by_type:
            continue
        items.append(
            {
                "artifact_id": f"{central_run_id}:{artifact_type}:missing",
                "artifact_type": artifact_type,
                "original_filename": "",
                "storage_relpath": "",
                "mime_type": mime_type,
                "compression_kind": "none",
                "content_sha256": "",
                "size_bytes": 0,
                "stored_at_utc": "",
                "is_required": True,
                "is_ready": False,
                "media_url": "",
            }
        )
    items.sort(key=lambda item: (str(item["artifact_type"]), str(item["original_filename"])))
    return items


def summarize_artifact_health(artifacts: list[dict]) -> dict:
    required_artifacts = [item for item in artifacts if item.get("is_required")]
    missing_required = [item for item in required_artifacts if not item.get("is_ready")]
    return {
        "required_artifact_count": len(required_artifacts),
        "ready_artifact_count": sum(1 for item in required_artifacts if item.get("is_ready")),
        "missing_required_artifact_count": len(missing_required),
        "missing_required_artifact_types": [str(item["artifact_type"]) for item in missing_required],
        "has_all_required_artifacts": len(missing_required) == 0,
    }


def summarize_run_row(row: sqlite3.Row, artifacts: list[dict]) -> dict:
    artifact_by_type = {item["artifact_type"]: item for item in artifacts}
    video_artifact = artifact_by_type.get("video_mp4")
    trace_artifact = artifact_by_type.get("trace_trc")
    manifest_artifact = artifact_by_type.get("run_manifest_json")
    artifact_health = summarize_artifact_health(artifacts)
    run_tags = json.loads(row["run_tags_json"] or "[]")
    run_tag_summary = json.loads(row["run_tag_summary_json"] or "{}")
    return {
        "central_run_id": row["central_run_id"],
        "local_run_id": row["local_run_id"],
        "label": row["label"],
        "source_name": row["source_name"],
        "process_gate": row["process_gate"],
        "stop_reason": row["stop_reason"],
        "started_at_local": row["started_at_local"],
        "stopped_at_local": row["stopped_at_local"],
        "duration_sec": row["duration_sec"],
        "hamilton_log_dir": row["hamilton_log_dir"],
        "hamilton_log_glob": row["hamilton_log_glob"],
        "trace_pairing_delta_sec": row["trace_pairing_delta_sec"],
        "replay_manifest_version": row["replay_manifest_version"],
        "replay_capabilities": json.loads(row["replay_capabilities_json"] or "[]"),
        "storage_tier": row["storage_tier"],
        "replay_default_mode": row["replay_default_mode"],
        "segment_count": row["segment_count"],
        "idle_segment_count": row["idle_segment_count"],
        "active_segment_count": row["active_segment_count"],
        "replay_status": row["replay_status"],
        "run_tags_version": row["run_tags_version"],
        "run_tags": run_tags,
        "run_tag_summary": run_tag_summary,
        "run_outcome": row["run_outcome_tag"] or run_tag_summary.get("outcome") or "",
        "primary_barcode": row["primary_barcode"] or run_tag_summary.get("primary_barcode") or "",
        "ready_artifact_count": artifact_health["ready_artifact_count"],
        "required_artifact_count": artifact_health["required_artifact_count"],
        "missing_required_artifact_count": artifact_health["missing_required_artifact_count"],
        "missing_required_artifact_types": artifact_health["missing_required_artifact_types"],
        "has_all_required_artifacts": artifact_health["has_all_required_artifacts"],
        "first_ingested_utc": row["first_ingested_utc"],
        "last_ingested_utc": row["last_ingested_utc"],
        "workstation_id": row["workstation_id"],
        "workstation_hostname": row["hostname"],
        "machine_alias": row["machine_alias"],
        "camera_profile_id": row["camera_profile_id"],
        "camera_profile_label": row["profile_label"],
        "video_filename": video_artifact["original_filename"] if video_artifact else "",
        "trace_filename": trace_artifact["original_filename"] if trace_artifact else "",
        "manifest_filename": manifest_artifact["original_filename"] if manifest_artifact else "",
        "video_url": video_artifact["media_url"] if video_artifact else "",
        "trace_events_url": f"/api/runs/{row['central_run_id']}/trace-events",
        "artifacts_url": f"/api/runs/{row['central_run_id']}/artifacts",
    }


def summarize_pending_run_row(row: sqlite3.Row) -> dict:
    artifacts = append_missing_required_artifacts([], str(row["pending_run_id"]))
    artifact_health = summarize_artifact_health(artifacts)
    return {
        "central_run_id": row["pending_run_id"],
        "local_run_id": row["local_run_id"],
        "label": row["label"],
        "source_name": row["source_name"],
        "process_gate": row["process_gate"],
        "stop_reason": row["stop_reason"],
        "started_at_local": row["started_at_local"],
        "stopped_at_local": row["stopped_at_local"],
        "duration_sec": row["duration_sec"],
        "hamilton_log_dir": row["hamilton_log_dir"],
        "hamilton_log_glob": row["hamilton_log_glob"],
        "trace_pairing_delta_sec": row["trace_pairing_delta_sec"],
        "replay_status": row["replay_status"],
        "ready_artifact_count": artifact_health["ready_artifact_count"],
        "required_artifact_count": artifact_health["required_artifact_count"],
        "missing_required_artifact_count": artifact_health["missing_required_artifact_count"],
        "missing_required_artifact_types": artifact_health["missing_required_artifact_types"],
        "has_all_required_artifacts": artifact_health["has_all_required_artifacts"],
        "first_ingested_utc": row["first_reported_utc"],
        "last_ingested_utc": row["last_reported_utc"],
        "workstation_id": row["workstation_id"],
        "workstation_hostname": row["hostname"],
        "machine_alias": row["machine_alias"],
        "camera_profile_id": row["camera_profile_id"],
        "camera_profile_label": row["profile_label"],
        "video_filename": Path(str(row["local_video_path"] or "")).name,
        "trace_filename": Path(str(row["local_trace_path"] or "")).name,
        "manifest_filename": Path(str(row["local_manifest_path"] or "")).name,
        "video_url": "",
        "trace_events_url": "",
        "artifacts_url": f"/api/runs/{row['pending_run_id']}/artifacts",
        "upload_phase": row["upload_phase"],
        "last_error": row["last_error"],
        "is_pending": True,
    }


def ensure_workstation_record(conn: sqlite3.Connection, payload: dict, *, now_utc: str) -> str:
    workstation = payload.get("workstation") or {}
    workstation_id = str(workstation.get("workstation_id") or workstation.get("hostname") or socket.gethostname()).strip().lower()
    hostname = str(workstation.get("hostname") or workstation_id).strip() or workstation_id
    machine_alias = str(workstation.get("machine_alias") or "").strip()
    repo_root = str(workstation.get("repo_root") or "").strip()
    existing = conn.execute(
        "SELECT workstation_id FROM workstations WHERE workstation_id = ?",
        (workstation_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO workstations (
                workstation_id,
                hostname,
                machine_alias,
                instrument_name,
                site_name,
                repo_root,
                first_seen_utc,
                last_seen_utc
            ) VALUES (?, ?, ?, '', '', ?, ?, ?)
            """,
            (workstation_id, hostname, machine_alias, repo_root, now_utc, now_utc),
        )
    else:
        conn.execute(
            """
            UPDATE workstations
            SET hostname = ?,
                machine_alias = ?,
                repo_root = ?,
                last_seen_utc = ?
            WHERE workstation_id = ?
            """,
            (hostname, machine_alias, repo_root, now_utc, workstation_id),
        )
    return workstation_id


def ensure_camera_profile_record(conn: sqlite3.Connection, payload: dict, workstation_id: str, *, now_utc: str) -> str:
    profile = payload.get("camera_profile") or {}
    profile_key = str(profile.get("profile_key") or profile.get("profile_id") or "default").strip() or "default"
    camera_profile_id = f"{workstation_id}:{profile_key}"
    profile_label = str(profile.get("profile_label") or profile_key).strip() or profile_key
    source_name = str(profile.get("source_name") or "").strip()
    existing = conn.execute(
        "SELECT camera_profile_id FROM camera_profiles WHERE camera_profile_id = ?",
        (camera_profile_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO camera_profiles (
                camera_profile_id,
                workstation_id,
                profile_key,
                profile_label,
                source_name,
                is_active,
                first_seen_utc,
                last_seen_utc
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (camera_profile_id, workstation_id, profile_key, profile_label, source_name, now_utc, now_utc),
        )
    else:
        conn.execute(
            """
            UPDATE camera_profiles
            SET profile_label = ?,
                source_name = ?,
                is_active = 1,
                last_seen_utc = ?
            WHERE camera_profile_id = ?
            """,
            (profile_label, source_name, now_utc, camera_profile_id),
        )
    return camera_profile_id


def record_workstation_status(catalog_path: Path, payload: dict, *, event_kind: str) -> dict:
    now_utc = utc_now_text()
    workstation = payload.get("workstation") or {}
    status = payload.get("status") or {}
    run_payload = payload.get("run") or {}

    with closing(get_db_connection(catalog_path)) as conn:
        workstation_id = ensure_workstation_record(conn, payload, now_utc=now_utc)
        camera_profile_id = ensure_camera_profile_record(conn, payload, workstation_id, now_utc=now_utc)
        conn.execute(
            """
            INSERT INTO workstation_runtime_status (
                workstation_id,
                hostname,
                machine_alias,
                repo_root,
                profile_key,
                profile_label,
                source_name,
                local_ip,
                software_version,
                current_state,
                upload_phase,
                current_local_run_id,
                current_label,
                current_started_at_local,
                last_error,
                last_event_kind,
                last_event_utc,
                last_heartbeat_utc,
                updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workstation_id) DO UPDATE SET
                hostname = excluded.hostname,
                machine_alias = excluded.machine_alias,
                repo_root = excluded.repo_root,
                profile_key = excluded.profile_key,
                profile_label = excluded.profile_label,
                source_name = excluded.source_name,
                local_ip = excluded.local_ip,
                software_version = excluded.software_version,
                current_state = excluded.current_state,
                upload_phase = excluded.upload_phase,
                current_local_run_id = excluded.current_local_run_id,
                current_label = excluded.current_label,
                current_started_at_local = excluded.current_started_at_local,
                last_error = excluded.last_error,
                last_event_kind = excluded.last_event_kind,
                last_event_utc = excluded.last_event_utc,
                last_heartbeat_utc = excluded.last_heartbeat_utc,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                workstation_id,
                str(workstation.get("hostname") or workstation_id),
                str(workstation.get("machine_alias") or ""),
                str(workstation.get("repo_root") or ""),
                str((payload.get("camera_profile") or {}).get("profile_key") or (payload.get("camera_profile") or {}).get("profile_id") or "default"),
                str((payload.get("camera_profile") or {}).get("profile_label") or ""),
                str((payload.get("camera_profile") or {}).get("source_name") or ""),
                str(workstation.get("local_ip") or ""),
                str(workstation.get("software_version") or ""),
                str(status.get("state") or "idle"),
                str(status.get("upload_phase") or ""),
                str(run_payload.get("local_run_id") or status.get("current_local_run_id") or ""),
                str(run_payload.get("label") or status.get("current_label") or ""),
                str(run_payload.get("started_at_local") or status.get("current_started_at_local") or ""),
                str(status.get("last_error") or ""),
                event_kind,
                now_utc,
                now_utc if event_kind == "heartbeat" else str(status.get("last_heartbeat_utc") or now_utc),
                now_utc,
            ),
        )
        conn.commit()

    return {
        "accepted": True,
        "workstation_id": workstation_id,
        "camera_profile_id": camera_profile_id,
        "updated_at_utc": now_utc,
        "event_kind": event_kind,
    }


def record_run_status(catalog_path: Path, payload: dict) -> dict:
    now_utc = utc_now_text()
    run_payload = payload.get("run") or {}
    local_run_id = str(run_payload.get("local_run_id") or "").strip()
    if not local_run_id:
        raise ValueError("run.local_run_id is required")

    with closing(get_db_connection(catalog_path)) as conn:
        workstation_id = ensure_workstation_record(conn, payload, now_utc=now_utc)
        camera_profile_id = ensure_camera_profile_record(conn, payload, workstation_id, now_utc=now_utc)
        pending_run_id = pending_run_id_for(workstation_id, local_run_id)
        replay_status = str(run_payload.get("replay_status") or "pending_upload").strip() or "pending_upload"
        if replay_status == "available":
            conn.execute(
                "DELETE FROM pending_runs WHERE workstation_id = ? AND local_run_id = ?",
                (workstation_id, local_run_id),
            )
        else:
            existing = conn.execute(
                """
                SELECT first_reported_utc
                FROM pending_runs
                WHERE workstation_id = ? AND local_run_id = ?
                LIMIT 1
                """,
                (workstation_id, local_run_id),
            ).fetchone()
            first_reported_utc = now_utc if existing is None else str(existing["first_reported_utc"] or now_utc)
            conn.execute(
                """
                INSERT INTO pending_runs (
                    pending_run_id,
                    workstation_id,
                    local_run_id,
                    camera_profile_id,
                    label,
                    source_name,
                    process_gate,
                    stop_reason,
                    started_at_local,
                    stopped_at_local,
                    duration_sec,
                    hamilton_log_dir,
                    hamilton_log_glob,
                    trace_pairing_delta_sec,
                    replay_status,
                    upload_phase,
                    local_manifest_path,
                    local_video_path,
                    local_trace_path,
                    last_error,
                    first_reported_utc,
                    last_reported_utc,
                    promoted_central_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workstation_id, local_run_id) DO UPDATE SET
                    camera_profile_id = excluded.camera_profile_id,
                    label = excluded.label,
                    source_name = excluded.source_name,
                    process_gate = excluded.process_gate,
                    stop_reason = excluded.stop_reason,
                    started_at_local = excluded.started_at_local,
                    stopped_at_local = excluded.stopped_at_local,
                    duration_sec = excluded.duration_sec,
                    hamilton_log_dir = excluded.hamilton_log_dir,
                    hamilton_log_glob = excluded.hamilton_log_glob,
                    trace_pairing_delta_sec = excluded.trace_pairing_delta_sec,
                    replay_status = excluded.replay_status,
                    upload_phase = excluded.upload_phase,
                    local_manifest_path = excluded.local_manifest_path,
                    local_video_path = excluded.local_video_path,
                    local_trace_path = excluded.local_trace_path,
                    last_error = excluded.last_error,
                    last_reported_utc = excluded.last_reported_utc,
                    promoted_central_run_id = excluded.promoted_central_run_id
                """,
                (
                    pending_run_id,
                    workstation_id,
                    local_run_id,
                    camera_profile_id,
                    str(run_payload.get("label") or "run"),
                    str(run_payload.get("source_name") or ""),
                    str(run_payload.get("process_gate") or ""),
                    str(run_payload.get("stop_reason") or ""),
                    str(run_payload.get("started_at_local") or ""),
                    str(run_payload.get("stopped_at_local") or ""),
                    float(run_payload.get("duration_sec") or 0),
                    str(run_payload.get("hamilton_log_dir") or ""),
                    str(run_payload.get("hamilton_log_glob") or ""),
                    run_payload.get("trace_pairing_delta_sec"),
                    replay_status,
                    str(run_payload.get("upload_phase") or ""),
                    str(run_payload.get("local_manifest_path") or ""),
                    str(run_payload.get("local_video_path") or ""),
                    str(run_payload.get("local_trace_path") or ""),
                    str(run_payload.get("last_error") or ""),
                    first_reported_utc,
                    now_utc,
                    str(run_payload.get("central_run_id") or ""),
                ),
            )
        conn.commit()

    return {
        "accepted": True,
        "workstation_id": workstation_id,
        "camera_profile_id": camera_profile_id,
        "pending_run_id": pending_run_id_for(workstation_id, local_run_id),
        "updated_at_utc": now_utc,
        "replay_status": replay_status,
    }


def list_workstations(catalog_path: Path, *, heartbeat_timeout_sec: float = 30.0) -> list[dict]:
    heartbeat_cutoff_utc = heartbeat_cutoff_text(heartbeat_timeout_sec)
    with closing(get_db_connection(catalog_path)) as conn:
        rows = conn.execute(
            """
            SELECT workstations.workstation_id,
                   workstations.hostname,
                   workstations.machine_alias,
                   workstations.instrument_name,
                   workstations.site_name,
                   workstations.repo_root,
                   workstations.first_seen_utc,
                   workstations.last_seen_utc,
                   workstation_runtime_status.local_ip,
                   workstation_runtime_status.software_version,
                   workstation_runtime_status.current_state AS last_reported_state,
                   CASE
                       WHEN workstation_runtime_status.last_heartbeat_utc >= ? THEN workstation_runtime_status.current_state
                       ELSE 'offline'
                   END AS current_state,
                   CASE
                       WHEN workstation_runtime_status.last_heartbeat_utc >= ? THEN workstation_runtime_status.upload_phase
                       ELSE ''
                   END AS upload_phase,
                   CASE
                       WHEN workstation_runtime_status.last_heartbeat_utc >= ? THEN workstation_runtime_status.current_local_run_id
                       ELSE ''
                   END AS current_local_run_id,
                   CASE
                       WHEN workstation_runtime_status.last_heartbeat_utc >= ? THEN workstation_runtime_status.current_label
                       ELSE ''
                   END AS current_label,
                   CASE
                       WHEN workstation_runtime_status.last_heartbeat_utc >= ? THEN workstation_runtime_status.current_started_at_local
                       ELSE ''
                   END AS current_started_at_local,
                   workstation_runtime_status.last_error,
                   workstation_runtime_status.last_event_kind,
                   workstation_runtime_status.last_event_utc,
                   workstation_runtime_status.last_heartbeat_utc,
                   CASE
                       WHEN workstation_runtime_status.last_heartbeat_utc >= ? THEN 1
                       ELSE 0
                   END AS is_online
            FROM workstations
            LEFT JOIN workstation_runtime_status
              ON workstation_runtime_status.workstation_id = workstations.workstation_id
            ORDER BY workstations.machine_alias ASC, workstations.hostname ASC, workstations.workstation_id ASC
            """,
            (
                heartbeat_cutoff_utc,
                heartbeat_cutoff_utc,
                heartbeat_cutoff_utc,
                heartbeat_cutoff_utc,
                heartbeat_cutoff_utc,
                heartbeat_cutoff_utc,
            ),
        ).fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        item["is_online"] = bool(item["is_online"])
        item["heartbeat_timeout_sec"] = heartbeat_timeout_sec
    return items


def list_camera_profiles(catalog_path: Path) -> list[dict]:
    with closing(get_db_connection(catalog_path)) as conn:
        rows = conn.execute(
            """
            SELECT camera_profile_id, workstation_id, profile_key, profile_label, source_name,
                   is_active, first_seen_utc, last_seen_utc
            FROM camera_profiles
            ORDER BY profile_label ASC, camera_profile_id ASC
            """
        ).fetchall()
    return [{**dict(row), "is_active": bool(row["is_active"])} for row in rows]


def list_runs(
    catalog_path: Path,
    *,
    workstation_id: str = "",
    camera_profile_id: str = "",
    replay_status: str = "",
    started_after: str = "",
    started_before: str = "",
    query_text: str = "",
    outcome: str = "",
    limit: int = 100,
) -> list[dict]:
    uploaded_query = """
        SELECT runs.central_run_id,
               runs.local_run_id,
               runs.label,
               runs.source_name,
               runs.process_gate,
               runs.stop_reason,
               runs.started_at_local,
               runs.stopped_at_local,
               runs.duration_sec,
               runs.hamilton_log_dir,
               runs.hamilton_log_glob,
               runs.trace_pairing_delta_sec,
               runs.replay_manifest_version,
               runs.replay_capabilities_json,
               runs.storage_tier,
               runs.replay_default_mode,
               runs.segment_count,
               runs.idle_segment_count,
               runs.active_segment_count,
               runs.replay_status,
               runs.run_tags_version,
               runs.run_tags_json,
               runs.run_tag_summary_json,
               runs.run_tag_search_text,
               runs.run_outcome_tag,
               runs.primary_barcode,
               runs.ready_artifact_count,
               runs.required_artifact_count,
               runs.first_ingested_utc,
               runs.last_ingested_utc,
               runs.workstation_id,
               workstations.hostname,
               workstations.machine_alias,
               runs.camera_profile_id,
               camera_profiles.profile_label
        FROM runs
        JOIN workstations ON workstations.workstation_id = runs.workstation_id
        JOIN camera_profiles ON camera_profiles.camera_profile_id = runs.camera_profile_id
        WHERE 1 = 1
    """
    params: list[object] = []
    if workstation_id:
        uploaded_query += " AND runs.workstation_id = ?"
        params.append(workstation_id)
    if camera_profile_id:
        uploaded_query += " AND runs.camera_profile_id = ?"
        params.append(camera_profile_id)
    if replay_status:
        uploaded_query += " AND runs.replay_status = ?"
        params.append(replay_status)
    if started_after:
        uploaded_query += " AND runs.started_at_local >= ?"
        params.append(started_after)
    if started_before:
        uploaded_query += " AND runs.started_at_local <= ?"
        params.append(started_before)
    normalized_query = query_text.strip().lower()
    if normalized_query:
        like_value = f"%{normalized_query}%"
        uploaded_query += """
            AND (
                LOWER(runs.label) LIKE ?
                OR LOWER(workstations.hostname) LIKE ?
                OR LOWER(workstations.machine_alias) LIKE ?
                OR LOWER(runs.run_tag_search_text) LIKE ?
                OR LOWER(runs.primary_barcode) LIKE ?
            )
        """
        params.extend([like_value, like_value, like_value, like_value, like_value])
    normalized_outcome = outcome.strip().lower()
    if normalized_outcome:
        uploaded_query += " AND LOWER(runs.run_outcome_tag) = ?"
        params.append(normalized_outcome)
    uploaded_query += " ORDER BY COALESCE(runs.started_at_local, '') DESC, runs.last_ingested_utc DESC"
    if limit > 0:
        uploaded_query += " LIMIT ?"
        params.append(limit)

    with closing(get_db_connection(catalog_path)) as conn:
        run_rows = conn.execute(uploaded_query, tuple(params)).fetchall()
        pending_query = """
            SELECT pending_runs.pending_run_id,
                   pending_runs.local_run_id,
                   pending_runs.label,
                   pending_runs.source_name,
                   pending_runs.process_gate,
                   pending_runs.stop_reason,
                   pending_runs.started_at_local,
                   pending_runs.stopped_at_local,
                   pending_runs.duration_sec,
                   pending_runs.hamilton_log_dir,
                   pending_runs.hamilton_log_glob,
                   pending_runs.trace_pairing_delta_sec,
                   pending_runs.replay_status,
                   pending_runs.upload_phase,
                   pending_runs.local_manifest_path,
                   pending_runs.local_video_path,
                   pending_runs.local_trace_path,
                   pending_runs.last_error,
                   pending_runs.first_reported_utc,
                   pending_runs.last_reported_utc,
                   pending_runs.workstation_id,
                   workstations.hostname,
                   workstations.machine_alias,
                   pending_runs.camera_profile_id,
                   camera_profiles.profile_label
            FROM pending_runs
            JOIN workstations ON workstations.workstation_id = pending_runs.workstation_id
            LEFT JOIN camera_profiles ON camera_profiles.camera_profile_id = pending_runs.camera_profile_id
            LEFT JOIN runs
              ON runs.workstation_id = pending_runs.workstation_id
             AND runs.local_run_id = pending_runs.local_run_id
            WHERE runs.central_run_id IS NULL
        """
        pending_params: list[object] = []
        if workstation_id:
            pending_query += " AND pending_runs.workstation_id = ?"
            pending_params.append(workstation_id)
        if camera_profile_id:
            pending_query += " AND pending_runs.camera_profile_id = ?"
            pending_params.append(camera_profile_id)
        if replay_status:
            pending_query += " AND pending_runs.replay_status = ?"
            pending_params.append(replay_status)
        if started_after:
            pending_query += " AND pending_runs.started_at_local >= ?"
            pending_params.append(started_after)
        if started_before:
            pending_query += " AND pending_runs.started_at_local <= ?"
            pending_params.append(started_before)
        pending_query += " ORDER BY COALESCE(pending_runs.started_at_local, '') DESC, pending_runs.last_reported_utc DESC"
        if limit > 0:
            pending_query += " LIMIT ?"
            pending_params.append(limit)
        pending_rows = conn.execute(pending_query, tuple(pending_params)).fetchall()

        run_ids = [str(row["central_run_id"]) for row in run_rows]
        artifacts_by_run: dict[str, list[dict]] = {run_id: [] for run_id in run_ids}
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            artifact_rows = conn.execute(
                f"""
                SELECT *
                FROM artifacts
                WHERE central_run_id IN ({placeholders})
                ORDER BY original_filename ASC
                """,
                tuple(run_ids),
            ).fetchall()
            for artifact in artifact_rows:
                artifacts_by_run[str(artifact["central_run_id"])].append(artifact_row_to_summary(artifact))
        for run_id in run_ids:
            artifacts_by_run[run_id] = append_missing_required_artifacts(artifacts_by_run[run_id], run_id)
    items = [summarize_run_row(row, artifacts_by_run.get(str(row["central_run_id"]), [])) for row in run_rows]
    items.extend(summarize_pending_run_row(row) for row in pending_rows)
    items.sort(key=lambda item: ((item.get("started_at_local") or ""), (item.get("last_ingested_utc") or "")), reverse=True)
    return items[:limit] if limit > 0 else items


def get_run_artifacts(catalog_path: Path, central_run_id: str) -> list[dict]:
    if is_pending_run_id(central_run_id):
        with closing(get_db_connection(catalog_path)) as conn:
            row = conn.execute(
                """
                SELECT local_manifest_path, local_video_path, local_trace_path
                FROM pending_runs
                WHERE pending_run_id = ?
                LIMIT 1
                """,
                (central_run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(central_run_id)
        items = []
        for artifact_type, key in (
            ("run_manifest_json", "local_manifest_path"),
            ("video_mp4", "local_video_path"),
            ("trace_trc", "local_trace_path"),
        ):
            local_path = str(row[key] or "")
            items.append(
                {
                    "artifact_id": f"{central_run_id}:{artifact_type}",
                    "artifact_type": artifact_type,
                    "original_filename": Path(local_path).name if local_path else "",
                    "storage_relpath": "",
                    "mime_type": "",
                    "compression_kind": "none",
                    "content_sha256": "",
                    "size_bytes": 0,
                    "stored_at_utc": "",
                    "is_required": True,
                    "is_ready": False,
                    "media_url": "",
                }
            )
        return append_missing_required_artifacts(items, central_run_id)
    with closing(get_db_connection(catalog_path)) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM artifacts
            WHERE central_run_id = ?
            ORDER BY artifact_type ASC
            """,
            (central_run_id,),
        ).fetchall()
    return append_missing_required_artifacts([artifact_row_to_summary(row) for row in rows], central_run_id)


def get_run_detail(catalog_path: Path, central_run_id: str) -> dict:
    if is_pending_run_id(central_run_id):
        with closing(get_db_connection(catalog_path)) as conn:
            row = conn.execute(
                """
                SELECT pending_runs.pending_run_id,
                       pending_runs.local_run_id,
                       pending_runs.label,
                       pending_runs.source_name,
                       pending_runs.process_gate,
                       pending_runs.stop_reason,
                       pending_runs.started_at_local,
                       pending_runs.stopped_at_local,
                       pending_runs.duration_sec,
                       pending_runs.hamilton_log_dir,
                       pending_runs.hamilton_log_glob,
                       pending_runs.trace_pairing_delta_sec,
                       pending_runs.replay_status,
                       pending_runs.upload_phase,
                       pending_runs.local_manifest_path,
                       pending_runs.local_video_path,
                       pending_runs.local_trace_path,
                       pending_runs.last_error,
                       pending_runs.first_reported_utc,
                       pending_runs.last_reported_utc,
                       pending_runs.workstation_id,
                       workstations.hostname,
                       workstations.machine_alias,
                       workstations.repo_root,
                       pending_runs.camera_profile_id,
                       camera_profiles.profile_key,
                       camera_profiles.profile_label,
                       camera_profiles.source_name AS camera_source_name
                FROM pending_runs
                JOIN workstations ON workstations.workstation_id = pending_runs.workstation_id
                LEFT JOIN camera_profiles ON camera_profiles.camera_profile_id = pending_runs.camera_profile_id
                WHERE pending_runs.pending_run_id = ?
                LIMIT 1
                """,
                (central_run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(central_run_id)
        artifacts = get_run_artifacts(catalog_path, central_run_id)
        run_payload = summarize_pending_run_row(row)
        return {
            "run": run_payload,
            "workstation": {
                "workstation_id": row["workstation_id"],
                "hostname": row["hostname"],
                "machine_alias": row["machine_alias"],
                "repo_root": row["repo_root"],
            },
            "camera_profile": {
                "camera_profile_id": row["camera_profile_id"],
                "profile_key": row["profile_key"] or "default",
                "profile_label": row["profile_label"] or "",
                "source_name": row["camera_source_name"] or "",
            },
            "artifacts": artifacts,
            "ingest_batch": None,
        }
    with closing(get_db_connection(catalog_path)) as conn:
        row = conn.execute(
            """
            SELECT runs.central_run_id,
                   runs.local_run_id,
                   runs.label,
                   runs.source_name,
                   runs.process_gate,
                   runs.stop_reason,
                   runs.started_at_local,
                   runs.stopped_at_local,
                   runs.duration_sec,
                   runs.hamilton_log_dir,
                   runs.hamilton_log_glob,
                   runs.trace_pairing_delta_sec,
                   runs.replay_manifest_version,
                   runs.replay_capabilities_json,
                   runs.storage_tier,
                   runs.replay_default_mode,
                   runs.segment_count,
                   runs.idle_segment_count,
                   runs.active_segment_count,
                   runs.replay_status,
                   runs.run_tags_version,
                   runs.run_tags_json,
                   runs.run_tag_summary_json,
                   runs.run_tag_search_text,
                   runs.run_outcome_tag,
                   runs.primary_barcode,
                   runs.ready_artifact_count,
                   runs.required_artifact_count,
                   runs.first_ingested_utc,
                   runs.last_ingested_utc,
                   runs.latest_ingest_batch_id,
                   runs.workstation_id,
                   workstations.hostname,
                   workstations.machine_alias,
                   workstations.repo_root,
                   runs.camera_profile_id,
                   camera_profiles.profile_key,
                   camera_profiles.profile_label,
                   camera_profiles.source_name AS camera_source_name
            FROM runs
            JOIN workstations ON workstations.workstation_id = runs.workstation_id
            JOIN camera_profiles ON camera_profiles.camera_profile_id = runs.camera_profile_id
            WHERE runs.central_run_id = ?
            LIMIT 1
            """,
            (central_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(central_run_id)

        artifacts = get_run_artifacts(catalog_path, central_run_id)
        ingest_row = conn.execute(
            """
            SELECT ingest_batch_id, uploader_version, uploader_hostname, started_at_utc,
                   completed_at_utc, status, notes
            FROM ingest_batches
            WHERE ingest_batch_id = ?
            LIMIT 1
            """,
            (row["latest_ingest_batch_id"],),
        ).fetchone()

    return {
        "run": summarize_run_row(row, artifacts),
        "workstation": {
            "workstation_id": row["workstation_id"],
            "hostname": row["hostname"],
            "machine_alias": row["machine_alias"],
            "repo_root": row["repo_root"],
        },
        "camera_profile": {
            "camera_profile_id": row["camera_profile_id"],
            "profile_key": row["profile_key"],
            "profile_label": row["profile_label"],
            "source_name": row["camera_source_name"],
        },
        "artifacts": artifacts,
        "ingest_batch": None if ingest_row is None else dict(ingest_row),
    }


def get_trace_events_for_run(upload_root: Path, catalog_path: Path, central_run_id: str) -> dict:
    artifacts = get_run_artifacts(catalog_path, central_run_id)
    trace_artifact = next((item for item in artifacts if item["artifact_type"] == "trace_trc"), None)
    if trace_artifact is None:
        raise FileNotFoundError("Trace artifact not found")
    trace_path = resolve_storage_path(upload_root, trace_artifact["storage_relpath"])
    if not trace_path.exists():
        raise FileNotFoundError(f"Stored trace missing: {trace_path}")
    events = [asdict(event) for event in parse_trace_events(trace_path)]
    return {
        "central_run_id": central_run_id,
        "trace_path": str(trace_path),
        "item_count": len(events),
        "items": events,
    }


def list_ingest_failures(catalog_path: Path, *, limit: int = 25) -> list[dict]:
    with closing(get_db_connection(catalog_path)) as conn:
        rows = conn.execute(
            """
            SELECT ingest_item_id, ingest_batch_id, central_run_id, artifact_type, source_path,
                   received_filename, status, message, created_at_utc
            FROM ingest_items
            WHERE status <> 'stored'
            ORDER BY created_at_utc DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_health_payload(
    upload_root: Path,
    catalog_path: Path,
    *,
    site_name: str,
    startup_utc: str,
) -> dict:
    with closing(get_db_connection(catalog_path)) as conn:
        run_count = conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"]
        artifact_count = conn.execute("SELECT COUNT(*) AS count FROM artifacts").fetchone()["count"]
        workstation_count = conn.execute("SELECT COUNT(*) AS count FROM workstations").fetchone()["count"]
        pending_run_count = conn.execute("SELECT COUNT(*) AS count FROM pending_runs").fetchone()["count"]
        active_workstation_count = conn.execute("SELECT COUNT(*) AS count FROM workstation_runtime_status").fetchone()["count"]
    return {
        "status": "ok",
        "site_name": site_name,
        "startup_utc": startup_utc,
        "upload_root": str(upload_root),
        "catalog_path": str(catalog_path),
        "counts": {
            "runs": run_count,
            "pending_runs": pending_run_count,
            "artifacts": artifact_count,
            "workstations": workstation_count,
            "active_workstations": active_workstation_count,
        },
    }


def backfill_run_tags(upload_root: Path, catalog_path: Path) -> int:
    """Index stored traces for runs uploaded before replay tags existed."""
    updated = 0
    with closing(get_db_connection(catalog_path)) as conn:
        init_central_db(conn)
        rows = conn.execute(
            """
            SELECT runs.central_run_id, artifacts.storage_relpath
            FROM runs
            JOIN artifacts ON artifacts.central_run_id = runs.central_run_id
            WHERE artifacts.artifact_type = 'trace_trc'
              AND runs.run_tags_version = ''
            """
        ).fetchall()
        for row in rows:
            trace_path = resolve_storage_path(upload_root, row["storage_relpath"])
            tag_payload = derive_run_tags(trace_path if trace_path.exists() else None)
            summary = tag_payload["summary"]
            conn.execute(
                """
                UPDATE runs
                SET run_tags_version = ?, run_tags_json = ?, run_tag_summary_json = ?,
                    run_tag_search_text = ?, run_outcome_tag = ?, primary_barcode = ?
                WHERE central_run_id = ?
                """,
                (
                    tag_payload["version"],
                    serialize_tags(tag_payload["tags"]),
                    serialize_summary(summary),
                    tag_payload["search_text"],
                    summary.get("outcome") or "",
                    summary.get("primary_barcode") or "",
                    row["central_run_id"],
                ),
            )
            updated += 1
        conn.commit()
    return updated


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("central_replay_server")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def make_handler(
    upload_root: Path,
    catalog_path: Path,
    *,
    site_name: str = "Central Replay",
    healthcheck_path: str = "/api/healthz",
    startup_utc: str = "",
    workstation_heartbeat_timeout_sec: float = 30.0,
    logger: logging.Logger | None = None,
):
    backfill_run_tags(upload_root, catalog_path)

    class CentralReplayHandler(BaseHTTPRequestHandler):
        server_version = "CentralHamiltonReplay/1.1"

        def send_response(self, code: int, message: str | None = None) -> None:
            self._status_code = code
            super().send_response(code, message)

        def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, status: HTTPStatus) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict:
            content_length = int(self.headers.get("Content-Length") or "0")
            if content_length <= 0:
                raise ValueError("Request body is required")
            body = self.rfile.read(content_length)
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Request JSON body must be an object")
            return payload

        def _parse_range_header(self, header_value: str, file_size: int) -> tuple[int, int]:
            match = re.match(r"bytes=(\d*)-(\d*)$", header_value.strip())
            if not match:
                raise ValueError("Unsupported Range header")
            start_text, end_text = match.groups()
            if not start_text and not end_text:
                raise ValueError("Empty Range header")
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else file_size - 1
            else:
                suffix_length = min(int(end_text), file_size)
                start = file_size - suffix_length
                end = file_size - 1
            if start < 0 or end < start or end >= file_size:
                raise ValueError("Invalid Range header")
            return start, end

        def _send_file(self, path: Path, *, content_type: str | None = None) -> None:
            mime = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            file_size = path.stat().st_size
            range_header = self.headers.get("Range")
            if range_header:
                start, end = self._parse_range_header(range_header, file_size)
                length = end - start + 1
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", mime)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                with path.open("rb") as handle:
                    stream_file_handle(handle, self.wfile, start=start, byte_count=length)
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            with path.open("rb") as handle:
                stream_file_handle(handle, self.wfile)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = unquote(parsed.path)
            params = parse_qs(parsed.query)

            try:
                if route == "/":
                    return self._send_file(STATIC_DIR / "index.html", content_type="text/html; charset=utf-8")

                if route == healthcheck_path:
                    return self._send_json(
                        get_health_payload(
                            upload_root,
                            catalog_path,
                            site_name=site_name,
                            startup_utc=startup_utc,
                        )
                    )

                if route.startswith("/static/"):
                    asset_path = (STATIC_DIR / route.removeprefix("/static/")).resolve()
                    if STATIC_DIR not in asset_path.parents and asset_path != STATIC_DIR:
                        return self._send_text("Invalid static path", HTTPStatus.BAD_REQUEST)
                    if not asset_path.exists() or not asset_path.is_file():
                        return self._send_text("Static asset not found", HTTPStatus.NOT_FOUND)
                    return self._send_file(asset_path)

                if route == "/api/runs":
                    workstation_id = (params.get("workstation_id") or [""])[0].strip()
                    camera_profile_id = (params.get("camera_profile_id") or [""])[0].strip()
                    replay_status = (params.get("replay_status") or [""])[0].strip()
                    started_after = (params.get("started_after") or [""])[0].strip()
                    started_before = (params.get("started_before") or [""])[0].strip()
                    query_text = (params.get("query") or [""])[0].strip()
                    outcome = (params.get("outcome") or [""])[0].strip()
                    limit_text = (params.get("limit") or ["100"])[0].strip()
                    try:
                        limit = max(1, int(limit_text))
                    except ValueError:
                        return self._send_text("limit must be an integer", HTTPStatus.BAD_REQUEST)
                    items = list_runs(
                        catalog_path,
                        workstation_id=workstation_id,
                        camera_profile_id=camera_profile_id,
                        replay_status=replay_status,
                        started_after=started_after,
                        started_before=started_before,
                        query_text=query_text,
                        outcome=outcome,
                        limit=limit,
                    )
                    return self._send_json(
                        {
                            "items": items,
                            "catalog_path": str(catalog_path),
                            "filters": {
                                "workstation_id": workstation_id,
                                "camera_profile_id": camera_profile_id,
                                "replay_status": replay_status,
                                "started_after": started_after,
                                "started_before": started_before,
                                "query": query_text,
                                "outcome": outcome,
                                "limit": limit,
                            },
                        }
                    )

                if route == "/api/workstations":
                    return self._send_json(
                        {"items": list_workstations(catalog_path, heartbeat_timeout_sec=workstation_heartbeat_timeout_sec)}
                    )

                if route == "/api/camera-profiles":
                    return self._send_json({"items": list_camera_profiles(catalog_path)})

                if route == "/api/admin/ingest-failures":
                    return self._send_json({"items": list_ingest_failures(catalog_path)})

                if route.startswith("/api/runs/"):
                    parts = [part for part in route.split("/") if part]
                    if len(parts) < 3:
                        return self._send_text("Invalid run route", HTTPStatus.BAD_REQUEST)
                    central_run_id = parts[2]
                    try:
                        if len(parts) == 3:
                            return self._send_json(get_run_detail(catalog_path, central_run_id))
                        if len(parts) == 4 and parts[3] == "artifacts":
                            return self._send_json({"items": get_run_artifacts(catalog_path, central_run_id)})
                        if len(parts) == 4 and parts[3] == "trace-events":
                            return self._send_json(get_trace_events_for_run(upload_root, catalog_path, central_run_id))
                    except KeyError:
                        return self._send_text("Run not found", HTTPStatus.NOT_FOUND)
                    except FileNotFoundError as exc:
                        return self._send_text(str(exc), HTTPStatus.NOT_FOUND)

                if route.startswith("/media/"):
                    storage_relpath = route.removeprefix("/media/")
                    try:
                        stored_path = resolve_storage_path(upload_root, storage_relpath)
                    except ValueError:
                        return self._send_text("Invalid media path", HTTPStatus.BAD_REQUEST)
                    if not stored_path.exists() or not stored_path.is_file():
                        return self._send_text("Stored media not found", HTTPStatus.NOT_FOUND)
                    try:
                        return self._send_file(stored_path)
                    except ValueError:
                        return self._send_text("Invalid Range header", HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)

                return self._send_text("Not found", HTTPStatus.NOT_FOUND)
            except sqlite3.Error:
                if logger:
                    logger.exception("HTTP request failed: %s", route)
                return self._send_text("Central replay catalog query failed", HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                if logger:
                    logger.exception("Unexpected request failure: %s", route)
                return self._send_text("Central replay server error", HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = unquote(parsed.path)

            try:
                if route == "/api/workstations/heartbeat":
                    payload = self._read_json_body()
                    return self._send_json(record_workstation_status(catalog_path, payload, event_kind="heartbeat"))

                if route == "/api/runs/status":
                    payload = self._read_json_body()
                    record_workstation_status(catalog_path, payload, event_kind="event")
                    return self._send_json(record_run_status(catalog_path, payload))

                return self._send_text("Not found", HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                return self._send_text(str(exc), HTTPStatus.BAD_REQUEST)
            except sqlite3.Error:
                if logger:
                    logger.exception("HTTP POST failed: %s", route)
                return self._send_text("Central replay catalog update failed", HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                if logger:
                    logger.exception("Unexpected POST failure: %s", route)
                return self._send_text("Central replay server error", HTTPStatus.INTERNAL_SERVER_ERROR)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            if logger:
                logger.info(
                    "%s %s %s",
                    getattr(self, "command", "-"),
                    getattr(self, "path", "-"),
                    getattr(self, "_status_code", "-"),
                )

    return CentralReplayHandler


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the central Hamilton replay catalog over HTTP")
    parser.add_argument(
        "--server-config",
        default=str(DEFAULT_SERVER_CONFIG_PATH),
        help="Path to the base central replay server config JSON",
    )
    parser.add_argument(
        "--server-local-config",
        default=str(DEFAULT_SERVER_LOCAL_CONFIG_PATH),
        help="Path to the optional central replay server local override JSON",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Legacy fallback camera config JSON")
    parser.add_argument("--local-config", default=str(DEFAULT_LOCAL_OVERRIDE_PATH), help="Legacy fallback camera override JSON")
    parser.add_argument("--upload-root", default="", help="Directory that holds the central replay storage tree and SQLite catalog")
    parser.add_argument("--catalog-path", default="", help="Explicit path to the central replay SQLite catalog")
    parser.add_argument("--host", default="", help="Bind host for the central replay server")
    parser.add_argument("--port", type=int, default=None, help="Bind port for the central replay server")
    parser.add_argument("--log-path", default="", help="Persistent host-local server log path")
    parser.add_argument("--site-name", default="", help="Friendly site label for health and diagnostics")
    parser.add_argument("--health-path", default="", help="Health-check route path")
    parser.add_argument(
        "--workstation-heartbeat-timeout-sec",
        type=float,
        default=None,
        help="Seconds before a workstation is treated as offline without a heartbeat",
    )
    parser.add_argument("--print-config", action="store_true", help="Print the resolved runtime config and exit")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON for --print-config")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    runtime = resolve_runtime_settings(args)
    validation = validate_runtime_settings(runtime)
    payload = {"runtime": asdict(runtime), "validation": validation}

    if args.print_config:
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(json.dumps(asdict(runtime), indent=2))
            for item in validation["warnings"]:
                print(f"warning: {item}")
            for item in validation["errors"]:
                print(f"error: {item}")
        return 1 if validation["errors"] else 0

    if validation["errors"]:
        for item in validation["errors"]:
            print(item)
        return 1

    log_path = Path(runtime.log_path)
    logger = configure_logging(log_path)
    for item in validation["warnings"]:
        logger.warning(item)

    upload_root = Path(runtime.upload_root).resolve()
    catalog_path = Path(runtime.catalog_path).resolve()
    if not catalog_path.exists():
        logger.error("Central replay catalog not found: %s", catalog_path)
        return 1

    startup_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    logger.info("Starting %s", runtime.site_name)
    logger.info("Listening on http://%s:%s", runtime.host, runtime.port)
    logger.info("Upload root: %s", upload_root)
    logger.info("Catalog path: %s", catalog_path)
    logger.info("Health path: %s", runtime.healthcheck_path)
    logger.info("Workstation heartbeat timeout: %.1fs", runtime.workstation_heartbeat_timeout_sec)

    handler = make_handler(
        upload_root,
        catalog_path,
        site_name=runtime.site_name,
        healthcheck_path=runtime.healthcheck_path,
        startup_utc=startup_utc,
        workstation_heartbeat_timeout_sec=runtime.workstation_heartbeat_timeout_sec,
        logger=logger,
    )
    with ThreadingHTTPServer((runtime.host, runtime.port), handler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
