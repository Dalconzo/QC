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
from contextlib import closing
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, load_effective_config
from upload_central_replay import CENTRAL_CATALOG_FILENAME


STATIC_DIR = Path(__file__).resolve().parent / "central_replay_static"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER_CONFIG_PATH = REPO_ROOT / "config" / "central-replay-server.json"
DEFAULT_SERVER_LOCAL_CONFIG_PATH = REPO_ROOT / "config" / "central-replay-server.local.json"
TRACE_LINE_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})> ?(?P<body>.*)$")

DEFAULT_SERVER_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 5080,
        "site_name": "Central Replay",
        "log_path": str(REPO_ROOT / "logs" / "central-replay-server.log"),
        "healthcheck_path": "/api/healthz",
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


def read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Central replay server config must be a JSON object: {path}")
    return payload


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


def get_db_connection(catalog_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(catalog_path)
    conn.row_factory = sqlite3.Row
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


def summarize_run_row(row: sqlite3.Row, artifacts: list[dict]) -> dict:
    artifact_by_type = {item["artifact_type"]: item for item in artifacts}
    video_artifact = artifact_by_type.get("video_mp4")
    trace_artifact = artifact_by_type.get("trace_trc")
    manifest_artifact = artifact_by_type.get("run_manifest_json")
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
        "replay_status": row["replay_status"],
        "ready_artifact_count": row["ready_artifact_count"],
        "required_artifact_count": row["required_artifact_count"],
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


def list_workstations(catalog_path: Path) -> list[dict]:
    with closing(get_db_connection(catalog_path)) as conn:
        rows = conn.execute(
            """
            SELECT workstation_id, hostname, machine_alias, instrument_name, site_name, repo_root,
                   first_seen_utc, last_seen_utc
            FROM workstations
            ORDER BY machine_alias ASC, hostname ASC, workstation_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


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
    replay_status: str = "",
    started_after: str = "",
    started_before: str = "",
    limit: int = 100,
) -> list[dict]:
    query = """
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
               runs.replay_status,
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
        query += " AND runs.workstation_id = ?"
        params.append(workstation_id)
    if replay_status:
        query += " AND runs.replay_status = ?"
        params.append(replay_status)
    if started_after:
        query += " AND runs.started_at_local >= ?"
        params.append(started_after)
    if started_before:
        query += " AND runs.started_at_local <= ?"
        params.append(started_before)
    query += " ORDER BY COALESCE(runs.started_at_local, '') DESC, runs.last_ingested_utc DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    with closing(get_db_connection(catalog_path)) as conn:
        run_rows = conn.execute(query, tuple(params)).fetchall()
        run_ids = [row["central_run_id"] for row in run_rows]
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

    return [summarize_run_row(row, artifacts_by_run.get(str(row["central_run_id"]), [])) for row in run_rows]


def get_run_artifacts(catalog_path: Path, central_run_id: str) -> list[dict]:
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
    return [artifact_row_to_summary(row) for row in rows]


def get_run_detail(catalog_path: Path, central_run_id: str) -> dict:
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
                   runs.replay_status,
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
    return {
        "status": "ok",
        "site_name": site_name,
        "startup_utc": startup_utc,
        "upload_root": str(upload_root),
        "catalog_path": str(catalog_path),
        "counts": {
            "runs": run_count,
            "artifacts": artifact_count,
            "workstations": workstation_count,
        },
    }


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
    logger: logging.Logger | None = None,
):
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
                    handle.seek(start)
                    self.wfile.write(handle.read(length))
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            with path.open("rb") as handle:
                self.wfile.write(handle.read())

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
                    replay_status = (params.get("replay_status") or [""])[0].strip()
                    started_after = (params.get("started_after") or [""])[0].strip()
                    started_before = (params.get("started_before") or [""])[0].strip()
                    limit_text = (params.get("limit") or ["100"])[0].strip()
                    try:
                        limit = max(1, int(limit_text))
                    except ValueError:
                        return self._send_text("limit must be an integer", HTTPStatus.BAD_REQUEST)
                    items = list_runs(
                        catalog_path,
                        workstation_id=workstation_id,
                        replay_status=replay_status,
                        started_after=started_after,
                        started_before=started_before,
                        limit=limit,
                    )
                    return self._send_json(
                        {
                            "items": items,
                            "catalog_path": str(catalog_path),
                            "filters": {
                                "workstation_id": workstation_id,
                                "replay_status": replay_status,
                                "started_after": started_after,
                                "started_before": started_before,
                                "limit": limit,
                            },
                        }
                    )

                if route == "/api/workstations":
                    return self._send_json({"items": list_workstations(catalog_path)})

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

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            if logger:
                logger.info("%s %s %s", self.command, self.path, getattr(self, "_status_code", "-"))

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

    handler = make_handler(
        upload_root,
        catalog_path,
        site_name=runtime.site_name,
        healthcheck_path=runtime.healthcheck_path,
        startup_utc=startup_utc,
        logger=logger,
    )
    with ThreadingHTTPServer((runtime.host, runtime.port), handler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
