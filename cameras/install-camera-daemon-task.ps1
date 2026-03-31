<#
  cameras/install-camera-daemon-task.ps1

  Install a Scheduled Task that launches the workstation-local camera daemon at
  user logon. Using Task Scheduler keeps the rollout simple and works better
  with interactive camera devices than a Windows service running under SYSTEM.
#>

param(
  [string]$Config = "",
  [string]$LocalConfig = "",
  [switch]$RunNow
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
$startScript = Join-Path $scriptDir "start-camera-daemon.ps1"

$argString = @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-WindowStyle", "Hidden",
  "-File", ('"{0}"' -f ([System.IO.Path]::GetFullPath($startScript))),
  "-Config", ('"{0}"' -f ([System.IO.Path]::GetFullPath($Config))),
  "-LocalConfig", ('"{0}"' -f ([System.IO.Path]::GetFullPath($LocalConfig)))
) -join " "

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argString
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable
$userId = if ($env:USERDOMAIN) { "$($env:USERDOMAIN)\$($env:USERNAME)" } else { $env:USERNAME }
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Installed scheduled task: $taskName" -ForegroundColor Green

if ($RunNow) {
  Start-ScheduledTask -TaskName $taskName
  Write-Host "Started scheduled task: $taskName" -ForegroundColor Green
}
