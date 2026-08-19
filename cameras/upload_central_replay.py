#!/usr/bin/env python3
"""Import-friendly wrapper around upload-central-replay.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().with_name("upload-central-replay.py")
SPEC = importlib.util.spec_from_file_location("camera_upload_central_replay_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


CENTRAL_CATALOG_FILENAME = MODULE.CENTRAL_CATALOG_FILENAME
init_central_db = MODULE.init_central_db
upload_staged_runs = MODULE.upload_staged_runs
