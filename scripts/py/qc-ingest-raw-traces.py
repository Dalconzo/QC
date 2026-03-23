#!/usr/bin/env python3
"""
qc-ingest-raw-traces.py

Ingest full Hamilton .trc files into a compact local SQLite store.

Design goals:
- Retain the full raw file contents so retention decisions on the network share
  can be based on a durable local copy.
- Compress payloads before storage so the local copy stays substantially
  smaller than a loose working tree of mirrored traces.
- Keep run traces and HxUsbComm traces in separate storage lanes even when they
  share the same source root.
- Deduplicate by payload hash within each lane so repeated copies of the same
  file do not multiply storage usage.
- Record each ingest batch and each file observation so later retention logic
  can prove exactly when a file was seen, what payload it mapped to, and
  whether the ingest created a new stored blob or reused an existing one.

The database keeps two related tables:
- traces: one row per unique compressed payload in a specific stream
- trace_sources: one row per observed source path that points at a payload
- ingest_batches: one row per ingest execution with operator/runtime metadata
- ingest_events: one row per observed file within a specific ingest batch
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


STREAM_PATTERNS = {
    "run_trace": "*_Trace.trc",
    "usbcomm": "HxUsbComm*.trc",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest raw Hamilton trace files into a compressed SQLite store"
    )
    parser.add_argument(
        "--source-root",
        default=os.path.join("Z:\\", "Logs"),
        help="Root directory that contains the Hamilton Logs tree",
    )
    parser.add_argument(
        "--database-path",
        default=os.path.join("archive", "raw-trace-store", "raw-traces.sqlite"),
        help="SQLite database that will store compressed raw trace payloads",
    )
    parser.add_argument(
        "--stream",
        default="all",
        choices=["all", "run_trace", "usbcomm"],
        help="Which trace stream to ingest",
    )
    parser.add_argument(
        "--out-manifest-csv",
        default="",
        help="Optional CSV path that records the rows observed in this ingest run",
    )
    parser.add_argument(
        "--out-manifest-jsonl",
        default="",
        help="Optional JSONL path that records the rows observed in this ingest run",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Optional global cap for bounded smoke tests",
    )
    parser.add_argument(
        "--max-files-per-machine",
        type=int,
        default=0,
        help="Optional per-machine cap for bounded smoke tests",
    )
    parser.add_argument(
        "--compression-level",
        type=int,
        default=6,
        help="gzip compression level from 0-9; 6 balances size and speed for scheduled runs",
    )
    parser.add_argument(
        "--recurse",
        action="store_true",
        help="Recursively scan under the source root",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-file progress during ingest",
    )
    parser.add_argument(
        "--batch-label",
        default="",
        help="Optional operator-friendly label for this ingest batch",
    )
    return parser.parse_args()


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_root(path: str) -> str:
    return os.path.abspath(path).rstrip("\\/")


def machine_from_path(path: str) -> str:
    parts = path.replace("/", "\\").split("\\")
    try:
        idx = next(i for i, part in enumerate(parts) if part.lower() == "logs")
        if idx + 1 < len(parts):
            return parts[idx + 1].upper()
    except StopIteration:
        pass

    for part in parts:
        upper = part.upper()
        if upper.startswith("H") and upper[1:].isdigit():
            return upper
    return ""


def log_date_from_path(file_path: str) -> str:
    parent = Path(file_path).parent.name
    if len(parent) == 10 and parent[4] == "-" and parent[7] == "-":
        return parent
    return ""


def relative_path(full_path: str, source_root: str) -> str:
    normalized_full = os.path.abspath(full_path)
    normalized_root = normalize_root(source_root)
    if normalized_full.lower().startswith(normalized_root.lower()):
        return normalized_full[len(normalized_root) :].lstrip("\\/")
    return normalized_full


def iter_files(source_root: str, recurse: bool, stream: str) -> Iterable[Tuple[str, str]]:
    root = Path(source_root)
    if not root.exists():
        return []

    streams = list(STREAM_PATTERNS.keys()) if stream == "all" else [stream]
    matches: List[Tuple[str, str]] = []
    for stream_name in streams:
        pattern = STREAM_PATTERNS[stream_name]
        walker = root.rglob(pattern) if recurse else root.glob(pattern)
        for path in walker:
            if path.is_file():
                matches.append((stream_name, str(path)))
    matches.sort(key=lambda item: (item[0], item[1].lower()))
    return matches


def apply_sampling(
    files: Sequence[Tuple[str, str]],
    per_machine_limit: int,
    global_limit: int,
) -> List[Tuple[str, str]]:
    if per_machine_limit > 0:
        grouped: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)
        for stream_name, file_path in files:
            grouped[(stream_name, machine_from_path(file_path))].append((stream_name, file_path))

        sampled: List[Tuple[str, str]] = []
        for key in sorted(grouped.keys()):
            sampled.extend(grouped[key][:per_machine_limit])
        files = sampled

    if global_limit > 0:
        return list(files[:global_limit])
    return list(files)


def ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def connect_db(database_path: str) -> sqlite3.Connection:
    ensure_parent(database_path)
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS traces (
            trace_id INTEGER PRIMARY KEY,
            stream_type TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            machine TEXT NOT NULL DEFAULT '',
            log_local_date TEXT NOT NULL DEFAULT '',
            file_name TEXT NOT NULL,
            canonical_relative_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            compressed_size_bytes INTEGER NOT NULL,
            compression_codec TEXT NOT NULL,
            file_mtime_utc TEXT NOT NULL,
            ingested_at_utc TEXT NOT NULL,
            content_gzip BLOB NOT NULL,
            UNIQUE(stream_type, sha256)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trace_sources (
            source_id INTEGER PRIMARY KEY,
            trace_id INTEGER NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
            stream_type TEXT NOT NULL,
            source_root TEXT NOT NULL,
            source_path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            seen_at_utc TEXT NOT NULL,
            UNIQUE(stream_type, source_path)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_traces_stream_machine_date ON traces(stream_type, machine, log_local_date)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trace_sources_trace_id ON trace_sources(trace_id)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_batches (
            batch_id INTEGER PRIMARY KEY,
            batch_started_at_utc TEXT NOT NULL,
            batch_completed_at_utc TEXT NOT NULL DEFAULT '',
            source_root TEXT NOT NULL,
            stream_filter TEXT NOT NULL,
            recurse INTEGER NOT NULL,
            max_files INTEGER NOT NULL,
            max_files_per_machine INTEGER NOT NULL,
            compression_level INTEGER NOT NULL,
            host_name TEXT NOT NULL,
            batch_label TEXT NOT NULL DEFAULT '',
            observed_files INTEGER NOT NULL DEFAULT 0,
            new_trace_payloads INTEGER NOT NULL DEFAULT 0,
            new_source_locations INTEGER NOT NULL DEFAULT 0,
            total_input_bytes INTEGER NOT NULL DEFAULT 0,
            total_compressed_bytes INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_events (
            event_id INTEGER PRIMARY KEY,
            batch_id INTEGER NOT NULL REFERENCES ingest_batches(batch_id) ON DELETE CASCADE,
            trace_id INTEGER NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
            stream_type TEXT NOT NULL,
            source_root TEXT NOT NULL,
            source_path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            machine TEXT NOT NULL DEFAULT '',
            log_local_date TEXT NOT NULL DEFAULT '',
            file_name TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            compressed_size_bytes INTEGER NOT NULL,
            file_mtime_utc TEXT NOT NULL,
            seen_at_utc TEXT NOT NULL,
            trace_inserted INTEGER NOT NULL,
            source_inserted INTEGER NOT NULL,
            is_duplicate_content INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_ingest_events_batch_id ON ingest_events(batch_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_ingest_events_trace_id ON ingest_events(trace_id)"
    )
    return connection


def create_batch(
    connection: sqlite3.Connection,
    *,
    started_at_utc: str,
    source_root: str,
    stream_filter: str,
    recurse: bool,
    max_files: int,
    max_files_per_machine: int,
    compression_level: int,
    batch_label: str,
) -> int:
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO ingest_batches (
            batch_started_at_utc,
            source_root,
            stream_filter,
            recurse,
            max_files,
            max_files_per_machine,
            compression_level,
            host_name,
            batch_label
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at_utc,
            source_root,
            stream_filter,
            1 if recurse else 0,
            max_files,
            max_files_per_machine,
            compression_level,
            os.environ.get("COMPUTERNAME", ""),
            batch_label,
        ),
    )
    batch_id = int(cursor.lastrowid)
    cursor.close()
    return batch_id


def finalize_batch(
    connection: sqlite3.Connection,
    *,
    batch_id: int,
    completed_at_utc: str,
    manifest_rows: Sequence[Dict[str, object]],
) -> None:
    connection.execute(
        """
        UPDATE ingest_batches
        SET batch_completed_at_utc = ?,
            observed_files = ?,
            new_trace_payloads = ?,
            new_source_locations = ?,
            total_input_bytes = ?,
            total_compressed_bytes = ?
        WHERE batch_id = ?
        """,
        (
            completed_at_utc,
            len(manifest_rows),
            sum(int(row["trace_inserted"]) for row in manifest_rows),
            sum(int(row["source_inserted"]) for row in manifest_rows),
            sum(int(row["size_bytes"]) for row in manifest_rows),
            sum(int(row["compressed_size_bytes"]) for row in manifest_rows),
            batch_id,
        ),
    )


def record_ingest_event(
    connection: sqlite3.Connection,
    *,
    batch_id: int,
    row: Dict[str, object],
    source_root: str,
    seen_at_utc: str,
) -> None:
    connection.execute(
        """
        INSERT INTO ingest_events (
            batch_id,
            trace_id,
            stream_type,
            source_root,
            source_path,
            relative_path,
            machine,
            log_local_date,
            file_name,
            sha256,
            size_bytes,
            compressed_size_bytes,
            file_mtime_utc,
            seen_at_utc,
            trace_inserted,
            source_inserted,
            is_duplicate_content
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            int(row["trace_id"]),
            str(row["stream_type"]),
            source_root,
            str(row["source_path"]),
            str(row["relative_path"]),
            str(row["machine"]),
            str(row["log_local_date"]),
            str(row["file_name"]),
            str(row["sha256"]),
            int(row["size_bytes"]),
            int(row["compressed_size_bytes"]),
            str(row["file_mtime_utc"]),
            seen_at_utc,
            int(row["trace_inserted"]),
            int(row["source_inserted"]),
            int(row["is_duplicate_content"]),
        ),
    )


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest().upper()


