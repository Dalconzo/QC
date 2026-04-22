#!/usr/bin/env python3
"""HTTP smoke tests for the first central replay browse server."""
from __future__ import annotations

import datetime as dt
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

import importlib.util


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
        helper.stage_one_ready_run(config_path, local_path, runs_root, staging_root)
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

                events = self.fetch_json(f"http://127.0.0.1:{server.server_port}/api/runs/{central_run_id}/trace-events")
                self.assertGreater(events["item_count"], 10)
                self.assertIn("Start method - progress", "\n".join(item["line"] for item in events["items"][:12]))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

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


if __name__ == "__main__":
    unittest.main()
