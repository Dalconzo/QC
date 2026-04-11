#!/usr/bin/env python3
"""Smoke tests for local central replay staging."""
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
MODULE_PATH = CAMERAS_DIR / "stage-central-replay.py"


def load_module(module_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_module(MODULE_PATH, "camera_stage_central_replay")


class CentralStagingTests(unittest.TestCase):
    def write_config(self, root: Path, runs_root: Path, staging_root: Path) -> tuple[Path, Path]:
        log_dir = root / "hamilton_logs"
        log_dir.mkdir()
        config_path = root / "camera-recorder.json"
        local_path = root / "camera-recorder.local.json"
        config_path.write_text(
            json.dumps(
                {
                    "hamilton": {"log_dir": str(log_dir), "log_glob": "*.trc", "process_name": "HxRun.exe"},
                    "storage": {"runs_root": str(runs_root), "manifest_dir": "", "recorder_log_dir": str(root / "logs")},
                    "central_ingest": {"staging_root": str(staging_root)},
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

    def write_ready_run(self, root: Path) -> tuple[Path, Path, Path]:
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
        return manifest_path, video_path, trace_path

    def test_stage_ready_run_creates_batch_payload_and_db_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs_root = root / "runs"
            staging_root = root / "staging"
            config_path, local_path = self.write_config(root, runs_root, staging_root)
            manifest_path, video_path, trace_path = self.write_ready_run(runs_root)

            result = MODULE.stage_runs(
                config_path=config_path,
                local_config_path=local_path,
                runs_root=runs_root,
                staging_root=staging_root,
                limit=0,
                restage=False,
            )

            self.assertEqual(result["staged_run_count"], 1)
            self.assertEqual(result["skipped_run_count"], 0)
            batch_dir = Path(result["batch_dir"])
            payload_path = batch_dir / "runs" / MODULE.INSPECT_MODULE.describe_manifest(manifest_path)["run_id"] / "run-upload.json"
            self.assertTrue(payload_path.exists())
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["run"]["label"], "demo-ready")
            self.assertEqual(len(payload["artifacts"]), 3)
            self.assertTrue((payload_path.parent / "video.mp4").exists())
            self.assertEqual((payload_path.parent / "video.mp4").read_bytes(), video_path.read_bytes())
            self.assertEqual((payload_path.parent / "trace.trc").read_bytes(), trace_path.read_bytes())

            catalog_path = Path(result["catalog_path"])
            with closing(sqlite3.connect(catalog_path)) as conn:
                run_count = conn.execute("SELECT COUNT(*) FROM staged_runs").fetchone()[0]
                artifact_count = conn.execute("SELECT COUNT(*) FROM staged_artifacts").fetchone()[0]
            self.assertEqual(run_count, 1)
            self.assertEqual(artifact_count, 3)

    def test_duplicate_run_is_skipped_without_restage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs_root = root / "runs"
            staging_root = root / "staging"
            config_path, local_path = self.write_config(root, runs_root, staging_root)
            self.write_ready_run(runs_root)

            first = MODULE.stage_runs(
                config_path=config_path,
                local_config_path=local_path,
                runs_root=runs_root,
                staging_root=staging_root,
                limit=0,
                restage=False,
            )
            second = MODULE.stage_runs(
                config_path=config_path,
                local_config_path=local_path,
                runs_root=runs_root,
                staging_root=staging_root,
                limit=0,
                restage=False,
            )

            self.assertEqual(first["staged_run_count"], 1)
            self.assertEqual(second["staged_run_count"], 0)
            self.assertEqual(second["skipped_run_count"], 1)

    def test_non_ready_run_is_not_staged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs_root = root / "runs"
            staging_root = root / "staging"
            config_path, local_path = self.write_config(root, runs_root, staging_root)
            manifest_path, _video_path, trace_path = self.write_ready_run(runs_root)
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_payload["video_path"] = str(runs_root / "missing.mp4")
            manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

            result = MODULE.stage_runs(
                config_path=config_path,
                local_config_path=local_path,
                runs_root=runs_root,
                staging_root=staging_root,
                limit=0,
                restage=False,
            )

            self.assertEqual(result["staged_run_count"], 0)
            self.assertEqual(result["skipped_run_count"], 1)
            self.assertFalse(any((Path(result["batch_dir"]) / "runs").glob("*/run-upload.json")))
            self.assertTrue(trace_path.exists())


if __name__ == "__main__":
    unittest.main()
