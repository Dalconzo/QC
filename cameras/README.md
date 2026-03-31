# Camera Recorder

This folder holds the local camera capture utilities used alongside Hamilton
runs.

The recorder now follows the Hamilton run lifecycle more directly:

- wait for `HxRun.exe`
- record one continuous MP4 for that run
- stop when `HxRun.exe` exits, a stop file is created, or a safety timeout hits
- look in the configured Hamilton log directory for the `.trc` file whose
  last-write time is closest to recorder shutdown
- write a run manifest that pairs the video and trace file for later replay

Machine-local Hamilton trace settings, storage roots, replay defaults, and
camera profiles live in [`camera-recorder.json`](/C:/QC/config/camera-recorder.json).
Use an optional workstation-local override at
`C:\QC\config\camera-recorder.local.json` when one Hamilton PC needs different
settings from the repo default.

## Current Recorder Flow

- `camera-recorder.py`
  - Records one camera source into one MP4 per HxRun session.
  - Can wait to start until a process appears with `--start-when-exe`.
  - Can stop automatically when that process disappears with `--stop-when-exe`.
  - Can apply a `--max-record-sec` safety cap for cases where operators leave
    HxRun open after the assay is finished.
  - Writes a `.run.json` manifest beside the video so replay tooling has a
    stable video/trace pairing artifact to load.
- `start-recorder.ps1`
  - Operator-friendly PowerShell wrapper around `camera-recorder.py`.
  - Forwards the shared config plus any explicit CLI overrides into the Python
    recorder.
  - Can write a persistent recorder diagnostics log with `-RecorderLog`.
- `replay-app.py`
  - Lightweight local replay server for completed runs.
  - Refreshes a local SQLite replay catalog from `.run.json` artifacts, serves
    the paired video, and rebuilds a trace terminal from Hamilton timestamps as
    playback moves forward or backward.
- `start-replay-app.ps1`
  - Operator-friendly PowerShell wrapper for the replay UI.
  - Can start the local replay server in the background and open the browser in
    replay or live-preview mode.
- `open-latest-run.ps1`
  - One-click launcher that opens the freshest replayable local run.
- `install-local-camera-tools.ps1`
  - Installs desktop/start-menu shortcuts for the local replay workflow.
- `uninstall-local-camera-tools.ps1`
  - Removes those workstation shortcuts.
- `install-camera-workstation.ps1`
  - One-command bootstrap for a new workstation: writes the local override,
    creates folders, validates config, installs shortcuts, and can install the
    daemon Scheduled Task.
- `show-camera-config.ps1`
  - Prints the effective camera workstation config, lists profiles, or validates
    the current machine before rollout.
- `camera-daemon.py`
  - Always-on workstation supervisor that waits for `HxRun.exe`, launches one
    recorder child per run, and returns to idle for the next run.
- `start-camera-daemon.ps1`
  - Starts the camera daemon in the background for normal workstation use or in
    the foreground for debugging.
- `stop-camera-daemon.ps1`
  - Stops the daemon and asks any active recorder child to finalize cleanly.
- `show-camera-daemon-status.ps1`
  - Shows whether the workstation is idle, recording, or stopped.
- `install-camera-daemon-task.ps1`
  - Installs a Scheduled Task so the daemon starts automatically at user logon.
- `uninstall-camera-daemon-task.ps1`
  - Removes that auto-start task when a workstation is being reconfigured.
- `stop-recorder.py`
  - Creates the stop-file sentinel for graceful shutdown.

## Device Discovery

List the effective camera profiles first if you want to see what this
workstation currently has configured:

```powershell
powershell -NoProfile -File C:\QC\cameras\show-camera-config.ps1 -ListProfiles
```

List available cameras before choosing a source:

```powershell
python C:\QC\cameras\camera-recorder.py --ffmpeg C:\QC\cameras\ffmpeg.exe --list-devices
```

Or interactively pick one:

```powershell
python C:\QC\cameras\camera-recorder.py --ffmpeg C:\QC\cameras\ffmpeg.exe --select-device
```

Or let the workstation bootstrap script show the same list:

```powershell
powershell -NoProfile -File C:\QC\cameras\install-camera-workstation.ps1 -ListDevices
```

## One-Command Workstation Bootstrap

For a new Hamilton PC, the fastest rollout path is now the bootstrap script:

```powershell
powershell -NoProfile -File C:\QC\cameras\install-camera-workstation.ps1 `
  -CameraSource 'dshow:video="YOUR CAMERA NAME"' `
  -CameraLabel 'Top Camera' `
  -RunDaemonNow
