#!/usr/bin/env python3
"""
test-camera-source.py

Grab one frame from a configured camera profile and optionally save it.

This is the lightweight rollout probe for workstations where DirectShow device
enumeration is flaky or too slow to trust.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, load_effective_config, validate_config
from camera_live import capture_live_frame


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe one Hamilton camera source by capturing a single frame")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the base camera config JSON")
    parser.add_argument("--local-config", default=str(DEFAULT_LOCAL_OVERRIDE_PATH), help="Path to the optional workstation-local override JSON")
    parser.add_argument("--profile", default="", help="Camera profile id to probe")
    parser.add_argument("--output", default="", help="Optional path where the captured JPEG frame should be written")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    config = load_effective_config(
        config_path=Path(args.config).resolve(),
        local_override_path=Path(args.local_config).resolve(),
    )
    validation = validate_config(config, require_hamilton_log_dir=False)
    if validation["errors"]:
        payload = {"ok": False, "errors": validation["errors"], "warnings": validation["warnings"]}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            for item in validation["errors"]:
                print(item)
        return 1

    image_bytes, profile, ffmpeg_path = capture_live_frame(config, args.profile or None)
    output_path = ""
    if args.output:
        target = Path(args.output).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image_bytes)
        output_path = str(target)

    payload = {
        "ok": True,
        "profile": profile,
        "ffmpeg_path": ffmpeg_path,
        "bytes_captured": len(image_bytes),
        "output_path": output_path,
        "warnings": validation["warnings"],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Captured {payload['bytes_captured']} bytes from profile {profile['id']} [{profile['label']}]")
        print(f"Source: {profile['source']}")
        print(f"ffmpeg: {ffmpeg_path}")
        if output_path:
            print(f"Saved frame: {output_path}")
        for warning in validation["warnings"]:
            print(f"Warning: {warning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
