<#
  cameras/test-camera-daemon.ps1

  Wrapper smoke test for the PowerShell daemon launcher. The heavier
  supervisor-cycle behavior is covered in test-camera-daemon.py; this script
  only checks that the PowerShell entrypoint can launch the daemon in the
  foreground with a temporary config and get a clean idle-timeout exit.
#>

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

$tmpRoot = Join-Path $repoRoot "tmp\camera-daemon-test"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $tmpRoot
New-Item -ItemType Directory -Force -Path $tmpRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $tmpRoot "hamilton") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $tmpRoot "logs") | Out-Null

$configPath = Join-Path $tmpRoot "camera-recorder.json"
$config = @{
  hamilton = @{
    log_dir = (Join-Path $tmpRoot "hamilton")
    process_name = "definitely-not-a-real-hxrun.exe"
  }
  storage = @{
    runs_root = (Join-Path $tmpRoot "runs")
    recorder_log_dir = (Join-Path $tmpRoot "logs")
  }
  recorder = @{
    stop_file = (Join-Path $tmpRoot "recorder.stop")
  }
  daemon = @{
    stop_file = (Join-Path $tmpRoot "daemon.stop")
    pid_file = (Join-Path $tmpRoot "daemon.pid")
    status_path = (Join-Path $tmpRoot "daemon-status.json")
    log_path = (Join-Path $tmpRoot "daemon.log")
    idle_poll_sec = 0.1
    heartbeat_sec = 0.1
    relaunch_delay_sec = 0.0
  }
  profiles = @(
    @{
      id = "default"
      label = "SmokeCam"
      source = "0"
    }
  )
} | ConvertTo-Json -Depth 6

Set-Content -LiteralPath $configPath -Value $config -Encoding UTF8

$launcher = Join-Path $scriptDir "start-camera-daemon.ps1"
& powershell -NoProfile -ExecutionPolicy Bypass -File $launcher -Config $configPath -Foreground -IdleTimeoutSec 1
if ($LASTEXITCODE -ne 0) {
  throw "Foreground daemon launcher returned exit code $LASTEXITCODE"
}

$statusPath = Join-Path $tmpRoot "daemon-status.json"
if (-not (Test-Path -LiteralPath $statusPath)) {
  throw "Daemon status file was not created: $statusPath"
}

$status = Get-Content -LiteralPath $statusPath | ConvertFrom-Json
if ($status.state -ne "stopped" -or $status.reason -ne "idle_timeout") {
  throw "Unexpected daemon status: state=$($status.state) reason=$($status.reason)"
}

Write-Host "camera-daemon PowerShell wrapper smoke test passed" -ForegroundColor Green
