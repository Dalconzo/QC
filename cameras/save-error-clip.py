#!/usr/bin/env python3
"""
save-error-clip.py

Given an error timestamp, extract a short mp4 clip around it from the segmented
files written by camera-recorder. Prefers ffmpeg for trimming; falls back to copying
the containing segment if ffmpeg is not available.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import subprocess
from pathlib import Path


def parse_iso_ts(s: str) -> dt.datetime:
    # Accept 'YYYY-MM-DD HH:MM:SS' or ISO variants
    s = s.strip().replace("T", " ")
    if "+" in s or s.endswith("Z"):
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    fmts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
    for f in fmts:
        try:
            return dt.datetime.strptime(s, f)
        except Exception:
            pass
    return dt.datetime.fromisoformat(s)


def parse_segment_ts(name: str) -> dt.datetime | None:
    # Expect YYYYMMDD_HHMMSS_label.mp4
    try:
        ts = name.split("_")[0] + "_" + name.split("_")[1]
        return dt.datetime.strptime(ts, "%Y%m%d_%H%M%S")
    except Exception:
        return None


def find_segment(video_dir: Path, target: dt.datetime) -> Path | None:
    candidates = sorted(video_dir.glob("*.mp4"))
    best = None
    best_delta = None
    for p in candidates:
        ts = parse_segment_ts(p.name)
        if not ts:
            continue
        delta = abs((target - ts).total_seconds())
        if best is None or delta < best_delta:
            best = p
            best_delta = delta
    return best


def has_ffmpeg(explicit: str | None = None) -> tuple[bool, str | None]:
    # 1) explicit path
    if explicit:
        p = Path(explicit)
        if p.exists():
            return True, str(p)
    # 2) side-by-side ffmpeg.exe next to this script/exe
    here = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) if 'sys' in globals() else Path(__file__).parent  # type: ignore
    local_ff = here / "ffmpeg.exe"
    if local_ff.exists():
        return True, str(local_ff)
    # 3) PATH
    exe = shutil.which("ffmpeg")
    return (exe is not None), exe


def find_latest_error_ts(log_dir: Path, pattern: str = "*.trc") -> dt.datetime | None:
    """Scan trace logs for the most recent line containing an error and return its timestamp.

    Assumes trace lines begin with "YYYY-MM-DD HH:MM:SS>" as in Hamilton logs.
    """
    files = sorted(log_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    latest: dt.datetime | None = None
    ts_regex = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})>")
    for f in files:
        try:
            with f.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    m = ts_regex.match(line)
                    if not m:
                        continue
                    ts_str = m.group(1)
                    try:
                        ts = dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue
                    # Heuristic: treat any line containing 'error' (case-insensitive) as an error event.
                    if "error" in line.lower():
                        if latest is None or ts > latest:
                            latest = ts
        except Exception:
            continue
    return latest


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract a small clip around an error time from segmented camera files")
    ap.add_argument("--timestamp", default="", help="Error timestamp (e.g., '2025-11-13 15:23:36'); if omitted, auto-detect latest error in logs")
    ap.add_argument("--video-dir", default="video_clips", help="Source video clips directory")
    ap.add_argument("--out-dir", default="error_clips", help="Output directory for extracted clips")
    ap.add_argument("--pre-sec", type=int, default=10, help="Seconds before timestamp to include")
    ap.add_argument("--post-sec", type=int, default=10, help="Seconds after timestamp to include")
    ap.add_argument("--ffmpeg", default="", help="Optional path to ffmpeg.exe; if omitted, searches next to the exe/script and in PATH")
    ap.add_argument("--log-dir", default=r"C:\\Program Files (x86)\\HAMILTON\\LogFiles", help="Hamilton trace log directory for auto error detection")
    ap.add_argument("--log-glob", default="*.trc", help="Glob pattern for trace files inside log-dir")
    args = ap.parse_args()

    # Determine target timestamp: explicit or derived from latest error in logs.
    if args.timestamp:
        target = parse_iso_ts(args.timestamp)
    else:
        log_dir = Path(args.log_dir)
        if not log_dir.exists():
            print(f"Log directory does not exist: {log_dir}")
            return 1
        latest = find_latest_error_ts(log_dir, args.log_glob)
        if not latest:
            print("No error timestamps found in logs; nothing to clip.")
            return 1
        target = latest

    vdir = Path(args.video_dir)
    odir = Path(args.out_dir)
    odir.mkdir(parents=True, exist_ok=True)

    seg = find_segment(vdir, target)
    if not seg:
        print("No segment found")
        return 1
    seg_start = parse_segment_ts(seg.name)
    if not seg_start:
        print("Segment lacks parsable timestamp")
        return 1

    total = args.pre_sec + args.post_sec
    start_offset = max(0, (target - seg_start).total_seconds() - args.pre_sec)
    out_path = odir / f"{target.strftime('%Y%m%d_%H%M%S')}_clip.mp4"

    ok, ff = has_ffmpeg(args.ffmpeg or None)
    if ok:
        cmd = [
            ff or "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(start_offset),
            "-i", str(seg),
            "-t", str(total),
            "-c", "copy",
            str(out_path),
        ]
        subprocess.check_call(cmd)
    else:
        # Fallback: just copy the whole segment
        shutil.copy2(seg, out_path)

    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
