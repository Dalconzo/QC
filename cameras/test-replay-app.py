#!/usr/bin/env python3
"""Smoke tests for the Hamilton run replay backend."""
from __future__ import annotations

import json
import os
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
    def make_test_config(self) -> dict:
        config = json.loads(json.dumps(MODULE.load_effective_config()))
        config["hamilton"]["log_dir"] = str(Path(tempfile.gettempdir()))
        return config

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
            self.assertGreaterEqual(runs_payload[0]["segment_count"], 1)
            self.assertEqual(runs_payload[0]["primary_barcode"], "TBDR80300001000236")
            self.assertEqual(runs_payload[0]["run_outcome"], "error")

    def test_parse_trace_events_uses_first_timestamp_as_zero(self) -> None:
        sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
        trace_path = next(sample_trace.glob("*.trc"))
        events = MODULE.parse_trace_events(trace_path)

        self.assertGreater(len(events), 10)
        self.assertEqual(events[0].elapsed_sec, 0.0)
        self.assertLessEqual(events[0].elapsed_sec, events[1].elapsed_sec)
        self.assertIn("Start method - progress", "\n".join(event.line for event in events[:12]))

    def test_refresh_catalog_extracts_abort_outcome_from_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video_path = root / "abort.mp4"
            trace_path = root / "abort.trc"
            manifest_path = root / "abort.run.json"

            video_path.write_bytes(b"fake video payload")
            trace_path.write_text(
                "\n".join(
                    [
                        "2026-03-24 14:00:00> SYSTEM : Start method - progress",
                        "2026-03-24 14:00:03> USER : Trace - complete; Barcode for NO1 pillar plate is:  TBDRTEST0001",
                        "2026-03-24 14:00:05> SYSTEM : Abort method - error; Wrong run control state detected",
                        "2026-03-24 14:00:06> SYSTEM : Execute method - progress; Step aborted. The method should be restarted.",
                    ]
                ),
                encoding="utf-8",
            )
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "demo-abort",
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

            MODULE.refresh_catalog(root)
            run = MODULE.list_catalog_runs(root)[0]
            self.assertEqual(run["primary_barcode"], "TBDRTEST0001")
            self.assertEqual(run["run_outcome"], "aborted")
            self.assertTrue(run["run_tag_summary"]["has_error"])
            self.assertTrue(run["run_tag_summary"]["has_abort"])

    def test_list_catalog_runs_supports_barcode_and_outcome_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            source_trace = next(sample_trace.glob("*.trc")).read_bytes()

            for label, trace_bytes in (
                ("tagged", source_trace),
                ("clean", b"2026-03-24 14:00:00> SYSTEM : Start method - progress\n2026-03-24 14:00:02> USER : Trace - complete; finished successfully\n"),
            ):
                video_path = root / f"{label}.mp4"
                trace_path = root / f"{label}.trc"
                manifest_path = root / f"{label}.run.json"
                video_path.write_bytes(label.encode("utf-8"))
                trace_path.write_bytes(trace_bytes)
                manifest_path.write_text(
                    json.dumps(
                        {
                            "label": label,
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
            barcode_results = MODULE.list_catalog_runs(root, query="TBDR80300001000236")
            self.assertEqual([item["label"] for item in barcode_results], ["tagged"])
            ok_results = MODULE.list_catalog_runs(root, outcome="ok")
            self.assertEqual([item["label"] for item in ok_results], ["clean"])

    def test_refresh_catalog_and_filters_emit_barcode_logging(self) -> None:
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
                        "label": "demo-log",
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

            with self.assertLogs(level="INFO") as captured:
                refresh_payload = MODULE.refresh_catalog(root)
                filtered = MODULE.list_catalog_runs(root, query="TBDR80300001000236", outcome="error")

            joined = "\n".join(captured.output)
            self.assertEqual(refresh_payload["run_count"], 1)
            self.assertEqual(len(filtered), 1)
            self.assertIn("primary_barcode=TBDR80300001000236", joined)
            self.assertIn("catalog_refresh", joined)
            self.assertIn("list_runs query=tbdr80300001000236 outcome=error results=1", joined)

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
            self.assertIn("segments", detail_payload)
            self.assertIn("chapters", detail_payload)
            self.assertGreaterEqual(len(detail_payload["segments"]), 1)
            self.assertEqual(detail_payload["manifest"]["replay_manifest_version"], "hybrid-replay.v1")
            self.assertEqual(detail_payload["manifest"]["replay_capabilities"], ["trace_chapters", "trace_segments", "idle_skip_default"])

    def test_load_run_manifest_upgrades_old_hybrid_fields_from_trace(self) -> None:
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
                        "label": "demo-old",
                        "source": "0",
                        "video_path": str(video_path),
                        "trace_path": str(trace_path),
                        "started_at_local": "2026-03-24T14:00:00",
                        "stopped_at_local": "2026-03-24T14:05:00",
                        "duration_sec": 300,
                        "stop_reason": "process_exit",
                        "process_gate": "HxRun.exe",
                        "chapters": "stale",
                        "segments": [{"kind": "idle"}],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            payload = MODULE.load_run_manifest(manifest_path)
            self.assertEqual(payload["replay_manifest_version"], "hybrid-replay.v1")
            self.assertGreaterEqual(len(payload["chapters"]), 1)
            self.assertGreaterEqual(len(payload["segments"]), 1)
            self.assertTrue(all(item["video_path"] == str(video_path.resolve()) for item in payload["segments"]))

    def test_load_run_manifest_keeps_run_id_stable_across_path_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_bytes = next(sample_trace.glob("*.trc")).read_bytes()

            def write_run(run_root: Path) -> Path:
                run_root.mkdir(parents=True, exist_ok=True)
                video_path = run_root / "demo.mp4"
                trace_path = run_root / "demo.trc"
                manifest_path = run_root / "demo.run.json"
                video_path.write_bytes(b"fake video payload")
                trace_path.write_bytes(trace_bytes)
                manifest_path.write_text(
                    json.dumps(
                        {
                            "label": "demo-stable-id",
                            "source": "0",
                            "video_path": str(video_path),
                            "trace_path": str(trace_path),
                            "started_at_local": "2026-03-24T14:00:00",
                            "stopped_at_local": "2026-03-24T14:05:00",
                            "duration_sec": 300,
                            "stop_reason": "process_exit",
                            "process_gate": "HxRun.exe",
                            "hamilton_log_dir": str(run_root),
                            "hamilton_log_glob": "*.trc",
                            "trace_mtime_delta_sec": 2.5,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                return manifest_path

            first_manifest = write_run(root / "runs-a")
            second_manifest = write_run(root / "runs-b")

            first_payload = MODULE.load_run_manifest(first_manifest)
            second_payload = MODULE.load_run_manifest(second_manifest)
            self.assertEqual(first_payload["run_id"], second_payload["run_id"])

    def test_refresh_catalog_keeps_newest_manifest_for_duplicate_logical_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_bytes = next(sample_trace.glob("*.trc")).read_bytes()

            def write_run(run_root: Path, label: str) -> Path:
                run_root.mkdir(parents=True, exist_ok=True)
                video_path = run_root / "demo.mp4"
                trace_path = run_root / "demo.trc"
                manifest_path = run_root / "demo.run.json"
                video_path.write_bytes(f"video-{label}".encode("utf-8"))
                trace_path.write_bytes(trace_bytes)
                manifest_path.write_text(
                    json.dumps(
                        {
                            "label": "demo-stable-id",
                            "source": "0",
                            "video_path": str(video_path),
                            "trace_path": str(trace_path),
                            "started_at_local": "2026-03-24T14:00:00",
                            "stopped_at_local": "2026-03-24T14:05:00",
                            "duration_sec": 300,
                            "stop_reason": "process_exit",
                            "process_gate": "HxRun.exe",
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                return manifest_path

            older_manifest = write_run(root / "runs-a", "older")
            newer_manifest = write_run(root / "runs-b", "newer")
            older_time = older_manifest.stat().st_mtime_ns
            os.utime(newer_manifest, ns=(older_time + 5_000_000, older_time + 5_000_000))

            refresh_payload = MODULE.refresh_catalog(root)
            self.assertEqual(refresh_payload["run_count"], 1)

            runs_payload = MODULE.list_catalog_runs(root)
            self.assertEqual(len(runs_payload), 1)
            run = MODULE.get_run_by_id(root, runs_payload[0]["run_id"])
            self.assertEqual(Path(run["manifest_path"]).resolve(), newer_manifest.resolve())
            self.assertEqual(Path(run["video_path"]).read_bytes(), b"video-newer")

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

            handler = MODULE.make_handler(root, self.make_test_config())
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
                self.assertIn("run_tags", payload["items"][0])
                self.assertEqual(payload["items"][0]["primary_barcode"], "TBDR80300001000236")
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

            handler = MODULE.make_handler(root, self.make_test_config())
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

    def test_http_api_returns_latest_ready_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            source_trace = next(sample_trace.glob("*.trc")).read_bytes()

            for label, started in (("older", "2026-03-24T14:00:00"), ("newer", "2026-03-24T15:00:00")):
                video_path = root / f"{label}.mp4"
                trace_path = root / f"{label}.trc"
                manifest_path = root / f"{label}.run.json"
                video_path.write_bytes(label.encode("utf-8"))
                trace_path.write_bytes(source_trace)
                manifest_path.write_text(
                    json.dumps(
                        {
                            "label": label,
                            "source": "0",
                            "video_path": str(video_path),
                            "trace_path": str(trace_path),
                            "started_at_local": started,
                            "stopped_at_local": started,
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

            handler = MODULE.make_handler(root, self.make_test_config())
            from http.server import ThreadingHTTPServer

            MODULE.refresh_catalog(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/runs/latest"
                payload = json.loads(urllib.request.urlopen(url).read().decode("utf-8"))
                self.assertEqual(payload["item"]["label"], "newer")
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
            handler = MODULE.make_handler(root, self.make_test_config())
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

    def test_http_video_endpoint_streams_full_file_without_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video_path = root / "demo.mp4"
            trace_path = root / "demo.trc"
            manifest_path = root / "demo.run.json"

            original_chunk_size = MODULE.FILE_STREAM_CHUNK_SIZE
            MODULE.FILE_STREAM_CHUNK_SIZE = 4
            try:
                video_path.write_bytes(b"0123456789abcdef")
                sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
                trace_path.write_bytes(next(sample_trace.glob("*.trc")).read_bytes())
                manifest_path.write_text(
                    json.dumps(
                        {
                            "label": "demo-video-full",
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
                handler = MODULE.make_handler(root, self.make_test_config())
                from http.server import ThreadingHTTPServer

                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"http://127.0.0.1:{server.server_port}/api/runs/{run_id}/video"
                    with urllib.request.urlopen(url) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), b"0123456789abcdef")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
            finally:
                MODULE.FILE_STREAM_CHUNK_SIZE = original_chunk_size

    def test_run_detail_reports_segment_only_playback_after_original_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_video_path = root / "demo.mp4"
            trace_path = root / "demo.trc"
            derived_root = root / "demo.derived"
            derived_root.mkdir()
            derived_path = derived_root / "idle-001_idle.mp4"
            manifest_path = root / "demo.run.json"

            original_video_path.write_bytes(b"original-source")
            derived_path.write_bytes(b"derived-segment")
            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_path.write_bytes(next(sample_trace.glob("*.trc")).read_bytes())
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "demo-derived",
                        "source": "0",
                        "replay_manifest_version": "hybrid-replay.v1",
                        "replay_capabilities": ["trace_chapters", "trace_segments", "idle_skip_default"],
                        "video_path": str(original_video_path),
                        "trace_path": str(trace_path),
                        "started_at_local": "2026-03-24T14:00:00",
                        "stopped_at_local": "2026-03-24T14:05:00",
                        "duration_sec": 300,
                        "stop_reason": "process_exit",
                        "process_gate": "HxRun.exe",
                        "chapters": [
                            {
                                "chapter_id": "chapter-001",
                                "start_offset_sec": 10.0,
                                "label": "Incubation",
                                "kind": "span",
                                "phase_source": "trace",
                                "is_idle": True,
                            }
                        ],
                        "local_retention": {
                            "enabled": True,
                            "retention_days": 7,
                            "require_upload_ack": True,
                            "require_local_compaction": False,
                            "upload_status": "acknowledged",
                            "central_run_id": "central-run-1",
                            "lan_available": True,
                            "original_deleted_at_local": "2026-03-31T12:00:00",
                        },
                        "segments": [
                            {
                                "segment_id": "idle-001",
                                "kind": "idle",
                                "start_offset_sec": 10.0,
                                "stop_offset_sec": 20.0,
                                "duration_sec": 10.0,
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
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            original_video_path.unlink()

            MODULE.refresh_catalog(root)
            run_id = MODULE.list_catalog_runs(root)[0]["run_id"]
            detail = MODULE.get_run_detail(root, run_id)
            self.assertEqual(detail["run"]["playback_status"], "ready_segments_only")
            self.assertEqual(detail["playback"]["status"], "ready_segments_only")
            self.assertEqual(detail["segments"][0]["playback_source_kind"], "local_derived")
            self.assertTrue(detail["segments"][0]["has_local_video"])
            self.assertTrue(detail["segments"][0]["video_url"].endswith("/segments/idle-001/video"))

    def test_http_segment_video_endpoint_serves_derived_segment_when_source_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_video_path = root / "demo.mp4"
            trace_path = root / "demo.trc"
            derived_root = root / "demo.derived"
            derived_root.mkdir()
            derived_path = derived_root / "idle-001_idle.mp4"
            manifest_path = root / "demo.run.json"

            original_video_path.write_bytes(b"original-source")
            derived_path.write_bytes(b"0123456789")
            sample_trace = Path(__file__).resolve().parents[1] / "data" / "samples"
            trace_path.write_bytes(next(sample_trace.glob("*.trc")).read_bytes())
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "demo-segment-video",
                        "source": "0",
                        "replay_manifest_version": "hybrid-replay.v1",
                        "replay_capabilities": ["trace_chapters", "trace_segments", "idle_skip_default"],
                        "video_path": str(original_video_path),
                        "trace_path": str(trace_path),
                        "started_at_local": "2026-03-24T14:00:00",
                        "stopped_at_local": "2026-03-24T14:05:00",
                        "duration_sec": 300,
                        "stop_reason": "process_exit",
                        "process_gate": "HxRun.exe",
                        "chapters": [
                            {
                                "chapter_id": "chapter-001",
                                "start_offset_sec": 10.0,
                                "label": "Incubation",
                                "kind": "span",
                                "phase_source": "trace",
                                "is_idle": True,
                            }
                        ],
                        "segments": [
                            {
                                "segment_id": "idle-001",
                                "kind": "idle",
                                "start_offset_sec": 10.0,
                                "stop_offset_sec": 20.0,
                                "duration_sec": 10.0,
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
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            original_video_path.unlink()

            MODULE.refresh_catalog(root)
            run_id = MODULE.list_catalog_runs(root)[0]["run_id"]
            handler = MODULE.make_handler(root, self.make_test_config())
            from http.server import ThreadingHTTPServer

            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/runs/{run_id}/segments/idle-001/video"
                request = urllib.request.Request(url, headers={"Range": "bytes=2-5"})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers.get("Content-Range"), "bytes 2-5/10")
                    self.assertEqual(response.read(), b"2345")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_http_live_profiles_returns_configured_cameras(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self.make_test_config()
            config["profiles"] = [
                {"id": "default", "label": "Top Cam", "source": 'dshow:video="Top Cam"', "framerate": 15, "video_size": "1280x720", "ffmpeg_path": ""},
                {"id": "side", "label": "Side Cam", "source": 'dshow:video="Side Cam"', "framerate": None, "video_size": None, "ffmpeg_path": ""},
            ]
            config["live"]["default_profile"] = "side"
            config["live"]["refresh_ms"] = 750
            handler = MODULE.make_handler(root, config)
            from http.server import ThreadingHTTPServer

            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/live/profiles"
                payload = json.loads(urllib.request.urlopen(url).read().decode("utf-8"))
                self.assertEqual(payload["default_profile"], "side")
                self.assertEqual(payload["refresh_ms"], 750)
                self.assertEqual(len(payload["items"]), 2)
                self.assertEqual(payload["items"][0]["label"], "Top Cam")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_http_live_frame_uses_capture_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = self.make_test_config()
            handler = MODULE.make_handler(root, config)
            from http.server import ThreadingHTTPServer

            original_capture = MODULE.capture_live_frame

            def fake_capture(_config: dict, profile_id: str | None = None) -> tuple[bytes, dict]:
                selected = profile_id or "default"
                return (b"\xff\xd8\xff\xd9", {"id": selected, "label": "Fake Cam", "source": "fake"})

            MODULE.capture_live_frame = fake_capture
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/live/frame.jpg?profile=default"
                with urllib.request.urlopen(url) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Content-Type"), "image/jpeg")
                    self.assertEqual(response.headers.get("X-Camera-Profile"), "default")
                    self.assertEqual(response.read(), b"\xff\xd8\xff\xd9")
            finally:
                MODULE.capture_live_frame = original_capture
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
