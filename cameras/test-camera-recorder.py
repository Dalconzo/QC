#!/usr/bin/env python3
"""Focused regression tests for the recorder helpers."""
from __future__ import annotations

import importlib.util
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


RECORDER = load_module("camera_recorder_module", CAMERAS_DIR / "camera-recorder.py")
LIVE = load_module("camera_live_module", CAMERAS_DIR / "camera_live.py")


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


if __name__ == "__main__":
    unittest.main()
