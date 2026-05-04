#!/usr/bin/env python3
"""
upload-central-replay.py

Upload staged replay bundles into a central filesystem-backed catalog.

This is the first concrete LAN transport for the replay artifacts. It reuses
the local staging batches from stage-central-replay.py, then ingests them into
one shared root that holds:

- a central SQLite catalog using sql/central-replay-schema.sql
- one managed artifact tree under storage/
- one acknowledgement file written back beside each staged run

The transport is intentionally simple for the prototype: a trusted LAN path
instead of an HTTP service. The resulting catalog and stored artifacts still
match the central schema and server-issued-id model.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import hashlib
import shutil
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, load_effective_config
from local_retention import record_upload_ack, record_upload_failure


CAMERAS_DIR = Path(__file__).resolve().parent
STAGE_MODULE_PATH = CAMERAS_DIR / "stage-central-replay.py"
SCHEMA_SQL_PATH = CAMERAS_DIR / "sql" / "central-replay-schema.sql"
CENTRAL_CATALOG_FILENAME = ".central_replay_catalog.sqlite3"
ACK_FILENAME = "run-ack.json"
UPLOADER_VERSION = "filesystem-uploader.v1"


def load_stage_module():
    """Import the staging module even though the filename uses hyphens."""
    spec = importlib.util.spec_from_file_location("camera_stage_central_replay", STAGE_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STAGE_MODULE = load_stage_module()


def utc_now_text() -> str:
    """Return a stable UTC timestamp string for ingest metadata."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def upload_batch_id() -> str:
    """Generate one central ingest batch id for this upload pass."""
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"ingest-{stamp}-{uuid.uuid4().hex[:8]}"


def init_central_db(conn: sqlite3.Connection) -> None:
    """Create the central replay catalog from the shared bootstrap schema."""
    conn.executescript(SCHEMA_SQL_PATH.read_text(encoding="utf-8"))
    ensure_central_db_migrations(conn)
    conn.commit()


def ensure_central_db_migrations(conn: sqlite3.Connection) -> None:
    """Add newer run metadata columns when the central catalog predates them."""
    expected_columns = {
        "replay_manifest_version": "TEXT NOT NULL DEFAULT ''",
        "replay_capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
        "storage_tier": "TEXT NOT NULL DEFAULT ''",
        "replay_default_mode": "TEXT NOT NULL DEFAULT ''",
        "segment_count": "INTEGER NOT NULL DEFAULT 0",
        "idle_segment_count": "INTEGER NOT NULL DEFAULT 0",
        "active_segment_count": "INTEGER NOT NULL DEFAULT 0",
    }
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(runs)")
    }
    for column_name, column_type in expected_columns.items():
        if column_name in existing_columns:
            continue
        conn.execute(f"ALTER TABLE runs ADD COLUMN {column_name} {column_type}")


def get_pending_runs(conn: sqlite3.Connection, *, batch_id: str = "", limit: int = 0) -> list[sqlite3.Row]:
    """Return staged runs that still need a central acknowledgement."""
    query = """
        SELECT *
        FROM staged_runs
        WHERE upload_status IN ('staged', 'upload_failed')
    """
    params: list[object] = []
    if batch_id:
        query += " AND batch_id = ?"
        params.append(batch_id)
    query += " ORDER BY staged_at_utc ASC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(query, tuple(params)).fetchall())


