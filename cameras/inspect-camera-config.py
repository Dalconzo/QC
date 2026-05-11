#!/usr/bin/env python3
"""
inspect-camera-config.py

Small operator-facing helper for camera config inspection.

This gives us one command to answer "what config is this workstation actually
using?" before we move on to the always-on daemon work.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from camera_config import DEFAULT_CONFIG_PATH, DEFAULT_LOCAL_OVERRIDE_PATH, get_profile, load_effective_config, validate_config
from workstation_release import build_contract_status, get_deployment_status


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect effective Hamilton camera workstation config")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the base camera config JSON")
    parser.add_argument(
        "--local-config",
        default=str(DEFAULT_LOCAL_OVERRIDE_PATH),
        help="Path to the optional workstation-local camera override JSON",
    )
    parser.add_argument("--profile", default="", help="Profile id to highlight in the output")
    parser.add_argument("--list-profiles", action="store_true", help="Print camera profiles only")
    parser.add_argument("--validate", action="store_true", help="Validate the effective config and return a non-zero exit code on errors")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    config = load_effective_config(
        config_path=Path(args.config),
        local_override_path=Path(args.local_config),
    )
    validation = validate_config(config)

    payload = {
        "config_path": config["config_path"],
        "local_override_path": config["local_override_path"],
        "local_override_exists": config["local_override_exists"],
        "deployment": get_deployment_status(Path(config["config_path"]).resolve().parents[1]),
        "contract_status": build_contract_status(config),
        "validation": validation,
    }

    if args.list_profiles:
        payload["profiles"] = config["profiles"]
    else:
        payload["config"] = config

    if args.profile:
        try:
            payload["selected_profile"] = get_profile(config, args.profile)
        except KeyError:
            payload["selected_profile"] = None
            validation["errors"].append(f"Unknown camera profile: {args.profile}")

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Base config: {payload['config_path']}")
        print(f"Local override: {payload['local_override_path']} (exists={payload['local_override_exists']})")
        deployment = payload["deployment"]
        print(
            "Deployment: "
            f"branch={deployment['git_branch'] or '(unknown)'} "
            f"commit={deployment['git_commit_short'] or '(unknown)'} "
            f"dirty={deployment['git_is_dirty']}"
        )
        contract = payload["contract_status"]
        print(
            "Recorder contract: "
            f"manifest={contract['replay_manifest_version']} "
            f"midrun_split={contract['midrun_split_enabled']} "
            f"auto_upload={contract['auto_upload_on_run_complete']} "
            f"retention_days={contract['original_retention_days']} "
            f"prune_after_ack={contract['prune_after_ack']}"
        )
        if args.list_profiles:
            print("Profiles:")
            for profile in payload["profiles"]:
                print(f"  - {profile['id']}: {profile['label']} [{profile['source']}]")
        else:
            print(json.dumps(payload["config"], indent=2))
        if validation["warnings"]:
            print("Warnings:")
            for item in validation["warnings"]:
                print(f"  - {item}")
        if validation["errors"]:
            print("Errors:")
            for item in validation["errors"]:
                print(f"  - {item}")
        elif args.validate:
            print("Validation passed.")

    return 1 if args.validate and validation["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
