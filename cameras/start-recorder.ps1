<#
  cameras/start-recorder.ps1

  Operator wrapper around camera-recorder.py.

  The wrapper now forwards the shared workstation config into Python instead of
  hardcoding recorder defaults in PowerShell. That keeps camera source changes,
  Hamilton log path changes, and future daemon behavior aligned around one
  config model.
#>

param(
  [string]$Config = "",
  [string]$LocalConfig = "",
  [string]$Profile = "",
  [string]$Source = "",
  [string]$OutDir = "",
  [string]$Label = "",
  [string]$StartWhenExe = "",
  [string]$StopWhenExe = "",
  [Nullable[int]]$StartupTimeoutSec = $null,
  [Nullable[double]]$PollSec = $null,
  [Nullable[int]]$MaxRecordSec = $null,
  [string]$LogDir = "",
  [string]$LogGlob = "",
  [string]$ManifestDir = "",
  [string]$RecorderLog = "",
  [string]$Ffmpeg = "",
  [string]$StopFile = "",
  [switch]$VerboseRecorder
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

$scriptPath = Join-Path $scriptDir "camera-recorder.py"
if ((-not (Test-Path -LiteralPath $scriptPath)) -and (-not (Test-Path -LiteralPath (Get-CameraPackagedToolPath -RepoRoot $repoRoot -ToolName "camera-recorder")))) {
  throw "Recorder script not found: $scriptPath"
}

$commandInfo = Resolve-CameraToolCommand `
  -RepoRoot $repoRoot `
  -ToolName "camera-recorder" `
  -ScriptPath $scriptPath `
  -ConfigPath ([System.IO.Path]::GetFullPath($Config)) `
  -LocalConfigPath ([System.IO.Path]::GetFullPath($LocalConfig))

$argsList = @(
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

if ($StopFile) {
  $argsList += "--stop-file"
  $argsList += ([System.IO.Path]::GetFullPath($StopFile))
}

if ($StartWhenExe) {
  $argsList += "--start-when-exe"
  $argsList += $StartWhenExe
}

if ($StopWhenExe) {
  $argsList += "--stop-when-exe"
  $argsList += $StopWhenExe
}

if ($StartupTimeoutSec -ne $null) {
  $argsList += "--startup-timeout-sec"
  $argsList += $StartupTimeoutSec
}

if ($PollSec -ne $null) {
  $argsList += "--poll-sec"
  $argsList += $PollSec
}

if ($MaxRecordSec -ne $null) {
  $argsList += "--max-record-sec"
  $argsList += $MaxRecordSec
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
Invoke-CameraTool -CommandInfo $commandInfo -Arguments $argsList
exit $LASTEXITCODE
