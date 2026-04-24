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
. (Join-Path $scriptDir "camera-env.ps1")

if (-not $Config) {
  $Config = Join-Path $repoRoot "config\camera-recorder.json"
}

if (-not $LocalConfig) {
  $LocalConfig = Join-Path $repoRoot "config\camera-recorder.local.json"
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
$compatibilityMode = [string]$configRoot.workstation.compatibility_mode
$taskName = [string]$configRoot.daemon.task_name
if (-not $taskName.Trim()) {
  $taskName = "HamiltonCameraRecorderDaemon"
}
$startScript = Join-Path $scriptDir "start-camera-daemon.ps1"
$taskBackend = Get-CameraTaskSchedulerBackend -CompatibilityMode $compatibilityMode

# Stop any already-running daemon before replacing the scheduled task. Without
# this, an older background process can keep using stale config even after the
# task definition is updated.
$stopScript = Join-Path $scriptDir "stop-camera-daemon.ps1"
try {
  & powershell -NoProfile -ExecutionPolicy Bypass -File $stopScript -Config $Config -LocalConfig $LocalConfig -WaitSec 20
} catch {
  Write-Warning "Unable to stop the existing camera daemon cleanly before reinstall. Continuing with task registration."
}

$argString = @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-WindowStyle", "Hidden",
  "-File", ('"{0}"' -f ([System.IO.Path]::GetFullPath($startScript))),
  "-Config", ('"{0}"' -f ([System.IO.Path]::GetFullPath($Config))),
  "-LocalConfig", ('"{0}"' -f ([System.IO.Path]::GetFullPath($LocalConfig)))
) -join " "

$userId = if ($env:USERDOMAIN) { "$($env:USERDOMAIN)\$($env:USERNAME)" } else { $env:USERNAME }

if ($taskBackend -eq "schtasks") {
  $legacyCommand = ('powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Config "{1}" -LocalConfig "{2}"' -f ([System.IO.Path]::GetFullPath($startScript)), ([System.IO.Path]::GetFullPath($Config)), ([System.IO.Path]::GetFullPath($LocalConfig)))
  & schtasks.exe /Delete /TN $taskName /F 2>$null | Out-Null
  & schtasks.exe /Create /SC ONLOGON /TN $taskName /TR $legacyCommand /RL LIMITED /F /IT /RU $userId | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "schtasks.exe failed to create scheduled task '$taskName'."
  }
  Write-Host "Installed scheduled task via schtasks.exe: $taskName" -ForegroundColor Green
  if ($RunNow) {
    & schtasks.exe /Run /TN $taskName | Out-Null
    if ($LASTEXITCODE -ne 0) {
      throw "schtasks.exe failed to start scheduled task '$taskName'."
    }
    Write-Host "Started scheduled task: $taskName" -ForegroundColor Green
  }
  exit 0
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argString
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

try {
  Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
} catch {
}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Installed scheduled task: $taskName" -ForegroundColor Green

if ($RunNow) {
  Start-ScheduledTask -TaskName $taskName
  Write-Host "Started scheduled task: $taskName" -ForegroundColor Green
}