def parse_payload(path: Path) -> dict:
    """Load one staged run payload."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse_local_timestamp(value: str) -> tuple[str, str]:
    """Turn local ISO timestamps into storage-friendly year/month segments."""
    if not value:
        return ("unknown-year", "unknown-month")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return ("unknown-year", "unknown-month")
    return (f"{parsed.year:04d}", f"{parsed.month:02d}")


def ensure_workstation(conn: sqlite3.Connection, payload: dict, *, now_utc: str) -> str:
    """Insert or refresh the workstation row referenced by one staged run."""
    workstation = payload["workstation"]
    workstation_id = str(workstation.get("workstation_id") or "").strip()
    row = conn.execute(
        "SELECT workstation_id FROM workstations WHERE workstation_id = ?",
        (workstation_id,),
    ).fetchone()
    if row is None:
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
            (
                workstation_id,
                payload["workstation"].get("hostname") or workstation_id,
                payload["workstation"].get("machine_alias") or "",
                payload["workstation"].get("repo_root") or "",
                now_utc,
                now_utc,
            ),
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
            (
                payload["workstation"].get("hostname") or workstation_id,
                payload["workstation"].get("machine_alias") or "",
                payload["workstation"].get("repo_root") or "",
                now_utc,
                workstation_id,
            ),
        )
    return workstation_id


def ensure_camera_profile(conn: sqlite3.Connection, payload: dict, workstation_id: str, *, now_utc: str) -> str:
    """Insert or refresh the camera profile referenced by one staged run."""
    profile = payload["camera_profile"]
    profile_key = str(profile.get("profile_id") or "default").strip() or "default"
    camera_profile_id = f"{workstation_id}:{profile_key}"
    row = conn.execute(
        "SELECT camera_profile_id FROM camera_profiles WHERE camera_profile_id = ?",
        (camera_profile_id,),
    ).fetchone()
    if row is None:
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
            (
                camera_profile_id,
                workstation_id,
                profile_key,
                profile.get("profile_label") or profile_key,
                profile.get("source_name") or "",
                now_utc,
                now_utc,
            ),
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
            (
                profile.get("profile_label") or profile_key,
                profile.get("source_name") or "",
                now_utc,
                camera_profile_id,
            ),
        )
    return camera_profile_id


def find_existing_central_run(conn: sqlite3.Connection, workstation_id: str, local_run_id: str, artifacts: list[dict]) -> str:
    """Deduplicate by workstation/local run id plus the staged artifact hashes."""
    hashes = {
        artifact["artifact_type"]: artifact["sha256"]
        for artifact in artifacts
    }
    row = conn.execute(
        """
        SELECT runs.central_run_id
        FROM runs
        JOIN artifacts manifest_artifact
            ON manifest_artifact.central_run_id = runs.central_run_id
           AND manifest_artifact.artifact_type = 'run_manifest_json'
        JOIN artifacts video_artifact
            ON video_artifact.central_run_id = runs.central_run_id
           AND video_artifact.artifact_type = 'video_mp4'
        JOIN artifacts trace_artifact
            ON trace_artifact.central_run_id = runs.central_run_id
           AND trace_artifact.artifact_type = 'trace_trc'
        WHERE runs.workstation_id = ?
          AND runs.local_run_id = ?
          AND manifest_artifact.content_sha256 = ?
          AND video_artifact.content_sha256 = ?
          AND trace_artifact.content_sha256 = ?
        LIMIT 1
        """,
        (
            workstation_id,
            local_run_id,
            hashes.get("run_manifest_json", ""),
            hashes.get("video_mp4", ""),
            hashes.get("trace_trc", ""),
        ),
    ).fetchone()
    return "" if row is None else str(row["central_run_id"])


def central_artifact_relpath(workstation_id: str, central_run_id: str, started_at_local: str, filename: str) -> str:
    """Build one managed-storage relative path for the central catalog."""
    year, month = parse_local_timestamp(started_at_local)
    return str(Path("storage") / "runs" / year / month / workstation_id / central_run_id / filename)


def copy_if_missing(source: Path, target: Path) -> None:
    """Copy one staged artifact into the managed central storage tree."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    shutil.copy2(source, target)


