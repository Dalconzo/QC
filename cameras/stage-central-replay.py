#!/usr/bin/env python3
"""
stage-central-replay.py

Prepare completed workstation-local replay runs for future central ingest.

This tool does not talk to the LAN service yet. Instead, it creates a durable
local staging batch that mirrors the upload contract we documented for the
central replay server:

- one staged batch per invocation
- one copied video/trace/manifest bundle per replayable run
- one `run-upload.json` payload that captures hashes, sizes, and workstation
  metadata in a server-friendly shape
- one SQLite catalog that remembers which exact artifact set has already been
  staged so later runs can skip duplicates cleanly
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import mimetypes
import shutil
import socket
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, load_effective_config


SCHEMA_VERSION = "central-replay-stage.v1"
CATALOG_FILENAME = ".central_ingest_staging.sqlite3"
INSPECT_MANIFESTS_PATH = Path(__file__).resolve().parent / "inspect-run-manifests.py"
RUN_UPLOAD_FILENAME = "run-upload.json"
RUN_ACK_FILENAME = "run-ack.json"
STAGED_METADATA_FILENAMES = {RUN_UPLOAD_FILENAME, RUN_ACK_FILENAME}


def load_inspect_module():
    """Import the manifest helper module even though the filename uses hyphens."""
    spec = importlib.util.spec_from_file_location("camera_inspect_run_manifests", INSPECT_MANIFESTS_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


INSPECT_MODULE = load_inspect_module()


def utc_now_text() -> str:
    """Return a stable UTC timestamp string for staging metadata."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def compute_sha256(path: Path) -> str:
    """Hash one file as the future ingest server will see it."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_text_sha256(text: str) -> str:
    """Hash generated JSON content before it is written to disk."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonicalize_manifest_for_staging(payload: dict) -> dict:
    """Strip workstation-local upload/cleanup churn before hashing or copying."""
    canonical = copy.deepcopy(payload)
    local_retention = canonical.get("local_retention")
    if isinstance(local_retention, dict):
        for field_name in (
            "upload_status",
            "upload_completed_at_utc",
            "upload_error",
            "ack_path",
            "central_run_id",
            "lan_available",
            "original_delete_eligible_at_local",
            "original_deleted_at_local",
            "last_cleanup_at_local",
            "last_cleanup_action",
            "last_cleanup_mode",
            "last_cleanup_reason",
        ):
            local_retention[field_name] = ""
        local_retention["lan_available"] = False
    return canonical


def stage_batch_id() -> str:
    """Generate one local batch id for this staging pass."""
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"batch-{stamp}-{uuid.uuid4().hex[:8]}"


