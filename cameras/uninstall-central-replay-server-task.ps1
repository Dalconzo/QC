<# Remove the central replay startup task without touching replay data. #>

[CmdletBinding()]
param(
  [string]$TaskName = "QCCentralReplayServer",
  [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"
if ($PlanOnly) {
  [ordered]@{ task_name = $TaskName; action = "uninstall" } | ConvertTo-Json
  exit 0
}

if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) {
  throw "The ScheduledTasks PowerShell module is unavailable."
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
  Write-Host "Scheduled task does not exist: $TaskName" -ForegroundColor Yellow
  exit 0
}

if ($task.State -eq "Running") {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed central replay startup task: $TaskName" -ForegroundColor Green