def read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def gzip_bytes(content: bytes, level: int) -> bytes:
    return gzip.compress(content, compresslevel=level)


def upsert_trace(
    connection: sqlite3.Connection,
    *,
    stream_type: str,
    source_root: str,
    source_path: str,
    relative_source_path: str,
    machine: str,
    log_local_date: str,
    file_name: str,
    file_mtime_utc: str,
    file_bytes: bytes,
    compression_level: int,
    seen_at_utc: str,
) -> Dict[str, object]:
    payload_sha = sha256_hex(file_bytes)
    compressed = gzip_bytes(file_bytes, compression_level)
    compressed_size = len(compressed)

    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO traces (
            stream_type,
            sha256,
            machine,
            log_local_date,
            file_name,
            canonical_relative_path,
            size_bytes,
            compressed_size_bytes,
            compression_codec,
            file_mtime_utc,
            ingested_at_utc,
            content_gzip
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stream_type,
            payload_sha,
            machine,
            log_local_date,
            file_name,
            relative_source_path,
            len(file_bytes),
            compressed_size,
            "gzip",
            file_mtime_utc,
            seen_at_utc,
            compressed,
        ),
    )
    inserted_trace = cursor.rowcount == 1

    cursor.execute(
        "SELECT trace_id FROM traces WHERE stream_type = ? AND sha256 = ?",
        (stream_type, payload_sha),
    )
    trace_id = cursor.fetchone()[0]

    cursor.execute(
        """
        INSERT OR IGNORE INTO trace_sources (
            trace_id,
            stream_type,
            source_root,
            source_path,
            relative_path,
            seen_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            trace_id,
            stream_type,
            source_root,
            source_path,
            relative_source_path,
            seen_at_utc,
        ),
    )
    inserted_source = cursor.rowcount == 1

    cursor.execute(
        """
        SELECT compressed_size_bytes
        FROM traces
        WHERE trace_id = ?
        """,
        (trace_id,),
    )
    stored_compressed_size = int(cursor.fetchone()[0])
    cursor.close()

    return {
        "trace_id": trace_id,
        "stream_type": stream_type,
        "machine": machine,
        "log_local_date": log_local_date,
        "file_name": file_name,
        "source_path": source_path,
        "relative_path": relative_source_path,
        "sha256": payload_sha,
        "size_bytes": len(file_bytes),
        "compressed_size_bytes": stored_compressed_size,
        "compression_codec": "gzip",
        "file_mtime_utc": file_mtime_utc,
        "trace_inserted": 1 if inserted_trace else 0,
        "source_inserted": 1 if inserted_source else 0,
        "is_duplicate_content": 0 if inserted_trace else 1,
    }


def write_manifest_csv(path: str, rows: Sequence[Dict[str, object]]) -> None:
    ensure_parent(path)
    fieldnames = [
        "batch_id",
        "trace_id",
        "stream_type",
        "machine",
        "log_local_date",
        "file_name",
        "relative_path",
        "source_path",
        "sha256",
        "size_bytes",
        "compressed_size_bytes",
        "compression_codec",
        "file_mtime_utc",
        "trace_inserted",
        "source_inserted",
        "is_duplicate_content",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_manifest_jsonl(path: str, rows: Sequence[Dict[str, object]]) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def main() -> int:
    args = parse_args()
    source_root = normalize_root(args.source_root)
    database_path = os.path.abspath(args.database_path)

    if not os.path.exists(source_root):
        print(f"Source root not found: {source_root}", file=sys.stderr)
        return 2

    if not (0 <= args.compression_level <= 9):
        print("Compression level must be between 0 and 9", file=sys.stderr)
        return 2

    files = list(iter_files(source_root, args.recurse, args.stream))
    files = apply_sampling(files, args.max_files_per_machine, args.max_files)
    if not files:
        print("No matching trace files found.")
        return 0

    connection = connect_db(database_path)
    batch_started_at_utc = utc_now_iso()
    manifest_rows: List[Dict[str, object]] = []

    try:
        with connection:
            batch_id = create_batch(
                connection,
                started_at_utc=batch_started_at_utc,
                source_root=source_root,
                stream_filter=args.stream,
                recurse=args.recurse,
                max_files=args.max_files,
                max_files_per_machine=args.max_files_per_machine,
                compression_level=args.compression_level,
                batch_label=args.batch_label,
            )
            for stream_type, source_path in files:
                file_bytes = read_file_bytes(source_path)
                file_name = os.path.basename(source_path)
                stat = os.stat(source_path)
                file_mtime_utc = dt.datetime.fromtimestamp(
                    stat.st_mtime, tz=dt.timezone.utc
                ).isoformat()
                seen_at_utc = utc_now_iso()
                row = upsert_trace(
                    connection,
                    stream_type=stream_type,
                    source_root=source_root,
                    source_path=os.path.abspath(source_path),
                    relative_source_path=relative_path(source_path, source_root),
                    machine=machine_from_path(source_path),
                    log_local_date=log_date_from_path(source_path),
                    file_name=file_name,
                    file_mtime_utc=file_mtime_utc,
                    file_bytes=file_bytes,
                    compression_level=args.compression_level,
                    seen_at_utc=seen_at_utc,
                )
                row["batch_id"] = batch_id
                manifest_rows.append(row)
                record_ingest_event(
                    connection,
                    batch_id=batch_id,
                    row=row,
                    source_root=source_root,
                    seen_at_utc=seen_at_utc,
                )
                if args.verbose:
                    print(
                        f"{row['stream_type']}: {row['relative_path']} -> trace_id={row['trace_id']} duplicate={row['is_duplicate_content']}"
                    )
            finalize_batch(
                connection,
                batch_id=batch_id,
                completed_at_utc=utc_now_iso(),
                manifest_rows=manifest_rows,
            )
    finally:
        connection.close()

    if args.out_manifest_csv:
        write_manifest_csv(args.out_manifest_csv, manifest_rows)
    if args.out_manifest_jsonl:
        write_manifest_jsonl(args.out_manifest_jsonl, manifest_rows)

    trace_inserts = sum(int(row["trace_inserted"]) for row in manifest_rows)
    source_inserts = sum(int(row["source_inserted"]) for row in manifest_rows)
    print(f"Wrote raw trace store: {database_path}")
    print(f"Observed files        : {len(manifest_rows)}")
    print(f"New trace payloads    : {trace_inserts}")
    print(f"New source locations  : {source_inserts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
