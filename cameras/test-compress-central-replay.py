#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from subprocess import CompletedProcess


MODULE_PATH = Path(__file__).with_name("compress-central-replay.py")
SPEC = importlib.util.spec_from_file_location("central_replay_compression", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeMediaRunner:
    def __init__(self, *, input_valid: bool = True, output_valid: bool = True, ffmpeg_ok: bool = True):
        self.input_valid = input_valid
        self.output_valid = output_valid
        self.ffmpeg_ok = ffmpeg_ok
        self.commands: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        command = [str(item) for item in command]
        self.commands.append(command)
        if "-show_entries" in command:
            path = Path(command[-1])
            is_output = ".compress-" in path.name
            valid = self.output_valid if is_output else self.input_valid
            if not valid:
                return CompletedProcess(command, 1, "", "moov atom not found")
            stream = {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264" if is_output else "mpeg4",
                "pix_fmt": "yuv420p",
                "duration": "120.0",
            }
            return CompletedProcess(command, 0, json.dumps({"format": {"duration": "120.0"}, "streams": [stream]}), "")
        if not self.ffmpeg_ok:
            return CompletedProcess(command, 1, "", "encoder failed")
        Path(command[-1]).write_bytes(b"compressed-video")
        return CompletedProcess(command, 0, "", "")


class CompressionTests(unittest.TestCase):
    def make_fixture(self, root: Path, *, count: int = 1, source_size: int = 4096) -> tuple[Path, list[dict]]:
        upload_root = root / "central"
        upload_root.mkdir()
        catalog_path = upload_root / MODULE.CATALOG_FILENAME
        rows = []
        with closing(sqlite3.connect(catalog_path)) as conn:
            conn.execute(
                """
                CREATE TABLE artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    central_run_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    storage_relpath TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    compression_kind TEXT NOT NULL,
                    stored_at_utc TEXT NOT NULL,
                    is_ready INTEGER NOT NULL
                )
                """
            )
            for index in range(count):
                data = bytes([65 + index]) * source_size
                relpath = f"storage/runs/2026/01/ws/run-{index}/video.mp4"
                path = upload_root / relpath
                path.parent.mkdir(parents=True)
                path.write_bytes(data)
                old_timestamp = 1_700_000_000 - index
                os.utime(path, (old_timestamp, old_timestamp))
                row = {
                    "artifact_id": f"artifact-{index}",
                    "central_run_id": f"run-{index}",
                    "storage_relpath": relpath,
                    "content_sha256": sha256(data),
                    "size_bytes": len(data),
                    "compression_kind": "none",
                    "stored_at_utc": "2026-01-01T00:00:00+00:00",
                }
                rows.append({**row, "path": path, "data": data})
                conn.execute(
                    """
                    INSERT INTO artifacts VALUES (?, ?, 'video_mp4', ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        row["artifact_id"], row["central_run_id"], relpath,
                        row["content_sha256"], row["size_bytes"],
                        row["compression_kind"], row["stored_at_utc"],
                    ),
                )
            conn.commit()
        return upload_root, rows

    def test_default_dry_run_does_not_change_files_catalog_or_create_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, rows = self.make_fixture(Path(tmpdir))
            runner = FakeMediaRunner()
            payload = MODULE.compress_central_replay(
                upload_root,
                min_age_hours=0,
                min_size_mb=0,
                runner=runner,
            )
            self.assertEqual(payload["mode"], "dry_run")
            self.assertEqual(payload["items"][0]["status"], "dry_run")
            self.assertEqual(rows[0]["path"].read_bytes(), rows[0]["data"])
            self.assertFalse((upload_root / MODULE.LEDGER_FILENAME).exists())
            with closing(sqlite3.connect(upload_root / MODULE.CATALOG_FILENAME)) as conn:
                self.assertEqual(conn.execute("SELECT compression_kind FROM artifacts").fetchone()[0], "none")
            self.assertFalse(any("-c:v" in command for command in runner.commands))

    def test_invalid_input_is_skipped_with_json_safe_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, rows = self.make_fixture(Path(tmpdir))
            payload = MODULE.compress_central_replay(
                upload_root,
                execute=True,
                min_age_hours=0,
                min_size_mb=0,
                runner=FakeMediaRunner(input_valid=False),
            )
            self.assertEqual(payload["items"][0]["status"], "skipped_invalid")
            self.assertIn("moov atom", payload["items"][0]["message"])
            self.assertEqual(rows[0]["path"].read_bytes(), rows[0]["data"])
            json.dumps(payload)

    def test_catalog_hash_mismatch_is_skipped_before_probe_or_transcode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, rows = self.make_fixture(Path(tmpdir))
            rows[0]["path"].write_bytes(b"changed-after-cataloging")
            runner = FakeMediaRunner()
            payload = MODULE.compress_central_replay(
                upload_root,
                execute=True,
                min_age_hours=0,
                min_size_mb=0,
                runner=runner,
            )
            self.assertEqual(payload["items"][0]["status"], "skipped_invalid")
            self.assertIn("SHA-256", payload["items"][0]["message"])
            self.assertEqual(runner.commands, [])

    def test_execute_validates_preserves_and_atomically_replaces_cataloged_video(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, rows = self.make_fixture(Path(tmpdir))
            runner = FakeMediaRunner()
            payload = MODULE.compress_central_replay(
                upload_root,
                execute=True,
                min_age_hours=0,
                min_size_mb=0,
                crf=31,
                preset="slow",
                runner=runner,
            )
            item = payload["items"][0]
            self.assertEqual(item["status"], "compressed")
            self.assertEqual(rows[0]["path"].read_bytes(), b"compressed-video")
            backup = Path(item["original_backup_path"])
            self.assertEqual(backup.read_bytes(), rows[0]["data"])
            ffmpeg_command = next(command for command in runner.commands if "-c:v" in command)
            self.assertIn("libx264", ffmpeg_command)
            self.assertIn("yuv420p", ffmpeg_command)
            self.assertIn("+faststart", ffmpeg_command)
            self.assertEqual(ffmpeg_command[ffmpeg_command.index("-crf") + 1], "31")
            self.assertEqual(ffmpeg_command[ffmpeg_command.index("-preset") + 1], "slow")
            with closing(sqlite3.connect(upload_root / MODULE.CATALOG_FILENAME)) as conn:
                row = conn.execute("SELECT compression_kind, content_sha256, size_bytes FROM artifacts").fetchone()
            self.assertEqual(row, (MODULE.COMPRESSION_KIND, sha256(b"compressed-video"), len(b"compressed-video")))
            with closing(sqlite3.connect(upload_root / MODULE.LEDGER_FILENAME)) as conn:
                self.assertEqual(conn.execute("SELECT status FROM compression_attempts").fetchone()[0], "completed")

    def test_bad_output_never_replaces_source_or_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, rows = self.make_fixture(Path(tmpdir))
            payload = MODULE.compress_central_replay(
                upload_root,
                execute=True,
                min_age_hours=0,
                min_size_mb=0,
                runner=FakeMediaRunner(output_valid=False),
            )
            self.assertEqual(payload["items"][0]["status"], "failed")
            self.assertEqual(rows[0]["path"].read_bytes(), rows[0]["data"])
            self.assertFalse((upload_root / MODULE.ORIGINALS_DIRNAME).exists())
            self.assertFalse(any(rows[0]["path"].parent.glob("*.tmp.mp4")))

    def test_selection_is_bounded_by_file_count_age_and_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, _rows = self.make_fixture(Path(tmpdir), count=3, source_size=2048)
            with closing(sqlite3.connect(upload_root / MODULE.CATALOG_FILENAME)) as conn:
                conn.row_factory = sqlite3.Row
                selected = MODULE.list_candidates(
                    conn,
                    upload_root,
                    max_files=2,
                    min_age_hours=1,
                    min_size_bytes=1024,
                    max_size_bytes=4096,
                )
            self.assertEqual(len(selected), 2)
            with closing(sqlite3.connect(upload_root / MODULE.CATALOG_FILENAME)) as conn:
                conn.row_factory = sqlite3.Row
                self.assertEqual(
                    MODULE.list_candidates(conn, upload_root, max_files=10, min_age_hours=1, min_size_bytes=4096, max_size_bytes=0),
                    [],
                )

    def test_completed_fingerprint_is_resumably_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, rows = self.make_fixture(Path(tmpdir))
            catalog_conn = sqlite3.connect(upload_root / MODULE.CATALOG_FILENAME)
            catalog_conn.row_factory = sqlite3.Row
            ledger_conn = sqlite3.connect(upload_root / MODULE.LEDGER_FILENAME)
            try:
                MODULE.init_ledger(ledger_conn)
                candidate = dict(catalog_conn.execute("SELECT * FROM artifacts").fetchone())
                candidate.update(path=str(rows[0]["path"]), actual_size_bytes=len(rows[0]["data"]), selection_error="")
                attempt = MODULE.start_attempt(ledger_conn, candidate)
                MODULE.finish_attempt(ledger_conn, attempt, status="completed")
                result = MODULE.process_candidate(
                    candidate,
                    upload_root=upload_root,
                    catalog_conn=catalog_conn,
                    ledger_conn=ledger_conn,
                    execute=True,
                    ffmpeg_bin="ffmpeg",
                    ffprobe_bin="ffprobe",
                    crf=30,
                    preset="veryfast",
                    runner=FakeMediaRunner(),
                )
                self.assertEqual(result["status"], "skipped_completed")
                self.assertEqual(rows[0]["path"].read_bytes(), rows[0]["data"])
            finally:
                ledger_conn.close()
                catalog_conn.close()

    def test_running_fingerprint_cannot_be_claimed_twice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, _rows = self.make_fixture(Path(tmpdir))
            with closing(sqlite3.connect(upload_root / MODULE.CATALOG_FILENAME)) as catalog_conn, closing(
                sqlite3.connect(upload_root / MODULE.LEDGER_FILENAME)
            ) as ledger_conn:
                catalog_conn.row_factory = sqlite3.Row
                MODULE.init_ledger(ledger_conn)
                candidate = dict(catalog_conn.execute("SELECT * FROM artifacts").fetchone())
                first_attempt = MODULE.start_attempt(ledger_conn, candidate)
                second_attempt = MODULE.start_attempt(ledger_conn, candidate)
                self.assertTrue(first_attempt)
                self.assertEqual(second_attempt, "")


if __name__ == "__main__":
    unittest.main()
