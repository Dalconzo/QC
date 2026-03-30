#!/usr/bin/env python3
"""
camera-daemon.py

Always-on supervisor for the Hamilton camera recorder.

This daemon is the workstation-local automation layer for camera capture. It
waits for the configured Hamilton Run Manager process to appear, launches one
continuous recorder for that run, waits for the recorder to finish when the
process exits, then returns to idle so the next run is captured automatically.

The daemon does not capture video itself. It owns lifecycle management,
singleton protection, status reporting, and auto-start friendliness, while
`camera-recorder.py` remains the component that talks to ffmpeg/OpenCV and
pairs each recording with its Hamilton trace file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, get_profile, load_effective_config, validate_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def emit_log(message: str, *, log_path: Path | None = None, is_error: bool = False) -> None:
    """Mirror daemon diagnostics to the console and an optional log file."""
    stream = sys.stderr if is_error else sys.stdout
    print(message, file=stream)
    if not log_path:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{dt.datetime.now().isoformat()} {message}\n"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def write_status(status_path: Path, payload: dict) -> None:
    """Persist the latest daemon state for local troubleshooting scripts."""
    status_path.parent.mkdir(parents=True, exist_ok=True)
    enriched = dict(payload)
    enriched["updated_at"] = dt.datetime.now().isoformat()
    with status_path.open("w", encoding="utf-8") as handle:
        json.dump(enriched, handle, indent=2)
        handle.write("\n")


def read_pid_file(pid_path: Path) -> int | None:
    """Return the recorded PID if the file exists and is readable."""
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def is_pid_running(pid: int | None) -> bool:
    """Check whether a process id is still alive without requiring psutil."""
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
            output = (result.stdout or "").lower()
            return str(pid) in output and "no tasks are running" not in output
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def claim_singleton(pid_path: Path) -> None:
    """Prevent multiple daemon instances from supervising the same workstation."""
    existing_pid = read_pid_file(pid_path)
    if existing_pid and is_pid_running(existing_pid):
        raise RuntimeError(f"Camera daemon is already running with PID {existing_pid}")

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")


def release_singleton(pid_path: Path) -> None:
    """Remove the pid file when the daemon exits cleanly."""
    try:
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass


def _normalize_proc_names(proc_names: str | list[str] | tuple[str, ...]) -> list[str]:
    """Normalize configured process selectors into executable names."""
    if isinstance(proc_names, (list, tuple)):
        items = [str(item).strip() for item in proc_names]
    else:
        text = str(proc_names or "").replace(";", ",")
        items = [item.strip() for item in text.split(",")]
    return [item for item in items if item]


def is_any_proc_running(proc_names: str | list[str] | tuple[str, ...]) -> bool:
    """Return True when any named executable is present on the workstation."""
    normalized = _normalize_proc_names(proc_names)
    if not normalized:
        return False

    try:
        import psutil  # type: ignore

        for proc in psutil.process_iter(attrs=["name"]):
            try:
                proc_name = (proc.info.get("name") or "").lower()
                if any(proc_name == expected.lower() for expected in normalized):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        try:
            result = subprocess.run(["tasklist"], capture_output=True, text=True, check=False)
            output = (result.stdout or "").lower()
            return any(expected.lower() in output for expected in normalized)
        except Exception:
            return False


def build_recorder_command(
    *,
    config_path: Path,
    local_config_path: Path,
    recorder_script: Path,
    recorder_log_path: Path | None,
    process_name: str,
    profile_id: str,
    source: str,
    out_dir: str,
    label: str,
) -> list[str]:
    """Construct one child recorder invocation for a single Hamilton run."""
    command = [
        sys.executable,
        str(recorder_script),
        "--config",
        str(config_path),
        "--local-config",
        str(local_config_path),
        "--profile",
        profile_id,
        "--start-when-exe",
        process_name,
        "--stop-when-exe",
        process_name,
    ]

    if source:
        command += ["--source", source]
    if out_dir:
        command += ["--out-dir", out_dir]
    if label:
        command += ["--label", label]
    if recorder_log_path:
        command += ["--recorder-log", str(recorder_log_path)]
    return command


def run_supervisor(
    *,
    config_path: Path,
    local_config_path: Path,
    profile_id: str,
    source_override: str,
    out_dir_override: str,
    label_override: str,
    stop_file: Path,
    pid_file: Path,
    status_path: Path,
    daemon_log_path: Path | None,
    recorder_log_path: Path | None,
    idle_poll_sec: float,
    heartbeat_sec: float,
    relaunch_delay_sec: float,
    idle_timeout_sec: int,
    run_once: bool,
    max_cycles: int,
    recorder_script: Path,
    is_process_running_fn=is_any_proc_running,
) -> int:
    """Run the idle->record->idle loop until explicitly stopped."""
    config = load_effective_config(config_path=config_path, local_override_path=local_config_path)
    validation = validate_config(config, require_hamilton_log_dir=True)
    if validation["errors"]:
        for item in validation["errors"]:
            emit_log(item, log_path=daemon_log_path, is_error=True)
        return 1

    try:
        profile = get_profile(config, profile_id or None)
    except KeyError:
        emit_log(f"Unknown camera profile: {profile_id}", log_path=daemon_log_path, is_error=True)
        return 1

    process_name = str(config["hamilton"]["process_name"])
    source = source_override or str(profile.get("source") or "")
    out_dir = out_dir_override or str(config["storage"]["runs_root"])
    label = label_override or str(profile.get("label") or profile["id"])
    recorder_stop_file = Path(str(config["recorder"]["stop_file"]))

    try:
        claim_singleton(pid_file)
    except RuntimeError as exc:
        emit_log(str(exc), log_path=daemon_log_path, is_error=True)
        return 2

    started_at = dt.datetime.now().isoformat()
    cycle_count = 0
    child_proc: subprocess.Popen | None = None
    child_started_at = 0.0
    loop_started_at = time.monotonic()
    last_heartbeat = 0.0

    def update_status(state: str, **extra: object) -> None:
        payload = {
            "state": state,
            "daemon_pid": os.getpid(),
            "profile_id": profile["id"],
            "profile_label": profile.get("label"),
            "source": source,
            "out_dir": out_dir,
            "process_name": process_name,
            "started_at": started_at,
            "cycle_count": cycle_count,
            "child_pid": child_proc.pid if child_proc else None,
            "stop_file": str(stop_file),
            "pid_file": str(pid_file),
            "status_path": str(status_path),
            "daemon_log_path": str(daemon_log_path) if daemon_log_path else "",
            "recorder_log_path": str(recorder_log_path) if recorder_log_path else "",
        }
        payload.update(extra)
        write_status(status_path, payload)

    emit_log(
        f"[daemon] Starting supervisor for profile '{profile['id']}' gated on {process_name}",
        log_path=daemon_log_path,
    )
    update_status("starting")

    try:
        while True:
            if stop_file.exists():
                emit_log("[daemon] Stop file detected. Exiting idle loop.", log_path=daemon_log_path)
                update_status("stopping", reason="daemon_stop_file")
                break

            if child_proc is not None:
                return_code = child_proc.poll()
                if return_code is None:
                    now = time.monotonic()
                    if (now - last_heartbeat) >= heartbeat_sec:
                        duration = round(now - child_started_at, 1)
                        emit_log(
                            f"[daemon] Recorder child {child_proc.pid} active for {duration}s",
                            log_path=daemon_log_path,
                        )
                        update_status("recording", child_runtime_sec=duration)
                        last_heartbeat = now

                    if stop_file.exists():
                        emit_log("[daemon] Stop requested while recording. Signaling recorder stop file.", log_path=daemon_log_path)
                        recorder_stop_file.parent.mkdir(parents=True, exist_ok=True)
                        recorder_stop_file.write_text("stop", encoding="utf-8")
                        update_status("stopping", reason="daemon_stop_file", child_runtime_sec=round(now - child_started_at, 1))
                    time.sleep(max(0.25, idle_poll_sec))
                    continue

                cycle_count += 1
                emit_log(
                    f"[daemon] Recorder child exited with code {return_code}",
                    log_path=daemon_log_path,
                    is_error=(return_code != 0),
                )
                update_status("idle", last_exit_code=return_code, last_cycle_completed_at=dt.datetime.now().isoformat())
                child_proc = None
                child_started_at = 0.0
                last_heartbeat = 0.0

                if run_once or (max_cycles > 0 and cycle_count >= max_cycles):
                    emit_log("[daemon] Run limit reached. Exiting.", log_path=daemon_log_path)
                    update_status("stopped", reason="run_limit", last_exit_code=return_code)
                    break

                if relaunch_delay_sec > 0:
                    time.sleep(relaunch_delay_sec)
                continue

            if idle_timeout_sec > 0 and (time.monotonic() - loop_started_at) >= idle_timeout_sec:
                emit_log("[daemon] Idle timeout reached before the next Hamilton run.", log_path=daemon_log_path)
                update_status("stopped", reason="idle_timeout")
                break

            if not is_process_running_fn(process_name):
                now = time.monotonic()
                if (now - last_heartbeat) >= heartbeat_sec:
                    emit_log(f"[daemon] Waiting for process start: {process_name}", log_path=daemon_log_path)
                    update_status("idle")
                    last_heartbeat = now
                time.sleep(max(0.25, idle_poll_sec))
                continue

            child_command = build_recorder_command(
                config_path=config_path,
                local_config_path=local_config_path,
                recorder_script=recorder_script,
                recorder_log_path=recorder_log_path,
                process_name=process_name,
                profile_id=profile["id"],
                source=source,
                out_dir=out_dir,
                label=label,
            )
            emit_log(f"[daemon] Launching recorder child for active {process_name} session", log_path=daemon_log_path)
            emit_log(f"[daemon] Recorder cmd: {' '.join(child_command)}", log_path=daemon_log_path)
            child_proc = subprocess.Popen(child_command)
            child_started_at = time.monotonic()
            last_heartbeat = 0.0
            update_status("recording", child_started_at=dt.datetime.now().isoformat())
            time.sleep(max(0.25, idle_poll_sec))

        return 0
    finally:
        if child_proc is not None and child_proc.poll() is None:
            try:
                recorder_stop_file.parent.mkdir(parents=True, exist_ok=True)
                recorder_stop_file.write_text("stop", encoding="utf-8")
                child_proc.wait(timeout=15)
            except Exception:
                try:
                    child_proc.send_signal(signal.SIGTERM)
                except Exception:
                    pass
        release_singleton(pid_file)
        try:
            stop_file.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Always-on supervisor for Hamilton camera capture")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the base camera config JSON")
    parser.add_argument("--local-config", default=str(DEFAULT_LOCAL_OVERRIDE_PATH), help="Path to the optional workstation-local override JSON")
    parser.add_argument("--profile", default="", help="Camera profile id from the shared config")
    parser.add_argument("--source", default="", help="Override the selected profile source")
    parser.add_argument("--out-dir", default="", help="Override the configured runs root")
    parser.add_argument("--label", default="", help="Override the recording label")
    parser.add_argument("--daemon-log", default="", help="Persistent log path for daemon diagnostics")
    parser.add_argument("--recorder-log", default="", help="Persistent log path for child recorder diagnostics")
    parser.add_argument("--status-path", default="", help="Status JSON written by the daemon")
    parser.add_argument("--pid-file", default="", help="PID file used for singleton protection")
    parser.add_argument("--stop-file", default="", help="Stop-file sentinel for shutting down the daemon")
    parser.add_argument("--idle-poll-sec", type=float, default=None, help="Polling interval while waiting for HxRun.exe")
    parser.add_argument("--heartbeat-sec", type=float, default=None, help="How often to log idle/recording heartbeats")
    parser.add_argument("--relaunch-delay-sec", type=float, default=None, help="Delay before the daemon returns to idle after one run finishes")
    parser.add_argument("--idle-timeout-sec", type=int, default=0, help="Exit if no run starts within this many seconds. 0 waits indefinitely.")
    parser.add_argument("--once", action="store_true", help="Exit after the first recorder session completes")
    parser.add_argument("--max-cycles", type=int, default=0, help="Exit after this many completed recorder sessions. 0 means unlimited.")
    parser.add_argument("--recorder-script", default="", help="Override recorder script path. Mainly useful for tests.")
    args = parser.parse_args()

    config = load_effective_config(
        config_path=Path(args.config),
        local_override_path=Path(args.local_config),
    )
    validation = validate_config(config, require_hamilton_log_dir=True)
    daemon_config = config.get("daemon", {})

    if validation["errors"]:
        for item in validation["errors"]:
            print(item, file=sys.stderr)
        return 1

    daemon_log_path = Path(args.daemon_log or daemon_config.get("log_path") or "") if (args.daemon_log or daemon_config.get("log_path")) else None
    status_path = Path(args.status_path or daemon_config.get("status_path") or (REPO_ROOT / "logs" / "camera-daemon-status.json"))
    pid_file = Path(args.pid_file or daemon_config.get("pid_file") or (REPO_ROOT / "logs" / "camera-daemon.pid"))
    stop_file = Path(args.stop_file or daemon_config.get("stop_file") or (REPO_ROOT / "cameras" / "camera-daemon.stop"))
    recorder_log_path = None
    recorder_log_value = args.recorder_log or ""
    if recorder_log_value:
        recorder_log_path = Path(recorder_log_value)
    else:
        recorder_log_dir = str(config.get("storage", {}).get("recorder_log_dir") or "")
        if recorder_log_dir:
            recorder_log_path = Path(recorder_log_dir) / "camera-recorder-daemon.log"
    recorder_script = Path(args.recorder_script or (Path(__file__).parent / "camera-recorder.py"))
    idle_poll_sec = args.idle_poll_sec if args.idle_poll_sec is not None else float(daemon_config.get("idle_poll_sec", 1.0))
    heartbeat_sec = args.heartbeat_sec if args.heartbeat_sec is not None else float(daemon_config.get("heartbeat_sec", 10.0))
    relaunch_delay_sec = args.relaunch_delay_sec if args.relaunch_delay_sec is not None else float(daemon_config.get("relaunch_delay_sec", 2.0))

    for item in validation["warnings"]:
        emit_log(item, log_path=daemon_log_path)

    return run_supervisor(
        config_path=Path(args.config),
        local_config_path=Path(args.local_config),
        profile_id=args.profile,
        source_override=args.source,
        out_dir_override=args.out_dir,
        label_override=args.label,
        stop_file=stop_file,
        pid_file=pid_file,
        status_path=status_path,
        daemon_log_path=daemon_log_path,
        recorder_log_path=recorder_log_path,
        idle_poll_sec=idle_poll_sec,
        heartbeat_sec=heartbeat_sec,
        relaunch_delay_sec=relaunch_delay_sec,
        idle_timeout_sec=args.idle_timeout_sec,
        run_once=args.once,
        max_cycles=args.max_cycles,
        recorder_script=recorder_script,
    )


if __name__ == "__main__":
    raise SystemExit(main())
