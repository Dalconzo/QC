<#
  cameras/start-recorder.ps1

  Operator wrapper around camera-recorder.py.

  The recorder now captures one continuous video per HxRun session. After the
  process gate closes, the Python script pairs the video with the nearest
  completed Hamilton trace file from the configured log directory and writes a
  run manifest beside the recording.
#>

param(
  [string]$Source = "0",
  [string]$OutDir = (Join-Path $PSScriptRoot "video_clips"),
  [string]$Label = "cam",
  [string]$StartWhenExe = "HxRun.exe",
  [string]$StopWhenExe = "",
  [int]$StartupTimeoutSec = 0,
  [double]$PollSec = 1.0,
  [int]$MaxRecordSec = 0,
  [string]$LogDir = "",
  [string]$LogGlob = "",
  [string]$ManifestDir = "",
  [string]$RecorderLog = "",
  [string]$Ffmpeg = (Join-Path $PSScriptRoot "ffmpeg.exe"),
  [string]$StopFile = (Join-Path $PSScriptRoot "cameras.recorder.stop"),
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

$argsList = @(
  $scriptPath,
  "--source", $Source,
  "--out-dir", ([System.IO.Path]::GetFullPath($OutDir)),
  "--label", $Label,
  "--stop-file", ([System.IO.Path]::GetFullPath($StopFile)),
  "--startup-timeout-sec", $StartupTimeoutSec,
  "--poll-sec", $PollSec,
  "--max-record-sec", $MaxRecordSec
)

if ($StartWhenExe) {
  $argsList += "--start-when-exe"
  $argsList += $StartWhenExe
}

if ($StopWhenExe) {
  $argsList += "--stop-when-exe"
  $argsList += $StopWhenExe
}

if ($LogDir) {
  $argsList += "--log-dir"
  $argsList += ([System.IO.Path]::GetFullPath($LogDir))
}

if ($LogGlob) {
  $argsList += "--log-glob"
  $argsList += $LogGlob
}

if ($ManifestDir) {
  $argsList += "--manifest-dir"
  $argsList += ([System.IO.Path]::GetFullPath($ManifestDir))
}

if ($RecorderLog) {
  $logPath = [System.IO.Path]::GetFullPath($RecorderLog)
  $logParent = Split-Path -Parent $logPath
  if ($logParent) {
    New-Item -ItemType Directory -Force -Path $logParent | Out-Null
  }
  $argsList += "--recorder-log"
  $argsList += $logPath
}

if ($Ffmpeg) {
  $argsList += "--ffmpeg"
  $argsList += ([System.IO.Path]::GetFullPath($Ffmpeg))
}

if ($VerboseRecorder) {
  $argsList += "--verbose"
}

Write-Host "Starting continuous run recorder ..." -ForegroundColor Cyan
& python @argsList
exit $LASTEXITCODE
