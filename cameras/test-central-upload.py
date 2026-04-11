#!/usr/bin/env python3
"""End-to-end tests for staged replay upload into the central catalog."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import importlib.util


CAMERAS_DIR = Path(__file__).resolve().parent
STAGE_MODULE_PATH = CAMERAS_DIR / "stage-central-replay.py"
UPLOAD_MODULE_PATH = CAMERAS_DIR / "upload-central-replay.py"


def load_module(module_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STAGE_MODULE = load_module(STAGE_MODULE_PATH, "camera_stage_central_replay_upload_tests")
UPLOAD_MODULE = load_module(UPLOAD_MODULE_PATH, "camera_upload_central_replay_tests")


class CentralUploadTests(unittest.TestCase):
    def write_config(self, root: Path, runs_root: Path, staging_root: Path, upload_root: Path) -> tuple[Path, Path]:
        log_dir = root / "hamilton_logs"
        log_dir.mkdir()
        config_path = root / "camera-recorder.json"
        local_path = root / "camera-recorder.local.json"
        config_path.write_text(
            json.dumps(
                {
                    "hamilton": {"log_dir": str(log_dir), "log_glob": "*.trc", "process_name": "HxRun.exe"},
                    "storage": {"runs_root": str(runs_root), "manifest_dir": "", "recorder_log_dir": str(root / "logs")},
                    "central_ingest": {
                        "staging_root": str(staging_root),
                        "upload_root": str(upload_root),
                        "transport": "filesystem",
                    },
                    "recorder": {
                        "default_profile": "default",
                        "poll_sec": 1.0,
                        "max_record_sec": 0,
                        "startup_timeout_sec": 0,
                        "dshow_rtbufsize": "256M",
                        "ffmpeg_path": "",
                        "stop_file": str(root / "recorder.stop"),
                    },
                    "replay": {"host": "127.0.0.1", "port": 5050, "log_path": str(root / "replay.log")},
                    "live": {"default_profile": "default", "frame_timeout_sec": 8, "refresh_ms": 1000, "jpeg_quality": 4},
                    "daemon": {
                        "task_name": "HamiltonCameraRecorderDaemon",
                        "stop_file": str(root / "daemon.stop"),
                        "pid_file": str(root / "daemon.pid"),
                        "status_path": str(root / "daemon-status.json"),
                        "log_path": str(root / "daemon.log"),
                        "idle_poll_sec": 1.0,
                        "heartbeat_sec": 10.0,
                        "relaunch_delay_sec": 2.0,
                    },
                    "profiles": [{"id": "default", "label": "Top Cam", "source": "Arducam USB Camera"}],
                }
            ),
            encoding="utf-8",
        )
        return config_path, local_path

    def write_ready_run(self, root: Path) -> Path:
        runs_dir = root / "H7" / "2026-04-10"
        runs_dir.mkdir(parents=True)
        sample_trace = next((Path(__file__).resolve().parents[1] / "data" / "samples").glob("*.trc"))
        trace_path = runs_dir / "demo.trc"
        trace_path.write_bytes(sample_trace.read_bytes())
        video_path = runs_dir / "demo.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x20ftypisomdemo-video")
        manifest_path = runs_dir / "demo.run.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "label": "demo-ready",
                    "source": "Arducam USB Camera",
                    "video_path": str(video_path),
                    "video_filename": video_path.name,
                    "started_at_local": "2026-04-10T10:00:00",
                    "stopped_at_local": "2026-04-10T10:01:00",
                    "duration_sec": 60.0,
                    "stop_reason": "process_exit",
                    "process_gate": "HxRun.exe",
                    "hamilton_log_dir": str(trace_path.parent),
                    "hamilton_log_glob": "*.trc",
                    "trace_path": str(trace_path),
                    "trace_filename": trace_path.name,
                    "trace_mtime_delta_sec": 1.5,
                }
            ),
            encoding="utf-8",
        )
        return manifest_path

    def stage_one_ready_run(self, config_path: Path, local_path: Path, runs_root: Path, staging_root: Path) -> dict:
        self.write_ready_run(runs_root)
        return STAGE_MODULE.stage_runs(
            config_path=config_path,
            local_config_path=local_path,
            runs_root=runs_root,
            staging_root=staging_root,
            limit=0,
            restage=False,
        )

    def test_upload_acknowledges_staged_run_and_populates_central_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs_root = root / "runs"
            staging_root = root / "staging"
            upload_root = root / "central"
            config_path, local_path = self.write_config(root, runs_root, staging_root, upload_root)
            self.stage_one_ready_run(config_path, local_path, runs_root, staging_root)

            result = UPLOAD_MODULE.upload_staged_runs(
                config_path=config_path,
                local_config_path=local_path,
                staging_root=staging_root,
                upload_root=upload_root,
                limit=0,
                batch_id="",
            )

            self.assertEqual(result["uploaded_run_count"], 1)
            self.assertEqual(result["failed_run_count"], 0)
            item = result["items"][0]
            self.assertEqual(item["action"], "acknowledged")
            self.assertTrue(Path(item["ack_path"]).exists())

            staging_catalog = staging_root / STAGE_MODULE.CATALOG_FILENAME
            with closing(STAGE_MODULE.get_db_connection(staging_catalog)) as conn:
                row = conn.execute(
                    "SELECT upload_status, central_run_id, ack_path, upload_batch_id FROM staged_runs"
                ).fetchone()
            self.assertEqual(row["upload_status"], "acknowledged")
            self.assertTrue(row["central_run_id"])
            self.assertTrue(row["ack_path"])
            self.assertEqual(row["upload_batch_id"], result["ingest_batch_id"])

            central_catalog = upload_root / UPLOAD_MODULE.CENTRAL_CATALOG_FILENAME
            with closing(sqlite3.connect(central_catalog)) as conn:
                run_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
                batch_count = conn.execute("SELECT COUNT(*) FROM ingest_batches").fetchone()[0]
            self.assertEqual(run_count, 1)
            self.assertEqual(artifact_count, 3)
            self.assertEqual(batch_count, 1)

    def test_restaged_duplicate_reuses_existing_central_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs_root = root / "runs"
            staging_root = root / "staging"
            upload_root = root / "central"
            config_path, local_path = self.write_config(root, runs_root, staging_root, upload_root)
            self.stage_one_ready_run(config_path, local_path, runs_root, staging_root)
            first = UPLOAD_MODULE.upload_staged_runs(
                config_path=config_path,
                local_config_path=local_path,
                staging_root=staging_root,
                upload_root=upload_root,
                limit=0,
                batch_id="",
            )

            STAGE_MODULE.stage_runs(
                config_path=config_path,
                local_config_path=local_path,
                runs_root=runs_root,
                staging_root=staging_root,
                limit=0,
                restage=True,
            )
            second = UPLOAD_MODULE.upload_staged_runs(
                config_path=config_path,
                local_config_path=local_path,
                staging_root=staging_root,
                upload_root=upload_root,
                limit=0,
                batch_id="",
            )

            self.assertEqual(second["uploaded_run_count"], 1)
            self.assertEqual(second["failed_run_count"], 0)
            self.assertFalse(second["items"][0]["created"])
            self.assertEqual(first["items"][0]["central_run_id"], second["items"][0]["central_run_id"])

            central_catalog = upload_root / UPLOAD_MODULE.CENTRAL_CATALOG_FILENAME
            with closing(sqlite3.connect(central_catalog)) as conn:
                run_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            self.assertEqual(run_count, 1)
            self.assertEqual(artifact_count, 3)


if __name__ == "__main__":
    unittest.main()
