<#
  cameras/show-camera-daemon-status.ps1

  Print the current daemon status JSON in a human-readable form. The daemon
  writes this file continuously so operators can see whether the workstation is
  idle, recording, or failing without attaching a debugger.
#>

param(
  [string]$Config = "",
  [string]$LocalConfig = "",
  [switch]$AsJson
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
$configJson = & python $inspectScript --config ([System.IO.Path]::GetFullPath($Config)) --local-config ([System.IO.Path]::GetFullPath($LocalConfig)) --json
if ($LASTEXITCODE -ne 0) {
  throw "Failed to read effective camera config."
}

$effective = $configJson | ConvertFrom-Json
$configRoot = if ($effective.config) { $effective.config } else { $effective }
$statusPath = [string]$configRoot.daemon.status_path
if (-not (Test-Path -LiteralPath $statusPath)) {
  throw "Camera daemon status file does not exist yet: $statusPath"
}

if ($AsJson) {
  Get-Content -LiteralPath $statusPath
  exit 0
}

$status = Get-Content -LiteralPath $statusPath | ConvertFrom-Json
$status | Format-List
