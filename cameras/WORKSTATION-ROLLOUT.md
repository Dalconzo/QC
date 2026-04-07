# Workstation Rollout

This is the operator-facing rollout checklist for the local Hamilton camera
stack on one workstation.

## Install

1. Clone or update the repo onto the target machine.

```powershell
cd C:\
git clone https://github.com/Dalconzo/QC.git camera-tools
cd C:\camera-tools
git pull
```

2. Install Python and ffmpeg if they are not already available.

```powershell
winget install --id Python.Python.3.12 -e --source winget
python -m pip install psutil
```

3. Bootstrap the workstation with the desired camera source.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\install-camera-workstation.ps1 `
  -InstallFfmpeg `
  -CameraSource 'Arducam USB Camera' `
  -CameraLabel 'Top Camera' `
  -RunDaemonNow
```

4. If Task Scheduler install requires elevation, rerun that step from an
   elevated shell.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\install-camera-daemon-task.ps1 -RunNow
```

## Validate

Run the preflight after install or after changing the local override.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\test-camera-workstation.ps1 `
  -ProbeCamera `
  -StartReplay
```

Healthy output should show:

- no validation errors
- writable runs root
- camera probe `ok: true`
- replay site `ready: true`

Inspect stale manifests if the replay site shows `video missing` or
`trace missing`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\show-run-health.ps1
```

## Run Test

1. Confirm the daemon is installed and/or running.

```powershell
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

5. Open the local site.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\open-camera-site.ps1
```

## Update

When the repo changes on a deployed workstation:

```powershell
cd C:\camera-tools
git pull
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\install-camera-workstation.ps1 `
  -CameraSource 'Arducam USB Camera' `
  -CameraLabel 'Top Camera'
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\uninstall-camera-daemon-task.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File C:\camera-tools\cameras\install-camera-daemon-task.ps1 -RunNow
```

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
  Run the install from an elevated PowerShell window.
- Camera source `0` records the wrong device:
  Replace it with a named DirectShow source in the local override by rerunning
  `install-camera-workstation.ps1 -CameraSource 'Arducam USB Camera'`.
- Replay site does not start:
  Check `C:\camera-tools\logs\camera-replay.log` and rerun
  `test-camera-workstation.ps1 -StartReplay`.
- Camera probe fails but config validates:
  The device may be busy, unavailable, or not shareable by the driver. Run
  `test-camera-source.py` directly and inspect the ffmpeg error output.
