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

Machine-local Hamilton trace settings live in
[`camera-recorder.json`](/C:/QC/config/camera-recorder.json). Update that file
if a workstation writes `.trc` files somewhere other than
`C:\Program Files (x86)\HAMILTON\LogFiles`.

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
  - Defaults the Hamilton process gate to `HxRun.exe`.
  - Can write a persistent recorder diagnostics log with `-RecorderLog`.
- `replay-app.py`
  - Lightweight local replay server for completed runs.
  - Refreshes a local SQLite replay catalog from `.run.json` artifacts, serves
    the paired video, and rebuilds a trace terminal from Hamilton timestamps as
    playback moves forward or backward.
- `start-replay-app.ps1`
  - Operator-friendly PowerShell wrapper for the replay UI.
- `stop-recorder.py`
  - Creates the stop-file sentinel for graceful shutdown.

## Device Discovery

List available cameras before choosing a source:

```powershell
python C:\QC\cameras\camera-recorder.py --ffmpeg C:\QC\cameras\ffmpeg.exe --list-devices
```

Or interactively pick one:

```powershell
python C:\QC\cameras\camera-recorder.py --ffmpeg C:\QC\cameras\ffmpeg.exe --select-device
```

## Gate Smoke Test

Before using a real camera, confirm the wrapper defaults to the Hamilton Run
Manager gate and times out cleanly when `HxRun.exe` is absent:

```powershell
powershell -NoProfile -File C:\QC\cameras\test-start-recorder-gate.ps1
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

Or point it at a different run-manifest root:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-replay-app.ps1 `
  -RunsRoot C:\QC\cameras\video_clips\sim `
  -Port 5051
```

Then open the printed URL in a browser.

The replay UI currently provides:

- a catalog-backed run picker for locally captured replay artifacts
- the paired video on top
- a trace terminal below
- deterministic forward/rewind behavior because terminal contents are rebuilt
  from the trace lines whose elapsed time is less than or equal to the current
  playback position
- a refresh control that re-indexes newly captured runs without restarting the
  app
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

The replay backend has a small Python smoke test around manifest loading and
trace timestamp parsing:

```powershell
python C:\QC\cameras\test-replay-app.py
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