```

That command:

- writes `C:\QC\config\camera-recorder.local.json`
- creates the local video/log folders
- validates the merged workstation config
- installs the desktop/start-menu replay shortcuts
- installs the daemon Scheduled Task
- starts the daemon immediately when `-RunDaemonNow` is supplied

Override the Hamilton trace path, output root, log root, or replay port if a
specific workstation differs from the repo defaults:

```powershell
powershell -NoProfile -File C:\QC\cameras\install-camera-workstation.ps1 `
  -CameraSource 'dshow:video="YOUR CAMERA NAME"' `
  -HamiltonLogDir 'D:\Hamilton\LogFiles' `
  -RunsRoot 'D:\QC\camera_runs' `
  -RecorderLogDir 'D:\QC\logs' `
  -ReplayPort 5055 `
  -RunDaemonNow
```

## Gate Smoke Test

Before using a real camera, confirm the wrapper defaults to the Hamilton Run
Manager gate and times out cleanly when `HxRun.exe` is absent:

```powershell
powershell -NoProfile -File C:\QC\cameras\test-start-recorder-gate.ps1
```

To validate the merged base config plus any local workstation override:

```powershell
powershell -NoProfile -File C:\QC\cameras\show-camera-config.ps1 -Validate
```

## Bench Test With The Real Camera

Use this first to prove the real camera, ffmpeg, and process gating all work
without involving Hamilton.

1. Start a harmless Windows process such as Notepad.
2. Start the recorder and gate it on that process:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-recorder.ps1 `
  -Source 'dshow:video="YOUR CAMERA NAME"' `
  -OutDir C:\QC\cameras\video_clips\bench `
  -Label bench `
  -StartWhenExe notepad.exe `
  -VerboseRecorder
```

3. Confirm one MP4 appears under `video_clips\bench`.
4. Close Notepad.
5. Confirm the recorder stops on its own and writes a `.run.json` manifest.

## Hamilton Simulation-Mode Test With The Real Camera

Use one of the test instruments (`H7`, `H13`, `H14`) so this does not touch
production.

1. Put the Hamilton into simulation mode.
2. Start the recorder against the real camera. The PowerShell wrapper already
   defaults the startup and stop gate to `HxRun.exe`:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-recorder.ps1 `
  -Source 'dshow:video="YOUR CAMERA NAME"' `
  -OutDir C:\QC\cameras\video_clips\sim `
  -Label h7-sim `
  -RecorderLog C:\QC\logs\h7-sim-recorder.log `
  -VerboseRecorder
```

3. Launch Run Manager on the Hamilton PC and start a short simulated method.
4. Confirm recording begins only after `HxRun.exe` appears.
5. Let the simulated method finish, then close Run Manager.
6. Confirm the recorder stops automatically and writes:
   - one MP4 for the run
   - one `.run.json` manifest with the paired trace path
   - one recorder log file if `-RecorderLog` was supplied

## Recorder Diagnostics

For live troubleshooting, create the output and log folders first:

```powershell
New-Item -ItemType Directory -Force -Path C:\QC\cameras\video_clips\sim
New-Item -ItemType Directory -Force -Path C:\QC\logs
```

Then run the recorder with a persistent log file:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-recorder.ps1 `
  -Source 'dshow:video="YOUR CAMERA NAME"' `
  -OutDir C:\QC\cameras\video_clips\sim `
  -Label h7-sim `
  -MaxRecordSec 1800 `
  -RecorderLog C:\QC\logs\h7-sim-recorder.log `
  -VerboseRecorder
```

The recorder log captures:

- startup gate polling
- backend selection and ffmpeg command line
- periodic capture heartbeats every 5 seconds
- stop reason
- final paired trace path and pairing delta

The daemon has its own persistent diagnostics and status files too:

- `C:\QC\logs\camera-daemon.log`
- `C:\QC\logs\camera-daemon-status.json`
- `C:\QC\logs\camera-daemon.pid`

The replay launcher also writes a local server log by default:

- `C:\QC\logs\camera-replay.log`

## Live Local View

The local camera console now has two modes:

- `Replay`
  - inspect completed run videos and their paired Hamilton traces
- `Live View`
  - grab still preview frames from a configured workstation camera profile
    during or outside a run without waiting for replay artifacts

Open the live preview directly in the browser with:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-replay-app.ps1 `
  -Background `
  -OpenBrowser `
  -LiveView
```

The live preview path is intentionally lightweight in v1:

- it uses the same configured camera profiles as the recorder
- each browser refresh asks ffmpeg for one JPEG frame
- it does not create a second long-lived capture daemon

That keeps the preview logic easy to deploy, but camera sharing is still up to
the device driver. Some cameras will allow preview while recording; others may
refuse a second reader.

## Always-On Workstation Mode

For the prototype rollout, the intended local workflow is:

- install one camera profile per workstation
- start the daemon once or install its Scheduled Task
- let the daemon sit idle until `HxRun.exe` appears
- let the daemon launch one recorder child for that run
- open the local replay app after the run if engineers need immediate review

