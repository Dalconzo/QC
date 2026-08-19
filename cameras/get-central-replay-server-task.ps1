<# Report Scheduled Task and HTTP health state for the central replay host. #>

[CmdletBinding()]
param(
  [string]$TaskName = "QCCentralReplayServer",
  [string]$HealthUrl = "http://127.0.0.1:5080/api/healthz",
  [switch]$AsJson
)

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue } else { $null }

$healthStatus = "unreachable"
$healthCode = $null
try {
  $response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 3
  $healthCode = [int]$response.StatusCode
  if ($healthCode -ge 200 -and $healthCode -lt 300) {
    $healthStatus = "healthy"
  } else {
    $healthStatus = "unhealthy"
  }
} catch {
}

$result = [ordered]@{
  task_name = $TaskName
  installed = [bool]$task
  task_state = if ($task) { [string]$task.State } else { "Missing" }
  last_run_time = if ($taskInfo) { $taskInfo.LastRunTime } else { $null }
  last_task_result = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
  next_run_time = if ($taskInfo) { $taskInfo.NextRunTime } else { $null }
  health_url = $HealthUrl
  health_status = $healthStatus
  health_status_code = $healthCode
}

if ($AsJson) {
  $result | ConvertTo-Json -Depth 3
} else {
  [pscustomobject]$result | Format-List
}

