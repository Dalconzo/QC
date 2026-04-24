<#
  cameras/uninstall-camera-daemon-task.ps1

  Remove the Scheduled Task used to auto-start the workstation-local camera
  daemon.
#>

param(
  [string]$Config = "",
  [string]$LocalConfig = ""
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
. (Join-Path $scriptDir "camera-env.ps1")

if (-not $Config) {
  $Config = Join-Path $repoRoot "config\camera-recorder.json"
}

if (-not $LocalConfig) {
  $LocalConfig = Join-Path $repoRoot "config\camera-recorder.local.json"
}

$stopScript = Join-Path $scriptDir "stop-camera-daemon.ps1"
try {
  & powershell -NoProfile -ExecutionPolicy Bypass -File $stopScript -Config $Config -LocalConfig $LocalConfig -WaitSec 20
} catch {
  Write-Warning "Unable to stop the existing camera daemon cleanly before uninstall. Continuing with task removal."
}

$inspectScript = Join-Path $scriptDir "inspect-camera-config.py"
$inspectCommand = Resolve-CameraToolCommand `
  -RepoRoot $repoRoot `
  -ToolName "inspect-camera-config" `
  -ScriptPath $inspectScript `
  -ConfigPath ([System.IO.Path]::GetFullPath($Config)) `
  -LocalConfigPath ([System.IO.Path]::GetFullPath($LocalConfig))

$configJson = Invoke-CameraTool -CommandInfo $inspectCommand -Arguments @(
  "--config", ([System.IO.Path]::GetFullPath($Config)),
  "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig)),
  "--json"
)
if ($LASTEXITCODE -ne 0) {
  throw "Failed to read effective camera config."
}

$effective = $configJson | ConvertFrom-Json
$configRoot = if ($effective.config) { $effective.config } else { $effective }
$taskName = [string]$configRoot.daemon.task_name
if (-not $taskName.Trim()) {
  $taskName = "HamiltonCameraRecorderDaemon"
}
$taskBackend = Get-CameraTaskSchedulerBackend -CompatibilityMode ([string]$configRoot.workstation.compatibility_mode)

if ($taskBackend -eq "schtasks") {
  & schtasks.exe /End /TN $taskName 2>$null | Out-Null
  & schtasks.exe /Query /TN $taskName 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Scheduled task does not exist: $taskName" -ForegroundColor Yellow
    exit 0
  }
  & schtasks.exe /Delete /TN $taskName /F | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "schtasks.exe failed to remove scheduled task '$taskName'."
  }
  Write-Host "Removed scheduled task via schtasks.exe: $taskName" -ForegroundColor Green
  exit 0
}

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
  Write-Host "Scheduled task does not exist: $taskName" -ForegroundColor Yellow
  exit 0
}

try {
  Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
} catch {
}

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
Write-Host "Removed scheduled task: $taskName" -ForegroundColor Green
