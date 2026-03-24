<#
  cameras/start-recorder.ps1

  Thin PowerShell wrapper around camera-recorder.py so operators can launch the
  recorder with readable parameters instead of a long Python command line.

  This is intentionally simple: it passes through the startup/stop process
  gates, camera source, ffmpeg path, and output settings that matter for bench
  and Hamilton simulation tests.
#>

param(
  [string]$Source = "0",
  [string]$OutDir = (Join-Path $PSScriptRoot "video_clips"),
  [string]$Label = "cam",
  [int]$SegmentSec = 60,
  [string]$StartWhenExe = "HxRun.exe",
  [string]$StopWhenExe = "",
  [int]$StartupTimeoutSec = 0,
  [double]$PollSec = 1.0,
  [string]$Ffmpeg = (Join-Path $PSScriptRoot "ffmpeg.exe"),
  [string]$StopFile = (Join-Path $PSScriptRoot "cameras.recorder.stop"),
  [string]$ErrorDir = (Join-Path $PSScriptRoot "error_clips"),
  [switch]$VerboseRecorder
)

$ErrorActionPreference = "Stop"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
  throw "Python is not available in PATH."
}

$scriptPath = Join-Path $PSScriptRoot "camera-recorder.py"
if (-not (Test-Path -LiteralPath $scriptPath)) {
  throw "Recorder script not found: $scriptPath"
}

# Default to Hamilton Run Manager gating for the common operator path. Bench
# tests can still override this with -StartWhenExe notepad.exe or a blank value.
$argsList = @(
  $scriptPath,
  "--source", $Source,
  "--out-dir", ([System.IO.Path]::GetFullPath($OutDir)),
  "--label", $Label,
  "--segment-sec", $SegmentSec,
  "--stop-file", ([System.IO.Path]::GetFullPath($StopFile)),
  "--error-dir", ([System.IO.Path]::GetFullPath($ErrorDir)),
  "--startup-timeout-sec", $StartupTimeoutSec,
  "--poll-sec", $PollSec
)

if ($StartWhenExe) {
  $argsList += "--start-when-exe"
  $argsList += $StartWhenExe
}

if ($StopWhenExe) {
  $argsList += "--stop-when-exe"
  $argsList += $StopWhenExe
}

if ($Ffmpeg) {
  $argsList += "--ffmpeg"
  $argsList += ([System.IO.Path]::GetFullPath($Ffmpeg))
}

if ($VerboseRecorder) {
  $argsList += "--verbose"
}

Write-Host "Starting camera recorder ..." -ForegroundColor Cyan
& python @argsList
exit $LASTEXITCODE
