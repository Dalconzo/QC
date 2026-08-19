<#
  Idempotently install the central replay server as a machine-startup task.
  The task runs as LocalSystem and therefore stores no user credentials.
#>

[CmdletBinding()]
param(
  [string]$TaskName = "QCCentralReplayServer",
  [string]$RepoRoot = "",
  [string]$ServerConfig = "",
  [string]$ServerLocalConfig = "",
  [string]$CameraConfig = "",
  [string]$CameraLocalConfig = "",
  [string]$ServerLog = "",
  [string]$PythonExecutable = "",
  [ValidateRange(0, 3600)][int]$StartupDelaySec = 15,
  [switch]$RunNow,
  [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir "central-replay-task-common.ps1")

$paths = Resolve-CentralReplayTaskPaths @PSBoundParameters
$actionArguments = New-CentralReplayTaskActionArguments -Paths $paths
$plan = [ordered]@{
  task_name = $TaskName
  principal = "NT AUTHORITY\SYSTEM"
  trigger = "AtStartup"
  startup_delay_sec = $StartupDelaySec
  executable = "powershell.exe"
  arguments = $actionArguments
  python_executable = $paths.PythonExecutable
  server_log = $paths.ServerLog
  run_now = [bool]$RunNow
}

if ($PlanOnly) {
  $plan | ConvertTo-Json -Depth 4
  exit 0
}

Assert-CentralReplayTaskFiles -Paths $paths
. (Join-Path $scriptDir "camera-env.ps1")
if (-not $paths.PythonExecutable) {
  $pythonCommand = Get-CameraPythonCommand -RepoRoot $paths.RepoRoot
  if ($pythonCommand.Count -ne 1) {
    throw "The central replay startup task requires a direct Python executable. Use -PythonExecutable to avoid a launcher command."
  }
  $paths.PythonExecutable = [System.IO.Path]::GetFullPath($pythonCommand[0])
  Assert-CentralReplayTaskFiles -Paths $paths
  $actionArguments = New-CentralReplayTaskActionArguments -Paths $paths
}
foreach ($command in @("Get-ScheduledTask", "Register-ScheduledTask", "New-ScheduledTaskAction", "New-ScheduledTaskTrigger")) {
  if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
    throw "ScheduledTasks command is unavailable: $command"
  }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Installing the central replay startup task requires an elevated PowerShell window."
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArguments -WorkingDirectory $paths.RepoRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
if ($StartupDelaySec -gt 0) {
  $trigger.Delay = "PT${StartupDelaySec}S"
}
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -RestartCount 5 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq "Running") {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
}

Register-ScheduledTask `
  -TaskName $TaskName `
  -Description "QC central camera replay website host" `
  -Action $action `
  -Trigger $trigger `
  -Principal $taskPrincipal `
  -Settings $settings `
  -Force | Out-Null

Write-Host "Installed central replay startup task: $TaskName" -ForegroundColor Green
Write-Host "Server log: $($paths.ServerLog)"

if ($RunNow) {
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "Started central replay task: $TaskName" -ForegroundColor Green
}
