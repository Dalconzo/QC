<#
  cameras/show-camera-config.ps1

  Operator wrapper around the shared camera config inspector.

  This lets us confirm the effective recorder and replay settings on a
  workstation without reading the Python source, which is the right shape for
  the upcoming background daemon rollout.
#>

[CmdletBinding()]
param(
  [string]$Config = "",
  [string]$LocalConfig = "",
  [string]$Profile = "",
  [switch]$ListProfiles,
  [switch]$Validate,
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

$scriptPath = Join-Path $scriptDir "inspect-camera-config.py"
if (-not (Test-Path -LiteralPath $scriptPath)) {
  throw "Camera config inspector not found: $scriptPath"
}

$argsList = @(
  $scriptPath,
  "--config", ([System.IO.Path]::GetFullPath($Config)),
  "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig))
)

if ($Profile) {
  $argsList += "--profile"
  $argsList += $Profile
}

if ($ListProfiles) {
  $argsList += "--list-profiles"
}

if ($Validate) {
  $argsList += "--validate"
}

if ($AsJson) {
  $argsList += "--json"
}

& python @argsList
exit $LASTEXITCODE
