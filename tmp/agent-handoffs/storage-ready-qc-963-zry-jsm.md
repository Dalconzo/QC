Ready For Review

Issue IDs covered:
- `QC-963`
- `QC-zry`
- `QC-jsm`

Branch and worktree:
- branch: `qc-storage`
- worktree: `C:\QC-Boundary-Detection`

Files changed:
- `cameras/upload-central-replay.py`
- `cameras/workstation_release.py`
- `cameras/inspect-camera-config.py`
- `cameras/camera-daemon.py`
- `cameras/install-camera-workstation.ps1`
- `cameras/install-camera-daemon-task.ps1`
- `cameras/WORKSTATION-ROLLOUT.md`
- `cameras/README.md`
- `cameras/test-central-upload.py`
- `cameras/test-camera-tooling.py`
- `cameras/test-camera-daemon.py`

What changed:
- Central upload retry reuse is now storage-safe: reusing an existing `central_run_id` verifies the managed artifact bytes on disk, repairs missing or mismatched files from the staged source, and records per-artifact sync actions in the upload result.
- Workstation config inspection now exposes deployment branch/commit plus recorder contract status, including `hybrid-replay.v1`, replay capabilities, auto-upload, retention, and staging-prune settings.
- Daemon status JSON now carries the same deployment and contract metadata so operators can confirm the running background process matches the expected checkout.
- Workstation bootstrap and daemon-task install now print the deployed branch/commit and recorder contract, and the rollout doc now standardizes `git fetch`, `checkout`, `pull --ff-only`, reinstall, daemon rearm, and post-update verification.

Tests run:
- `python cameras\test-central-upload.py`
- `python cameras\test-central-pipeline-e2e.py`
- `python cameras\test-camera-tooling.py`
- `python cameras\test-camera-daemon.py`
- `powershell -NoProfile -ExecutionPolicy Bypass -File cameras\test-install-camera-workstation.ps1`

Storage-risk notes:
- Retry reuse no longer trusts an existing central catalog row blindly; corrupt or missing central artifact bytes are rewritten from the staged source before the run is acknowledged again.
- The new workstation metadata is read-only and should not affect artifact contents or replay identity.
- This slice does not add a full historical migration for already-uploaded stale central rows; it hardens future retries and reuploads.

Commit readiness:
- commit-ready

Notes:
- Repo-local `bd` in this worktree still points at the analytics tracker, so I could not apply `ready-for-review` / `review:*` labels for these storage issue IDs from here.
