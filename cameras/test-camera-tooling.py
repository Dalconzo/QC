#!/usr/bin/env python3
"""Smoke tests for camera workstation quality-of-life tools."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import importlib.util


CAMERAS_DIR = Path(__file__).resolve().parent
INSPECT_MANIFESTS_PATH = CAMERAS_DIR / "inspect-run-manifests.py"
TEST_CAMERA_SOURCE_PATH = CAMERAS_DIR / "test-camera-source.py"
INSPECT_CAMERA_CONFIG_PATH = CAMERAS_DIR / "inspect-camera-config.py"


def load_module(module_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INSPECT_MODULE = load_module(INSPECT_MANIFESTS_PATH, "camera_inspect_run_manifests")
PROBE_MODULE = load_module(TEST_CAMERA_SOURCE_PATH, "camera_test_camera_source")


class CameraToolingTests(unittest.TestCase):
    def test_inspect_camera_config_reports_deployment_and_contract_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            hamilton_dir = root / "hamilton"
            hamilton_dir.mkdir()
            config_path = root / "camera-recorder.json"
            local_path = root / "camera-recorder.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "hamilton": {"log_dir": str(hamilton_dir), "process_name": "HxRun.exe"},
                        "storage": {"runs_root": str(root / "runs"), "recorder_log_dir": str(root / "logs")},
                        "central_ingest": {
                            "staging_root": str(root / "staging"),
                            "upload_root": str(root / "central"),
                            "transport": "filesystem",
                        },
                        "profiles": [{"id": "default", "label": "Top Cam", "source": "Arducam USB Camera"}],
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(INSPECT_CAMERA_CONFIG_PATH),
                    "--config",
                    str(config_path),
                    "--local-config",
                    str(local_path),
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["contract_status"]["replay_manifest_version"], "hybrid-replay.v1")
            self.assertIn("trace_segments", payload["contract_status"]["replay_capabilities"])
            self.assertFalse(payload["contract_status"]["midrun_split_enabled"])
            self.assertEqual(payload["contract_status"]["derived_retention_days"], 30)
            self.assertIn("git_commit_short", payload["deployment"])

    def test_manifest_inspector_reports_missing_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trace_path = root / "demo.trc"
            manifest_path = root / "demo.run.json"

            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_path.write_bytes(next(sample_trace.glob("*.trc")).read_bytes())
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "demo-missing-video",
                        "source": "0",
                        "video_path": str(root / "missing.mp4"),
                        "trace_path": str(trace_path),
                        "started_at_local": "2026-04-03T10:00:00",
                    }
                ),
                encoding="utf-8",
            )

            item = INSPECT_MODULE.describe_manifest(manifest_path)
            self.assertEqual(item["replay_status"], "missing_video")
            self.assertTrue(any("Video path is missing" in problem for problem in item["problems"]))

    def test_manifest_cleanup_quarantines_stale_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trace_path = root / "demo.trc"
            manifest_path = root / "demo.run.json"
            quarantine_root = root / "_quarantine"

            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_path.write_bytes(next(sample_trace.glob("*.trc")).read_bytes())
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "demo-quarantine",
                        "source": "0",
                        "video_path": str(root / "missing.mp4"),
                        "trace_path": str(trace_path),
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(INSPECT_MANIFESTS_PATH),
                    "--runs-root",
                    str(root),
                    "--cleanup",
                    "missing-video",
                    "--quarantine-dir",
                    str(quarantine_root),
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["cleanup"]), 1)
            self.assertFalse(manifest_path.exists())
            self.assertTrue((quarantine_root / manifest_path.name).exists())

    def test_manifest_inspector_reports_segment_only_playback_after_original_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trace_path = root / "demo.trc"
            original_video_path = root / "demo.mp4"
            derived_root = root / "demo.derived"
            derived_root.mkdir()
            derived_path = derived_root / "idle-001_idle.mp4"
            manifest_path = root / "demo.run.json"

            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_path.write_bytes(next(sample_trace.glob("*.trc")).read_bytes())
            original_video_path.write_bytes(b"original")
            derived_path.write_bytes(b"derived")
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "demo-derived",
                        "source": "0",
                        "replay_manifest_version": "hybrid-replay.v1",
                        "replay_capabilities": ["trace_chapters", "trace_segments", "idle_skip_default"],
                        "video_path": str(original_video_path),
                        "trace_path": str(trace_path),
                        "started_at_local": "2026-04-03T10:00:00",
                        "chapters": [
                            {
                                "chapter_id": "chapter-001",
                                "start_offset_sec": 0,
                                "label": "Incubation",
                                "kind": "span",
                                "phase_source": "trace",
                                "is_idle": True,
                            }
                        ],
                        "local_retention": {
                            "enabled": True,
                            "upload_status": "acknowledged",
                            "lan_available": True,
                            "central_run_id": "central-run-1",
                            "original_deleted_at_local": "2026-04-10T10:00:00",
                        },
                        "segments": [
                            {
                                "segment_id": "idle-001",
                                "kind": "idle",
                                "start_offset_sec": 0,
                                "stop_offset_sec": 10,
                                "duration_sec": 10,
                                "phase_label": "Incubation",
                                "phase_source": "trace",
                                "video_path": str(original_video_path),
                                "video_encoding_profile": "source_full_run",
                                "is_skipped_by_default": True,
                                "derived_video_path": str(derived_path),
                                "derived_video_filename": derived_path.name,
                                "derived_video_encoding_profile": "derived_idle_h264_2fps",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            original_video_path.unlink()

            item = INSPECT_MODULE.describe_manifest(manifest_path)
            self.assertEqual(item["replay_status"], "missing_video")
            self.assertEqual(item["playback_status"], "ready_segments_only")
            self.assertEqual(item["local_derived_segment_count"], 1)
            self.assertTrue(item["lan_available"])
            self.assertEqual(item["derived_deleted_at_local"], "")

    def test_camera_probe_script_uses_capture_hook(self) -> None:
        original_capture = PROBE_MODULE.capture_live_frame
        original_load_effective_config = PROBE_MODULE.load_effective_config

        def fake_load_effective_config(*, config_path, local_override_path):
            return {
                "hamilton": {"log_dir": str(config_path.parent), "log_glob": "*.trc", "process_name": "HxRun.exe"},
                "storage": {
                    "runs_root": str(config_path.parent),
                    "manifest_dir": "",
                    "recorder_log_dir": str(config_path.parent),
                    "compaction": {
                        "enabled": False,
                        "artifacts_root": "",
                        "min_segment_duration_sec": 5.0,
                        "active_crf": 30,
                        "active_preset": "veryfast",
                        "idle_crf": 36,
                        "idle_preset": "veryfast",
                        "idle_fps": 2,
                    },
                    "retention": {
                        "enabled": True,
                        "original_retention_days": 7,
                        "derived_retention_days": 30,
                        "require_upload_ack": True,
                        "require_local_compaction": False,
                        "cleanup_on_run_complete": True,
                        "emergency": {"enabled": True, "min_free_gb": 20, "target_free_gb": 30, "block_new_recording_free_gb": 8},
                    },
                },
                "recorder": {"default_profile": "default", "poll_sec": 1.0, "max_record_sec": 0, "startup_timeout_sec": 0, "dshow_rtbufsize": "256M", "ffmpeg_path": "", "stop_file": str(config_path.parent / "stop")},
                "replay": {"host": "127.0.0.1", "port": 5050, "log_path": str(config_path.parent / "replay.log")},
                "live": {"default_profile": "default", "frame_timeout_sec": 8, "refresh_ms": 1000, "jpeg_quality": 4},
                "central_ingest": {
                    "staging_root": str(config_path.parent / "staging"),
                    "upload_root": str(config_path.parent / "central"),
                    "transport": "filesystem",
                },
                "daemon": {"task_name": "HamiltonCameraRecorderDaemon", "stop_file": "x", "pid_file": "x", "status_path": "x", "log_path": "x", "idle_poll_sec": 1.0, "heartbeat_sec": 5.0, "relaunch_delay_sec": 2.0},
                "profiles": [{"id": "default", "label": "Top Cam", "source": "0", "framerate": None, "video_size": None, "ffmpeg_path": ""}],
            }

        def fake_capture_live_frame(config, profile_id=None):
            return b"\xff\xd8\xff\xd9", {"id": "default", "label": "Top Cam", "source": "0"}, "C:\\ffmpeg.exe"

        PROBE_MODULE.load_effective_config = fake_load_effective_config
        PROBE_MODULE.capture_live_frame = fake_capture_live_frame
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                output_path = Path(tmpdir) / "probe.jpg"
                argv = sys.argv
                sys.argv = [
                    "test-camera-source.py",
                    "--config",
                    str(Path(tmpdir) / "camera-recorder.json"),
                    "--local-config",
                    str(Path(tmpdir) / "camera-recorder.local.json"),
                    "--output",
                    str(output_path),
                    "--json",
                ]
                try:
                    exit_code = PROBE_MODULE.main()
                finally:
                    sys.argv = argv
                self.assertEqual(exit_code, 0)
                self.assertTrue(output_path.exists())
                self.assertEqual(output_path.read_bytes(), b"\xff\xd8\xff\xd9")
        finally:
            PROBE_MODULE.capture_live_frame = original_capture
            PROBE_MODULE.load_effective_config = original_load_effective_config


if __name__ == "__main__":
    unittest.main()
