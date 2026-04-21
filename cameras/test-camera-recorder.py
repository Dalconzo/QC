#!/usr/bin/env python3
"""Focused regression tests for the recorder helpers."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


RECORDER = load_module("camera_recorder_module", CAMERAS_DIR / "camera-recorder.py")
LIVE = load_module("camera_live_module", CAMERAS_DIR / "camera_live.py")
COMPACTION = load_module("camera_local_compaction_module", CAMERAS_DIR / "local_compaction.py")


class CameraRecorderTests(unittest.TestCase):
    def test_plain_camera_name_becomes_dshow_input(self) -> None:
        command = RECORDER.build_ffmpeg_command(
            "ffmpeg",
            "Arducam USB Camera",
            Path("out.mp4"),
            framerate=None,
            video_size=None,
            dshow_rtbufsize="256M",
        )
        self.assertIn("-f", command)
        self.assertIn("dshow", command)
        self.assertIn("-rtbufsize", command)
        self.assertIn("256M", command)
        self.assertIn("video=Arducam USB Camera", command)
        self.assertIn("-an", command)

    def test_live_preview_uses_same_plain_camera_name_rule(self) -> None:
        command = LIVE.build_live_frame_command(
            "ffmpeg",
            "Arducam USB Camera",
            framerate=None,
            video_size=None,
            dshow_rtbufsize="256M",
            jpeg_quality=4,
        )
        self.assertIn("-f", command)
        self.assertIn("dshow", command)
        self.assertIn("-rtbufsize", command)
        self.assertIn("256M", command)
        self.assertIn("video=Arducam USB Camera", command)

    def test_invalid_recording_rejects_missing_or_empty_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing = root / "missing.mp4"
            empty = root / "empty.mp4"
            empty.write_bytes(b"")
            tiny = root / "tiny.mp4"
            tiny.write_bytes(b"x" * 16)
            good = root / "good.mp4"
            good.write_bytes(b"x" * 2048)

            self.assertFalse(RECORDER._is_valid_recording(missing, stop_reason="backend_exit"))
            self.assertFalse(RECORDER._is_valid_recording(empty, stop_reason="backend_exit"))
            self.assertFalse(RECORDER._is_valid_recording(tiny, stop_reason="backend_exit"))
            self.assertTrue(RECORDER._is_valid_recording(good, stop_reason="process_exit"))

    def test_numeric_source_does_not_become_dshow(self) -> None:
        command = RECORDER.build_ffmpeg_command(
            "ffmpeg",
            "0",
            Path("out.mp4"),
            framerate=None,
            video_size=None,
            dshow_rtbufsize="256M",
        )
        self.assertNotIn("dshow", command)

    def test_quoted_numeric_source_recovers_to_numeric(self) -> None:
        command = RECORDER.build_ffmpeg_command(
            "ffmpeg",
            "'0'",
            Path("out.mp4"),
            framerate=None,
            video_size=None,
            dshow_rtbufsize="256M",
        )
        self.assertNotIn("dshow", command)

    def test_choose_trace_file_returns_rich_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            older = root / "older.trc"
            newer = root / "newer.trc"
            older.write_text("older", encoding="utf-8")
            newer.write_text("newer", encoding="utf-8")
            older_ts = dt.datetime(2026, 4, 16, 12, 0, 0).timestamp()
            newer_ts = dt.datetime(2026, 4, 16, 12, 0, 8).timestamp()
            os.utime(older, (older_ts, older_ts))
            os.utime(newer, (newer_ts, newer_ts))

            match = RECORDER.choose_trace_file(root, "*.trc", dt.datetime(2026, 4, 16, 12, 0, 6))

            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.path, newer)
            self.assertEqual(match.delta_sec, 2.0)
            self.assertEqual(match.modified_at, dt.datetime(2026, 4, 16, 12, 0, 8))

    def test_detect_log_activity_reports_created_and_updated_files(self) -> None:
        previous = {
            str(Path(r"C:\logs\existing.trc")): dt.datetime(2026, 4, 16, 12, 0, 0).timestamp(),
        }
        current = {
            str(Path(r"C:\logs\existing.trc")): dt.datetime(2026, 4, 16, 12, 0, 5).timestamp(),
            str(Path(r"C:\logs\new.trc")): dt.datetime(2026, 4, 16, 12, 0, 6).timestamp(),
        }

        changes = RECORDER.detect_log_activity(previous, current)

        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[0]["change_type"], "updated")
        self.assertEqual(changes[1]["change_type"], "created")

    def test_manifest_payload_includes_capture_and_logical_windows(self) -> None:
        run_window = RECORDER.build_run_window(
            capture_started_at=dt.datetime(2026, 4, 16, 12, 0, 0),
            capture_stopped_at=dt.datetime(2026, 4, 16, 12, 8, 0),
            stop_reason="process_exit",
            logical_stopped_at=dt.datetime(2026, 4, 16, 12, 5, 30),
            logical_stop_reason="trace_log_activity",
            logical_stop_details={"changed_files": [{"path": "x.trc"}]},
        )
        trace_match = RECORDER.TraceMatch(
            path=Path(r"C:\logs\run.trc"),
            delta_sec=30.0,
            modified_at=dt.datetime(2026, 4, 16, 12, 6, 0),
        )

        payload = RECORDER.build_run_manifest_payload(
            label="h7",
            source="camera0",
            video_path=Path(r"C:\video\run.mp4"),
            run_window=run_window,
            stop_reason="trace_log_activity",
            process_gate="HxRun.exe",
            log_dir=Path(r"C:\logs"),
            log_glob="*.trc",
            trace_match=trace_match,
        )

        self.assertEqual(payload["logical_run_stop_offset_sec"], 330.0)
        self.assertEqual(payload["logical_stop_reason"], "trace_log_activity")
        self.assertEqual(payload["trace_finalized_after_logical_stop_sec"], 30.0)
        self.assertEqual(payload["replay_manifest_version"], "hybrid-replay.v1")
        self.assertEqual(payload["replay_capabilities"], ["trace_chapters", "trace_segments", "idle_skip_default"])
        self.assertEqual(payload["storage_tier"], "full_run_source")
        self.assertEqual(payload["replay_default_mode"], "skip_idle")
        self.assertIn("segments", payload)
        self.assertIn("chapters", payload)
        self.assertTrue(all(item["video_path"] == str(Path(r"C:\video\run.mp4").resolve()) for item in payload["segments"]))

    def test_write_run_manifest_persists_new_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "run.run.json"
            video_path = root / "run.mp4"
            trace_path = root / "run.trc"
            video_path.write_bytes(b"x" * 2048)
            trace_path.write_text("trace", encoding="utf-8")

            run_window = RECORDER.build_run_window(
                capture_started_at=dt.datetime(2026, 4, 16, 12, 0, 0),
                capture_stopped_at=dt.datetime(2026, 4, 16, 12, 8, 0),
                stop_reason="trace_log_activity",
                logical_stopped_at=dt.datetime(2026, 4, 16, 12, 5, 0),
                logical_stop_reason="trace_log_activity",
                logical_stop_details={"candidate": True},
            )
            trace_match = RECORDER.TraceMatch(
                path=trace_path,
                delta_sec=45.0,
                modified_at=dt.datetime(2026, 4, 16, 12, 5, 45),
            )

            RECORDER.write_run_manifest(
                manifest_path,
                label="h7",
                source="camera0",
                video_path=video_path,
                run_window=run_window,
                stop_reason="trace_log_activity",
                process_gate="HxRun.exe",
                log_dir=root,
                log_glob="*.trc",
                trace_match=trace_match,
            )

            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["logical_stop_reason"], "trace_log_activity")
            self.assertEqual(payload["trace_filename"], "run.trc")
            self.assertEqual(payload["trace_finalized_after_logical_stop_sec"], 45.0)
            self.assertEqual(payload["replay_manifest_version"], "hybrid-replay.v1")
            self.assertEqual(payload["storage_tier"], "full_run_source")
            self.assertIn("segments", payload)
            self.assertIn("chapters", payload)
            self.assertEqual(payload["local_compaction"]["status"], "not_requested")

    def test_write_run_manifest_persists_local_compaction_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "run.run.json"
            video_path = root / "run.mp4"
            trace_path = root / "run.trc"
            video_path.write_bytes(b"x" * 2048)
            trace_path.write_text("trace", encoding="utf-8")

            run_window = RECORDER.build_run_window(
                capture_started_at=dt.datetime(2026, 4, 16, 12, 0, 0),
                capture_stopped_at=dt.datetime(2026, 4, 16, 12, 8, 0),
                stop_reason="process_exit",
            )
            trace_match = RECORDER.TraceMatch(
                path=trace_path,
                delta_sec=12.0,
                modified_at=dt.datetime(2026, 4, 16, 12, 8, 12),
            )
            segments = [
                {
                    "segment_id": "idle-001",
                    "kind": "idle",
                    "start_offset_sec": 0,
                    "stop_offset_sec": 300,
                    "duration_sec": 300,
                    "phase_label": "Idle",
                    "phase_source": "trace",
                    "video_path": str(video_path.resolve()),
                    "video_encoding_profile": "source_full_run",
                    "is_skipped_by_default": True,
                    "derived_video_path": str((root / "run.derived" / "idle-001_idle.mp4").resolve()),
                    "derived_video_filename": "idle-001_idle.mp4",
                    "derived_video_encoding_profile": "derived_idle_h264_2fps",
                    "derived_size_bytes": 1024,
                }
            ]
            local_compaction = {
                "status": "succeeded",
                "artifacts_root": str((root / "run.derived").resolve()),
                "generated_at_local": "2026-04-16T12:08:30",
                "failure": "",
                "source_video_path": str(video_path.resolve()),
                "source_video_size_bytes": 2048,
                "total_derived_size_bytes": 1024,
                "segment_derivatives": [
                    {
                        "segment_id": "idle-001",
                        "kind": "idle",
                        "video_path": str((root / "run.derived" / "idle-001_idle.mp4").resolve()),
                        "video_filename": "idle-001_idle.mp4",
                        "video_encoding_profile": "derived_idle_h264_2fps",
                        "size_bytes": 1024,
                    }
                ],
            }

            RECORDER.write_run_manifest(
                manifest_path,
                label="h7",
                source="camera0",
                video_path=video_path,
                run_window=run_window,
                stop_reason="process_exit",
                process_gate="HxRun.exe",
                log_dir=root,
                log_glob="*.trc",
                trace_match=trace_match,
                storage_tier="full_run_plus_local_derivatives",
                segments_override=segments,
                local_compaction=local_compaction,
            )

            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["storage_tier"], "full_run_plus_local_derivatives")
            self.assertEqual(payload["local_compaction"]["status"], "succeeded")
            self.assertEqual(payload["segments"][0]["derived_video_filename"], "idle-001_idle.mp4")
            self.assertEqual(payload["local_compaction"]["segment_derivatives"][0]["video_encoding_profile"], "derived_idle_h264_2fps")

    def test_generate_local_compaction_builds_active_and_idle_derivatives(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_video = root / "run.mp4"
            source_video.write_bytes(b"x" * 4096)
            created_outputs: list[Path] = []

            def fake_run(command, check, capture_output, text):
                output_path = Path(command[-1])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"x" * 512)
                created_outputs.append(output_path)
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(COMPACTION.subprocess, "run", side_effect=fake_run) as patched:
                result = COMPACTION.generate_local_compaction(
                    ffmpeg_bin="ffmpeg",
                    source_video_path=source_video,
                    segments=[
                        {
                            "segment_id": "active-001",
                            "kind": "active",
                            "start_offset_sec": 0,
                            "stop_offset_sec": 30,
                            "duration_sec": 30,
                        },
                        {
                            "segment_id": "idle-002",
                            "kind": "idle",
                            "start_offset_sec": 30,
                            "stop_offset_sec": 330,
                            "duration_sec": 300,
                        },
                    ],
                    min_segment_duration_sec=5.0,
                )

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(len(result["segment_derivatives"]), 2)
            self.assertEqual(result["segment_derivatives"][1]["video_encoding_profile"], "derived_idle_h264_2fps")
            self.assertEqual(result["total_derived_size_bytes"], 1024)
            self.assertEqual(patched.call_count, 2)
            idle_command = patched.call_args_list[1].args[0]
            self.assertIn("-vf", idle_command)
            self.assertIn("fps=2", idle_command)
            self.assertTrue(all(path.exists() for path in created_outputs))

    def test_generate_local_compaction_handles_ffmpeg_failure_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_video = root / "run.mp4"
            source_video.write_bytes(b"x" * 4096)

            with mock.patch.object(
                COMPACTION.subprocess,
                "run",
                side_effect=RuntimeError("ffmpeg failed"),
            ):
                result = COMPACTION.generate_local_compaction(
                    ffmpeg_bin="ffmpeg",
                    source_video_path=source_video,
                    segments=[
                        {
                            "segment_id": "idle-001",
                            "kind": "idle",
                            "start_offset_sec": 0,
                            "stop_offset_sec": 180,
                            "duration_sec": 180,
                        }
                    ],
                )

            self.assertEqual(result["status"], "failed")
            self.assertIn("ffmpeg failed", result["failure"])
            self.assertTrue(source_video.exists())


if __name__ == "__main__":
    unittest.main()
