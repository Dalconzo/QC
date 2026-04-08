<#
  cameras/stop-camera-daemon.ps1

  Ask the workstation-local camera daemon to stop and optionally wait for it to
  exit. This also signals the child recorder stop-file so an active recording
  can shut down cleanly.
#>

param(
  [string]$Config = "",
  [string]$LocalConfig = "",
  [Nullable[int]]$WaitSec = 20
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
if (-not (Test-Path -LiteralPath $inspectScript)) {
  throw "Config inspection script not found: $inspectScript"
}

$configJson = & python $inspectScript --config ([System.IO.Path]::GetFullPath($Config)) --local-config ([System.IO.Path]::GetFullPath($LocalConfig)) --json
if ($LASTEXITCODE -ne 0) {
  throw "Failed to read effective camera config."
}

$config = $configJson | ConvertFrom-Json
$daemonStopFile = [string]$config.daemon.stop_file
$pidFile = [string]$config.daemon.pid_file
$recorderStopFile = [string]$config.recorder.stop_file

# Broken local overrides should not prevent the operator from stopping the
# daemon. Fall back to the repo-local defaults that the daemon uses when the
# configured paths are blank.
if (-not $daemonStopFile) {
  $daemonStopFile = [System.IO.Path]::GetFullPath((Join-Path $scriptDir "camera-daemon.stop"))
}
if (-not $pidFile) {
  $pidFile = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "logs\camera-daemon.pid"))
}
if (-not $recorderStopFile) {
  $recorderStopFile = [System.IO.Path]::GetFullPath((Join-Path $scriptDir "cameras.recorder.stop"))
}

$daemonStopParent = Split-Path -Parent $daemonStopFile
if ($daemonStopParent) {
  New-Item -ItemType Directory -Force -Path $daemonStopParent | Out-Null
}
Set-Content -LiteralPath $daemonStopFile -Value "stop" -Encoding UTF8
Write-Host "Created daemon stop file: $daemonStopFile" -ForegroundColor Yellow

if ($recorderStopFile) {
  $recorderStopParent = Split-Path -Parent $recorderStopFile
  if ($recorderStopParent) {
    New-Item -ItemType Directory -Force -Path $recorderStopParent | Out-Null
  }
  Set-Content -LiteralPath $recorderStopFile -Value "stop" -Encoding UTF8
  Write-Host "Created recorder stop file: $recorderStopFile" -ForegroundColor Yellow
}

if ($WaitSec -le 0) {
  exit 0
}

$deadline = (Get-Date).AddSeconds($WaitSec)
while ((Get-Date) -lt $deadline) {
  if (-not (Test-Path -LiteralPath $pidFile)) {
    Remove-Item -LiteralPath $daemonStopFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $recorderStopFile -Force -ErrorAction SilentlyContinue
    Write-Host "Camera daemon pid file removed; daemon has stopped." -ForegroundColor Green
    exit 0
  }

  $pidText = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  $pidValue = 0
  [void][int]::TryParse("$pidText", [ref]$pidValue)
  if ($pidValue -gt 0) {
    $proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if (-not $proc) {
      Remove-Item -LiteralPath $daemonStopFile -Force -ErrorAction SilentlyContinue
      Remove-Item -LiteralPath $recorderStopFile -Force -ErrorAction SilentlyContinue
      Write-Host "Camera daemon process is no longer running." -ForegroundColor Green
      exit 0
    }
  }

  Start-Sleep -Seconds 1
}

Write-Warning "Camera daemon stop request timed out after $WaitSec seconds."
exit 1