def compute_sha256(path: Path) -> str:
    """Hash one stored artifact to verify retry reuse safety."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_central_artifact(
    *,
    source_path: Path,
    target_path: Path,
    expected_sha256: str,
) -> dict:
    """Ensure the managed artifact path exists and matches the expected bytes."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not target_path.exists():
        shutil.copy2(source_path, target_path)
        copied_sha256 = compute_sha256(target_path)
        if copied_sha256 != expected_sha256:
            raise RuntimeError(
                f"Central artifact verification failed after initial copy for {target_path}: "
                f"expected {expected_sha256}, got {copied_sha256}"
            )
        return {"action": "copied_missing", "verified": True}

    existing_sha256 = compute_sha256(target_path)
    if existing_sha256 == expected_sha256:
        return {"action": "verified_existing", "verified": True}

    shutil.copy2(source_path, target_path)
    repaired_sha256 = compute_sha256(target_path)
    if repaired_sha256 != expected_sha256:
        raise RuntimeError(
            f"Central artifact verification failed after repair for {target_path}: "
            f"expected {expected_sha256}, got {repaired_sha256}"
        )
    return {"action": "repaired_mismatch", "verified": True}


def record_ingest_items(
    conn: sqlite3.Connection,
    *,
    ingest_batch_id: str,
    central_run_id: str,
    artifact_rows: list[dict],
    created_at_utc: str,
) -> None:
    """Write one ingest_items row per artifact observed in this upload pass."""
    for artifact in artifact_rows:
        conn.execute(
            """
            INSERT INTO ingest_items (
                ingest_item_id,
                ingest_batch_id,
                central_run_id,
                artifact_id,
                artifact_type,
                source_path,
                received_filename,
                received_size_bytes,
                received_sha256,
                status,
                message,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"ingest-item-{uuid.uuid4().hex}",
                ingest_batch_id,
                central_run_id,
                artifact["artifact_id"],
                artifact["artifact_type"],
                artifact["source_path"],
                Path(artifact["staged_path"]).name,
                artifact["size_bytes"],
                artifact["sha256"],
                "stored",
                "",
                created_at_utc,
            ),
        )


def ingest_one_run(
    conn: sqlite3.Connection,
    *,
    central_root: Path,
    ingest_batch_id: str,
    stage_row: sqlite3.Row,
    payload: dict,
) -> dict:
    """Ingest one staged run into the shared central artifact store and SQL catalog."""
    now_utc = utc_now_text()
    workstation_id = ensure_workstation(conn, payload, now_utc=now_utc)
    camera_profile_id = ensure_camera_profile(conn, payload, workstation_id, now_utc=now_utc)
    existing_central_run_id = find_existing_central_run(
        conn,
        workstation_id,
        payload["run"]["local_run_id"],
        payload["artifacts"],
    )

    created = False
    central_run_id = existing_central_run_id or f"central-run-{uuid.uuid4().hex}"
    run = payload["run"]
    artifact_rows: list[dict] = []
    artifact_sync_actions: list[dict] = []
    for artifact in payload["artifacts"]:
        storage_relpath = central_artifact_relpath(
            workstation_id,
            central_run_id,
            run.get("started_at_local") or "",
            artifact["staged_filename"],
        )
        stored_path = central_root / storage_relpath
        sync_result = ensure_central_artifact(
            source_path=Path(artifact["staged_path"]),
            target_path=stored_path,
            expected_sha256=str(artifact["sha256"] or ""),
        )
        artifact_sync_actions.append(
            {
                "artifact_type": artifact["artifact_type"],
                "storage_relpath": storage_relpath.replace("\\", "/"),
                "sync_action": sync_result["action"],
            }
        )
        artifact_rows.append(
            {
                **artifact,
                "artifact_id": f"artifact-{uuid.uuid4().hex}",
                "storage_relpath": storage_relpath.replace("\\", "/"),
                "stored_at_utc": now_utc,
            }
        )

    if not existing_central_run_id:
        created = True
        conn.execute(
            """
            INSERT INTO runs (
                central_run_id,
                workstation_id,
                camera_profile_id,
                latest_ingest_batch_id,
                local_run_id,
                local_manifest_path,
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
                replay_manifest_version,
                replay_capabilities_json,
                storage_tier,
                replay_default_mode,
                segment_count,
                idle_segment_count,
                active_segment_count,
                replay_status,
                ready_artifact_count,
                required_artifact_count,
                first_ingested_utc,
                last_ingested_utc,
                archived_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3, 3, ?, ?, '')
            """,
            (
                central_run_id,
                workstation_id,
                camera_profile_id,
                ingest_batch_id,
                run.get("local_run_id") or "",
                run.get("local_manifest_path") or "",
                run.get("label") or "run",
                run.get("source_name") or "",
                run.get("process_gate") or "",
                run.get("stop_reason") or "",
                run.get("started_at_local") or "",
                run.get("stopped_at_local") or "",
                float(run.get("duration_sec") or 0),
                run.get("hamilton_log_dir") or "",
                run.get("hamilton_log_glob") or "",
                run.get("trace_pairing_delta_sec"),
                run.get("replay_manifest_version") or "",
                json.dumps(run.get("replay_capabilities") or []),
                run.get("storage_tier") or "",
                run.get("replay_default_mode") or "",
                int(run.get("segment_count") or 0),
                int(run.get("idle_segment_count") or 0),
                int(run.get("active_segment_count") or 0),
                run.get("replay_status") or "ready",
                now_utc,
                now_utc,
            ),
        )
        for artifact in artifact_rows:
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id,
                    central_run_id,
                    artifact_type,
                    original_filename,
                    storage_relpath,
                    mime_type,
                    compression_kind,
                    content_sha256,
                    size_bytes,
                    stored_at_utc,
                    is_required,
                    is_ready
                ) VALUES (?, ?, ?, ?, ?, ?, 'none', ?, ?, ?, 1, 1)
                """,
                (
                    artifact["artifact_id"],
                    central_run_id,
                    artifact["artifact_type"],
                    artifact["original_filename"],
                    artifact["storage_relpath"],
                    artifact["mime_type"],
                    artifact["sha256"],
                    artifact["size_bytes"],
                    artifact["stored_at_utc"],
                ),
            )
    else:
        conn.execute(
            """
            UPDATE runs
            SET latest_ingest_batch_id = ?,
                camera_profile_id = ?,
                label = ?,
                source_name = ?,
                process_gate = ?,
                stop_reason = ?,
                started_at_local = ?,
                stopped_at_local = ?,
                duration_sec = ?,
                hamilton_log_dir = ?,
                hamilton_log_glob = ?,
                trace_pairing_delta_sec = ?,
                replay_manifest_version = ?,
                replay_capabilities_json = ?,
                storage_tier = ?,
                replay_default_mode = ?,
                segment_count = ?,
                idle_segment_count = ?,
                active_segment_count = ?,
                replay_status = ?,
                ready_artifact_count = 3,
                required_artifact_count = 3,
                last_ingested_utc = ?
            WHERE central_run_id = ?
            """,
            (
                ingest_batch_id,
                camera_profile_id,
                run.get("label") or "run",
                run.get("source_name") or "",
                run.get("process_gate") or "",
                run.get("stop_reason") or "",
                run.get("started_at_local") or "",
                run.get("stopped_at_local") or "",
                float(run.get("duration_sec") or 0),
                run.get("hamilton_log_dir") or "",
                run.get("hamilton_log_glob") or "",
                run.get("trace_pairing_delta_sec"),
                run.get("replay_manifest_version") or "",
                json.dumps(run.get("replay_capabilities") or []),
                run.get("storage_tier") or "",
                run.get("replay_default_mode") or "",
                int(run.get("segment_count") or 0),
                int(run.get("idle_segment_count") or 0),
                int(run.get("active_segment_count") or 0),
                run.get("replay_status") or "ready",
                now_utc,
                central_run_id,
            ),
        )
        existing_artifact_ids = {
            row["artifact_type"]: row["artifact_id"]
            for row in conn.execute(
                "SELECT artifact_id, artifact_type FROM artifacts WHERE central_run_id = ?",
                (central_run_id,),
            ).fetchall()
        }
        for artifact in artifact_rows:
            artifact["artifact_id"] = existing_artifact_ids.get(
                artifact["artifact_type"],
                artifact["artifact_id"],
            )
            conn.execute(
                """
                UPDATE artifacts
                SET original_filename = ?,
                    storage_relpath = ?,
                    mime_type = ?,
                    compression_kind = 'none',
                    content_sha256 = ?,
                    size_bytes = ?,
                    stored_at_utc = ?,
                    is_required = 1,
                    is_ready = 1
                WHERE central_run_id = ? AND artifact_type = ?
                """,
                (
                    artifact["original_filename"],
                    artifact["storage_relpath"],
                    artifact["mime_type"],
                    artifact["sha256"],
                    artifact["size_bytes"],
                    artifact["stored_at_utc"],
                    central_run_id,
                    artifact["artifact_type"],
                ),
            )

    record_ingest_items(
        conn,
        ingest_batch_id=ingest_batch_id,
        central_run_id=central_run_id,
        artifact_rows=artifact_rows,
        created_at_utc=now_utc,
    )

    return {
        "central_run_id": central_run_id,
        "created": created,
        "workstation_id": workstation_id,
        "camera_profile_id": camera_profile_id,
        "artifact_count": len(artifact_rows),
        "artifact_sync_actions": artifact_sync_actions,
        "stored_artifacts": [
            {
                "artifact_type": artifact["artifact_type"],
                "storage_relpath": artifact["storage_relpath"],
                "sha256": artifact["sha256"],
                "size_bytes": artifact["size_bytes"],
            }
            for artifact in artifact_rows
        ],
    }


