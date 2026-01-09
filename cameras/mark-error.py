#!/usr/bin/env python3
"""mark-error.py

Create a small mark file indicating that an error occurred "now" so the
camera-recorder can cut the last N seconds into an error clip.

Usage (from C:\\QC):
  python cameras\\mark-error.py --marks-dir cameras\\marks

When wrapped as an .exe and called by Hamilton's "On error" hook, this will
drop a mark file named YYYYMMDD_HHMMSS.mark under the marks directory.
The recorder polls this directory and writes error clips to error_clips/.
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="Create an error mark file for camera-recorder")
    ap.add_argument("--marks-dir", default="cameras/marks", help="Directory to place mark files")
    args = ap.parse_args()

    marks = Path(args.marks_dir)
    marks.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    name = now.strftime("%Y%m%d_%H%M%S") + ".mark"
    path = marks / name
    path.write_text("error", encoding="utf-8")
    print(f"Created mark: {path}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