def compute_stage_signature(item: dict, artifact_hashes: dict[str, str]) -> str:
    """Build a durable duplicate key from the local run identity plus file hashes."""
    identity = {
        "run_id": item["run_id"],
        "manifest_sha256": artifact_hashes["run_manifest_json"],
        "video_sha256": artifact_hashes["video_mp4"],
        "trace_sha256": artifact_hashes["trace_trc"],
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


def detect_machine_alias(runs_root: Path, manifest_path: Path) -> str:
    """Infer the Hamilton alias from a canonical runs-root-relative path if possible."""
    try:
        relative_path = manifest_path.resolve().relative_to(runs_root.resolve())
    except ValueError:
        return ""
    parts = list(relative_path.parts)
    if len(parts) >= 2:
        return parts[0]
    return ""


def infer_profile(config: dict, payload: dict) -> dict:
    """Match a recorded run back to the configured camera profile when possible."""
    recorded_source = str(payload.get("source") or "").strip()
    for profile in config.get("profiles", []):
        if recorded_source and recorded_source == str(profile.get("source") or "").strip():
            return profile
    for profile in config.get("profiles", []):
        if recorded_source and recorded_source == str(profile.get("label") or "").strip():
            return profile
    return config.get("profiles", [])[0]


def artifact_metadata(artifact_type: str, source_path: Path, staged_path: Path) -> dict:
    """Describe one artifact exactly as the future central service will need it."""
    mime_type, _encoding = mimetypes.guess_type(source_path.name)
    return {
        "artifact_type": artifact_type,
        "original_filename": source_path.name,
        "source_path": str(source_path.resolve()),
        "staged_path": str(staged_path.resolve()),
        "staged_filename": staged_path.name,
        "mime_type": mime_type or "application/octet-stream",
        "size_bytes": staged_path.stat().st_size,
        "sha256": compute_sha256(staged_path),
    }


def init_catalog_db(conn: sqlite3.Connection) -> None:
    """Create the local staging ledger if it does not exist yet."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS staging_batches (
            batch_id TEXT PRIMARY KEY,
            staging_root TEXT NOT NULL,
            batch_dir TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            completed_at_utc TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            staged_run_count INTEGER NOT NULL DEFAULT 0,
            skipped_run_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS staged_runs (
            stage_run_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            stage_signature TEXT NOT NULL UNIQUE,
            local_run_id TEXT NOT NULL,
            label TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            video_path TEXT NOT NULL,
            trace_path TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            video_sha256 TEXT NOT NULL,
            trace_sha256 TEXT NOT NULL,
            payload_path TEXT NOT NULL,
            run_dir TEXT NOT NULL,
            replay_status TEXT NOT NULL,
            upload_status TEXT NOT NULL,
            central_run_id TEXT NOT NULL DEFAULT '',
            upload_batch_id TEXT NOT NULL DEFAULT '',
            upload_attempt_count INTEGER NOT NULL DEFAULT 0,
            last_upload_attempt_utc TEXT NOT NULL DEFAULT '',
            upload_completed_at_utc TEXT NOT NULL DEFAULT '',
            ack_path TEXT NOT NULL DEFAULT '',
            last_upload_error TEXT NOT NULL DEFAULT '',
            staged_bundle_status TEXT NOT NULL DEFAULT 'full',
            pruned_at_utc TEXT NOT NULL DEFAULT '',
            pruned_bytes INTEGER NOT NULL DEFAULT 0,
            started_at_local TEXT NOT NULL DEFAULT '',
            stopped_at_local TEXT NOT NULL DEFAULT '',
            staged_at_utc TEXT NOT NULL,
            FOREIGN KEY (batch_id) REFERENCES staging_batches(batch_id)
        );

        CREATE TABLE IF NOT EXISTS staged_artifacts (
            stage_artifact_id TEXT PRIMARY KEY,
            stage_run_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            source_path TEXT NOT NULL,
            staged_path TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mime_type TEXT NOT NULL DEFAULT '',
            created_at_utc TEXT NOT NULL,
            UNIQUE (stage_run_id, artifact_type),
            FOREIGN KEY (stage_run_id) REFERENCES staged_runs(stage_run_id)
        );

        CREATE INDEX IF NOT EXISTS idx_staged_runs_batch
            ON staged_runs(batch_id);

        CREATE INDEX IF NOT EXISTS idx_staged_runs_status
            ON staged_runs(upload_status, staged_at_utc DESC);

        CREATE INDEX IF NOT EXISTS idx_staged_artifacts_run
            ON staged_artifacts(stage_run_id);
        """
    )
    ensure_catalog_migrations(conn)
    conn.commit()


def ensure_catalog_migrations(conn: sqlite3.Connection) -> None:
    """Add newer staging columns when an older local ledger already exists."""
    expected_columns = {
        "central_run_id": "TEXT NOT NULL DEFAULT ''",
        "upload_batch_id": "TEXT NOT NULL DEFAULT ''",
        "upload_attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "last_upload_attempt_utc": "TEXT NOT NULL DEFAULT ''",
        "upload_completed_at_utc": "TEXT NOT NULL DEFAULT ''",
        "ack_path": "TEXT NOT NULL DEFAULT ''",
        "last_upload_error": "TEXT NOT NULL DEFAULT ''",
        "staged_bundle_status": "TEXT NOT NULL DEFAULT 'full'",
        "pruned_at_utc": "TEXT NOT NULL DEFAULT ''",
        "pruned_bytes": "INTEGER NOT NULL DEFAULT 0",
    }
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(staged_runs)")
    }
    for column_name, column_type in expected_columns.items():
        if column_name in existing_columns:
            continue
        conn.execute(f"ALTER TABLE staged_runs ADD COLUMN {column_name} {column_type}")