def write_ack(stage_row: sqlite3.Row, ack_payload: dict) -> Path:
    """Persist one acknowledgement beside the staged run bundle."""
    ack_path = Path(stage_row["run_dir"]) / ACK_FILENAME
    ack_path.write_text(json.dumps(ack_payload, indent=2), encoding="utf-8")
    return ack_path


def update_local_manifest_after_upload(stage_row: sqlite3.Row, *, ack_payload: dict | None = None, error_text: str = "") -> None:
    manifest_path = Path(str(stage_row["manifest_path"] or "")).resolve()
    if not manifest_path.exists():
        return
    if ack_payload is not None:
        record_upload_ack(
            manifest_path,
            central_run_id=str(ack_payload.get("central_run_id") or ""),
            acknowledged_at_utc=str(ack_payload.get("acknowledged_at_utc") or ""),
            ack_path=str(Path(str(stage_row["run_dir"])) / ACK_FILENAME),
        )
        return
    if error_text:
        record_upload_failure(manifest_path, error_text=error_text)


def mark_upload_result(
    conn: sqlite3.Connection,
    *,
    stage_run_id: str,
    upload_batch_id: str,
    attempted_at_utc: str,
    upload_status: str,
    central_run_id: str = "",
    ack_path: str = "",
    last_upload_error: str = "",
) -> None:
    """Update the local staging ledger with one upload outcome."""
    completed_at = attempted_at_utc if upload_status == "acknowledged" else ""
    conn.execute(
        """
        UPDATE staged_runs
        SET upload_status = ?,
            central_run_id = ?,
            upload_batch_id = ?,
            upload_attempt_count = upload_attempt_count + 1,
            last_upload_attempt_utc = ?,
            upload_completed_at_utc = CASE WHEN ? <> '' THEN ? ELSE upload_completed_at_utc END,
            ack_path = ?,
            last_upload_error = ?
        WHERE stage_run_id = ?
        """,
        (
            upload_status,
            central_run_id,
            upload_batch_id,
            attempted_at_utc,
            completed_at,
            completed_at,
            ack_path,
            last_upload_error,
            stage_run_id,
        ),
    )


