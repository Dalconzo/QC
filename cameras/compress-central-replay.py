#!/usr/bin/env python3
"""Conservatively compress cataloged Central Replay video artifacts."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from pathlib import Path
from typing import Callable


CATALOG_FILENAME = ".central_replay_catalog.sqlite3"
LEDGER_FILENAME = ".central_replay_compression.sqlite3"
ORIGINALS_DIRNAME = ".central_replay_originals"
COMPRESSION_KIND = "h264_crf"
STALE_ATTEMPT_HOURS = 24


def utc_now_text() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def init_ledger(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS compression_attempts (
            attempt_id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_size_bytes INTEGER NOT NULL,
            status TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            completed_at_utc TEXT NOT NULL DEFAULT '',
            output_sha256 TEXT NOT NULL DEFAULT '',
            output_size_bytes INTEGER NOT NULL DEFAULT 0,
            original_backup_path TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_compression_attempts_artifact
            ON compression_attempts(artifact_id, source_sha256, status);
        """
    )
    stale_before = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=STALE_ATTEMPT_HOURS)).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE compression_attempts
        SET status = 'interrupted', completed_at_utc = ?,
            message = 'Recovered stale running attempt'
        WHERE status = 'running' AND started_at_utc < ?
        """,
        (utc_now_text(), stale_before),
    )
    conn.commit()


def is_completed(conn: sqlite3.Connection, artifact_id: str, source_sha256: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM compression_attempts
        WHERE artifact_id = ? AND source_sha256 = ? AND status = 'completed'
        LIMIT 1
        """,
        (artifact_id, source_sha256),
    ).fetchone()
    return row is not None


def start_attempt(conn: sqlite3.Connection, candidate: dict) -> str:
    attempt_id = f"compression-{uuid.uuid4().hex}"
    conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        """
        SELECT status FROM compression_attempts
        WHERE artifact_id = ? AND source_sha256 = ?
          AND status IN ('running', 'completed')
        LIMIT 1
        """,
        (candidate["artifact_id"], candidate["content_sha256"]),
    ).fetchone()
    if existing is not None:
        conn.rollback()
        return ""
    conn.execute(
        """
        INSERT INTO compression_attempts (
            attempt_id, artifact_id, source_sha256, source_size_bytes,
            status, started_at_utc
        ) VALUES (?, ?, ?, ?, 'running', ?)
        """,
        (
            attempt_id,
            candidate["artifact_id"],
            candidate["content_sha256"],
            candidate["size_bytes"],
            utc_now_text(),
        ),
    )
    conn.commit()
    return attempt_id


def finish_attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    status: str,
    message: str = "",
    output_sha256: str = "",
    output_size_bytes: int = 0,
    backup_path: str = "",
) -> None:
    conn.execute(
        """
        UPDATE compression_attempts
        SET status = ?, completed_at_utc = ?, output_sha256 = ?,
            output_size_bytes = ?, original_backup_path = ?, message = ?
        WHERE attempt_id = ?
        """,
        (status, utc_now_text(), output_sha256, output_size_bytes, backup_path, message, attempt_id),
    )
    conn.commit()


def resolve_managed_path(upload_root: Path, storage_relpath: str) -> Path:
    root = upload_root.resolve()
    path = (root / Path(storage_relpath)).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Artifact path escapes upload root: {storage_relpath}")
    return path


def list_candidates(
    catalog_conn: sqlite3.Connection,
    upload_root: Path,
    *,
    max_files: int,
    min_age_hours: float,
    min_size_bytes: int,
    max_size_bytes: int,
) -> list[dict]:
    rows = catalog_conn.execute(
        """
        SELECT artifact_id, central_run_id, storage_relpath, content_sha256,
               size_bytes, compression_kind, stored_at_utc
        FROM artifacts
        WHERE artifact_type = 'video_mp4'
          AND is_ready = 1
          AND compression_kind = 'none'
        ORDER BY stored_at_utc ASC, artifact_id ASC
        """
    ).fetchall()
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - (min_age_hours * 3600.0)
    candidates: list[dict] = []
    for row in rows:
        item = dict(row)
        try:
            path = resolve_managed_path(upload_root, item["storage_relpath"])
        except ValueError as exc:
            item.update(path="", selection_error=str(exc))
            candidates.append(item)
            if len(candidates) >= max_files:
                break
            continue
        item["path"] = str(path)
        item["selection_error"] = ""
        if not path.is_file():
            item["selection_error"] = "Cataloged video file is missing"
        else:
            actual_size = path.stat().st_size
            item["actual_size_bytes"] = actual_size
            if actual_size < min_size_bytes:
                continue
            if max_size_bytes > 0 and actual_size > max_size_bytes:
                continue
            if path.stat().st_mtime > cutoff:
                continue
        candidates.append(item)
        if len(candidates) >= max_files:
            break
    return candidates