def get_db_connection(catalog_path: Path) -> sqlite3.Connection:
    """Open the SQLite staging ledger with row-style access."""
    conn = sqlite3.connect(catalog_path)
    conn.row_factory = sqlite3.Row
    return conn


def is_already_staged(conn: sqlite3.Connection, stage_signature: str) -> bool:
    """Skip restaging when the exact same artifact set was already captured."""
    row = conn.execute(
        "SELECT stage_run_id FROM staged_runs WHERE stage_signature = ?",
        (stage_signature,),
    ).fetchone()
    return row is not None


def build_payload(
    *,
    batch_id: str,
    config: dict,
    item: dict,
    payload: dict,
    profile: dict,
    workstation_id: str,
    machine_alias: str,
    artifact_rows: list[dict],
) -> dict:
    """Assemble the future upload contract for one completed local run."""
    hostname = socket.gethostname()
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": utc_now_text(),
        "ingest_batch_id": batch_id,
        "workstation": {
            "workstation_id": workstation_id,
            "hostname": hostname,
            "machine_alias": machine_alias,
            "repo_root": str(Path(config["config_path"]).resolve().parents[1]),
            "config_path": config["config_path"],
            "local_override_path": config["local_override_path"],
        },
        "camera_profile": {
            "profile_id": str(profile.get("id") or ""),
            "profile_label": str(profile.get("label") or ""),
            "source_name": str(payload.get("source") or profile.get("source") or ""),
        },
        "run": {
            "local_run_id": item["run_id"],
            "label": payload.get("label") or "run",
            "source_name": payload.get("source") or "",
            "process_gate": payload.get("process_gate") or "",
            "stop_reason": payload.get("stop_reason") or "",
            "started_at_local": payload.get("started_at_local") or "",
            "stopped_at_local": payload.get("stopped_at_local") or "",
            "duration_sec": payload.get("duration_sec") or 0,
            "hamilton_log_dir": payload.get("hamilton_log_dir") or "",
            "hamilton_log_glob": payload.get("hamilton_log_glob") or "",
            "trace_pairing_delta_sec": payload.get("trace_mtime_delta_sec"),
            "local_manifest_path": payload.get("manifest_path") or "",
            "local_video_path": payload.get("video_path") or "",
            "local_trace_path": payload.get("trace_path") or "",
            "replay_status": item["replay_status"],
            "replay_manifest_version": payload.get("replay_manifest_version") or "",
            "replay_capabilities": payload.get("replay_capabilities") or [],
            "storage_tier": payload.get("storage_tier") or "",
            "replay_default_mode": payload.get("replay_default_mode") or "",
            "full_detail_retained_until_local": payload.get("full_detail_retained_until_local") or "",
            "segment_count": len(payload.get("segments") or []),
            "idle_segment_count": int(payload.get("idle_segment_count") or 0),
            "active_segment_count": int(payload.get("active_segment_count") or 0),
        },
        "artifacts": artifact_rows,
    }


