#!/usr/bin/env python3
"""HTTP smoke tests for the first central replay browse server."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
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

    def start_server(self, upload_root: Path):
        catalog_path = upload_root / UPLOAD_HELPER.CENTRAL_CATALOG_FILENAME
        handler = SERVER_MODULE.make_handler(upload_root, catalog_path)
        from http.server import ThreadingHTTPServer

        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def fetch_json(self, url: str) -> dict:
        with urllib.request.urlopen(url) as response:
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


if __name__ == "__main__":
    unittest.main()