def build_ffprobe_command(ffprobe_bin: str, path: Path) -> list[str]:
    return [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,pix_fmt,duration",
        "-of",
        "json",
        str(path),
    ]


def probe_video(
    ffprobe_bin: str,
    path: Path,
    *,
    runner: Callable = subprocess.run,
) -> dict:
    completed = runner(
        build_ffprobe_command(ffprobe_bin, path),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "ffprobe failed").strip()
        return {"valid": False, "message": message, "duration_sec": 0.0, "video_stream": {}}
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {"valid": False, "message": f"Invalid ffprobe JSON: {exc}", "duration_sec": 0.0, "video_stream": {}}
    streams = [stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"]
    if not streams:
        return {"valid": False, "message": "No video stream", "duration_sec": 0.0, "video_stream": {}}
    stream = streams[0]
    raw_duration = (payload.get("format") or {}).get("duration") or stream.get("duration") or 0
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        return {"valid": False, "message": "Video duration is missing or non-positive", "duration_sec": duration, "video_stream": stream}
    return {"valid": True, "message": "", "duration_sec": duration, "video_stream": stream}


def build_ffmpeg_command(
    ffmpeg_bin: str,
    source: Path,
    temporary_output: Path,
    *,
    crf: int,
    preset: str,
) -> list[str]:
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(temporary_output),
    ]


def validate_transcode(source_probe: dict, output_probe: dict) -> str:
    if not output_probe.get("valid"):
        return str(output_probe.get("message") or "Compressed output is not playable")
    stream = output_probe.get("video_stream") or {}
    if stream.get("codec_name") != "h264":
        return f"Compressed output codec is {stream.get('codec_name')!r}, expected 'h264'"
    if stream.get("pix_fmt") != "yuv420p":
        return f"Compressed output pixel format is {stream.get('pix_fmt')!r}, expected 'yuv420p'"
    source_duration = float(source_probe["duration_sec"])
    output_duration = float(output_probe["duration_sec"])
    tolerance = max(2.0, source_duration * 0.02)
    if abs(source_duration - output_duration) > tolerance:
        return f"Duration mismatch: source={source_duration:.3f}s output={output_duration:.3f}s tolerance={tolerance:.3f}s"
    return ""


def preserve_original(source: Path, backup: Path, *, expected_sha256: str) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        if compute_sha256(backup) == expected_sha256:
            return
        raise FileExistsError(f"Original backup exists with unexpected content: {backup}")
    try:
        os.link(source, backup)
    except OSError:
        shutil.copy2(source, backup)