def upload_staged_runs(
    *,
    config_path: Path,
    local_config_path: Path,
    staging_root: Path | None,
    upload_root: Path | None,
    limit: int,
    batch_id: str,
) -> dict:
    """Upload pending staged runs into the shared central replay root."""
    config = load_effective_config(config_path=config_path, local_override_path=local_config_path)
    staging_cleanup = (config.get("central_ingest") or {}).get("staging_cleanup") or {}
    effective_staging_root = (staging_root or Path(config["central_ingest"]["staging_root"])).resolve()
    effective_upload_root = (upload_root or Path(config["central_ingest"]["upload_root"])).resolve()
    effective_staging_root.mkdir(parents=True, exist_ok=True)
    effective_upload_root.mkdir(parents=True, exist_ok=True)

    staging_catalog_path = effective_staging_root / STAGE_MODULE.CATALOG_FILENAME
    if not staging_catalog_path.exists():
        raise FileNotFoundError(f"Staging catalog not found: {staging_catalog_path}")

    central_catalog_path = effective_upload_root / CENTRAL_CATALOG_FILENAME
    ingest_batch_id = upload_batch_id()
    started_at_utc = utc_now_text()

    with closing(STAGE_MODULE.get_db_connection(staging_catalog_path)) as stage_conn:
        STAGE_MODULE.init_catalog_db(stage_conn)
        pending_runs = get_pending_runs(stage_conn, batch_id=batch_id, limit=limit)
        if not pending_runs:
            return {
                "ingest_batch_id": "",
                "started_at_utc": started_at_utc,
                "completed_at_utc": started_at_utc,
                "staging_root": str(effective_staging_root),
                "upload_root": str(effective_upload_root),
                "staging_catalog_path": str(staging_catalog_path.resolve()),
                "central_catalog_path": str(central_catalog_path.resolve()),
                "uploaded_run_count": 0,
                "failed_run_count": 0,
                "items": [],
            }

        with closing(sqlite3.connect(central_catalog_path)) as central_conn:
            central_conn.row_factory = sqlite3.Row
            init_central_db(central_conn)
            first_payload = parse_payload(Path(pending_runs[0]["payload_path"]))
            workstation_id = ensure_workstation(central_conn, first_payload, now_utc=started_at_utc)
            uploader_hostname = str(first_payload["workstation"].get("hostname") or workstation_id)
            central_conn.execute(
                """
                INSERT INTO ingest_batches (
                    ingest_batch_id,
                    workstation_id,
                    uploader_version,
                    uploader_hostname,
                    started_at_utc,
                    completed_at_utc,
                    status,
                    notes
                ) VALUES (?, ?, ?, ?, ?, '', 'uploading', '')
                """,
                (
                    ingest_batch_id,
                    workstation_id,
                    UPLOADER_VERSION,
                    uploader_hostname,
                    started_at_utc,
                ),
            )

            items: list[dict] = []
            uploaded_count = 0
            failed_count = 0
            for stage_row in pending_runs:
                attempted_at_utc = utc_now_text()
                payload = parse_payload(Path(stage_row["payload_path"]))
                try:
                    ingest_result = ingest_one_run(
                        central_conn,
                        central_root=effective_upload_root,
                        ingest_batch_id=ingest_batch_id,
                        stage_row=stage_row,
                        payload=payload,
                    )
                    ack_payload = {
                        "schema_version": "central-replay-ack.v1",
                        "acknowledged_at_utc": attempted_at_utc,
                        "ingest_batch_id": ingest_batch_id,
                        "stage_run_id": stage_row["stage_run_id"],
                        "local_batch_id": stage_row["batch_id"],
                        "central_run_id": ingest_result["central_run_id"],
                        "status": "acknowledged",
                        "created": ingest_result["created"],
                        "workstation_id": ingest_result["workstation_id"],
                        "camera_profile_id": ingest_result["camera_profile_id"],
                        "stored_artifacts": ingest_result["stored_artifacts"],
                    }
                    ack_path = write_ack(stage_row, ack_payload)
                    update_local_manifest_after_upload(stage_row, ack_payload=ack_payload)
                    mark_upload_result(
                        stage_conn,
                        stage_run_id=stage_row["stage_run_id"],
                        upload_batch_id=ingest_batch_id,
                        attempted_at_utc=attempted_at_utc,
                        upload_status="acknowledged",
                        central_run_id=ingest_result["central_run_id"],
                        ack_path=str(ack_path.resolve()),
                    )
                    pruned = {
                        "action": "skipped_disabled",
                        "pruned_bytes": 0,
                    }
                    if bool(staging_cleanup.get("enabled", True)) and bool(staging_cleanup.get("prune_after_ack", True)):
                        refreshed_row = stage_conn.execute(
                            "SELECT * FROM staged_runs WHERE stage_run_id = ?",
                            (stage_row["stage_run_id"],),
                        ).fetchone()
                        if refreshed_row is not None:
                            pruned = STAGE_MODULE.prune_stage_run_bundle(stage_conn, refreshed_row)
                    uploaded_count += 1
                    items.append(
                        {
                            "action": "acknowledged",
                            "stage_run_id": stage_row["stage_run_id"],
                            "local_run_id": stage_row["local_run_id"],
                        "central_run_id": ingest_result["central_run_id"],
                        "created": ingest_result["created"],
                        "ack_path": str(ack_path.resolve()),
                        "artifact_sync_actions": ingest_result["artifact_sync_actions"],
                        "prune_action": pruned["action"],
                        "pruned_bytes": int(pruned.get("pruned_bytes") or 0),
                    }
                    )
                except Exception as exc:
                    failed_count += 1
                    update_local_manifest_after_upload(stage_row, error_text=str(exc))
                    mark_upload_result(
                        stage_conn,
                        stage_run_id=stage_row["stage_run_id"],
                        upload_batch_id=ingest_batch_id,
                        attempted_at_utc=attempted_at_utc,
                        upload_status="upload_failed",
                        last_upload_error=str(exc),
                    )
                    items.append(
                        {
                            "action": "upload_failed",
                            "stage_run_id": stage_row["stage_run_id"],
                            "local_run_id": stage_row["local_run_id"],
                            "error": str(exc),
                        }
                    )

            completed_at_utc = utc_now_text()
            central_conn.execute(
                """
                UPDATE ingest_batches
                SET completed_at_utc = ?,
                    status = ?,
                    notes = ?
                WHERE ingest_batch_id = ?
                """,
                (
                    completed_at_utc,
                    "complete" if failed_count == 0 else "partial_failure",
                    f"Uploaded {uploaded_count} runs; failed {failed_count}.",
                    ingest_batch_id,
                ),
            )
            central_conn.commit()
            stage_conn.commit()

    return {
        "ingest_batch_id": ingest_batch_id,
        "started_at_utc": started_at_utc,
        "completed_at_utc": completed_at_utc,
        "staging_root": str(effective_staging_root),
        "upload_root": str(effective_upload_root),
        "staging_catalog_path": str(staging_catalog_path.resolve()),
        "central_catalog_path": str(central_catalog_path.resolve()),
        "uploaded_run_count": uploaded_count,
        "failed_run_count": failed_count,
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload staged replay runs into the central replay root")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the base camera config JSON")
    parser.add_argument("--local-config", default=str(DEFAULT_LOCAL_OVERRIDE_PATH), help="Path to the optional workstation-local override JSON")
    parser.add_argument("--staging-root", default="", help="Directory that holds central ingest staging batches")
    parser.add_argument("--upload-root", default="", help="Directory that holds the central replay catalog and storage tree")
    parser.add_argument("--batch-id", default="", help="Optional local staging batch id filter")
    parser.add_argument("--limit", type=int, default=0, help="Optional maximum number of staged runs to upload")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    payload = upload_staged_runs(
        config_path=Path(args.config).resolve(),
        local_config_path=Path(args.local_config).resolve(),
        staging_root=Path(args.staging_root).resolve() if args.staging_root else None,
        upload_root=Path(args.upload_root).resolve() if args.upload_root else None,
        limit=args.limit,
        batch_id=args.batch_id.strip(),
    )

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Ingest batch: {payload['ingest_batch_id'] or '(none)'}")
        print(f"Uploaded: {payload['uploaded_run_count']}")
        print(f"Failed: {payload['failed_run_count']}")
        print(f"Central catalog: {payload['central_catalog_path']}")
        print(f"Upload root: {payload['upload_root']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
