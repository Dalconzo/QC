#!/usr/bin/env python3
"""Focused tests for trace-derived replay chapters and segments."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import textwrap
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


MODULE = load_module("trace_replay_module", CAMERAS_DIR / "trace_replay.py")


class TraceReplayTests(unittest.TestCase):
    def test_build_trace_replay_summary_marks_long_timer_gap_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = Path(tmpdir) / "demo.trc"
            trace_path.write_text(
                textwrap.dedent(
                    """
                    2026-04-16 12:00:00> SYSTEM : Execute method - start;
                    2026-04-16 12:00:05> USER : Trace - complete; starting timer: timer_Incubation1
                    2026-04-16 12:03:15> USER : Trace - complete; timer_Incubation1 elapsed
                    2026-04-16 12:03:20> SYSTEM : File checksum - written; checksum=ABC valid=1
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            payload = MODULE.build_trace_replay_summary(trace_path)

            self.assertEqual(payload["idle_segment_count"], 1)
            self.assertEqual(payload["active_segment_count"], 2)
            self.assertEqual(len(payload["segments"]), 3)
            self.assertEqual(payload["segments"][1]["kind"], "idle")
            self.assertTrue(payload["segments"][1]["is_skipped_by_default"])
            self.assertIn("timer_Incubation1", payload["segments"][1]["phase_label"])
            self.assertGreaterEqual(len(payload["chapters"]), 3)

    def test_build_trace_replay_summary_degrades_to_single_active_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = Path(tmpdir) / "demo.trc"
            trace_path.write_text(
                textwrap.dedent(
                    """
                    2026-04-16 12:00:00> SYSTEM : Execute method - start;
                    2026-04-16 12:00:10> SYSTEM : File checksum - written; checksum=ABC valid=1
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            payload = MODULE.build_trace_replay_summary(trace_path)

            self.assertEqual(payload["idle_segment_count"], 0)
            self.assertEqual(payload["active_segment_count"], 1)
            self.assertEqual(len(payload["segments"]), 1)
            self.assertEqual(payload["segments"][0]["kind"], "active")


if __name__ == "__main__":
    unittest.main()