def restore_original(source: Path, backup: Path) -> None:
    restore_path = source.with_name(f".{source.name}.restore-{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(backup, restore_path)
        os.replace(restore_path, source)
    finally:
        restore_path.unlink(missing_ok=True)


def catalog_record_compression(
    conn: sqlite3.Connection,
    candidate: dict,
    *,
    output_sha256: str,
    output_size_bytes: int,
) -> None:
    cursor = conn.execute(
        """
        UPDATE artifacts
        SET compression_kind = ?, content_sha256 = ?, size_bytes = ?, stored_at_utc = ?
        WHERE artifact_id = ? AND content_sha256 = ? AND compression_kind = 'none'
        """,
        (
            COMPRESSION_KIND,
            output_sha256,
            output_size_bytes,
            utc_now_text(),
            candidate["artifact_id"],
            candidate["content_sha256"],
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Catalog artifact changed while compression was running")
    conn.commit()


def catalog_restore_original(conn: sqlite3.Connection, candidate: dict) -> None:
    conn.execute(
        """
        UPDATE artifacts
        SET compression_kind = 'none', content_sha256 = ?, size_bytes = ?, stored_at_utc = ?
        WHERE artifact_id = ?
        """,
        (
            candidate["content_sha256"],
            candidate["size_bytes"],
            candidate["stored_at_utc"],
            candidate["artifact_id"],
        ),
    )
    conn.commit()


def process_candidate(
    candidate: dict,
    *,
    upload_root: Path,
    catalog_conn: sqlite3.Connection,
    ledger_conn: sqlite3.Connection,
    execute: bool,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    crf: int,
    preset: str,
    runner: Callable = subprocess.run,
) -> dict:
    result = {
        "artifact_id": candidate["artifact_id"],
        "central_run_id": candidate["central_run_id"],
        "path": candidate.get("path", ""),
        "status": "",
        "message": "",
        "source_size_bytes": int(candidate.get("actual_size_bytes") or candidate.get("size_bytes") or 0),
        "output_size_bytes": 0,
        "bytes_saved": 0,
        "original_backup_path": "",
    }
    if candidate.get("selection_error"):
        result.update(status="skipped_invalid", message=candidate["selection_error"])
        return result
    source = Path(candidate["path"])
    try:
        actual_source_sha256 = compute_sha256(source)
    except OSError as exc:
        result.update(status="skipped_invalid", message=f"Input could not be read: {exc}")
        return result
    if actual_source_sha256 != candidate["content_sha256"]:
        result.update(
            status="skipped_invalid",
            message=(
                "Input SHA-256 does not match the central catalog: "
                f"catalog={candidate['content_sha256']} actual={actual_source_sha256}"
            ),
        )
        return result
    try:
        source_probe = probe_video(ffprobe_bin, source, runner=runner)
    except OSError as exc:
        result.update(status="skipped_invalid", message=f"Input probe could not run: {exc}")
        return result
    if not source_probe["valid"]:
        result.update(status="skipped_invalid", message=f"Input is not playable: {source_probe['message']}")
        return result
    if is_completed(ledger_conn, candidate["artifact_id"], candidate["content_sha256"]):
        result.update(status="skipped_completed", message="Ledger already records this source fingerprint as completed")
        return result
    if not execute:
        result.update(status="dry_run", message="Eligible and playable; no files changed")
        return result

    attempt_id = start_attempt(ledger_conn, candidate)
    if not attempt_id:
        result.update(status="skipped_in_progress", message="This source fingerprint is already running or completed")
        return result
    temp_path = source.with_name(f".{source.stem}.compress-{uuid.uuid4().hex}.tmp.mp4")
    backup = upload_root / ORIGINALS_DIRNAME / candidate["artifact_id"] / source.name
    replaced = False
    catalog_updated = False
    try:
        command = build_ffmpeg_command(ffmpeg_bin, source, temp_path, crf=crf, preset=preset)
        completed = runner(command, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "ffmpeg failed").strip())
        if not temp_path.is_file() or temp_path.stat().st_size <= 0:
            raise RuntimeError("ffmpeg did not produce a non-empty output file")
        output_probe = probe_video(ffprobe_bin, temp_path, runner=runner)
        validation_error = validate_transcode(source_probe, output_probe)
        if validation_error:
            raise RuntimeError(validation_error)
        output_sha256 = compute_sha256(temp_path)
        output_size = temp_path.stat().st_size
        if output_size >= result["source_size_bytes"]:
            finish_attempt(
                ledger_conn,
                attempt_id,
                status="skipped",
                message="Validated output was not smaller than the source",
                output_sha256=output_sha256,
                output_size_bytes=output_size,
            )
            result.update(
                status="skipped_not_smaller",
                message="Validated output was not smaller than the source; original left unchanged",
                output_size_bytes=output_size,
            )
            return result
        preserve_original(source, backup, expected_sha256=candidate["content_sha256"])
        os.replace(temp_path, source)
        replaced = True
        try:
            catalog_record_compression(
                catalog_conn,
                candidate,
                output_sha256=output_sha256,
                output_size_bytes=output_size,
            )
            catalog_updated = True
        except Exception:
            catalog_conn.rollback()
            restore_original(source, backup)
            replaced = False
            raise
        finish_attempt(
            ledger_conn,
            attempt_id,
            status="completed",
            output_sha256=output_sha256,
            output_size_bytes=output_size,
            backup_path=str(backup.resolve()),
        )
        result.update(
            status="compressed",
            message="Validated output atomically replaced the cataloged video; original preserved",
            output_size_bytes=output_size,
            bytes_saved=max(0, result["source_size_bytes"] - output_size),
            original_backup_path=str(backup.resolve()),
        )
        return result
    except Exception as exc:
        if replaced:
            try:
                restore_original(source, backup)
            except Exception as restore_exc:
                exc = RuntimeError(f"{exc}; original restoration also failed: {restore_exc}")
        if catalog_updated:
            try:
                catalog_restore_original(catalog_conn, candidate)
            except Exception as catalog_exc:
                exc = RuntimeError(f"{exc}; catalog restoration also failed: {catalog_exc}")
        finish_attempt(
            ledger_conn,
            attempt_id,
            status="failed",
            message=str(exc),
            backup_path=str(backup.resolve()) if backup.exists() else "",
        )
        result.update(status="failed", message=str(exc), original_backup_path=str(backup.resolve()) if backup.exists() else "")
        return result
    finally:
        temp_path.unlink(missing_ok=True)


def compress_central_replay(
    upload_root: Path,
    *,
    execute: bool = False,
    catalog_path: Path | None = None,
    ledger_path: Path | None = None,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    max_files: int = 10,
    min_age_hours: float = 24.0,
    min_size_mb: float = 100.0,
    max_size_mb: float = 0.0,
    crf: int = 30,
    preset: str = "veryfast",
    runner: Callable = subprocess.run,
) -> dict:
    upload_root = upload_root.resolve()
    catalog_path = (catalog_path or upload_root / CATALOG_FILENAME).resolve()
    ledger_path = (ledger_path or upload_root / LEDGER_FILENAME).resolve()
    if max_files < 1:
        raise ValueError("max_files must be at least 1")
    if min_age_hours < 0 or min_size_mb < 0 or max_size_mb < 0:
        raise ValueError("age and size bounds cannot be negative")
    if max_size_mb and max_size_mb < min_size_mb:
        raise ValueError("max_size_mb cannot be less than min_size_mb")
    if not 0 <= crf <= 51:
        raise ValueError("crf must be between 0 and 51")
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Central replay catalog not found: {catalog_path}")

    # Dry-run must not create or mutate even the ledger file.
    effective_ledger_path: str | Path = ledger_path if execute else ":memory:"
    with closing(sqlite3.connect(catalog_path)) as catalog_conn, closing(sqlite3.connect(effective_ledger_path)) as ledger_conn:
        catalog_conn.row_factory = sqlite3.Row
        init_ledger(ledger_conn)
        candidates = list_candidates(
            catalog_conn,
            upload_root,
            max_files=max_files,
            min_age_hours=min_age_hours,
            min_size_bytes=int(min_size_mb * 1024 * 1024),
            max_size_bytes=int(max_size_mb * 1024 * 1024),
        )
        items = [
            process_candidate(
                candidate,
                upload_root=upload_root,
                catalog_conn=catalog_conn,
                ledger_conn=ledger_conn,
                execute=execute,
                ffmpeg_bin=ffmpeg_bin,
                ffprobe_bin=ffprobe_bin,
                crf=crf,
                preset=preset,
                runner=runner,
            )
            for candidate in candidates
        ]
    return {
        "mode": "execute" if execute else "dry_run",
        "upload_root": str(upload_root),
        "catalog_path": str(catalog_path),
        "ledger_path": str(ledger_path),
        "selected_count": len(candidates),
        "compressed_count": sum(item["status"] == "compressed" for item in items),
        "failed_count": sum(item["status"] == "failed" for item in items),
        "skipped_count": sum(item["status"].startswith("skipped_") for item in items),
        "total_bytes_saved": sum(item["bytes_saved"] for item in items),
        "items": items,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload-root", required=True, help="Central Replay upload/storage root")
    parser.add_argument("--catalog-path", default="", help="Override central catalog path")
    parser.add_argument("--ledger-path", default="", help="Override resumable compression ledger path")
    parser.add_argument("--execute", action="store_true", help="Perform compression; without this flag the utility is read-only")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable or absolute path")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable or absolute path")
    parser.add_argument("--max-files", type=int, default=10, help="Maximum catalog artifacts selected per invocation")
    parser.add_argument("--min-age-hours", type=float, default=24.0, help="Only select files at least this old")
    parser.add_argument("--min-size-mb", type=float, default=100.0, help="Only select files at least this large")
    parser.add_argument("--max-size-mb", type=float, default=0.0, help="Maximum selected size; zero means unlimited")
    parser.add_argument("--crf", type=int, default=30, help="libx264 CRF (0-51)")
    parser.add_argument("--preset", default="veryfast", help="libx264 encoding preset")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = compress_central_replay(
            Path(args.upload_root),
            execute=args.execute,
            catalog_path=Path(args.catalog_path) if args.catalog_path else None,
            ledger_path=Path(args.ledger_path) if args.ledger_path else None,
            ffmpeg_bin=args.ffmpeg,
            ffprobe_bin=args.ffprobe,
            max_files=args.max_files,
            min_age_hours=args.min_age_hours,
            min_size_mb=args.min_size_mb,
            max_size_mb=args.max_size_mb,
            crf=args.crf,
            preset=args.preset,
        )
    except Exception as exc:
        print(json.dumps({"mode": "execute" if args.execute else "dry_run", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(payload, indent=2))
    return 2 if payload["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
