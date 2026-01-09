#!/usr/bin/env python3
"""
stop-recorder.py

Signals the camera recorder to stop by creating the configured stop file.
Recorder removes the file on graceful shutdown.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="Signal the camera recorder to stop")
    ap.add_argument("--stop-file", default="cameras.recorder.stop", help="Stop file to create (default: cameras.recorder.stop)")
    args = ap.parse_args()

    p = Path(args.stop_file)
    p.write_text("stop", encoding="utf-8")
    print(f"Created stop file: {p}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

