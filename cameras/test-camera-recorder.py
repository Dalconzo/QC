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


class CameraRecorderTests(unittest.TestCase):
    def test_normalize_dshow_source_preserves_device_quotes(self) -> None:
        source = 'dshow:video="Arducam USB Camera"'
        self.assertEqual(RECORDER._normalize_dshow_name(source), 'video="Arducam USB Camera"')

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


if __name__ == "__main__":
    unittest.main()
