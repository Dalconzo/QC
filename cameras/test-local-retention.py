#!/usr/bin/env python3
"""Regression tests for workstation-local retention metadata and cleanup."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


CAMERAS_DIR = Path(__file__).resolve().parent
if str(CAMERAS_DIR) not in sys.path:
    sys.path.insert(0, str(CAMERAS_DIR))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RETENTION = load_module("camera_local_retention_module", CAMERAS_DIR / "local_retention.py")


class LocalRetentionTests(unittest.TestCase):
    def write_manifest(self, root: Path, *, stopped_at: str) -> Path:
        video_path = root / "run.mp4"
        video_path.write_bytes(b"x" * 4096)
        trace_path = root / "run.trc"
        trace_path.write_text("trace", encoding="utf-8")
        manifest_path = root / "run.run.json"
        payload = {
            "label": "demo",
            "source": "camera0",
            "video_path": str(video_path.resolve()),
            "video_filename": video_path.name,
            "started_at_local": "2026-04-10T10:00:00",
            "stopped_at_local": stopped_at,
            "duration_sec": 60.0,
            "stop_reason": "process_exit",
            "process_gate": "HxRun.exe",
            "hamilton_log_dir": str(root.resolve()),
            "hamilton_log_glob": "*.trc",
            "trace_path": str(trace_path.resolve()),
            "trace_filename": trace_path.name,
            "trace_mtime_delta_sec": 1.5,
            "local_compaction": {
                "status": "succeeded",
                "artifacts_root": "",
                "generated_at_local": "2026-04-10T10:02:00",
                "failure": "",
                "source_video_path": str(video_path.resolve()),
                "source_video_size_bytes": 4096,
                "total_derived_size_bytes": 1024,
                "segment_derivatives": [],
            },
        }
        initialized = RETENTION.initialize_local_retention(
            payload,
            enabled=True,
            retention_days=7,
            require_upload_ack=True,
            require_local_compaction=False,
        )
        manifest_path.write_text(json.dumps(initialized, indent=2) + "\n", encoding="utf-8")
        return manifest_path

    def test_record_upload_ack_sets_lan_availability_and_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = self.write_manifest(Path(tmpdir), stopped_at="2026-04-10T10:01:00")
            payload = RETENTION.record_upload_ack(
                manifest_path,
                central_run_id="central-run-1",
                acknowledged_at_utc="2026-04-11T00:00:00Z",
                ack_path=str(Path(tmpdir) / "run-ack.json"),
            )

            retention = payload["local_retention"]
            self.assertEqual(retention["upload_status"], "acknowledged")
            self.assertTrue(retention["lan_available"])
            self.assertEqual(retention["central_run_id"], "central-run-1")
            self.assertEqual(retention["retain_until_local"], "2026-04-17T10:01:00")
            self.assertEqual(retention["original_delete_eligible_at_local"], "2026-04-17T10:01:00")

    def test_cleanup_deletes_only_after_upload_and_retention_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = self.write_manifest(root, stopped_at="2026-04-10T10:01:00")
            RETENTION.record_upload_ack(
                manifest_path,
                central_run_id="central-run-1",
                acknowledged_at_utc="2026-04-11T00:00:00Z",
                ack_path=str(root / "run-ack.json"),
            )

            result = RETENTION.cleanup_one_manifest(
                manifest_path,
                now_local=dt.datetime(2026, 4, 18, 12, 0, 0),
                delete=True,
            )

            self.assertEqual(result["action"], "deleted")
            self.assertFalse((root / "run.mp4").exists())
            payload = RETENTION.load_manifest(manifest_path)
            self.assertTrue(payload["local_retention"]["original_deleted_at_local"])

    def test_cleanup_blocks_when_upload_ack_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = self.write_manifest(root, stopped_at="2026-04-10T10:01:00")

            result = RETENTION.cleanup_one_manifest(
                manifest_path,
                now_local=dt.datetime(2026, 4, 18, 12, 0, 0),
                delete=True,
            )

            self.assertEqual(result["action"], "blocked")
            self.assertEqual(result["reason"], "upload_not_acknowledged")
            self.assertTrue((root / "run.mp4").exists())

    def test_emergency_cleanup_deletes_uploaded_original_before_age_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = self.write_manifest(root, stopped_at="2026-04-10T10:01:00")
            RETENTION.record_upload_ack(
                manifest_path,
                central_run_id="central-run-1",
                acknowledged_at_utc="2026-04-10T12:00:00Z",
                ack_path=str(root / "run-ack.json"),
            )

            def fake_disk_usage(_path: Path):
                free_value = 35 * (1024**3) if not (root / "run.mp4").exists() else 4 * (1024**3)
                return shutil._ntuple_diskusage(100 * (1024**3), 100 * (1024**3) - free_value, free_value)

            payload = RETENTION.cleanup_runs(
                runs_root=root,
                now_local=dt.datetime(2026, 4, 11, 9, 0, 0),
                delete=True,
                emergency_config={
                    "enabled": True,
                    "min_free_gb": 20,
                    "target_free_gb": 30,
                    "block_new_recording_free_gb": 8,
                },
                disk_usage_fn=fake_disk_usage,
            )

            self.assertEqual(payload["emergency_deleted_run_count"], 1)
            self.assertTrue(payload["emergency_active"])
            self.assertFalse(payload["critical_pressure_remaining"])
            updated = RETENTION.load_manifest(manifest_path)
            self.assertEqual(updated["local_retention"]["last_cleanup_mode"], "emergency")
            self.assertFalse((root / "run.mp4").exists())

    def test_emergency_cleanup_never_deletes_without_upload_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_manifest(root, stopped_at="2026-04-10T10:01:00")

            def fake_disk_usage(_path: Path):
                return shutil._ntuple_diskusage(100 * (1024**3), 96 * (1024**3), 4 * (1024**3))

            payload = RETENTION.cleanup_runs(
                runs_root=root,
                now_local=dt.datetime(2026, 4, 11, 9, 0, 0),
                delete=True,
                emergency_config={
                    "enabled": True,
                    "min_free_gb": 20,
                    "target_free_gb": 30,
                    "block_new_recording_free_gb": 8,
                },
                run_normal_cleanup=False,
                disk_usage_fn=fake_disk_usage,
            )

            self.assertEqual(payload["deleted_run_count"], 0)
            self.assertTrue(payload["critical_pressure_remaining"])
            self.assertTrue((root / "run.mp4").exists())

    def test_emergency_cleanup_honors_local_only_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = self.write_manifest(root, stopped_at="2026-04-10T10:01:00")
            payload = RETENTION.load_manifest(manifest_path)
            payload = RETENTION.initialize_local_retention(
                payload,
                enabled=True,
                retention_days=7,
                require_upload_ack=False,
                require_local_compaction=False,
            )
            manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

            def fake_disk_usage(_path: Path):
                free_value = 35 * (1024**3) if not (root / "run.mp4").exists() else 4 * (1024**3)
                return shutil._ntuple_diskusage(100 * (1024**3), 100 * (1024**3) - free_value, free_value)

            result = RETENTION.cleanup_runs(
                runs_root=root,
                now_local=dt.datetime(2026, 4, 11, 9, 0, 0),
                delete=True,
                emergency_config={
                    "enabled": True,
                    "min_free_gb": 20,
                    "target_free_gb": 30,
                    "block_new_recording_free_gb": 8,
                },
                run_normal_cleanup=False,
                disk_usage_fn=fake_disk_usage,
            )

            self.assertEqual(result["emergency_deleted_run_count"], 1)
            self.assertFalse((root / "run.mp4").exists())


if __name__ == "__main__":
    unittest.main()