Start it manually in the background:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-camera-daemon.ps1
```

Start it in the foreground while debugging rollout:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-camera-daemon.ps1 -Foreground
```

Inspect its current state:

```powershell
powershell -NoProfile -File C:\QC\cameras\show-camera-daemon-status.ps1
```

Stop it cleanly:

```powershell
powershell -NoProfile -File C:\QC\cameras\stop-camera-daemon.ps1
```

Install auto-start at user logon:

```powershell
powershell -NoProfile -File C:\QC\cameras\install-camera-daemon-task.ps1 -RunNow
```

Remove the auto-start task:

```powershell
powershell -NoProfile -File C:\QC\cameras\uninstall-camera-daemon-task.ps1 -StopFirst
```

The current auto-start story uses Windows Task Scheduler instead of a Windows
service because interactive camera devices are usually more reliable in the
logged-in workstation session than under `SYSTEM`.

## Run Manifest

Each completed recording writes a sidecar manifest like:

```json
{
  "video_path": "C:\\QC\\cameras\\video_clips\\sim\\20260324_130000_h7-sim.mp4",
  "trace_path": "C:\\Program Files (x86)\\HAMILTON\\LogFiles\\SomeRun_Trace.trc",
  "started_at_local": "2026-03-24T13:00:00.000000",
  "stopped_at_local": "2026-03-24T13:08:12.000000",
  "stop_reason": "process_exit",
  "trace_mtime_delta_sec": 4.12
}
```

That manifest is the handoff point for the upcoming replay UI.

## Replay UI

Once you have one or more completed `.run.json` manifests, launch the replay
app with:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-replay-app.ps1
```

That keeps the replay server attached to the current terminal. For normal
workstation use, start it in the background and open the browser immediately:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-replay-app.ps1 `
  -Background `
  -OpenBrowser
```

Or jump straight into the newest replayable local run:

```powershell
powershell -NoProfile -File C:\QC\cameras\open-latest-run.ps1
```

Or open the local live camera view:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-replay-app.ps1 `
  -Background `
  -OpenBrowser `
  -LiveView
```

Or point the replay app at a different run-manifest root:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-replay-app.ps1 `
  -RunsRoot C:\QC\cameras\video_clips\sim `
  -Port 5051
```

To install desktop/start-menu shortcuts for engineers on a workstation:

```powershell
powershell -NoProfile -File C:\QC\cameras\install-local-camera-tools.ps1
```

Remove those shortcuts with:

```powershell
powershell -NoProfile -File C:\QC\cameras\uninstall-local-camera-tools.ps1
```

`start-replay-app.ps1` now uses the replay host, port, and default runs root
from the shared camera config unless you override them on the command line. In
background mode it also waits for the server to come up before opening the
browser, so operators do not land on a dead page during startup.

The replay UI currently provides:

- a catalog-backed run picker for locally captured replay artifacts
- a live-view mode that uses configured camera profiles
- the paired video on top
- a trace terminal below
- deterministic forward/rewind behavior because terminal contents are rebuilt
  from the trace lines whose elapsed time is less than or equal to the current
  playback position
- a refresh control that re-indexes newly captured runs without restarting the
  app
- URL-addressable run selection, so workstation shortcuts can open the latest
  replayable run directly
- URL-addressable mode selection, so workstation shortcuts can open live view
  directly
- placeholder camera-view tabs so the later multi-camera view work has a stable
  UI slot to grow into

The timing model is intentionally simple:

- the first timestamped trace line is treated as time zero
- each later trace line is assigned an elapsed time from that first line
- when the video playhead moves, the terminal is regenerated from the trace
  events up to that elapsed time

That means rewind does not try to "undo" state line by line; it just
reconstructs the terminal for the newly selected playback time.

## Replay Smoke Test

The replay backend has a small Python smoke test around manifest loading, trace
timestamp parsing, and live-preview API endpoints:

```powershell
python C:\QC\cameras\test-replay-app.py
python C:\QC\cameras\test-camera-config.py
python C:\QC\cameras\test-camera-daemon.py
powershell -NoProfile -File C:\QC\cameras\test-camera-daemon.ps1
powershell -NoProfile -File C:\QC\cameras\test-install-camera-workstation.ps1
```

When the replay app starts, it also creates or updates a local SQLite catalog
beside the selected runs root:

- `C:\QC\cameras\video_clips\<folder>\.replay_catalog.sqlite3`

That catalog is the current bridge between workstation-local recorder output
and the later central SQL-backed replay service.

## Legacy Utilities

`associate-clips-with-logs.py` and `validate-run-sync.ps1` are still in the
repo from the older segment-based flow. They are no longer the primary path for
new work now that the recorder is moving to continuous per-run capture and
trace-driven replay.
