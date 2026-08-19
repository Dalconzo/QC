# Central Replay Host Autostart

The central replay website runs as the `QCCentralReplayServer` Windows
Scheduled Task. It starts with the machine, runs as `NT AUTHORITY\SYSTEM`, and
does not store a user password.

Run installation commands from an elevated PowerShell window in the deployed
repository. The installer resolves and pins an absolute Python executable so
the startup environment does not depend on a user's `PATH`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\QC\cameras\install-central-replay-server-task.ps1 -RunNow
```

Machine-specific paths can be supplied explicitly:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\QC\cameras\install-central-replay-server-task.ps1 `
  -ServerConfig C:\QC\config\central-replay-server.json `
  -ServerLocalConfig C:\QC\config\central-replay-server.local.json `
  -CameraConfig C:\QC\config\camera-recorder.json `
  -CameraLocalConfig C:\QC\config\camera-recorder.local.json `
  -ServerLog C:\QC\logs\central-replay-server.log `
  -PythonExecutable C:\QC\venv\Scripts\python.exe `
  -RunNow
```

Inspect task state and the local health endpoint:

```powershell
powershell -NoProfile -File C:\QC\cameras\get-central-replay-server-task.ps1
powershell -NoProfile -File C:\QC\cameras\get-central-replay-server-task.ps1 -AsJson
```

Re-running the installer stops a running old definition, replaces it, and can
start the new definition with `-RunNow`. Removal is also idempotent:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\QC\cameras\uninstall-central-replay-server-task.ps1
```

Use `-PlanOnly` on install or uninstall to inspect the intended operation
without changing Task Scheduler. The smoke test only exercises this safe mode:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\QC\cameras\test-central-replay-server-task.ps1
```

The task owns the foreground PowerShell/Python process. Task Scheduler retries
up to five times at one-minute intervals after failure. Application logs go to
the explicit `-ServerLog` path. Local override files are optional; the server's
existing config loader handles them when absent.

