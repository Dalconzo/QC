<#
  cameras/start-camera-daemon.ps1

  Launch the workstation-local camera daemon.

  By default this starts the daemon in the background so engineers do not need
  to keep a terminal open. Use -Foreground when debugging rollout problems.
#>

param(
  [string]$Config = "",
  [string]$LocalConfig = "",
  [string]$Profile = "",
  [string]$Source = "",
  [string]$OutDir = "",
  [string]$Label = "",
  [string]$DaemonLog = "",
  [string]$RecorderLog = "",
  [string]$StatusPath = "",
  [string]$PidFile = "",
  [string]$StopFile = "",
  [Nullable[double]]$IdlePollSec = $null,
  [Nullable[double]]$HeartbeatSec = $null,
  [Nullable[double]]$RelaunchDelaySec = $null,
  [Nullable[int]]$IdleTimeoutSec = $null,
  [switch]$Foreground
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

$effective = $configJson | ConvertFrom-Json
$configRoot = if ($effective.config) { $effective.config } else { $effective }
$effectiveDaemonStopFile = [string]$configRoot.daemon.stop_file
$effectiveRecorderStopFile = [string]$configRoot.recorder.stop_file

# A prior stop request leaves sentinel files behind on disk. Clear them before
# relaunching so a fresh daemon instance does not exit immediately on startup.
if (-not $effectiveDaemonStopFile) {
  $effectiveDaemonStopFile = [System.IO.Path]::GetFullPath((Join-Path $scriptDir "camera-daemon.stop"))
}
if (-not $effectiveRecorderStopFile) {
  $effectiveRecorderStopFile = [System.IO.Path]::GetFullPath((Join-Path $scriptDir "cameras.recorder.stop"))
}

Remove-Item -LiteralPath $effectiveDaemonStopFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $effectiveRecorderStopFile -Force -ErrorAction SilentlyContinue

$daemonScript = Join-Path $scriptDir "camera-daemon.py"
if (-not (Test-Path -LiteralPath $daemonScript)) {
  throw "Camera daemon script not found: $daemonScript"
}

$argsList = @(
  $daemonScript,
  "--config", ([System.IO.Path]::GetFullPath($Config)),
  "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig))
)

if ($Profile) {
  $argsList += "--profile"
  $argsList += $Profile
}

if ($Source) {
  $argsList += "--source"
  $argsList += $Source
}

if ($OutDir) {
  $argsList += "--out-dir"
  $argsList += ([System.IO.Path]::GetFullPath($OutDir))
}

if ($Label) {
  $argsList += "--label"
  $argsList += $Label
}

if ($DaemonLog) {
  $argsList += "--daemon-log"
  $argsList += ([System.IO.Path]::GetFullPath($DaemonLog))
}

if ($RecorderLog) {
  $argsList += "--recorder-log"
  $argsList += ([System.IO.Path]::GetFullPath($RecorderLog))
}

if ($StatusPath) {
  $argsList += "--status-path"
  $argsList += ([System.IO.Path]::GetFullPath($StatusPath))
}

if ($PidFile) {
  $argsList += "--pid-file"
  $argsList += ([System.IO.Path]::GetFullPath($PidFile))
}

if ($StopFile) {
  $argsList += "--stop-file"
  $argsList += ([System.IO.Path]::GetFullPath($StopFile))
}

if ($IdlePollSec -ne $null) {
  $argsList += "--idle-poll-sec"
  $argsList += $IdlePollSec
}

if ($HeartbeatSec -ne $null) {
  $argsList += "--heartbeat-sec"
  $argsList += $HeartbeatSec
}

if ($RelaunchDelaySec -ne $null) {
  $argsList += "--relaunch-delay-sec"
  $argsList += $RelaunchDelaySec
}

if ($IdleTimeoutSec -ne $null) {
  $argsList += "--idle-timeout-sec"
  $argsList += $IdleTimeoutSec
}

if ($Foreground) {
  Write-Host "Starting camera daemon in foreground ..." -ForegroundColor Cyan
  & python @argsList
  exit $LASTEXITCODE
}

Write-Host "Starting camera daemon in background ..." -ForegroundColor Cyan
$proc = Start-Process -FilePath $python.Source -ArgumentList $argsList -WindowStyle Hidden -PassThru
Write-Host ("Camera daemon process started with PID {0}" -f $proc.Id) -ForegroundColor Green
