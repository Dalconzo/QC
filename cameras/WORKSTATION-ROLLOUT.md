# Workstation Rollout

This is the operator-facing rollout checklist for the local Hamilton camera
stack on one workstation.

## Install

Choose the rollout lane first:

- `modern`
  Windows 10/11 workstation with modern PowerShell and the current Python camera stack.
- `legacy-windows`
  Older/offline workstation such as Windows 7. This lane keeps the same local artifact format, but does not assume `git`, `winget`, or a workstation-local Python install.

If you do not pass `-CompatibilityMode`, the bootstrap now auto-detects the OS
and switches to `legacy-windows` on pre-Windows-10 machines.

1. Clone or update the repo onto the target machine.

```powershell
cd C:\
git clone https://github.com/Dalconzo/QC.git camera-tools
cd C:\camera-tools
git fetch origin
git checkout main
git pull --ff-only origin main
```

2. Install Python and ffmpeg if they are not already available.

```powershell
winget install --id Python.Python.3.12 -e --source winget
python -m pip install psutil
```

3. Bootstrap the workstation with the desired camera source and run the
   lower-layer camera probe immediately.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\install-camera-workstation.ps1 `
  -InstallFfmpeg `
  -MachineAlias H7 `
  -CameraSource 'Arducam USB Camera' `
  -CameraLabel 'Top Camera' `
  -ProbeCamera
```

4. After the camera probe passes, install the daemon task. If Task Scheduler
   install requires elevation, run this step from an elevated shell. On older
   Windows versions the installer now falls back to `schtasks.exe`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\install-camera-daemon-task.ps1 -RunNow
```

## Legacy Windows Install

Use this path for Windows 7 or other offline/older workstations.

1. On a newer build machine, build the packaged runtime tools:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\QC\cameras\build-camera-runtime.ps1
```

2. Copy the repo folder to the workstation without `git`, including
   `C:\camera-tools\cameras\dist\legacy-runtime\`.
3. Use the bundled `C:\camera-tools\cameras\dist\ffmpeg.exe` or provide an
   explicit `-FfmpegPath`.
4. Bootstrap in legacy mode:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\install-camera-workstation.ps1 `
  -CompatibilityMode legacy-windows `
  -FfmpegPath C:\camera-tools\cameras\dist\ffmpeg.exe `
  -MachineAlias H7 `
  -CameraSource 'Arducam USB Camera' `
  -CameraLabel 'Top Camera' `
  -ProbeCamera
```

5. Validate the local layer:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\test-camera-workstation.ps1 -ProbeCamera
```

6. Install daemon auto-start. Legacy mode now uses `schtasks.exe` automatically:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\install-camera-daemon-task.ps1 -RunNow
```

Legacy notes:

- `install-camera-daemon-task.ps1` now uses `schtasks.exe` automatically on
  legacy systems instead of the newer ScheduledTasks cmdlets.
- `install-camera-workstation.ps1`, `start-camera-daemon.ps1`,
  `start-recorder.ps1`, and preflight/status scripts now prefer the packaged
  runtime tools under `cameras\dist\legacy-runtime` when
  `workstation.compatibility_mode` is `legacy-windows`.
- If `cameras\dist\legacy-runtime\camera-daemon.exe` or
  `camera-recorder.exe` is missing, legacy mode will warn during bootstrap and
  runtime commands will still fail on machines without a usable Python install.
- Keep central ingest optional. Copy finished artifacts or staged bundles to a
  newer LAN-connected machine when needed.

## Validate

Run the full workstation preflight after install or after changing the local override.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\test-camera-workstation.ps1 `
  -ProbeCamera `
  -StartReplay
```

Healthy output should show:

- no validation errors
- the expected deployment branch and commit
- recorder contract `hybrid-replay.v1`
- writable runs root
- the expected `workstation.machine_alias`
- the expected `workstation.compatibility_mode`
- the expected daemon `runtime_mode` for that workstation class
- camera probe `ok: true`
- replay site `ready: true`

Inspect stale manifests if the replay site shows `video missing` or
`trace missing`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\show-run-health.ps1
```

To verify the configured machine alias directly:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\show-camera-config.ps1 -AsJson
```

## Run Test

1. Confirm the daemon is installed and/or running.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\show-camera-config.ps1 -Validate
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\show-camera-daemon-status.ps1
```

2. Run one short simulated method in `HxRun.exe`.
3. Close `HxRun.exe`.
4. Confirm one new `.mp4` and `.run.json` exist under the local runs root.

```powershell
Get-ChildItem C:\camera-tools\cameras\video_clips -Recurse |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 10 LastWriteTime, Length, FullName
```

The newest files should now land under a machine subfolder such as
`C:\camera-tools\cameras\video_clips\H7\`. When a trace match exists, the
finalized names should end in `_H7.mp4` and `_H7.run.json`.

5. Open the local site.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\open-camera-site.ps1
```

## Update

When the repo changes on a deployed workstation:

```powershell
cd C:\camera-tools
git fetch origin
git checkout main
git pull --ff-only origin main
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\install-camera-workstation.ps1 `
  -MachineAlias H7 `
  -CameraSource 'Arducam USB Camera' `
  -CameraLabel 'Top Camera'
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\uninstall-camera-daemon-task.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\install-camera-daemon-task.ps1 -RunNow
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\show-camera-config.ps1 -Validate
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\show-camera-daemon-status.ps1
```

The post-update checks should confirm:

- the expected deployment branch and commit
- recorder contract `hybrid-replay.v1`
- the daemon is running from the refreshed checkout

## Rollback

Remove the auto-start daemon:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\uninstall-camera-daemon-task.ps1 -StopFirst
```

Remove the desktop/start-menu shortcuts:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\uninstall-local-camera-tools.ps1
```

If needed, remove the workstation-local override:

```powershell
Remove-Item C:\camera-tools\config\camera-recorder.local.json
```

If local test artifacts should be cleared:

```powershell
Remove-Item C:\camera-tools\cameras\video_clips -Recurse -Force
Remove-Item C:\camera-tools\logs -Recurse -Force
```

## Common Failures

- `video missing` in the site:
  The `.run.json` manifest points at an `.mp4` path that no longer exists.
  Run `show-run-health.ps1` and quarantine or delete the stale manifest.
- Scheduled Task install says `Access is denied`:
  The bootstrap keeps the workstation config even if task registration fails
  unless `-RequireDaemonTask` is supplied. Rerun the daemon-task install from
  an elevated PowerShell window.
- Daemon task install behaves differently on a legacy workstation:
  That is expected when `workstation.compatibility_mode` is `legacy-windows`.
  The script now falls back to `schtasks.exe` rather than the newer
  ScheduledTasks cmdlets.
- Camera source `0` records the wrong device:
  Numeric fallback sources are now blocked by default. Rerun
  `install-camera-workstation.ps1 -CameraSource 'Arducam USB Camera'`.
- Replay site does not start:
  Check `C:\camera-tools\logs\camera-replay.log` and rerun
  `test-camera-workstation.ps1 -StartReplay`.
- Camera probe fails but config validates:
  The device may be busy, unavailable, or not shareable by the driver. Validate
  bottom-up: raw ffmpeg capture, then `test-camera-source.py`, then the
  foreground recorder, then the daemon.
