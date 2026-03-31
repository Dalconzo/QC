<#
  cameras/uninstall-camera-daemon-task.ps1

  Remove the Scheduled Task used to auto-start the workstation-local camera
  daemon.
#>

param(
  [string]$Config = "",
  [string]$LocalConfig = "",
  [switch]$StopFirst
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

if (-not $Config) {
  $Config = Join-Path $repoRoot "config\camera-recorder.json"
}

if (-not $LocalConfig) {
  $LocalConfig = Join-Path $repoRoot "config\camera-recorder.local.json"
}

if ($StopFirst) {
  $stopScript = Join-Path $scriptDir "stop-camera-daemon.ps1"
  & powershell -NoProfile -ExecutionPolicy Bypass -File $stopScript -Config $Config -LocalConfig $LocalConfig -WaitSec 20
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
  throw "Python is not available in PATH."
}

$inspectScript = Join-Path $scriptDir "inspect-camera-config.py"
$configJson = & python $inspectScript --config ([System.IO.Path]::GetFullPath($Config)) --local-config ([System.IO.Path]::GetFullPath($LocalConfig)) --json
if ($LASTEXITCODE -ne 0) {
  throw "Failed to read effective camera config."
}

$effective = $configJson | ConvertFrom-Json
$configRoot = if ($effective.config) { $effective.config } else { $effective }
$taskName = [string]$configRoot.daemon.task_name
if (-not $taskName.Trim()) {
  $taskName = "HamiltonCameraRecorderDaemon"
}

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
  Write-Host "Scheduled task does not exist: $taskName" -ForegroundColor Yellow
  exit 0
}

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
Write-Host "Removed scheduled task: $taskName" -ForegroundColor Green
