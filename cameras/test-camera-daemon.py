#!/usr/bin/env python3
"""Smoke tests for the workstation-local camera daemon supervisor."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import textwrap
import time
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
    def test_build_recorder_command_uses_packaged_exe_directly(self) -> None:
        command = DAEMON.build_recorder_command(
            config_path=Path(r"C:\camera\config.json"),
            local_config_path=Path(r"C:\camera\local.json"),
            recorder_script=Path(r"C:\camera\dist\legacy-runtime\camera-recorder.exe"),
            recorder_log_path=Path(r"C:\camera\logs\recorder.log"),
            process_name="HxRun.exe",
            profile_id="default",
            source='dshow:video="Bench Cam"',
            out_dir=r"C:\camera\runs",
            label="BenchCam",
        )

        self.assertEqual(command[0], r"C:\camera\dist\legacy-runtime\camera-recorder.exe")
        self.assertNotIn(str(sys.executable), command[0:1])
        self.assertIn("--recorder-log", command)

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
            self.assertEqual(status["contract_status"]["replay_manifest_version"], "hybrid-replay.v1")
            self.assertIn("git_commit_short", status["deployment"])

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
                            "enable_midrun_split": True,
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
                }

            def fake_post_run_cleanup(**kwargs):
                return {
                    "deleted_run_count": 1,
                    "deleted_bytes": 2048,
                    "eligible_run_count": 0,
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
                post_run_cleanup_fn=fake_post_run_cleanup,
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
            self.assertEqual(status["ingest_state"], "completed")
            self.assertEqual(status["pending_ingest_count"], 0)

    def test_supervisor_runs_local_cleanup_without_auto_upload(self) -> None:
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
                            "retention": {
                                "cleanup_on_run_complete": True,
                            },
                        },
                        "central_ingest": {
                            "staging_root": str(root / "staging"),
                            "upload_root": str(root / "upload"),
                            "transport": "filesystem",
                            "auto_upload_on_run_complete": False,
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

            ingest_calls = {"count": 0}
            cleanup_calls: list[dict] = []

            def fake_post_run_ingest(**kwargs):
                ingest_calls["count"] += 1
                return None

            def fake_post_run_cleanup(**kwargs):
                cleanup_calls.append(kwargs)
                return {
                    "deleted_run_count": 2,
                    "deleted_bytes": 8192,
                    "eligible_run_count": 1,
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
                post_run_cleanup_fn=fake_post_run_cleanup,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(ingest_calls["count"], 0)
            self.assertEqual(len(cleanup_calls), 1)
            status = json.loads((root / "daemon-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["last_cleanup_deleted_run_count"], 2)
            self.assertEqual(status["last_cleanup_deleted_bytes"], 8192)
            self.assertEqual(status["last_cleanup_eligible_run_count"], 1)

    def test_post_run_central_ingest_does_not_own_local_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            runs_root = root / "runs"
            staging_root = root / "staging"
            upload_root = root / "upload"
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
                            "retention": {
                                "cleanup_on_run_complete": True,
                            },
                        },
                        "central_ingest": {
                            "staging_root": str(staging_root),
                            "upload_root": str(upload_root),
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

            cleanup_calls: list[dict] = []
            original_stage_runs = DAEMON.stage_runs
            original_upload_staged_runs = DAEMON.upload_staged_runs
            original_cleanup_runs = DAEMON.cleanup_runs
            try:
                DAEMON.stage_runs = lambda **kwargs: {
                    "batch_id": "stage-1",
                    "staged_run_count": 1,
                    "skipped_run_count": 0,
                }
                DAEMON.upload_staged_runs = lambda **kwargs: {
                    "ingest_batch_id": "upload-1",
                    "uploaded_run_count": 1,
                    "failed_run_count": 0,
                }

                def fake_cleanup_runs(**kwargs):
                    cleanup_calls.append(kwargs)
                    return {"deleted_run_count": 1}

                DAEMON.cleanup_runs = fake_cleanup_runs

                result = DAEMON.run_post_run_central_ingest(
                    config_path=config_path,
                    local_config_path=local_path,
                    daemon_log_path=root / "daemon.log",
                )
            finally:
                DAEMON.stage_runs = original_stage_runs
                DAEMON.upload_staged_runs = original_upload_staged_runs
                DAEMON.cleanup_runs = original_cleanup_runs

            self.assertEqual(result["stage"]["batch_id"], "stage-1")
            self.assertEqual(result["upload"]["ingest_batch_id"], "upload-1")
            self.assertIsNone(result["cleanup"])
            self.assertEqual(cleanup_calls, [])
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

    def test_supervisor_rearms_for_next_run_while_upload_runs_in_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            runs_root = root / "runs"
            logs_root = root / "logs"
            logs_root.mkdir()

            config_path = root / "camera-recorder.json"
            local_path = root / "camera-recorder.local.json"
            stop_file = root / "daemon.stop"
            counter_path = root / "launch-count.txt"
            recorder_script = root / "mock-recorder.py"
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
                            "stop_file": str(stop_file),
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
            recorder_script.write_text(
                textwrap.dedent(
                    f"""
                    import pathlib
                    import time

                    counter_path = pathlib.Path(r"{counter_path}")
                    count = int(counter_path.read_text(encoding="utf-8") or "0") if counter_path.exists() else 0
                    count += 1
                    counter_path.write_text(str(count), encoding="utf-8")
                    time.sleep(0.03)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            ingest_calls = {"count": 0}
            observed = {"second_launch_before_upload_complete": False}

            def fake_post_run_ingest(**kwargs):
                ingest_calls["count"] += 1
                deadline = time.monotonic() + 0.30
                while time.monotonic() < deadline:
                    if counter_path.exists() and counter_path.read_text(encoding="utf-8").strip() == "2":
                        observed["second_launch_before_upload_complete"] = True
                        break
                    time.sleep(0.01)
                return {
                    "stage": {
                        "batch_id": f"batch-auto-{ingest_calls['count']}",
                        "staged_run_count": 1,
                        "skipped_run_count": 0,
                    },
                    "upload": {
                        "ingest_batch_id": f"ingest-auto-{ingest_calls['count']}",
                        "uploaded_run_count": 1,
                        "failed_run_count": 0,
                        "items": [],
                    },
                }

            poll_calls = {"count": 0, "between_runs": 0}

            def fake_is_running(_name: str) -> bool:
                poll_calls["count"] += 1
                launches = int(counter_path.read_text(encoding="utf-8").strip()) if counter_path.exists() else 0
                if launches == 0:
                    return poll_calls["count"] >= 2
                if launches == 1:
                    poll_calls["between_runs"] += 1
                    return poll_calls["between_runs"] >= 3
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
                max_cycles=2,
                recorder_script=recorder_script,
                is_process_running_fn=fake_is_running,
                post_run_ingest_fn=fake_post_run_ingest,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(counter_path.read_text(encoding="utf-8").strip(), "2")
            self.assertEqual(ingest_calls["count"], 2)
            self.assertTrue(observed["second_launch_before_upload_complete"])
            status = json.loads((root / "daemon-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["ingest_state"], "completed")
            self.assertEqual(status["pending_ingest_count"], 0)

    def test_supervisor_rearms_immediately_after_exit_code_20(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            runs_root = root / "runs"
            logs_root = root / "logs"
            logs_root.mkdir()

            config_path = root / "camera-recorder.json"
            local_path = root / "camera-recorder.local.json"
            stop_file = root / "daemon.stop"
            counter_path = root / "launch-count.txt"
            recorder_script = root / "mock-recorder.py"
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
                            "stop_file": str(stop_file),
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
            recorder_script.write_text(
                textwrap.dedent(
                    f"""
                    import pathlib
                    import sys

                    counter_path = pathlib.Path(r"{counter_path}")
                    count = int(counter_path.read_text(encoding="utf-8") or "0") if counter_path.exists() else 0
                    count += 1
                    counter_path.write_text(str(count), encoding="utf-8")
                    sys.exit(20 if count == 1 else 0)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            poll_calls = {"count": 0}

            def fake_is_running(_name: str) -> bool:
                poll_calls["count"] += 1
                if counter_path.exists() and counter_path.read_text(encoding="utf-8").strip() == "2":
                    stop_file.write_text("stop", encoding="utf-8")
                return poll_calls["count"] >= 2

            ingest_calls = {"count": 0}

            def fake_post_run_ingest(**kwargs):
                ingest_calls["count"] += 1
                return {}

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
                max_cycles=1,
                recorder_script=recorder_script,
                is_process_running_fn=fake_is_running,
                post_run_ingest_fn=fake_post_run_ingest,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(counter_path.read_text(encoding="utf-8").strip(), "2")
            self.assertEqual(ingest_calls["count"], 1)
            status = json.loads((root / "daemon-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["last_exit_code"], 0)
            self.assertEqual(status["last_exit_contract"], "finalized_run")

    def test_supervisor_skips_auto_upload_for_rearm_exit_code_20(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            runs_root = root / "runs"
            logs_root = root / "logs"
            logs_root.mkdir()

            config_path = root / "camera-recorder.json"
            local_path = root / "camera-recorder.local.json"
            stop_file = root / "daemon.stop"
            recorder_script = root / "mock-recorder.py"
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
                            "stop_file": str(stop_file),
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
            recorder_script.write_text("import sys\nsys.exit(20)\n", encoding="utf-8")

            calls = {"count": 0}

            def fake_post_run_ingest(**kwargs):
                calls["count"] += 1
                return {}

            poll_calls = {"count": 0}

            def fake_is_running(_name: str) -> bool:
                poll_calls["count"] += 1
                if poll_calls["count"] >= 4:
                    stop_file.write_text("stop", encoding="utf-8")
                return poll_calls["count"] >= 2

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
                post_run_ingest_fn=fake_post_run_ingest,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(calls["count"], 0)
            status = json.loads((root / "daemon-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["last_exit_code"], 20)
            self.assertEqual(status["last_exit_contract"], "rearm_segment")
            self.assertFalse(status["waiting_for_process_exit"])

    def test_supervisor_rearms_immediately_after_midrun_split(self) -> None:
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
                            "enable_midrun_split": True,
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

            launches_path = root / "launches.jsonl"
            recorder_script = root / "mock-recorder.py"
            recorder_script.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import pathlib
                    import sys

                    launches = pathlib.Path(r"{launches_path}")
                    count = 0
                    if launches.exists():
                        count = len([line for line in launches.read_text(encoding="utf-8").splitlines() if line.strip()])
                    argv = sys.argv[1:]
                    with launches.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({{"argv": argv}}) + "\\n")
                    raise SystemExit(20 if count == 0 else 0)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            polls = {"count": 0}

            def fake_is_running(_name: str) -> bool:
                polls["count"] += 1
                return polls["count"] < 8

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
                idle_poll_sec=0.02,
                heartbeat_sec=0.02,
                relaunch_delay_sec=0.0,
                idle_timeout_sec=2,
                run_once=False,
                max_cycles=2,
                recorder_script=recorder_script,
                is_process_running_fn=fake_is_running,
            )

            self.assertEqual(rc, 0)
            launches = [json.loads(line) for line in launches_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(launches), 2)
            self.assertIn("--enable-midrun-split", launches[0]["argv"])
            self.assertNotIn("--discard-without-trace", launches[0]["argv"])
            self.assertIn("--discard-without-trace", launches[1]["argv"])

    def test_run_post_run_central_ingest_uses_recent_days_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            runs_root = root / "runs"
            staging_root = root / "staging"
            upload_root = root / "upload"
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
                            "staging_root": str(staging_root),
                            "upload_root": str(upload_root),
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

            stage_calls: list[dict] = []
            upload_calls: list[dict] = []
            original_stage_runs = DAEMON.stage_runs
            original_upload_staged_runs = DAEMON.upload_staged_runs
            try:
                def fake_stage_runs(**kwargs):
                    stage_calls.append(kwargs)
                    return {
                        "batch_id": "batch-auto-1",
                        "staged_run_count": 1,
                        "skipped_run_count": 0,
                    }

                def fake_upload_staged_runs(**kwargs):
                    upload_calls.append(kwargs)
                    return {
                        "ingest_batch_id": "ingest-auto-1",
                        "uploaded_run_count": 1,
                        "failed_run_count": 0,
                    }

                DAEMON.stage_runs = fake_stage_runs
                DAEMON.upload_staged_runs = fake_upload_staged_runs

                payload = DAEMON.run_post_run_central_ingest(
                    config_path=config_path,
                    local_config_path=local_path,
                    daemon_log_path=None,
                )
            finally:
                DAEMON.stage_runs = original_stage_runs
                DAEMON.upload_staged_runs = original_upload_staged_runs

            self.assertIsNotNone(payload)
            self.assertEqual(len(stage_calls), 1)
            self.assertEqual(stage_calls[0]["recent_days"], 2.0)
            self.assertEqual(len(upload_calls), 1)

    def test_supervisor_does_not_enable_midrun_split_by_default(self) -> None:
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

            launches_path = root / "launches.jsonl"
            recorder_script = root / "mock-recorder.py"
            recorder_script.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import pathlib
                    import sys

                    launches = pathlib.Path(r"{launches_path}")
                    with launches.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({{"argv": sys.argv[1:]}}) + "\\n")
                    raise SystemExit(0)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            polls = {"count": 0}

            def fake_is_running(_name: str) -> bool:
                polls["count"] += 1
                return polls["count"] < 4

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
                idle_poll_sec=0.02,
                heartbeat_sec=0.02,
                relaunch_delay_sec=0.0,
                idle_timeout_sec=2,
                run_once=True,
                max_cycles=0,
                recorder_script=recorder_script,
                is_process_running_fn=fake_is_running,
            )

            self.assertEqual(rc, 0)
            launches = [json.loads(line) for line in launches_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(launches), 1)
            self.assertNotIn("--enable-midrun-split", launches[0]["argv"])

    def test_supervisor_rearm_does_not_consume_run_limit(self) -> None:
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

            launches_path = root / "launches.jsonl"
            recorder_script = root / "mock-recorder.py"
            recorder_script.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import pathlib
                    import sys

                    launches = pathlib.Path(r"{launches_path}")
                    count = 0
                    if launches.exists():
                        count = len([line for line in launches.read_text(encoding="utf-8").splitlines() if line.strip()])
                    with launches.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({{"count": count}}) + "\\n")
                    raise SystemExit(20 if count == 0 else 0)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            polls = {"count": 0}

            def fake_is_running(_name: str) -> bool:
                polls["count"] += 1
                return polls["count"] < 8

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
                idle_poll_sec=0.02,
                heartbeat_sec=0.02,
                relaunch_delay_sec=0.0,
                idle_timeout_sec=2,
                run_once=False,
                max_cycles=1,
                recorder_script=recorder_script,
                is_process_running_fn=fake_is_running,
            )

            self.assertEqual(rc, 0)
            launches = [json.loads(line) for line in launches_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(launches), 2)
            status = json.loads((root / "daemon-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["reason"], "run_limit")
            self.assertEqual(status["cycle_count"], 1)

    def test_supervisor_blocks_recording_when_critical_low_disk_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "hamilton"
            log_dir.mkdir()
            runs_root = root / "runs"
            logs_root = root / "logs"
            logs_root.mkdir()

            config_path = root / "camera-recorder.json"
            local_path = root / "camera-recorder.local.json"
            stop_file = root / "daemon.stop"
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
                            "retention": {
                                "emergency": {
                                    "enabled": True,
                                    "min_free_gb": 20,
                                    "target_free_gb": 30,
                                    "block_new_recording_free_gb": 8,
                                }
                            },
                        },
                        "recorder": {
                            "stop_file": str(root / "recorder.stop"),
                        },
                        "daemon": {
                            "stop_file": str(stop_file),
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

            recorder_script = root / "mock-recorder.py"
            recorder_script.write_text("raise SystemExit(0)\n", encoding="utf-8")

            calls = {"count": 0}
            original_cleanup_runs = DAEMON.cleanup_runs

            def fake_cleanup_runs(**kwargs):
                calls["count"] += 1
                stop_file.write_text("stop", encoding="utf-8")
                return {
                    "deleted_run_count": 0,
                    "eligible_run_count": 0,
                    "emergency_deleted_run_count": 0,
                    "disk_free_bytes_after": 2 * (1024**3),
                    "emergency_active": True,
                    "critical_pressure_remaining": True,
                }

            def fake_is_running(_name: str) -> bool:
                return True

            try:
                DAEMON.cleanup_runs = fake_cleanup_runs
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
                    idle_timeout_sec=1,
                    run_once=False,
                    max_cycles=0,
                    recorder_script=recorder_script,
                    is_process_running_fn=fake_is_running,
                )
            finally:
                DAEMON.cleanup_runs = original_cleanup_runs

            self.assertEqual(rc, 0)
            self.assertEqual(calls["count"], 1)
            status = json.loads((root / "daemon-status.json").read_text(encoding="utf-8"))
            self.assertTrue(status["low_disk_critical"])
            self.assertEqual(status["low_disk_free_bytes"], 2 * (1024**3))


if __name__ == "__main__":
    unittest.main()
