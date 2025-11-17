# Hamilton Daily Copy Utility

`qc-hamilton-dailycopy.ps1` copies Hamilton instrument trace logs into the shared
network drive (`Z:\Logs`) so each computer drops its logs under a unique folder.

## How it works

- Looks for files in `C:\Program Files (x86)\HAMILTON\LogFiles` whose last write date
  matches the day you ask it to collect.
- Creates `Z:\Logs\<COMPUTERNAME>\YYYY-MM-DD\...` directories as needed and copies
  files while preserving their subfolder structure.
- Skips files that are already present in the destination with the same size and
  timestamp.
- Supports a `-DryRun` switch so you can preview the work without copying.
- Each run logs to `Z:\Logs\<COMPUTERNAME>\dailycopy.log` (5 MB rolling window, 5 backups; adjust with `-MaxLogBytes` / `-LogRetention`).

## Typical daily run

Run near the end of the day (or schedule for a few minutes after midnight and add
`-DaysBack 1` to gather yesterday's files):

```powershell
powershell.exe -NoProfile -File "C:\QC\scripts\ps\qc-hamilton-dailycopy.ps1"
```

You can override defaults if needed:

```powershell
powershell.exe -NoProfile -File "C:\QC\scripts\ps\qc-hamilton-dailycopy.ps1" `
  -SourceRoot "D:\HamiltonLogs" `
  -NetworkRoot "Z:\Logs" `
  -MachineName "Hamilton-Prep-01" `
  -DaysBack 1 `
  -Extensions @("*.trc","*.log")
```

## Scheduling suggestions

1. Open **Task Scheduler** and choose **Create Task**.
2. General: run whether user is logged on or not; set highest privileges.
3. Triggers: daily at 23:55 (or 00:05 with `-DaysBack 1`).
4. Actions: start program  
   Program/script: `powershell.exe`  
   Add arguments: `-NoProfile -ExecutionPolicy Bypass -File "C:\QC\scripts\ps\qc-hamilton-dailycopy.ps1" -MachineName "H8"`
5. Conditions: ensure the network drive `Z:` (with its `Logs` share) is mapped for the service account.
6. Settings: allow task to run on demand; stop if running longer than 2 hours.

## Backfill historical logs

To move existing trace history, run the companion script once per machine:

```powershell
powershell.exe -NoProfile -File "C:\QC\scripts\ps\qc-hamilton-backfill.ps1"
```

It will copy every matching file under the Hamilton log folder into dated
subdirectories (`YYYY-MM-DD`) beneath `Z:\Logs\<COMPUTERNAME>`. Use `-DryRun`
to review the plan before copying, or `-Extensions` to widen the filter.
Backfill runs log to `Z:\Logs\<COMPUTERNAME>\backfill.log` (same rotation parameters).

## Monitoring

- The script writes progress to STDOUT as well as the shared rolling log; capture Task Scheduler output if you also want a local transcript.
- Exit code `0` means success, `1` indicates some files failed to copy. Logs also land
  in the shared log directory if you need a centralised history.

## Compatibility notes

- PowerShell versions prior to 3.0 do not support `Get-ChildItem -File`. The scripts
  now filter out directories explicitly so they run on PS 2.0 as well.
- On 32-bit Windows, the default `SourceRoot` may be under `C:\Program Files\HAMILTON\LogFiles`
  instead of `C:\Program Files (x86)\...`. The scripts auto-fallback to the 32-bit path
  when the default does not exist.
