# associate_clips_with_logs.py
import re
from datetime import datetime
from pathlib import Path
import csv

import argparse

VIDEO_DIR = Path("video_clips")
LOG_DIR = Path("logs")
OUTPUT_CSV = Path("clip_log_mapping.csv")

# Hamilton trace lines begin with "YYYY-MM-DD HH:MM:SS>"; we parse the 19-char timestamp.
LOG_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

def parse_clip_timestamp(filename: str):
    # expects "YYYYMMDD_HHMMSS_..." at start
    m = re.match(r"(\d{8}_\d{6})_", filename)
    if not m:
        return None
    ts_str = m.group(1)
    return datetime.strptime(ts_str, "%Y%m%d_%H%M%S")

def extract_log_timestamps(log_path: Path):
    timestamps = []
    with log_path.open("r", encoding="utf-8", errors="ignore") as f:
        for lineno, line in enumerate(f, start=1):
            # Parse the first 19 chars as "YYYY-MM-DD HH:MM:SS" (before the '>')
            raw = line[:19]
            try:
                ts = datetime.strptime(raw, LOG_TS_FORMAT)
                timestamps.append((ts, lineno, line.strip()))
            except ValueError:
                continue
    return timestamps

def find_best_match(ts, log_ts_list):
    if not log_ts_list:
        return None
    best = None
    best_delta = None
    for log_ts, lineno, line in log_ts_list:
        delta = abs((log_ts - ts).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = (log_ts, lineno, line, best_delta)
    return best

def main():
    ap = argparse.ArgumentParser(description="Associate camera clips to nearest log/trace lines by timestamp")
    ap.add_argument("--video-dir", default=str(VIDEO_DIR), help="Directory containing mp4 clips (default: video_clips)")
    ap.add_argument("--log-dir", default=str(LOG_DIR), help="Directory containing logs or traces (default: logs)")
    ap.add_argument("--log-glob", default="*.log;*.trc", help="Semicolon-separated glob(s) to match logs (default: *.log;*.trc)")
    ap.add_argument("--out-csv", default=str(OUTPUT_CSV), help="Output CSV path (default: clip_log_mapping.csv)")
    args = ap.parse_args()

    vdir = Path(args.video_dir)
    ldir = Path(args.log_dir)
    out_csv = Path(args.out_csv)

    patterns = [p for p in args.log_glob.split(";") if p]
    logs = []
    for pat in patterns:
        logs.extend(ldir.glob(pat))
    if not logs:
        print("No log files found.")
        return

    # Preload timestamps for each log
    log_index = {}
    for log in logs:
        log_index[log] = extract_log_timestamps(log)

    clips = list(vdir.glob("*.mp4"))
    if not clips:
        print("No clips found.")
        return

    rows = []
    for clip in clips:
        ts = parse_clip_timestamp(clip.name)
        if ts is None:
            continue

        best_overall = None
        best_log = None

        for log, ts_list in log_index.items():
            match = find_best_match(ts, ts_list)
            if match is None:
                continue
            log_ts, lineno, line, delta = match
            if best_overall is None or delta < best_overall[-1]:
                best_overall = (log_ts, lineno, line, delta)
                best_log = log

        if best_overall:
            log_ts, lineno, line, delta = best_overall
            rows.append({
                "clip": clip.name,
                "clip_ts": ts.isoformat(),
                "log_file": best_log.name if best_log else "",
                "log_ts": log_ts.isoformat(),
                "line_no": lineno,
                "delta_s": f"{delta:.2f}",
                "log_line": line,
            })

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "clip", "clip_ts", "log_file", "log_ts", "line_no", "delta_s", "log_line"
        ])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    print(f"Wrote mapping to {out_csv}")

if __name__ == "__main__":
    main()
