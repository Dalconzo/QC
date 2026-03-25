#!/usr/bin/env python3
"""Smoke tests for the Hamilton run replay backend."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

import importlib.util


MODULE_PATH = Path(__file__).resolve().parent / "replay-app.py"
SPEC = importlib.util.spec_from_file_location("camera_replay_app", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ReplayAppTests(unittest.TestCase):
    def test_refresh_catalog_builds_replayable_run_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video_path = root / "demo.mp4"
            trace_path = root / "demo.trc"
            manifest_path = root / "demo.run.json"

            video_path.write_bytes(b"fake video payload")
            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_path.write_bytes(next(sample_trace.glob("*.trc")).read_bytes())
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "demo-catalog",
                        "source": "0",
                        "video_path": str(video_path),
                        "trace_path": str(trace_path),
                        "started_at_local": "2026-03-24T14:00:00",
                        "stopped_at_local": "2026-03-24T14:05:00",
                        "duration_sec": 300,
                        "stop_reason": "process_exit",
                        "process_gate": "HxRun.exe",
                        "hamilton_log_dir": str(root),
                        "hamilton_log_glob": "*.trc",
                        "trace_mtime_delta_sec": 2.5,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            refresh_payload = MODULE.refresh_catalog(root)
            self.assertEqual(refresh_payload["run_count"], 1)

            runs_payload = MODULE.list_catalog_runs(root)
            self.assertEqual(len(runs_payload), 1)
            self.assertEqual(runs_payload[0]["label"], "demo-catalog")
            self.assertEqual(runs_payload[0]["replay_status"], "ready")

    def test_parse_trace_events_uses_first_timestamp_as_zero(self) -> None:
        sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
        trace_path = next(sample_trace.glob("*.trc"))
        events = MODULE.parse_trace_events(trace_path)

        self.assertGreater(len(events), 10)
        self.assertEqual(events[0].elapsed_sec, 0.0)
        self.assertLessEqual(events[0].elapsed_sec, events[1].elapsed_sec)
        self.assertIn("Start method - progress", "\n".join(event.line for event in events[:12]))

    def test_run_detail_returns_manifest_and_trace_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video_path = root / "demo.mp4"
            trace_path = root / "demo.trc"
            manifest_path = root / "demo.run.json"

            video_path.write_bytes(b"fake video payload")
            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_path.write_bytes(next(sample_trace.glob("*.trc")).read_bytes())
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "demo",
                        "source": "0",
                        "video_path": str(video_path),
                        "trace_path": str(trace_path),
                        "started_at_local": "2026-03-24T14:00:00",
                        "stopped_at_local": "2026-03-24T14:05:00",
                        "duration_sec": 300,
                        "stop_reason": "process_exit",
                        "process_gate": "HxRun.exe",
                        "hamilton_log_dir": str(root),
                        "hamilton_log_glob": "*.trc",
                        "trace_mtime_delta_sec": 3.5,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            MODULE.refresh_catalog(root)
            runs_payload = MODULE.list_catalog_runs(root)
            self.assertEqual(len(runs_payload), 1)

            run_id = runs_payload[0]["run_id"]
            detail_payload = MODULE.get_run_detail(root, run_id)
            self.assertEqual(detail_payload["run"]["label"], "demo")
            self.assertGreater(len(detail_payload["events"]), 10)

    def test_http_api_serves_run_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video_path = root / "demo.mp4"
            trace_path = root / "demo.trc"
            manifest_path = root / "demo.run.json"

            video_path.write_bytes(b"fake video payload")
            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_path.write_bytes(next(sample_trace.glob("*.trc")).read_bytes())
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "demo-http",
                        "source": "0",
                        "video_path": str(video_path),
                        "trace_path": str(trace_path),
                        "started_at_local": "2026-03-24T14:00:00",
                        "stopped_at_local": "2026-03-24T14:05:00",
                        "duration_sec": 300,
                        "stop_reason": "process_exit",
                        "process_gate": "HxRun.exe",
                        "hamilton_log_dir": str(root),
                        "hamilton_log_glob": "*.trc",
                        "trace_mtime_delta_sec": 1.0,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            handler = MODULE.make_handler(root)
            from http.server import ThreadingHTTPServer

            MODULE.refresh_catalog(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/runs"
                payload = json.loads(urllib.request.urlopen(url).read().decode("utf-8"))
                self.assertEqual(len(payload["items"]), 1)
                self.assertEqual(payload["items"][0]["label"], "demo-http")
                self.assertIn("catalog_path", payload)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_http_api_refreshes_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video_path = root / "demo.mp4"
            trace_path = root / "demo.trc"
            manifest_path = root / "demo.run.json"

            video_path.write_bytes(b"fake video payload")
            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_path.write_bytes(next(sample_trace.glob("*.trc")).read_bytes())
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "demo-refresh",
                        "source": "0",
                        "video_path": str(video_path),
                        "trace_path": str(trace_path),
                        "started_at_local": "2026-03-24T14:00:00",
                        "stopped_at_local": "2026-03-24T14:05:00",
                        "duration_sec": 300,
                        "stop_reason": "process_exit",
                        "process_gate": "HxRun.exe",
                        "hamilton_log_dir": str(root),
                        "hamilton_log_glob": "*.trc",
                        "trace_mtime_delta_sec": 1.0,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            handler = MODULE.make_handler(root)
            from http.server import ThreadingHTTPServer

            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/catalog/refresh"
                payload = json.loads(urllib.request.urlopen(url).read().decode("utf-8"))
                self.assertEqual(payload["run_count"], 1)
                self.assertIn("catalog_path", payload)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_http_video_endpoint_supports_byte_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video_path = root / "demo.mp4"
            trace_path = root / "demo.trc"
            manifest_path = root / "demo.run.json"

            video_path.write_bytes(b"0123456789abcdef")
            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_path.write_bytes(next(sample_trace.glob("*.trc")).read_bytes())
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "demo-video",
                        "source": "0",
                        "video_path": str(video_path),
                        "trace_path": str(trace_path),
                        "started_at_local": "2026-03-24T14:00:00",
                        "stopped_at_local": "2026-03-24T14:05:00",
                        "duration_sec": 300,
                        "stop_reason": "process_exit",
                        "process_gate": "HxRun.exe",
                        "hamilton_log_dir": str(root),
                        "hamilton_log_glob": "*.trc",
                        "trace_mtime_delta_sec": 1.0,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            MODULE.refresh_catalog(root)
            run_id = MODULE.list_catalog_runs(root)[0]["run_id"]
            handler = MODULE.make_handler(root)
            from http.server import ThreadingHTTPServer

            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/runs/{run_id}/video"
                request = urllib.request.Request(url, headers={"Range": "bytes=4-7"})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers.get("Content-Range"), "bytes 4-7/16")
                    self.assertEqual(response.read(), b"4567")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
