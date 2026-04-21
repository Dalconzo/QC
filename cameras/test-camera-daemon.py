#!/usr/bin/env python3
"""Smoke tests for the workstation-local camera daemon supervisor."""
from __future__ import annotations

import importlib.util
import json
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


DAEMON = load_module("camera_daemon_module", CAMERAS_DIR / "camera-daemon.py")


class CameraDaemonTests(unittest.TestCase):
    def test_supervisor_waits_then_runs_one_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            runs_root = root / "runs"
            logs_root = root / "logs"
            logs_root.mkdir()

            config_path = root / "camera-recorder.json"
            local_path = root / "camera-recorder.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "hamilton": {
                            "log_dir": str(log_dir),
                            "process_name": "HxRun.exe",
                        },
                        "storage": {
                            "runs_root": str(runs_root),
                            "recorder_log_dir": str(logs_root),
                        },
                        "recorder": {
                            "stop_file": str(root / "recorder.stop"),
                        },
                        "daemon": {
                            "stop_file": str(root / "daemon.stop"),
                            "pid_file": str(root / "daemon.pid"),
                            "status_path": str(root / "daemon-status.json"),
                            "log_path": str(root / "daemon.log"),
                            "idle_poll_sec": 0.05,
                            "heartbeat_sec": 0.05,
                            "relaunch_delay_sec": 0.0,
                        },
                        "profiles": [
                            {
                                "id": "default",
                                "label": "BenchCam",
                                "source": 'dshow:video="Bench Cam"',
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            marker_path = root / "mock-recorder.json"
            recorder_script = root / "mock-recorder.py"
            recorder_script.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import pathlib
                    import sys
                    import time

                    marker = pathlib.Path(r"{marker_path}")
                    marker.write_text(json.dumps({{"argv": sys.argv[1:]}}), encoding="utf-8")
                    time.sleep(0.15)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            poll_calls = {"count": 0}

            def fake_is_running(_name: str) -> bool:
                poll_calls["count"] += 1
                return poll_calls["count"] >= 3

            rc = DAEMON.run_supervisor(
                config_path=config_path,
                local_config_path=local_path,
                profile_id="default",
                source_override="",
                out_dir_override="",
                label_override="",
                stop_file=root / "daemon.stop",
                pid_file=root / "daemon.pid",
                status_path=root / "daemon-status.json",
                daemon_log_path=root / "daemon.log",
                recorder_log_path=root / "recorder.log",
                idle_poll_sec=0.05,
                heartbeat_sec=0.05,
                relaunch_delay_sec=0.0,
                idle_timeout_sec=2,
                run_once=True,
                max_cycles=0,
                recorder_script=recorder_script,
                is_process_running_fn=fake_is_running,
            )

            self.assertEqual(rc, 0)
            self.assertTrue(marker_path.exists())
            self.assertFalse((root / "daemon.pid").exists())

            status = json.loads((root / "daemon-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "stopped")
            self.assertEqual(status["reason"], "run_limit")
            self.assertEqual(status["cycle_count"], 1)

            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            argv = " ".join(marker["argv"])
            self.assertIn("--start-when-exe HxRun.exe", argv)
            self.assertIn("--stop-when-exe HxRun.exe", argv)

    def test_supervisor_does_not_hot_loop_while_process_stays_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            runs_root = root / "runs"
            logs_root = root / "logs"
            logs_root.mkdir()

            config_path = root / "camera-recorder.json"
            local_path = root / "camera-recorder.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "hamilton": {
                            "log_dir": str(log_dir),
                            "process_name": "HxRun.exe",
                        },
                        "storage": {
                            "runs_root": str(runs_root),
                            "recorder_log_dir": str(logs_root),
                        },
                        "recorder": {
                            "stop_file": str(root / "recorder.stop"),
                        },
                        "daemon": {
                            "stop_file": str(root / "daemon.stop"),
                            "pid_file": str(root / "daemon.pid"),
                            "status_path": str(root / "daemon-status.json"),
                            "log_path": str(root / "daemon.log"),
                            "idle_poll_sec": 0.02,
                            "heartbeat_sec": 0.02,
                            "relaunch_delay_sec": 0.0,
                        },
                        "profiles": [
                            {
                                "id": "default",
                                "label": "BenchCam",
                                "source": 'dshow:video="Bench Cam"',
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            counter_path = root / "launch-count.txt"
            stop_file = root / "daemon.stop"
            recorder_script = root / "mock-recorder.py"
            recorder_script.write_text(
                textwrap.dedent(
                    f"""
                    import pathlib
                    import time

                    counter_path = pathlib.Path(r"{counter_path}")
                    count = int(counter_path.read_text(encoding="utf-8") or "0") if counter_path.exists() else 0
                    count += 1
                    counter_path.write_text(str(count), encoding="utf-8")
                    time.sleep(0.05)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            calls = {"count": 0}

            def fake_is_running(_name: str) -> bool:
                calls["count"] += 1
                if calls["count"] == 1:
                    return False
                if calls["count"] <= 15:
                    return True
                stop_file.write_text("stop", encoding="utf-8")
                return False

            rc = DAEMON.run_supervisor(
                config_path=config_path,
                local_config_path=local_path,
                profile_id="default",
                source_override="",
                out_dir_override="",
                label_override="",
                stop_file=stop_file,
                pid_file=root / "daemon.pid",
                status_path=root / "daemon-status.json",
                daemon_log_path=root / "daemon.log",
                recorder_log_path=root / "recorder.log",
                idle_poll_sec=0.02,
                heartbeat_sec=0.02,
                relaunch_delay_sec=0.0,
                idle_timeout_sec=2,
                run_once=False,
                max_cycles=0,
                recorder_script=recorder_script,
                is_process_running_fn=fake_is_running,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(counter_path.read_text(encoding="utf-8").strip(), "1")

    def test_supervisor_runs_auto_upload_after_successful_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            runs_root = root / "runs"
            logs_root = root / "logs"
            logs_root.mkdir()

            config_path = root / "camera-recorder.json"
            local_path = root / "camera-recorder.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "hamilton": {
                            "log_dir": str(log_dir),
                            "process_name": "HxRun.exe",
                        },
                        "storage": {
                            "runs_root": str(runs_root),
                            "recorder_log_dir": str(logs_root),
                        },
                        "central_ingest": {
                            "staging_root": str(root / "staging"),
                            "upload_root": str(root / "upload"),
                            "transport": "filesystem",
                            "auto_upload_on_run_complete": True,
                        },
                        "recorder": {
                            "stop_file": str(root / "recorder.stop"),
                        },
                        "daemon": {
                            "stop_file": str(root / "daemon.stop"),
                            "pid_file": str(root / "daemon.pid"),
                            "status_path": str(root / "daemon-status.json"),
                            "log_path": str(root / "daemon.log"),
                            "idle_poll_sec": 0.05,
                            "heartbeat_sec": 0.05,
                            "relaunch_delay_sec": 0.0,
                        },
                        "profiles": [
                            {
                                "id": "default",
                                "label": "BenchCam",
                                "source": 'dshow:video="Bench Cam"',
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            recorder_script = root / "mock-recorder.py"
            recorder_script.write_text("import time\ntime.sleep(0.05)\n", encoding="utf-8")

            calls: list[dict] = []

            def fake_post_run_ingest(**kwargs):
                calls.append(kwargs)
                return {
                    "stage": {
                        "batch_id": "batch-auto-1",
                        "staged_run_count": 1,
                        "skipped_run_count": 0,
                    },
                    "upload": {
                        "ingest_batch_id": "ingest-auto-1",
                        "uploaded_run_count": 1,
                        "failed_run_count": 0,
                    },
                    "cleanup": {
                        "deleted_run_count": 1,
                        "deleted_bytes": 2048,
                        "eligible_run_count": 0,
                    },
                }

            poll_calls = {"count": 0}

            def fake_is_running(_name: str) -> bool:
                poll_calls["count"] += 1
                return poll_calls["count"] >= 2

            rc = DAEMON.run_supervisor(
                config_path=config_path,
                local_config_path=local_path,
                profile_id="default",
                source_override="",
                out_dir_override="",
                label_override="",
                stop_file=root / "daemon.stop",
                pid_file=root / "daemon.pid",
                status_path=root / "daemon-status.json",
                daemon_log_path=root / "daemon.log",
                recorder_log_path=root / "recorder.log",
                idle_poll_sec=0.05,
                heartbeat_sec=0.05,
                relaunch_delay_sec=0.0,
                idle_timeout_sec=2,
                run_once=True,
                max_cycles=0,
                recorder_script=recorder_script,
                is_process_running_fn=fake_is_running,
                post_run_ingest_fn=fake_post_run_ingest,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            status = json.loads((root / "daemon-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["last_stage_batch_id"], "batch-auto-1")
            self.assertEqual(status["last_upload_batch_id"], "ingest-auto-1")
            self.assertEqual(status["last_uploaded_run_count"], 1)
            self.assertEqual(status["last_failed_upload_run_count"], 0)
            self.assertEqual(status["last_cleanup_deleted_run_count"], 1)
            self.assertEqual(status["last_cleanup_deleted_bytes"], 2048)

    def test_supervisor_skips_auto_upload_after_failed_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            runs_root = root / "runs"
            logs_root = root / "logs"
            logs_root.mkdir()

            config_path = root / "camera-recorder.json"
            local_path = root / "camera-recorder.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "hamilton": {
                            "log_dir": str(log_dir),
                            "process_name": "HxRun.exe",
                        },
                        "storage": {
                            "runs_root": str(runs_root),
                            "recorder_log_dir": str(logs_root),
                        },
                        "central_ingest": {
                            "staging_root": str(root / "staging"),
                            "upload_root": str(root / "upload"),
                            "transport": "filesystem",
                            "auto_upload_on_run_complete": True,
                        },
                        "recorder": {
                            "stop_file": str(root / "recorder.stop"),
                        },
                        "daemon": {
                            "stop_file": str(root / "daemon.stop"),
                            "pid_file": str(root / "daemon.pid"),
                            "status_path": str(root / "daemon-status.json"),
                            "log_path": str(root / "daemon.log"),
                            "idle_poll_sec": 0.05,
                            "heartbeat_sec": 0.05,
                            "relaunch_delay_sec": 0.0,
                        },
                        "profiles": [
                            {
                                "id": "default",
                                "label": "BenchCam",
                                "source": 'dshow:video="Bench Cam"',
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            recorder_script = root / "mock-recorder.py"
            recorder_script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")

            calls = {"count": 0}

            def fake_post_run_ingest(**kwargs):
                calls["count"] += 1
                return {}

            poll_calls = {"count": 0}

            def fake_is_running(_name: str) -> bool:
                poll_calls["count"] += 1
                return poll_calls["count"] >= 2

            rc = DAEMON.run_supervisor(
                config_path=config_path,
                local_config_path=local_path,
                profile_id="default",
                source_override="",
                out_dir_override="",
                label_override="",
                stop_file=root / "daemon.stop",
                pid_file=root / "daemon.pid",
                status_path=root / "daemon-status.json",
                daemon_log_path=root / "daemon.log",
                recorder_log_path=root / "recorder.log",
                idle_poll_sec=0.05,
                heartbeat_sec=0.05,
                relaunch_delay_sec=0.0,
                idle_timeout_sec=2,
                run_once=True,
                max_cycles=0,
                recorder_script=recorder_script,
                is_process_running_fn=fake_is_running,
                post_run_ingest_fn=fake_post_run_ingest,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(calls["count"], 0)


if __name__ == "__main__":
    unittest.main()
