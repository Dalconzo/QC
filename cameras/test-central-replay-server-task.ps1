<# Safe smoke tests. No Scheduled Task is created, started, stopped, or removed. #>

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$install = Join-Path $scriptDir "install-central-replay-server-task.ps1"
$uninstall = Join-Path $scriptDir "uninstall-central-replay-server-task.ps1"

$testRoot = Join-Path $repoRoot "tmp\central-replay-task-test"
$plan = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $install `
  -RepoRoot $repoRoot `
  -ServerConfig (Join-Path $testRoot "server.json") `
  -ServerLocalConfig (Join-Path $testRoot "server.local.json") `
  -CameraConfig (Join-Path $testRoot "camera.json") `
  -CameraLocalConfig (Join-Path $testRoot "camera.local.json") `
  -ServerLog (Join-Path $testRoot "logs\server.log") `
  -PythonExecutable "C:\Runtime\python.exe" `
  -StartupDelaySec 22 `
  -RunNow `
  -PlanOnly | ConvertFrom-Json

if ($LASTEXITCODE -ne 0) { throw "Install plan failed." }
if ($plan.principal -ne "NT AUTHORITY\SYSTEM") { throw "Task must run as LocalSystem without stored credentials." }
if ($plan.trigger -ne "AtStartup") { throw "Task trigger must be AtStartup." }
if ($plan.startup_delay_sec -ne 22) { throw "Startup delay was not preserved." }
if ($plan.arguments -notmatch [regex]::Escape($testRoot)) { throw "Explicit config paths are missing from task arguments." }
if ($plan.arguments -notmatch "run-central-replay-server-task.ps1") { throw "Task does not invoke the foreground runner." }
if ($plan.arguments -notmatch [regex]::Escape("C:\Runtime\python.exe")) { throw "Configured Python executable is missing from task arguments." }
if ($plan.server_log -ne (Join-Path $testRoot "logs\server.log")) { throw "Explicit server log was not preserved." }
if (-not $plan.run_now) { throw "RunNow was not represented in the plan." }

$removePlan = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $uninstall `
  -TaskName "QCCentralReplayServer-Test" -PlanOnly | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Uninstall plan failed." }
if ($removePlan.action -ne "uninstall") { throw "Unexpected uninstall plan." }

# Guard against accidental mutation during the test.
if (Get-ScheduledTask -TaskName "QCCentralReplayServer-Test" -ErrorAction SilentlyContinue) {
  throw "Smoke test unexpectedly created a Scheduled Task."
}

Write-Host "central replay startup task smoke tests passed" -ForegroundColor Green
