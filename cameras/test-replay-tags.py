#!/usr/bin/env python3
"""Behavioral tests for trace-derived replay tags."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from replay_tags import TRACE_TAGS_VERSION, derive_run_tags


class ReplayTagTests(unittest.TestCase):
    def derive(self, *lines: str) -> dict:
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = Path(tmpdir) / "run.trc"
            trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return derive_run_tags(trace_path)

    def test_aborted_root_trace_forms_dedupe_and_keep_authoritative_slot(self) -> None:
        result = self.derive(
            "2026-06-16 22:16:35> USER : Trace - complete; pillar barcode: ZIGG",
            "2026-06-16 22:16:42> USER : Trace - complete; Barcode for NO1 pillar plate is:  zigg",
            "2026-06-16 22:17:32> SYSTEM : Method has been aborted by the user - complete;",
            "2026-06-16 22:17:33> SYSTEM : End method - complete;",
        )

        barcode_tags = [tag for tag in result["tags"] if tag["key"] == "pillar_plate_barcode"]
        self.assertEqual(result["version"], TRACE_TAGS_VERSION)
        self.assertEqual(len(barcode_tags), 1)
        self.assertEqual(barcode_tags[0]["value"], "zigg")
        self.assertEqual(barcode_tags[0]["metadata"]["slot"], "NO1")
        self.assertEqual(barcode_tags[0]["metadata"]["slots"], ["NO1"])
        self.assertEqual(result["summary"]["outcome"], "aborted")

    def test_duplicate_barcode_keeps_all_slot_metadata(self) -> None:
        result = self.derive(
            "USER : Barcode for NO1 pillar plate is: ABC-123",
            "USER : Barcode for NO2 pillar plate is: abc-123",
            "USER : Barcode for NO3 pillar plate is: DEF_456",
            "SYSTEM : End method - complete;",
        )

        barcode_tags = [tag for tag in result["tags"] if tag["key"] == "pillar_plate_barcode"]
        self.assertEqual(len(barcode_tags), 2)
        self.assertEqual(barcode_tags[0]["metadata"]["slots"], ["NO1", "NO2"])
        self.assertEqual(result["summary"]["pillar_plate_barcodes"], ["abc-123", "DEF_456"])
        self.assertEqual(result["summary"]["outcome"], "ok")

    def test_error_precedes_completed_success(self) -> None:
        result = self.derive(
            "Microlab STAR : Aspirate - error; failed",
            "SYSTEM : End method - complete;",
        )

        self.assertTrue(result["summary"]["has_error"])
        self.assertTrue(result["summary"]["end_method_complete"])
        self.assertEqual(result["summary"]["outcome"], "error")

    def test_clean_incomplete_trace_is_unknown(self) -> None:
        result = self.derive("USER : Trace - complete; pillar barcode: INCOMPLETE-1")

        self.assertFalse(result["summary"]["end_method_complete"])
        self.assertEqual(result["summary"]["outcome"], "unknown")

    def test_clean_completed_trace_is_ok(self) -> None:
        result = self.derive(
            "USER : Trace - complete; pillar barcode: COMPLETE-1",
            "SYSTEM : End method - complete;",
        )

        self.assertTrue(result["summary"]["end_method_complete"])
        self.assertEqual(result["summary"]["outcome"], "ok")


if __name__ == "__main__":
    unittest.main()
