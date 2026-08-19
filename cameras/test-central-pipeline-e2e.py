#!/usr/bin/env python3
"""Synthetic end-to-end test for the central replay pipeline."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

import importlib.util

from replay_tags import TRACE_TAGS_VERSION


CAMERAS_DIR = Path(__file__).resolve().parent
STAGE_MODULE_PATH = CAMERAS_DIR / "stage-central-replay.py"
UPLOAD_HELPER_PATH = CAMERAS_DIR / "upload_central_replay.py"
SERVER_MODULE_PATH = CAMERAS_DIR / "central-replay-server.py"
TEST_STAGING_MODULE_PATH = CAMERAS_DIR / "test-central-staging.py"
TEST_UPLOAD_MODULE_PATH = CAMERAS_DIR / "test-central-upload.py"


def load_module(module_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STAGE_MODULE = load_module(STAGE_MODULE_PATH, "camera_stage_central_replay_e2e")
UPLOAD_HELPER = load_module(UPLOAD_HELPER_PATH, "camera_upload_central_replay_e2e")
SERVER_MODULE = load_module(SERVER_MODULE_PATH, "camera_central_replay_server_e2e")
TEST_STAGING_MODULE = load_module(TEST_STAGING_MODULE_PATH, "camera_test_central_staging_e2e")
TEST_UPLOAD_MODULE = load_module(TEST_UPLOAD_MODULE_PATH, "camera_test_central_upload_e2e")


class CentralReplayPipelineE2ETests(unittest.TestCase):
    def fetch_json(self, url: str) -> dict:
        with urllib.request.urlopen(url) as response:
            return json.loads(response.read().decode("utf-8"))

    def start_server(self, upload_root: Path):
        catalog_path = upload_root / UPLOAD_HELPER.CENTRAL_CATALOG_FILENAME
        handler = SERVER_MODULE.make_handler(upload_root, catalog_path)
        from http.server import ThreadingHTTPServer

        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_synthetic_pipeline_covers_staging_upload_ack_and_browse(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs_root = root / "runs"
            staging_root = root / "staging"
            upload_root = root / "central"

            staging_helper = TEST_STAGING_MODULE.CentralStagingTests()
            upload_helper = TEST_UPLOAD_MODULE.CentralUploadTests()
            config_path, local_path = staging_helper.write_config(root, runs_root, staging_root)
            manifest_path = upload_helper.write_ready_run(runs_root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            with Path(manifest["trace_path"]).open("a", encoding="utf-8") as handle:
                handle.write(
                    "\n2026-04-10 10:00:10 Barcode for NO1 pillar plate is: E2E-BC-900\n"
                    "2026-04-10 10:00:11 Step aborted\n"
                )

            stage_result = STAGE_MODULE.stage_runs(
                config_path=config_path,
                local_config_path=local_path,
                runs_root=runs_root,
                staging_root=staging_root,
                limit=0,
                restage=False,
            )
            self.assertEqual(stage_result["staged_run_count"], 1)
            self.assertEqual(stage_result["skipped_run_count"], 0)

            stage_item = stage_result["items"][0]
            self.assertEqual(stage_item["action"], "staged")
            payload_path = Path(stage_item["payload_path"])
            self.assertTrue(payload_path.exists())
            upload_payload = json.loads(payload_path.read_text(encoding="utf-8"))
            self.assertEqual(upload_payload["run"]["replay_status"], "ready")
            self.assertEqual(len(upload_payload["artifacts"]), 3)
            self.assertEqual(upload_payload["run"]["run_tags_version"], TRACE_TAGS_VERSION)
            self.assertIn("E2E-BC-900", upload_payload["run"]["run_tag_summary"]["pillar_plate_barcodes"])
            self.assertEqual(upload_payload["run"]["run_tag_summary"]["outcome"], "aborted")

            upload_result = UPLOAD_HELPER.upload_staged_runs(
                config_path=config_path,
                local_config_path=local_path,
                staging_root=staging_root,
                upload_root=upload_root,
                limit=0,
                batch_id="",
            )
            self.assertEqual(upload_result["uploaded_run_count"], 1)
            self.assertEqual(upload_result["failed_run_count"], 0)

            upload_item = upload_result["items"][0]
            self.assertEqual(upload_item["action"], "acknowledged")
            central_run_id = upload_item["central_run_id"]
            ack_path = Path(upload_item["ack_path"])
            self.assertTrue(ack_path.exists())

            ack_payload = json.loads(ack_path.read_text(encoding="utf-8"))
            self.assertEqual(ack_payload["status"], "acknowledged")
            self.assertEqual(ack_payload["central_run_id"], central_run_id)
            self.assertEqual(ack_payload["ingest_batch_id"], upload_result["ingest_batch_id"])
            self.assertEqual(len(ack_payload["stored_artifacts"]), 3)

            staging_catalog = staging_root / STAGE_MODULE.CATALOG_FILENAME
            with closing(STAGE_MODULE.get_db_connection(staging_catalog)) as conn:
                stage_row = conn.execute(
                    """
                    SELECT upload_status, central_run_id, ack_path, upload_batch_id
                    FROM staged_runs
                    LIMIT 1
                    """
                ).fetchone()
            self.assertEqual(stage_row["upload_status"], "acknowledged")
            self.assertEqual(stage_row["central_run_id"], central_run_id)
            self.assertEqual(Path(stage_row["ack_path"]), ack_path)
            self.assertEqual(stage_row["upload_batch_id"], upload_result["ingest_batch_id"])

            central_catalog = upload_root / UPLOAD_HELPER.CENTRAL_CATALOG_FILENAME
            with closing(sqlite3.connect(central_catalog)) as conn:
                run_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
                item_count = conn.execute("SELECT COUNT(*) FROM ingest_items").fetchone()[0]
                stored_tags = conn.execute(
                    "SELECT run_tags_version, run_tags_json, run_outcome_tag, primary_barcode FROM runs"
                ).fetchone()
            self.assertEqual(run_count, 1)
            self.assertEqual(artifact_count, 3)
            self.assertEqual(item_count, 3)
            self.assertEqual(stored_tags[0], TRACE_TAGS_VERSION)
            self.assertTrue(any(tag["value"] == "E2E-BC-900" for tag in json.loads(stored_tags[1])))
            self.assertEqual(stored_tags[2], "aborted")
            self.assertTrue(stored_tags[3])

            server, thread = self.start_server(upload_root)
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                workstation_id = upload_payload["workstation"]["workstation_id"]

                with urllib.request.urlopen(f"{base_url}/") as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("text/html", response.headers.get("Content-Type", ""))
                    self.assertIn("<h1>Central Replay</h1>", response.read().decode("utf-8"))

                with urllib.request.urlopen(f"{base_url}/static/central.js") as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("javascript", response.headers.get("Content-Type", ""))

                runs = self.fetch_json(f"{base_url}/api/runs?workstation_id={workstation_id}&limit=5")
                self.assertEqual(len(runs["items"]), 1)
                self.assertEqual(runs["items"][0]["central_run_id"], central_run_id)

                tagged_runs = self.fetch_json(
                    f"{base_url}/api/runs?{urlencode({'query': 'e2e-bc-900', 'outcome': 'aborted'})}"
                )
                self.assertEqual([item["central_run_id"] for item in tagged_runs["items"]], [central_run_id])
                self.assertEqual(tagged_runs["items"][0]["run_outcome"], "aborted")

                no_match = self.fetch_json(f"{base_url}/api/runs?{urlencode({'query': 'not-this-barcode'})}")
                self.assertEqual(no_match["items"], [])

                detail = self.fetch_json(f"{base_url}/api/runs/{central_run_id}")
                self.assertEqual(detail["run"]["central_run_id"], central_run_id)
                self.assertEqual(detail["workstation"]["workstation_id"], workstation_id)
                self.assertEqual(len(detail["artifacts"]), 3)
                self.assertEqual(detail["run"]["run_outcome"], "aborted")
                self.assertTrue(any(tag["value"] == "E2E-BC-900" for tag in detail["run"]["run_tags"]))

                artifacts = self.fetch_json(f"{base_url}/api/runs/{central_run_id}/artifacts")
                self.assertEqual(len(artifacts["items"]), 3)

                events = self.fetch_json(f"{base_url}/api/runs/{central_run_id}/trace-events")
                self.assertGreater(events["item_count"], 10)

                request = urllib.request.Request(
                    f"{base_url}{detail['run']['video_url']}",
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
