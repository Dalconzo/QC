## LAN Replay Handoff

- Worktree: `C:\QC-LAN-Network`
- Branch: `codex/lan-website`
- Current local HEAD: `2ae6b3a` (`Add LAN heartbeat and pending run status flow`)
- Working tree: modified, not committed
- Push state: local follow-up fixes are still uncommitted and not pushed

### What Landed After Review

- Fixed the `/api/runs` limit regression in `cameras/central-replay-server.py`:
  - uploaded runs query now applies `LIMIT ?` in SQL
  - pending runs query now applies `LIMIT ?` in SQL
  - artifact lookup stays bounded to the limited uploaded run set
- Fixed stale workstation presence:
  - added `server.workstation_heartbeat_timeout_sec` with default `30.0`
  - `/api/workstations` now reports machines as offline unless a heartbeat is
    received within that timeout window
  - response now includes `is_online`, `last_reported_state`, and
    `heartbeat_timeout_sec`
- Added follow-up ops surface so the timeout is discoverable and configurable:
  - `config/central-replay-server.json`
  - `cameras/start-central-replay-server.ps1`
  - `cameras/README.md`
  - `cameras/CENTRAL-REPLAY-LAN-DEPLOYMENT.md`

### Validation

- Passed:
  - `python cameras\\test-central-replay-server.py`
  - `python cameras\\test-camera-daemon.py`

### Files Changed In Working Tree

- `cameras/central-replay-server.py`
- `cameras/test-central-replay-server.py`
- `config/central-replay-server.json`
- `cameras/start-central-replay-server.ps1`
- `cameras/README.md`
- `cameras/CENTRAL-REPLAY-LAN-DEPLOYMENT.md`
- `docs/LAN-NEXT-CHAT-HANDOFF.md`

### Recommended Next Step

1. Review the current uncommitted diff in `C:\QC-LAN-Network`.
2. Commit the follow-up fixes on `codex/lan-website`.
3. Push only after that commit is in place.
4. Continue with `QC-az4` unless the user redirects.

### Notes

- Beads is shared from `C:\QC\\.beads`.
- If `bd` is run from this worktree, use:
  - `BEADS_DIR=C:\\QC\\.beads`
  - `BEADS_NO_DAEMON=1`
- The repo's `bd` git hook integration has been flaky; if a commit hook blocks
  despite passing tests, inspect the hook failure before retrying.
