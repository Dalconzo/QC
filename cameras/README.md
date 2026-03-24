# Camera Recorder

This folder holds the local camera capture utilities used alongside Hamilton
runs. The current recorder writes timestamped MP4 segments and can optionally
gate recording on a watched process lifecycle.

## Current Recorder Flow

- `camera-recorder.py`
  - Records one camera source into timestamped MP4 segments.
  - Can wait to start until a process appears with `--start-when-exe`.
  - Can stop automatically when that process disappears with `--stop-when-exe`.
  - Supports DirectShow webcams through ffmpeg and falls back to OpenCV if
    ffmpeg is unavailable.
- `stop-recorder.py`
  - Creates the stop-file sentinel for graceful shutdown.
- `mark-error.py`
  - Drops a timestamped marker file so the recorder can promote the next
    completed segment into `error_clips`.
- `start-recorder.ps1`
  - Operator-friendly PowerShell wrapper around `camera-recorder.py`.
  - Defaults the Hamilton process gate to `HxRun.exe` so the normal recorder
    path starts and stops with Run Manager unless overridden.

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

Before using a real camera, you can confirm the wrapper defaults to the
Hamilton Run Manager gate and times out cleanly when `HxRun.exe` is absent:

```powershell
powershell -NoProfile -File C:\QC\cameras\test-start-recorder-gate.ps1
```

## Bench Test With The Real Camera

Use this first to prove the real camera, ffmpeg, segmentation, and process
gating all work without involving Hamilton.

1. Start a harmless Windows process such as Notepad.
2. Start the recorder and gate it on that process:

```powershell
powershell -NoProfile -File C:\QC\cameras\start-recorder.ps1 `
  -Source 'dshow:video="YOUR CAMERA NAME"' `
  -OutDir C:\QC\cameras\video_clips\bench `
  -Label bench `
  -SegmentSec 15 `
  -StartWhenExe notepad.exe `
  -VerboseRecorder
```

3. Confirm clips appear under `video_clips\bench`.
4. Close Notepad.
5. Confirm the recorder stops on its own.

This validates the startup gate and the stop gate before testing against the
actual Run Manager lifecycle.

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
  -SegmentSec 15 `
  -VerboseRecorder
```

3. Launch Run Manager on the Hamilton PC and start a short simulated method.
4. Confirm recording begins only after `HxRun.exe` appears.
5. Let the simulated method finish, then close Run Manager.
6. Confirm the recorder stops automatically and clips are present under
   `video_clips\sim`.
7. If needed, trigger `mark-error.py` during the simulated run to confirm the
   error-clip path also works with the real camera.

## Recommended Sim Test Acceptance Criteria

- No clips are created before Run Manager starts.
- Segments are created once Run Manager is active.
- The recorder exits automatically when Run Manager closes.
- Clip timestamps line up with the simulated run window.
- If an error mark is triggered, at least one clip is copied into
  `error_clips`.

## Next Recorder Work

- Add a multi-camera launcher that starts one recorder process per selected
  camera and keeps labels/output paths stable.
