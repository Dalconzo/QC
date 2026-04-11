-- Central replay catalog schema
--
-- This is the first SQL bootstrap for the future LAN replay service. It is
-- intentionally small and aligned with the current workstation-local artifacts:
-- one MP4, one Hamilton trace, and one `.run.json` manifest per completed run.
--
-- The central service should store large artifacts on disk/network storage and
-- use this catalog to track metadata, ingest state, and replay readiness.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workstations (
    workstation_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL UNIQUE,
    machine_alias TEXT NOT NULL DEFAULT '',
    instrument_name TEXT NOT NULL DEFAULT '',
    site_name TEXT NOT NULL DEFAULT '',
    repo_root TEXT NOT NULL DEFAULT '',
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS camera_profiles (
    camera_profile_id TEXT PRIMARY KEY,
    workstation_id TEXT NOT NULL,
    profile_key TEXT NOT NULL,
    profile_label TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    UNIQUE (workstation_id, profile_key),
    FOREIGN KEY (workstation_id) REFERENCES workstations(workstation_id)
);

CREATE TABLE IF NOT EXISTS ingest_batches (
    ingest_batch_id TEXT PRIMARY KEY,
    workstation_id TEXT NOT NULL,
    uploader_version TEXT NOT NULL DEFAULT '',
    uploader_hostname TEXT NOT NULL DEFAULT '',
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (workstation_id) REFERENCES workstations(workstation_id)
);

CREATE TABLE IF NOT EXISTS runs (
    central_run_id TEXT PRIMARY KEY,
    workstation_id TEXT NOT NULL,
    camera_profile_id TEXT NOT NULL,
    latest_ingest_batch_id TEXT NOT NULL DEFAULT '',
    local_run_id TEXT NOT NULL DEFAULT '',
    local_manifest_path TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    process_gate TEXT NOT NULL DEFAULT '',
    stop_reason TEXT NOT NULL DEFAULT '',
    started_at_local TEXT NOT NULL,
    stopped_at_local TEXT NOT NULL,
    duration_sec REAL NOT NULL DEFAULT 0,
    hamilton_log_dir TEXT NOT NULL DEFAULT '',
    hamilton_log_glob TEXT NOT NULL DEFAULT '',
    trace_pairing_delta_sec REAL,
    replay_status TEXT NOT NULL,
    ready_artifact_count INTEGER NOT NULL DEFAULT 0,
    required_artifact_count INTEGER NOT NULL DEFAULT 3,
    first_ingested_utc TEXT NOT NULL,
    last_ingested_utc TEXT NOT NULL,
    archived_at_utc TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (workstation_id) REFERENCES workstations(workstation_id),
    FOREIGN KEY (camera_profile_id) REFERENCES camera_profiles(camera_profile_id),
    FOREIGN KEY (latest_ingest_batch_id) REFERENCES ingest_batches(ingest_batch_id)
);

CREATE INDEX IF NOT EXISTS idx_runs_started_local
    ON runs(started_at_local DESC);

CREATE INDEX IF NOT EXISTS idx_runs_status_started
    ON runs(replay_status, started_at_local DESC);

CREATE INDEX IF NOT EXISTS idx_runs_workstation_started
    ON runs(workstation_id, started_at_local DESC);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    central_run_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    storage_relpath TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT '',
    compression_kind TEXT NOT NULL DEFAULT 'none',
    content_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    stored_at_utc TEXT NOT NULL,
    is_required INTEGER NOT NULL DEFAULT 1,
    is_ready INTEGER NOT NULL DEFAULT 1,
    UNIQUE (central_run_id, artifact_type),
    FOREIGN KEY (central_run_id) REFERENCES runs(central_run_id)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run
    ON artifacts(central_run_id);

CREATE INDEX IF NOT EXISTS idx_artifacts_hash
    ON artifacts(content_sha256);

CREATE TABLE IF NOT EXISTS ingest_items (
    ingest_item_id TEXT PRIMARY KEY,
    ingest_batch_id TEXT NOT NULL,
    central_run_id TEXT NOT NULL DEFAULT '',
    artifact_id TEXT NOT NULL DEFAULT '',
    artifact_type TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    received_filename TEXT NOT NULL DEFAULT '',
    received_size_bytes INTEGER NOT NULL DEFAULT 0,
    received_sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (ingest_batch_id) REFERENCES ingest_batches(ingest_batch_id),
    FOREIGN KEY (central_run_id) REFERENCES runs(central_run_id),
    FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_ingest_items_batch
    ON ingest_items(ingest_batch_id);

CREATE INDEX IF NOT EXISTS idx_ingest_items_run
    ON ingest_items(central_run_id);