def stage_one_run(
    *,
    conn: sqlite3.Connection,
    batch_id: str,
    batch_dir: Path,
    runs_root: Path,
    config: dict,
    item: dict,
    restage: bool,
) -> dict:
    """Copy one completed local run into a durable staging bundle."""
    payload = INSPECT_MODULE.load_run_manifest(Path(item["manifest_path"]))
    profile = infer_profile(config, payload)
    manifest_path = Path(payload["manifest_path"])
    video_path = Path(payload["video_path"])
    trace_path = Path(payload["trace_path"])

    if item["replay_status"] != "ready":
        return {"action": "skipped_not_ready", "run_id": item["run_id"], "label": item["label"]}

    staging_manifest_payload = canonicalize_manifest_for_staging(payload)
    normalized_manifest_text = json.dumps(staging_manifest_payload, indent=2)
    artifact_hashes = {
        "run_manifest_json": compute_text_sha256(normalized_manifest_text),
        "video_mp4": compute_sha256(video_path),
        "trace_trc": compute_sha256(trace_path),
    }
    signature = compute_stage_signature(item, artifact_hashes)
    if (not restage) and is_already_staged(conn, signature):
        return {"action": "skipped_duplicate", "run_id": item["run_id"], "label": item["label"]}
    if restage:
        signature = f"{signature}:{batch_id}"

    run_dir = batch_dir / "runs" / item["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)

    # Keep deterministic staged filenames so the future uploader and server can
    # reason about artifact types without inspecting the original paths.
    staged_manifest = run_dir / "run_manifest.json"
    staged_video = run_dir / "video.mp4"
    staged_trace = run_dir / "trace.trc"
    staged_manifest.write_text(normalized_manifest_text, encoding="utf-8")
    shutil.copy2(video_path, staged_video)
    shutil.copy2(trace_path, staged_trace)

    artifact_rows = [
        artifact_metadata("run_manifest_json", manifest_path, staged_manifest),
        artifact_metadata("video_mp4", video_path, staged_video),
        artifact_metadata("trace_trc", trace_path, staged_trace),
    ]

    workstation_id = socket.gethostname().lower()
    machine_alias = detect_machine_alias(runs_root, manifest_path)
    payload_path = run_dir / RUN_UPLOAD_FILENAME
    upload_payload = build_payload(
        batch_id=batch_id,
        config=config,
        item=item,
        payload=payload,
        profile=profile,
        workstation_id=workstation_id,
        machine_alias=machine_alias,
        artifact_rows=artifact_rows,
    )
    payload_path.write_text(json.dumps(upload_payload, indent=2), encoding="utf-8")

    stage_run_id = f"stage-run-{uuid.uuid4().hex}"
    staged_at_utc = utc_now_text()
    conn.execute(
        """
        INSERT INTO staged_runs (
            stage_run_id,
            batch_id,
            stage_signature,
            local_run_id,
            label,
            manifest_path,
            video_path,
            trace_path,
            manifest_sha256,
            video_sha256,
            trace_sha256,
            payload_path,
            run_dir,
            replay_status,
            upload_status,
            staged_bundle_status,
            pruned_at_utc,
            pruned_bytes,
            started_at_local,
            stopped_at_local,
            staged_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stage_run_id,
            batch_id,
            signature,
            item["run_id"],
            item["label"],
            str(manifest_path),
            str(video_path),
            str(trace_path),
            artifact_hashes["run_manifest_json"],
            artifact_hashes["video_mp4"],
            artifact_hashes["trace_trc"],
            str(payload_path.resolve()),
            str(run_dir.resolve()),
            item["replay_status"],
            "staged",
            "full",
            "",
            0,
            payload.get("started_at_local") or "",
            payload.get("stopped_at_local") or "",
            staged_at_utc,
        ),
    )

    for artifact_row in artifact_rows:
        conn.execute(
            """
            INSERT INTO staged_artifacts (
                stage_artifact_id,
                stage_run_id,
                artifact_type,
                source_path,
                staged_path,
                content_sha256,
                size_bytes,
                mime_type,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"stage-artifact-{uuid.uuid4().hex}",
                stage_run_id,
                artifact_row["artifact_type"],
                artifact_row["source_path"],
                artifact_row["staged_path"],
                artifact_row["sha256"],
                artifact_row["size_bytes"],
                artifact_row["mime_type"],
                staged_at_utc,
            ),
        )

    return {
        "action": "staged",
        "run_id": item["run_id"],
        "label": item["label"],
        "payload_path": str(payload_path.resolve()),
        "run_dir": str(run_dir.resolve()),
    }


def write_batch_summary(batch_path: Path, payload: dict) -> None:
    """Persist one human-readable batch summary beside the staged artifacts."""
    batch_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def prune_stage_run_bundle(conn: sqlite3.Connection, stage_row: sqlite3.Row) -> dict:
    """Delete staged media bytes for an acknowledged run while keeping audit files."""
    if str(stage_row["upload_status"] or "") != "acknowledged":
        return {"action": "skipped_not_acknowledged", "stage_run_id": stage_row["stage_run_id"], "pruned_bytes": 0}
    if str(stage_row["pruned_at_utc"] or ""):
        return {"action": "skipped_already_pruned", "stage_run_id": stage_row["stage_run_id"], "pruned_bytes": 0}

    run_dir = Path(str(stage_row["run_dir"] or "")).resolve()
    if not run_dir.exists():
        pruned_at_utc = utc_now_text()
        conn.execute(
            """
            UPDATE staged_runs
            SET staged_bundle_status = 'missing',
                pruned_at_utc = ?,
                pruned_bytes = 0
            WHERE stage_run_id = ?
            """,
            (pruned_at_utc, stage_row["stage_run_id"]),
        )
        return {"action": "marked_missing", "stage_run_id": stage_row["stage_run_id"], "pruned_bytes": 0}

    prunable_paths: list[Path] = []
    pruned_bytes = 0
    for child in run_dir.iterdir():
        if child.name in STAGED_METADATA_FILENAMES:
            continue
        if child.is_file():
            pruned_bytes += child.stat().st_size
            prunable_paths.append(child)

    for child in prunable_paths:
        child.unlink(missing_ok=True)

    pruned_at_utc = utc_now_text()
    conn.execute(
        """
        UPDATE staged_runs
        SET staged_bundle_status = ?,
            pruned_at_utc = ?,
            pruned_bytes = ?
        WHERE stage_run_id = ?
        """,
        ("metadata_only", pruned_at_utc, pruned_bytes, stage_row["stage_run_id"]),
    )
    return {
        "action": "pruned",
        "stage_run_id": stage_row["stage_run_id"],
        "run_dir": str(run_dir),
        "pruned_bytes": pruned_bytes,
        "retained_filenames": sorted(name for name in STAGED_METADATA_FILENAMES if (run_dir / name).exists()),
    }


def prune_acknowledged_stage_runs(
    *,
    config_path: Path,
    local_config_path: Path,
    staging_root: Path | None,
    limit: int = 0,
) -> dict:
    """Reclaim local staging bytes for acknowledged runs while preserving audit files."""
    config = load_effective_config(config_path=config_path, local_override_path=local_config_path)
    cleanup_config = (config.get("central_ingest") or {}).get("staging_cleanup") or {}
    effective_staging_root = (staging_root or Path(config["central_ingest"]["staging_root"])).resolve()
    effective_staging_root.mkdir(parents=True, exist_ok=True)
    catalog_path = effective_staging_root / CATALOG_FILENAME
    if not bool(cleanup_config.get("enabled", True)):
        return {
            "staging_root": str(effective_staging_root),
            "catalog_path": str(catalog_path.resolve()),
            "pruned_run_count": 0,
            "pruned_bytes": 0,
            "items": [],
            "action": "disabled",
        }
    if not catalog_path.exists():
        return {
            "staging_root": str(effective_staging_root),
            "catalog_path": str(catalog_path.resolve()),
            "pruned_run_count": 0,
            "pruned_bytes": 0,
            "items": [],
            "action": "missing_catalog",
        }

    with closing(get_db_connection(catalog_path)) as conn:
        init_catalog_db(conn)
        query = """
            SELECT *
            FROM staged_runs
            WHERE upload_status = 'acknowledged'
              AND pruned_at_utc = ''
            ORDER BY upload_completed_at_utc ASC, staged_at_utc ASC
        """
        params: tuple[object, ...] = ()
        if limit > 0:
            query += " LIMIT ?"
            params = (limit,)
        rows = list(conn.execute(query, params).fetchall())
        items = [prune_stage_run_bundle(conn, row) for row in rows]
        conn.commit()

    return {
        "staging_root": str(effective_staging_root),
        "catalog_path": str(catalog_path.resolve()),
        "pruned_run_count": sum(1 for item in items if item["action"] == "pruned"),
        "pruned_bytes": sum(int(item.get("pruned_bytes") or 0) for item in items),
        "items": items,
        "action": "ok",
    }


def stage_runs(
    *,
    config_path: Path,
    local_config_path: Path,
    runs_root: Path | None,
    staging_root: Path | None,
    limit: int,
    restage: bool,
) -> dict:
    """Stage replayable runs into one local batch and update the SQLite ledger."""
    config = load_effective_config(config_path=config_path, local_override_path=local_config_path)
    effective_runs_root = (runs_root or Path(config["storage"]["runs_root"])).resolve()
    effective_staging_root = (staging_root or Path(config["central_ingest"]["staging_root"])).resolve()
    effective_runs_root.mkdir(parents=True, exist_ok=True)
    effective_staging_root.mkdir(parents=True, exist_ok=True)

    catalog_path = effective_staging_root / CATALOG_FILENAME
    batch_id = stage_batch_id()
    batch_dir = effective_staging_root / "batches" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    items = [INSPECT_MODULE.describe_manifest(path) for path in INSPECT_MODULE.iter_manifest_paths(effective_runs_root)]
    if limit > 0:
        items = items[:limit]

    created_at_utc = utc_now_text()
    with closing(get_db_connection(catalog_path)) as conn:
        init_catalog_db(conn)
        conn.execute(
            """
            INSERT INTO staging_batches (
                batch_id,
                staging_root,
                batch_dir,
                created_at_utc,
                status,
                staged_run_count,
                skipped_run_count,
                notes
            ) VALUES (?, ?, ?, ?, ?, 0, 0, '')
            """,
            (
                batch_id,
                str(effective_staging_root),
                str(batch_dir),
                created_at_utc,
                "staging",
            ),
        )

        results: list[dict] = []
        staged_count = 0
        skipped_count = 0
        for item in items:
            result = stage_one_run(
                conn=conn,
                batch_id=batch_id,
                batch_dir=batch_dir,
                runs_root=effective_runs_root,
                config=config,
                item=item,
                restage=restage,
            )
            results.append(result)
            if result["action"] == "staged":
                staged_count += 1
            else:
                skipped_count += 1

        completed_at_utc = utc_now_text()
        conn.execute(
            """
            UPDATE staging_batches
            SET completed_at_utc = ?,
                status = ?,
                staged_run_count = ?,
                skipped_run_count = ?,
                notes = ?
            WHERE batch_id = ?
            """,
            (
                completed_at_utc,
                "complete",
                staged_count,
                skipped_count,
                "Initial local staging batch for central replay ingest.",
                batch_id,
            ),
        )
        conn.commit()

    batch_summary = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "created_at_utc": created_at_utc,
        "completed_at_utc": completed_at_utc,
        "runs_root": str(effective_runs_root),
        "staging_root": str(effective_staging_root),
        "catalog_path": str(catalog_path.resolve()),
        "batch_dir": str(batch_dir.resolve()),
        "staged_run_count": staged_count,
        "skipped_run_count": skipped_count,
        "items": results,
    }
    write_batch_summary(batch_dir / "batch-summary.json", batch_summary)
    return batch_summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage completed replay runs for central replay ingest")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the base camera config JSON")
    parser.add_argument("--local-config", default=str(DEFAULT_LOCAL_OVERRIDE_PATH), help="Path to the optional workstation-local override JSON")
    parser.add_argument("--runs-root", default="", help="Directory that contains .run.json replay manifests")
    parser.add_argument("--staging-root", default="", help="Directory that should hold central ingest staging batches")
    parser.add_argument("--limit", type=int, default=0, help="Optional maximum number of manifests to inspect")
    parser.add_argument("--restage", action="store_true", help="Stage runs even if the same artifact set was already staged")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    payload = stage_runs(
        config_path=Path(args.config).resolve(),
        local_config_path=Path(args.local_config).resolve(),
        runs_root=Path(args.runs_root).resolve() if args.runs_root else None,
        staging_root=Path(args.staging_root).resolve() if args.staging_root else None,
        limit=args.limit,
        restage=args.restage,
    )

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Batch: {payload['batch_id']}")
        print(f"Staged: {payload['staged_run_count']}")
        print(f"Skipped: {payload['skipped_run_count']}")
        print(f"Batch dir: {payload['batch_dir']}")
        print(f"Catalog: {payload['catalog_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
