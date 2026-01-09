#!/usr/bin/env python3
"""
camera-recorder.py

Segmented camera recorder that writes timestamped mp4 clips to a directory.

Filenames: YYYYMMDD_HHMMSS_<label>.mp4 where timestamp is the start time of the segment.
Stops gracefully when a stop file appears.

Prefers ffmpeg for robust segmentation; falls back to OpenCV if ffmpeg not available.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def find_ffmpeg(explicit: str | None = None) -> tuple[bool, str | None]:
    # 1) explicit path argument
    if explicit:
        p = Path(explicit)
        if p.exists():
            return True, str(p)
    # 2) side-by-side ffmpeg.exe next to this script
    here = Path(__file__).parent
    local_ff = here / "ffmpeg.exe"
    if local_ff.exists():
        return True, str(local_ff)
    local_ff2 = here / "dist" / "ffmpeg.exe"
    if local_ff2.exists():
        return True, str(local_ff2)
    # 3) repo-level cameras/ffmpeg.exe (if run from repo root)
    repo_ff = Path.cwd() / "cameras" / "ffmpeg.exe"
    if repo_ff.exists():
        return True, str(repo_ff)
    repo_ff2 = Path.cwd() / "cameras" / "dist" / "ffmpeg.exe"
    if repo_ff2.exists():
        return True, str(repo_ff2)
    # 4) PATH
    exe = shutil.which("ffmpeg")
    return (exe is not None), exe


def list_dshow_devices(ffmpeg_bin: str) -> list[dict]:
    """Return a list of video devices using ffmpeg dshow probe: [{index,name,alt}]"""
    try:
        # Run the device probe
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
            capture_output=True,
            text=True,
            check=False,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        lines = out.splitlines()
        devices: list[dict] = []
        pending: dict | None = None
        import re
        re_name = re.compile(r"\] \"([^\"]+)\" \(video\)")
        re_alt = re.compile(r"Alternative name \"([^\"]+)\"")
        for ln in lines:
            m = re_name.search(ln)
            if m:
                # Start a new device record
                if pending:
                    devices.append(pending)
                pending = {"name": m.group(1), "alt": None}
                continue
            if pending:
                m2 = re_alt.search(ln)
                if m2:
                    pending["alt"] = m2.group(1)
        if pending:
            devices.append(pending)
        # Add indices
        for i, d in enumerate(devices):
            d["index"] = i
        return devices
    except Exception:
        return []


def ts_now() -> dt.datetime:
    return dt.datetime.now()


def ts_label(t: dt.datetime) -> str:
    return t.strftime("%Y%m%d_%H%M%S")


def _is_proc_running(proc_name: str) -> bool:
    if not proc_name:
        return True
    try:
        import psutil  # type: ignore
        for p in psutil.process_iter(attrs=["name"]):
            try:
                if (p.info.get("name") or "").lower() == proc_name.lower():
                    return True
            except Exception:
                continue
        return False
    except Exception:
        # Fallback: Windows 'tasklist'
        try:
            import subprocess
            res = subprocess.run(["tasklist"], capture_output=True, text=True, check=False)
            return proc_name.lower() in (res.stdout or "").lower()
        except Exception:
            return True


def _parse_segment_ts(name: str) -> dt.datetime | None:
    """Parse YYYYMMDD_HHMMSS from segment filename prefix."""
    try:
        base = Path(name).name
        ts = base.split("_")[0] + "_" + base.split("_")[1]
        return dt.datetime.strptime(ts, "%Y%m%d_%H%M%S")
    except Exception:
        return None


def _consume_error_marks(rec_start: dt.datetime, error_dir: Path, verbose: bool = False) -> bool:
    """Consume mark files created after recorder start and signal if any are relevant.

    - Only marks whose timestamp (from filename) is >= rec_start are treated as
      valid error signals; older marks are considered stale and removed.
    - Valid marks are moved into error_dir/marks for later validation/logging.
    - Returns True if at least one valid mark was seen.
    """
    marks_dir = Path("cameras") / "marks"
    if not marks_dir.exists():
        if verbose:
            print(f"[recorder] No marks dir yet: {marks_dir}")
        return False
    mark_files = sorted(marks_dir.glob("*.mark"))
    if not mark_files:
        return False
    valid = False
    archive_dir = error_dir / "marks"
    archive_dir.mkdir(parents=True, exist_ok=True)
    if verbose:
        print(f"[recorder] Inspecting {len(mark_files)} mark(s) in {marks_dir}")
    for m in mark_files:
        try:
            ts_str = m.stem
            mark_dt = dt.datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
        except Exception:
            if verbose:
                print(f"[recorder] Skipping malformed mark {m}; deleting.")
            m.unlink(missing_ok=True)
            continue

        if mark_dt < rec_start:
            # Stale mark from a previous recorder session
            if verbose:
                print(f"[recorder] Discarding stale mark {m} (before recorder start).")
            m.unlink(missing_ok=True)
            continue

        # Valid mark for this session: move it to archive and signal
        try:
            dest = archive_dir / m.name
            if verbose:
                print(f"[recorder] Consuming mark {m} -> {dest}")
            m.replace(dest)
            valid = True
        except Exception as e:
            if verbose:
                print(f"[recorder] Failed to archive mark {m}: {e}")
    return valid


def _promote_segment_to_error(ffmpeg_bin: str, segment: Path, error_dir: Path, label: str, verbose: bool = False) -> None:
    """Copy a finished segment into error_dir as a tagged error clip."""
    error_dir.mkdir(parents=True, exist_ok=True)
    sdt = _parse_segment_ts(segment.name) or dt.datetime.now()
    out_name = f"{sdt.strftime('%Y%m%d_%H%M%S')}_error_{label}.mp4"
    out_path = error_dir / out_name
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(segment),
        "-c",
        "copy",
        str(out_path),
    ]
    if verbose:
        print(f"[recorder] Promoting segment to error clip: {segment} -> {out_path}")
        print("[recorder] ffmpeg cmd:", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
        if verbose:
            print(f"[recorder] Wrote error clip: {out_path}")
    except Exception as e:
        if verbose:
            print(f"[recorder] Failed to promote segment {segment} to error clip: {e}")


def run_ffmpeg(ffmpeg_bin: str, source: str, out_dir: Path, segment_sec: int, label: str, stop_file: Path, stop_when_exe: str = "", verbose: bool = False, error_dir: Path | None = None, error_window_sec: int = 30):
    out_dir.mkdir(parents=True, exist_ok=True)
    # Build output pattern with timestamp at segment boundaries
    # ffmpeg time pattern uses strftime-like expansion when using -strftime 1
    # For Windows path escaping, avoid % in folder name
    pattern = str(out_dir / ("%Y%m%d_%H%M%S_" + label + ".mp4"))
    src = source.strip()
    is_rtsp = src.lower().startswith("rtsp")
    is_dshow = src.lower().startswith("dshow:") or src.lower().startswith("video=") or src.lower().startswith("audio=")
    def _normalize_dshow_name(x: str) -> str:
        # Build a dshow device spec for argument-vector invocation (no shell quoting needed)
        # Accept forms like 'dshow:video="Name"' or 'video="Name"' or 'video=Name'
        s = x
        if s.lower().startswith("dshow:"):
            s = s.split(":", 1)[1]
        if s.lower().startswith("video="):
            key, val = s.split("=", 1)
            # Strip any surrounding quotes; spaces are fine in an argv element
            val = val.strip().strip('"')
            return f"{key}={val}"
        return s
    input_args = ["-i", src]
    codec_args: list[str] = ["-c", "copy", "-an"]
    if is_rtsp:
        input_args = ["-rtsp_transport", "tcp", "-i", src]
        codec_args = ["-c", "copy", "-an"]
    elif is_dshow:
        # Windows webcam via DirectShow: transcode to H.264 for mp4 compatibility
        # Accept forms: 'dshow:video="Integrated Camera"' or 'video="Integrated Camera"'
        name = _normalize_dshow_name(src)
        # Insert optional framerate and video_size before -i
        input_args = ["-f", "dshow"]
        # Pull desired mode from environment (for quick testing) or CLI via global args if present
        dshow_fps = os.environ.get("QC_CAM_FPS")
        dshow_vs = os.environ.get("QC_CAM_VSIZE")
        # We pass through framerate/video_size later if provided by CLI
        if dshow_fps:
            input_args += ["-framerate", str(dshow_fps)]
        if dshow_vs:
            input_args += ["-video_size", str(dshow_vs)]
        input_args += ["-i", name]
        codec_args = [
            "-vcodec", "libx264",
            "-preset", "veryfast",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-an",
        ]

    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", *input_args, *codec_args,
           "-f", "segment", "-segment_time", str(segment_sec), "-reset_timestamps", "1", "-strftime", "1", pattern]
    if verbose:
        print("ffmpeg cmd:")
        print(" ", " ".join(cmd))
    # Run ffmpeg in a child process while polling for stop file; if stop requested, terminate ffmpeg.
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    # Error handling state: mark when an error is signaled, and on the next
    # segment rollover promote the finished segment exactly once.
    error_pending = False
    last_segment: Path | None = None
    rec_start = ts_now()
    no_mark_since: dt.datetime | None = None
    strobe_on = False
    try:
        while True:
            # Stop if sentinel file exists or watched process has exited
            if stop_file.exists() or (stop_when_exe and not _is_proc_running(stop_when_exe)):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                break

            if error_dir is not None:
                # Consume any mark files created since recorder start and
                # remember that an error occurred.
                try:
                    if _consume_error_marks(rec_start, error_dir, verbose):
                        if verbose:
                            # Finish any in-place "no marks" line before logging
                            if no_mark_since is not None:
                                print()
                            print("[recorder] Error mark consumed; will promote next finished segment.")
                        error_pending = True
                        no_mark_since = None
                    else:
                        # No marks this cycle; update an in-place status line occasionally.
                        if verbose and not error_pending:
                            now = ts_now()
                            if no_mark_since is None:
                                no_mark_since = now
                            strobe_on = not strobe_on
                            indicator = "*" if strobe_on else " "
                            msg = f"[recorder] {indicator} No mark files (since {no_mark_since.strftime('%H:%M:%S')} -> {now.strftime('%H:%M:%S')})"
                            # In-place update without spamming new lines; tint green for visibility
                            print("\x1b[32m" + msg + "\x1b[0m", end="\r", flush=True)
                except Exception:
                    pass

                # Detect segment rollover: when a new segment appears, the
                # previous one is finished and safe to promote.
                try:
                    segments = sorted(out_dir.glob("*.mp4"))
                    if segments:
                        newest = segments[-1]
                        if last_segment is None:
                            last_segment = newest
                        elif newest != last_segment:
                            prev = last_segment
                            last_segment = newest
                            # Segment rollover: any pending error gets promoted once.
                            if error_pending:
                                # Finish any in-place "no marks" line.
                                if verbose and no_mark_since is not None:
                                    print()
                                _promote_segment_to_error(ffmpeg_bin, prev, error_dir, label, verbose)
                                error_pending = False
                                no_mark_since = None
                    elif verbose and no_mark_since is None:
                        # Only log this once per run when we haven't yet seen any segments.
                        print(f"[recorder] No segments yet in {out_dir}")
                except Exception as e:
                    if verbose:
                        print(f"[recorder] Failed to inspect segments in {out_dir}: {e}")

            ret = proc.poll()
            if ret is not None:
                # If ffmpeg exited quickly with error, surface diagnostics
                if ret != 0:
                    err = None
                    try:
                        err = proc.stderr.read() if proc.stderr else ""
                    except Exception:
                        err = ""
                    print("ffmpeg exited early with code", ret)
                    if err:
                        print(err.strip())
                    print("Troubleshooting: ensure the dshow device name is quoted, e.g., --source 'dshow:video=\"Arducam USB Camera\"'", file=sys.stderr)
                break

            time.sleep(1)
    finally:
        try:
            stop_file.unlink(missing_ok=True)
        except Exception:
            pass


def run_opencv(source: str, out_dir: Path, segment_sec: int, label: str, stop_file: Path, stop_when_exe: str = "", fps: int = 20, fourcc: str = "mp4v"):
    # Lazy import to avoid hard dependency if ffmpeg is present
    import cv2  # type: ignore

    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(0 if source.isdigit() else source)
    if not cap.isOpened():
        print("ERROR: Failed to open camera source", file=sys.stderr)
        return 2
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
    code = cv2.VideoWriter_fourcc(*fourcc)

    def new_writer(start: dt.datetime):
        path = out_dir / f"{ts_label(start)}_{label}.mp4"
        return path, cv2.VideoWriter(str(path), code, fps, (width, height))

    start = ts_now()
    seg_end = start + dt.timedelta(seconds=segment_sec)
    path, writer = new_writer(start)
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            writer.write(frame)
            now = ts_now()
            if now >= seg_end:
                writer.release()
                start = now
                seg_end = start + dt.timedelta(seconds=segment_sec)
                path, writer = new_writer(start)
            if stop_file.exists() or (stop_when_exe and not _is_proc_running(stop_when_exe)):
                break
    finally:
        try:
            writer.release()
        except Exception:
            pass
        cap.release()
        try:
            stop_file.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Segmented camera recorder")
    ap.add_argument("--source", default="0", help="Camera source (index or URL). Default: 0")
    ap.add_argument("--out-dir", default="video_clips", help="Output directory for clips")
    ap.add_argument("--segment-sec", type=int, default=60, help="Segment length in seconds (default: 60)")
    ap.add_argument("--label", default="cam", help="Label to include in filenames (default: cam)")
    ap.add_argument("--stop-file", default="cameras.recorder.stop", help="Path to stop file to end recording")
    ap.add_argument("--stop-when-exe", default="", help="Stop when the given process name is no longer running (e.g., 'Microlab STAR.exe')")
    ap.add_argument("--ffmpeg", default="", help="Optional path to ffmpeg.exe; if provided, recorder uses ffmpeg backend")
    ap.add_argument("--verbose", action="store_true", help="Print backend selection and underlying ffmpeg command")
    ap.add_argument("--list-devices", action="store_true", help="List DirectShow cameras and exit (requires ffmpeg)")
    ap.add_argument("--select-device", action="store_true", help="Interactively select a DirectShow camera (requires ffmpeg)")
    ap.add_argument("--framerate", type=int, default=None, help="Desired framerate for dshow webcams (e.g., 30)")
    ap.add_argument("--video-size", default=None, help="Desired resolution WxH for dshow webcams (e.g., 1280x720)")
    ap.add_argument("--error-dir", default="error_clips", help="Directory for error clips (used with mark files)")
    ap.add_argument("--error-window-sec", type=int, default=30, help="Seconds of video to keep before error mark")
    args = ap.parse_args()

    out = Path(args.out_dir)
    stop_file = Path(args.stop_file)
    error_dir = Path(args.error_dir)
    msg = f"Recording segments to {out} (every {args.segment_sec}s). Create '{stop_file}' to stop. Error clips in '{error_dir}'."
    if args.stop_when_exe:
        msg += f" Also watching process: {args.stop_when_exe}"
    print(msg)

    ok, ff = find_ffmpeg(args.ffmpeg or None)
    if ok and ff and (args.list_devices or args.select_device):
        devs = list_dshow_devices(ff)
        if not devs:
            print("No DirectShow devices found or probe failed.")
            return 2
        print("DirectShow video devices:")
        for d in devs:
            print(f"  [{d['index']}] {d['name']}" + (f"  (alt: {d['alt']})" if d.get('alt') else ""))
        if args.list_devices:
            return 0
        # interactive selection
        try:
            choice = input("Select device index: ").strip()
            idx = int(choice)
            sel = next((d for d in devs if d["index"] == idx), None)
            if not sel:
                print("Invalid selection")
                return 2
            # Prefer friendly name; fall back to alt
            name = sel.get("name") or sel.get("alt")
            if not name:
                print("Selected device has no usable name")
                return 2
            args.source = f'dshow:video="{name}"'
        except Exception:
            print("Invalid selection input")
            return 2

    if ok and ff:
        print(f"Using ffmpeg backend: {ff}") if args.verbose else None
        # Propagate CLI framerate/video-size via env for the dshow builder above
        if args.framerate:
            os.environ["QC_CAM_FPS"] = str(args.framerate)
        if args.video_size:
            os.environ["QC_CAM_VSIZE"] = str(args.video_size)
        run_ffmpeg(ff, args.source, out, args.segment_sec, args.label, stop_file, args.stop_when_exe, args.verbose, error_dir, args.error_window_sec)
        return 0
    # Fallback to OpenCV if ffmpeg not found
    try:
        print("Using OpenCV backend (ffmpeg not found)") if args.verbose else None
        return int(run_opencv(args.source, out, args.segment_sec, args.label, stop_file, args.stop_when_exe) or 0)
    except Exception as e:
        print("ERROR: OpenCV backend failed and ffmpeg not found.", file=sys.stderr)
        print("Install OpenCV: python -m pip install opencv-python", file=sys.stderr)
        print("Or specify ffmpeg: --ffmpeg C:\\QC\\cameras\\ffmpeg.exe", file=sys.stderr)
        print(f"Details: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
