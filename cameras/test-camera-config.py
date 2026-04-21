#!/usr/bin/env python3
"""Smoke tests for the shared Hamilton camera config layer."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import importlib.util
import sys


MODULE_PATH = Path(__file__).resolve().parent / "camera_config.py"
SPEC = importlib.util.spec_from_file_location("camera_config_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CameraConfigTests(unittest.TestCase):
    def test_effective_config_merges_local_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs_root = root / "runs"
            log_dir = root / "logs"
            log_dir.mkdir()

            base_path = root / "camera-recorder.json"
            local_path = root / "camera-recorder.local.json"

            base_path.write_text(
                json.dumps(
                    {
                        "hamilton": {
                            "log_dir": str(log_dir),
                            "process_name": "HxRun.exe",
                        },
                        "storage": {
                            "runs_root": str(runs_root),
                        },
                        "profiles": [
                            {"id": "default", "label": "USB", "source": "USB Cam"},
                            {"id": "top", "label": "Top", "source": "Top Cam"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            local_path.write_text(
                json.dumps(
                    {
                        "recorder": {"default_profile": "top", "max_record_sec": 900},
                        "replay": {"port": 5055},
                    }
                ),
                encoding="utf-8",
            )

            config = MODULE.load_effective_config(config_path=base_path, local_override_path=local_path)
            self.assertEqual(config["recorder"]["default_profile"], "top")
            self.assertEqual(config["replay"]["port"], 5055)
            self.assertTrue(config["replay"]["log_path"])
            self.assertTrue(config["central_ingest"]["staging_root"])
            self.assertTrue(config["central_ingest"]["upload_root"])
            self.assertEqual(config["central_ingest"]["transport"], "filesystem")
            self.assertEqual(config["live"]["default_profile"], "default")
            self.assertEqual(config["live"]["refresh_ms"], 1000)
            self.assertEqual(config["live"]["jpeg_quality"], 4)
            self.assertIn("compaction", config["storage"])
            self.assertFalse(config["storage"]["compaction"]["enabled"])
            self.assertIn("retention", config["storage"])
            self.assertEqual(config["storage"]["retention"]["original_retention_days"], 7)
            self.assertEqual(MODULE.get_profile(config)["id"], "top")
            self.assertTrue(config["daemon"]["task_name"])

    def test_legacy_flat_keys_still_feed_nested_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            base_path = root / "camera-recorder.json"
            base_path.write_text(
                json.dumps(
                    {
                        "hamilton_log_dir": str(log_dir),
                        "default_source": "2",
                        "default_poll_sec": 2.5,
                        "default_max_record_sec": 120,
                    }
                ),
                encoding="utf-8",
            )

            config = MODULE.load_effective_config(config_path=base_path, local_override_path=root / "missing.local.json")
            self.assertEqual(config["hamilton"]["log_dir"], str(log_dir))
            self.assertEqual(config["profiles"][0]["source"], "2")
            self.assertEqual(config["recorder"]["poll_sec"], 2.5)
            self.assertEqual(config["recorder"]["max_record_sec"], 120)

    def test_validation_can_relax_missing_hamilton_log_dir_for_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base_path = root / "camera-recorder.json"
            base_path.write_text(json.dumps({"hamilton": {"log_dir": str(root / "missing")}}), encoding="utf-8")

            config = MODULE.load_effective_config(config_path=base_path, local_override_path=root / "missing.local.json")
            replay_validation = MODULE.validate_config(config, require_hamilton_log_dir=False)
            recorder_validation = MODULE.validate_config(config, require_hamilton_log_dir=True)
            self.assertFalse(replay_validation["errors"])
            self.assertTrue(recorder_validation["errors"])

    def test_validation_rejects_missing_live_default_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            base_path = root / "camera-recorder.json"
            base_path.write_text(
                json.dumps(
                    {
                        "hamilton": {"log_dir": str(log_dir), "process_name": "HxRun.exe"},
                        "live": {"default_profile": "missing", "frame_timeout_sec": 8, "refresh_ms": 1000, "jpeg_quality": 4},
                        "profiles": [{"id": "default", "label": "USB", "source": "USB Cam"}],
                    }
                ),
                encoding="utf-8",
            )

            config = MODULE.load_effective_config(config_path=base_path, local_override_path=root / "missing.local.json")
            validation = MODULE.validate_config(config, require_hamilton_log_dir=False)
            self.assertTrue(any("live.default_profile" in item for item in validation["errors"]))

    def test_validation_rejects_invalid_compaction_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            base_path = root / "camera-recorder.json"
            base_path.write_text(
                json.dumps(
                    {
                        "hamilton": {"log_dir": str(log_dir), "process_name": "HxRun.exe"},
                        "storage": {
                            "compaction": {
                                "enabled": True,
                                "active_crf": 100,
                                "active_preset": "",
                                "idle_crf": -1,
                                "idle_preset": "",
                                "idle_fps": -2,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = MODULE.load_effective_config(config_path=base_path, local_override_path=root / "missing.local.json")
            validation = MODULE.validate_config(config, require_hamilton_log_dir=False)
            self.assertTrue(any("storage.compaction.active_crf" in item for item in validation["errors"]))
            self.assertTrue(any("storage.compaction.active_preset" in item for item in validation["errors"]))
            self.assertTrue(any("storage.compaction.idle_crf" in item for item in validation["errors"]))
            self.assertTrue(any("storage.compaction.idle_preset" in item for item in validation["errors"]))
            self.assertTrue(any("storage.compaction.idle_fps" in item for item in validation["errors"]))

    def test_validation_rejects_negative_retention_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            base_path = root / "camera-recorder.json"
            base_path.write_text(
                json.dumps(
                    {
                        "hamilton": {"log_dir": str(log_dir), "process_name": "HxRun.exe"},
                        "storage": {"retention": {"original_retention_days": -7}},
                    }
                ),
                encoding="utf-8",
            )

            config = MODULE.load_effective_config(config_path=base_path, local_override_path=root / "missing.local.json")
            validation = MODULE.validate_config(config, require_hamilton_log_dir=False)
            self.assertTrue(any("storage.retention.original_retention_days" in item for item in validation["errors"]))


if __name__ == "__main__":
    unittest.main()
