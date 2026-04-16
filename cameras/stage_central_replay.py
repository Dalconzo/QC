#!/usr/bin/env python3
"""Import-friendly wrapper around stage-central-replay.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().with_name("stage-central-replay.py")
SPEC = importlib.util.spec_from_file_location("camera_stage_central_replay_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


CATALOG_FILENAME = MODULE.CATALOG_FILENAME
stage_runs = MODULE.stage_runs
