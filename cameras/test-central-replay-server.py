#!/usr/bin/env python3
"""HTTP smoke tests for the first central replay browse server."""
from __future__ import annotations

import datetime as dt
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

import importlib.util

from replay_tags import TRACE_TAGS_VERSION


CAMERAS_DIR = Path(__file__).resolve().parent
UPLOAD_HELPER_PATH = CAMERAS_DIR / "upload_central_replay.py"
SERVER_MODULE_PATH = CAMERAS_DIR / "central-replay-server.py"
TEST_UPLOAD_MODULE_PATH = CAMERAS_DIR / "test-central-upload.py"


def load_module(module_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SERVER_MODULE = load_module(SERVER_MODULE_PATH, "camera_central_replay_server_tests")
TEST_UPLOAD_MODULE = load_module(TEST_UPLOAD_MODULE_PATH, "camera_test_central_upload_helpers")
UPLOAD_HELPER = load_module(UPLOAD_HELPER_PATH, "camera_upload_central_replay_helper_tests")


class CentralReplayServerTests(unittest.TestCase):
    def test_stream_file_handle_uses_bounded_reads(self) -> None:
        class RecordingBytesIO(io.BytesIO):
            def __init__(self, payload: bytes):
                super().__init__(payload)
                self.read_sizes: list[int] = []

            def read(self, size: int = -1) -> bytes:
                self.read_sizes.append(size)
                if size < 0:
                    raise AssertionError("stream_file_handle requested an unbounded read")
                return super().read(size)

        payload = b"0123456789"
        reader = RecordingBytesIO(payload)
        writer = io.BytesIO()
        SERVER_MODULE.stream_file_handle(reader, writer, chunk_size=4)
        self.assertEqual(writer.getvalue(), payload)
        self.assertEqual(reader.read_sizes, [4, 4, 4, 4])

        ranged_reader = RecordingBytesIO(payload)
        ranged_writer = io.BytesIO()
        SERVER_MODULE.stream_file_handle(ranged_reader, ranged_writer, start=2, byte_count=5, chunk_size=4)
        self.assertEqual(ranged_writer.getvalue(), b"23456")
        self.assertEqual(ranged_reader.read_sizes, [4, 1])

    def test_runtime_settings_use_server_config_and_catalog_convention(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            upload_root = root / "central"
            log_path = root / "logs" / "central.log"
            config_path = root / "central-replay-server.json"
            config_path.write_text(
                json.dumps(
                    {
                        "server": {
                            "host": "0.0.0.0",
                            "port": 6090,
                            "site_name": "Fixture Replay",
                            "log_path": str(log_path),
                            "healthcheck_path": "/healthz",
                        },
                        "storage": {
                            "upload_root": str(upload_root),
                        },
                    }
                ),
                encoding="utf-8",
            )

            parser = SERVER_MODULE.build_argument_parser()
            runtime = SERVER_MODULE.resolve_runtime_settings(
                parser.parse_args(
                    [
                        "--server-config",
                        str(config_path),
                        "--server-local-config",
                        str(root / "central-replay-server.local.json"),
                    ]
                )
            )

            self.assertEqual(runtime.host, "0.0.0.0")
            self.assertEqual(runtime.port, 6090)
            self.assertEqual(runtime.site_name, "Fixture Replay")
            self.assertEqual(runtime.healthcheck_path, "/healthz")
            self.assertEqual(runtime.workstation_heartbeat_timeout_sec, 30.0)
            self.assertEqual(runtime.upload_root, str(upload_root.resolve()))
            self.assertEqual(
                runtime.catalog_path,
                str((upload_root / UPLOAD_HELPER.CENTRAL_CATALOG_FILENAME).resolve()),
            )
            validation = SERVER_MODULE.validate_runtime_settings(runtime)
            self.assertEqual(validation["errors"], [])

    def create_catalog_fixture(self, root: Path) -> tuple[Path, str]:
        runs_root = root / "runs"
        staging_root = root / "staging"
        upload_root = root / "central"
        helper = TEST_UPLOAD_MODULE.CentralUploadTests()
        config_path, local_path = helper.write_config(root, runs_root, staging_root, upload_root)
        helper.stage_one_ready_run(
            config_path,
            local_path,
            runs_root,
            staging_root,
            trace_lines=[
                "2026-04-10 10:00:10 Barcode for NO1 pillar plate is: CENTRAL-BC-001",
                "2026-04-10 10:00:11 Step aborted",
            ],
        )
        result = UPLOAD_HELPER.upload_staged_runs(
            config_path=config_path,
            local_config_path=local_path,
            staging_root=staging_root,
            upload_root=upload_root,
            limit=0,
            batch_id="",
        )
        central_run_id = result["items"][0]["central_run_id"]
        return upload_root, central_run_id

    def start_server(self, upload_root: Path, *, heartbeat_timeout_sec: float = 30.0):
        catalog_path = upload_root / UPLOAD_HELPER.CENTRAL_CATALOG_FILENAME
        handler = SERVER_MODULE.make_handler(
            upload_root,
            catalog_path,
            workstation_heartbeat_timeout_sec=heartbeat_timeout_sec,
        )
        from http.server import ThreadingHTTPServer

        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def add_uploaded_run(self, upload_root: Path, *, suffix: str, started_at_local: str) -> None:
        catalog_path = upload_root / UPLOAD_HELPER.CENTRAL_CATALOG_FILENAME
        with closing(sqlite3.connect(catalog_path)) as conn:
            conn.row_factory = sqlite3.Row
            template = conn.execute(
                """
                SELECT *
                FROM runs
                ORDER BY COALESCE(started_at_local, '') DESC, last_ingested_utc DESC
                LIMIT 1
                """
                ).fetchone()
            assert template is not None
            conn.execute(
                """
                INSERT INTO runs (
                    central_run_id,
                    workstation_id,
                    camera_profile_id,
                    latest_ingest_batch_id,
                    local_run_id,
                    local_manifest_path,
                    label,
                    source_name,
                    process_gate,
                    stop_reason,
                    started_at_local,
                    stopped_at_local,
                    duration_sec,
                    hamilton_log_dir,
                    hamilton_log_glob,
                    trace_pairing_delta_sec,
                    replay_status,
                    ready_artifact_count,
                    required_artifact_count,
                    first_ingested_utc,
                    last_ingested_utc,
                    archived_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"central-run-{suffix}",
                    template["workstation_id"],
                    template["camera_profile_id"],
                    template["latest_ingest_batch_id"],
                    f"local-run-{suffix}",
                    template["local_manifest_path"],
                    f"run-{suffix}",
                    template["source_name"],
                    template["process_gate"],
                    template["stop_reason"],
                    started_at_local,
                    started_at_local,
                    template["duration_sec"],
                    template["hamilton_log_dir"],
                    template["hamilton_log_glob"],
                    template["trace_pairing_delta_sec"],
                    template["replay_status"],
                    template["ready_artifact_count"],
                    template["required_artifact_count"],
                    template["first_ingested_utc"],
                    template["last_ingested_utc"],
                    template["archived_at_utc"],
                ),
            )
            conn.commit()

    def fetch_json(self, url: str) -> dict:
        with urllib.request.urlopen(url) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, url: str, payload: dict) -> dict:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_catalog_api_lists_runs_and_workstations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, central_run_id = self.create_catalog_fixture(Path(tmpdir))
            server, thread = self.start_server(upload_root)
            try:
                health = self.fetch_json(f"http://127.0.0.1:{server.server_port}/api/healthz")
                self.assertEqual(health["status"], "ok")
                self.assertEqual(health["counts"]["runs"], 1)
                self.assertEqual(health["counts"]["workstations"], 1)

                runs = self.fetch_json(f"http://127.0.0.1:{server.server_port}/api/runs")
                self.assertEqual(len(runs["items"]), 1)
                self.assertEqual(runs["items"][0]["central_run_id"], central_run_id)

                workstations = self.fetch_json(f"http://127.0.0.1:{server.server_port}/api/workstations")
                self.assertEqual(len(workstations["items"]), 1)
                self.assertTrue(workstations["items"][0]["workstation_id"])
                self.assertFalse(workstations["items"][0]["is_online"])
                self.assertEqual(workstations["items"][0]["current_state"], "offline")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_runs_limit_is_applied_before_loading_all_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, _ = self.create_catalog_fixture(Path(tmpdir))
            self.add_uploaded_run(upload_root, suffix="older", started_at_local="2026-04-18T09:00:00")
            self.add_uploaded_run(upload_root, suffix="newer", started_at_local="2026-04-20T09:00:00")
            server, thread = self.start_server(upload_root)
            try:
                runs = self.fetch_json(f"http://127.0.0.1:{server.server_port}/api/runs?limit=1")
                self.assertEqual(len(runs["items"]), 1)
                self.assertEqual(runs["items"][0]["central_run_id"], "central-run-newer")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_run_detail_and_trace_events_are_served(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, central_run_id = self.create_catalog_fixture(Path(tmpdir))
            server, thread = self.start_server(upload_root)
            try:
                detail = self.fetch_json(f"http://127.0.0.1:{server.server_port}/api/runs/{central_run_id}")
                self.assertEqual(detail["run"]["central_run_id"], central_run_id)
                self.assertEqual(len(detail["artifacts"]), 3)
                self.assertTrue(detail["run"]["video_url"].startswith("/media/"))
                self.assertEqual(detail["run"]["replay_manifest_version"], "hybrid-replay.v1")
                self.assertIn("trace_segments", detail["run"]["replay_capabilities"])
                self.assertGreaterEqual(detail["run"]["segment_count"], 1)
                self.assertEqual(detail["run"]["run_tags_version"], TRACE_TAGS_VERSION)
                self.assertEqual(detail["run"]["run_outcome"], "aborted")
                self.assertIn("CENTRAL-BC-001", detail["run"]["run_tag_summary"]["pillar_plate_barcodes"])
                self.assertTrue(
                    any(
                        tag["key"] == "pillar_plate_barcode" and tag["value"] == "CENTRAL-BC-001"
                        for tag in detail["run"]["run_tags"]
                    )
                )

                events = self.fetch_json(f"http://127.0.0.1:{server.server_port}/api/runs/{central_run_id}/trace-events")
                self.assertGreater(events["item_count"], 10)
                self.assertIn("Start method - progress", "\n".join(item["line"] for item in events["items"][:12]))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_runs_api_filters_by_query_and_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, central_run_id = self.create_catalog_fixture(Path(tmpdir))
            server, thread = self.start_server(upload_root)
            try:
                base_url = f"http://127.0.0.1:{server.server_port}/api/runs"
                matching = self.fetch_json(f"{base_url}?{urlencode({'query': 'central-bc-001', 'outcome': 'ABORTED'})}")
                self.assertEqual([item["central_run_id"] for item in matching["items"]], [central_run_id])
                self.assertEqual(matching["filters"]["query"], "central-bc-001")
                self.assertEqual(matching["filters"]["outcome"], "ABORTED")

                no_query_match = self.fetch_json(f"{base_url}?{urlencode({'query': 'missing-barcode'})}")
                self.assertEqual(no_query_match["items"], [])
                no_outcome_match = self.fetch_json(f"{base_url}?{urlencode({'outcome': 'ok'})}")
                self.assertEqual(no_outcome_match["items"], [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_stored_historical_trace_tags_are_backfilled_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, central_run_id = self.create_catalog_fixture(Path(tmpdir))
            catalog_path = upload_root / UPLOAD_HELPER.CENTRAL_CATALOG_FILENAME
            import sqlite3

            with closing(sqlite3.connect(catalog_path)) as conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET run_tags_version = '', run_tags_json = '[]', run_tag_summary_json = '{}',
                        run_tag_search_text = '', run_outcome_tag = '', primary_barcode = ''
                    WHERE central_run_id = ?
                    """,
                    (central_run_id,),
                )
                conn.commit()

            self.assertEqual(SERVER_MODULE.backfill_run_tags(upload_root, catalog_path), 1)
            self.assertEqual(SERVER_MODULE.backfill_run_tags(upload_root, catalog_path), 0)
            with closing(sqlite3.connect(catalog_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM runs WHERE central_run_id = ?", (central_run_id,)).fetchone()
            self.assertEqual(row["run_tags_version"], TRACE_TAGS_VERSION)
            self.assertEqual(row["run_outcome_tag"], "aborted")
            self.assertIn("central-bc-001", row["run_tag_search_text"])
            self.assertTrue(any(tag["value"] == "CENTRAL-BC-001" for tag in json.loads(row["run_tags_json"])))

    def test_media_endpoint_supports_byte_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, central_run_id = self.create_catalog_fixture(Path(tmpdir))
            server, thread = self.start_server(upload_root)
            try:
                detail = self.fetch_json(f"http://127.0.0.1:{server.server_port}/api/runs/{central_run_id}")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}{detail['run']['video_url']}",
                    headers={"Range": "bytes=4-7"},
                )
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers.get("Content-Range"), "bytes 4-7/22")
                    self.assertEqual(response.read(), b"ftyp")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_media_endpoint_serves_full_files_without_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, central_run_id = self.create_catalog_fixture(Path(tmpdir))
            server, thread = self.start_server(upload_root)
            try:
                detail = self.fetch_json(f"http://127.0.0.1:{server.server_port}/api/runs/{central_run_id}")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}{detail['run']['video_url']}"
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get("Content-Length"), "22")
                    self.assertEqual(response.read(), b"\x00\x00\x00 ftypisomdemo-video")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_status_posts_surface_pending_runs_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root = Path(tmpdir) / "central"
            upload_root.mkdir(parents=True, exist_ok=True)
            server, thread = self.start_server(upload_root)
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                heartbeat_payload = {
                    "workstation": {
                        "workstation_id": "bench-1",
                        "hostname": "BENCH-1",
                        "machine_alias": "Bench 1",
                        "repo_root": "C:\\camera-tools",
                        "local_ip": "192.168.70.55",
                        "software_version": "camera-daemon.jdp.v1",
                    },
                    "camera_profile": {
                        "profile_id": "default",
                        "profile_key": "default",
                        "profile_label": "Top Camera",
                        "source_name": "Arducam USB Camera",
                    },
                    "status": {
                        "state": "idle",
                    },
                }
                heartbeat_result = self.post_json(f"{base_url}/api/workstations/heartbeat", heartbeat_payload)
                self.assertEqual(heartbeat_result["workstation_id"], "bench-1")

                run_payload = {
                    **heartbeat_payload,
                    "status": {
                        "state": "pending_upload",
                        "upload_phase": "",
                    },
                    "run": {
                        "local_run_id": "run-123",
                        "label": "Top Camera",
                        "source_name": "Arducam USB Camera",
                        "process_gate": "HxRun.exe",
                        "stop_reason": "process_exit",
                        "started_at_local": "2026-04-19T09:15:00",
                        "stopped_at_local": "2026-04-19T09:25:00",
                        "duration_sec": 600,
                        "hamilton_log_dir": "C:\\Program Files (x86)\\HAMILTON\\LogFiles",
                        "hamilton_log_glob": "*.trc",
                        "trace_pairing_delta_sec": 1.2,
                        "local_manifest_path": "C:\\camera-tools\\runs\\run-123.run.json",
                        "local_video_path": "C:\\camera-tools\\runs\\run-123.mp4",
                        "local_trace_path": "C:\\camera-tools\\runs\\run-123.trc",
                        "replay_status": "pending_upload",
                    },
                }
                run_result = self.post_json(f"{base_url}/api/runs/status", run_payload)
                pending_run_id = run_result["pending_run_id"]
                self.assertTrue(pending_run_id.startswith("pending-run:"))

                workstations = self.fetch_json(f"{base_url}/api/workstations")
                self.assertTrue(workstations["items"][0]["is_online"])
                self.assertEqual(workstations["items"][0]["current_state"], "pending_upload")
                self.assertEqual(workstations["items"][0]["local_ip"], "192.168.70.55")

                runs = self.fetch_json(f"{base_url}/api/runs")
                self.assertEqual(len(runs["items"]), 1)
                self.assertEqual(runs["items"][0]["central_run_id"], pending_run_id)
                self.assertEqual(runs["items"][0]["replay_status"], "pending_upload")
                self.assertEqual(runs["items"][0]["video_url"], "")

                detail = self.fetch_json(f"{base_url}/api/runs/{pending_run_id}")
                self.assertEqual(detail["run"]["trace_events_url"], "")
                self.assertEqual(len(detail["artifacts"]), 3)

                stale_utc = (
                    dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
                catalog_path = upload_root / UPLOAD_HELPER.CENTRAL_CATALOG_FILENAME
                with closing(sqlite3.connect(catalog_path)) as conn:
                    conn.execute(
                        """
                        UPDATE workstation_runtime_status
                        SET last_heartbeat_utc = ?
                        WHERE workstation_id = ?
                        """,
                        (stale_utc, "bench-1"),
                    )
                    conn.commit()

                workstations = self.fetch_json(f"{base_url}/api/workstations")
                self.assertFalse(workstations["items"][0]["is_online"])
                self.assertEqual(workstations["items"][0]["current_state"], "offline")
                self.assertEqual(workstations["items"][0]["last_reported_state"], "pending_upload")
                self.assertEqual(workstations["items"][0]["upload_phase"], "")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_runs_can_filter_by_camera_profile_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root = Path(tmpdir) / "central"
            upload_root.mkdir(parents=True, exist_ok=True)
            server, thread = self.start_server(upload_root)
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                workstation = {
                    "workstation_id": "bench-1",
                    "hostname": "BENCH-1",
                    "machine_alias": "Bench 1",
                    "repo_root": "C:\\camera-tools",
                    "local_ip": "192.168.70.55",
                    "software_version": "camera-daemon.jdp.v1",
                }

                default_payload = {
                    "workstation": workstation,
                    "camera_profile": {
                        "profile_id": "default",
                        "profile_key": "default",
                        "profile_label": "Top Camera",
                        "source_name": "Arducam USB Camera",
                    },
                    "status": {
                        "state": "pending_upload",
                        "upload_phase": "",
                    },
                    "run": {
                        "local_run_id": "run-top",
                        "label": "Top Camera",
                        "source_name": "Arducam USB Camera",
                        "process_gate": "HxRun.exe",
                        "stop_reason": "process_exit",
                        "started_at_local": "2026-04-19T09:15:00",
                        "stopped_at_local": "2026-04-19T09:25:00",
                        "duration_sec": 600,
                        "hamilton_log_dir": "C:\\Program Files (x86)\\HAMILTON\\LogFiles",
                        "hamilton_log_glob": "*.trc",
                        "trace_pairing_delta_sec": 1.2,
                        "local_manifest_path": "C:\\camera-tools\\runs\\run-top.run.json",
                        "local_video_path": "C:\\camera-tools\\runs\\run-top.mp4",
                        "local_trace_path": "C:\\camera-tools\\runs\\run-top.trc",
                        "replay_status": "pending_upload",
                    },
                }
                side_payload = {
                    **default_payload,
                    "camera_profile": {
                        "profile_id": "side",
                        "profile_key": "side",
                        "profile_label": "Side Camera",
                        "source_name": "Logitech C920",
                    },
                    "run": {
                        **default_payload["run"],
                        "local_run_id": "run-side",
                        "label": "Side Camera",
                        "local_manifest_path": "C:\\camera-tools\\runs\\run-side.run.json",
                        "local_video_path": "C:\\camera-tools\\runs\\run-side.mp4",
                        "local_trace_path": "C:\\camera-tools\\runs\\run-side.trc",
                    },
                }

                self.post_json(f"{base_url}/api/runs/status", default_payload)
                self.post_json(f"{base_url}/api/runs/status", side_payload)

                runs = self.fetch_json(f"{base_url}/api/runs?camera_profile_id=bench-1:side")
                self.assertEqual(runs["filters"]["camera_profile_id"], "bench-1:side")
                self.assertEqual(len(runs["items"]), 1)
                self.assertEqual(runs["items"][0]["camera_profile_id"], "bench-1:side")
                self.assertEqual(runs["items"][0]["label"], "Side Camera")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_uploaded_run_detail_reports_missing_required_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root, central_run_id = self.create_catalog_fixture(Path(tmpdir))
            catalog_path = upload_root / UPLOAD_HELPER.CENTRAL_CATALOG_FILENAME
            with closing(sqlite3.connect(catalog_path)) as conn:
                conn.execute(
                    """
                    DELETE FROM artifacts
                    WHERE central_run_id = ? AND artifact_type = 'trace_trc'
                    """,
                    (central_run_id,),
                )
                conn.commit()

            server, thread = self.start_server(upload_root)
            try:
                detail = self.fetch_json(f"http://127.0.0.1:{server.server_port}/api/runs/{central_run_id}")
                self.assertEqual(detail["run"]["missing_required_artifact_count"], 1)
                self.assertEqual(detail["run"]["missing_required_artifact_types"], ["trace_trc"])
                self.assertFalse(detail["run"]["has_all_required_artifacts"])

                trace_artifact = next(
                    item for item in detail["artifacts"] if item["artifact_type"] == "trace_trc"
                )
                self.assertFalse(trace_artifact["is_ready"])
                self.assertEqual(trace_artifact["storage_relpath"], "")
                self.assertEqual(trace_artifact["media_url"], "")

                runs = self.fetch_json(f"http://127.0.0.1:{server.server_port}/api/runs")
                self.assertEqual(runs["items"][0]["missing_required_artifact_count"], 1)
                self.assertEqual(runs["items"][0]["missing_required_artifact_types"], ["trace_trc"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
