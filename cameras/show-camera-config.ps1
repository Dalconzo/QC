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
. (Join-Path $scriptDir "camera-env.ps1")

if (-not $Config) {
  $Config = Join-Path $repoRoot "config\camera-recorder.json"
}

if (-not $LocalConfig) {
  $LocalConfig = Join-Path $repoRoot "config\camera-recorder.local.json"
}

$scriptPath = Join-Path $scriptDir "inspect-camera-config.py"
if ((-not (Test-Path -LiteralPath $scriptPath)) -and (-not (Test-Path -LiteralPath (Get-CameraPackagedToolPath -RepoRoot $repoRoot -ToolName "inspect-camera-config")))) {
  throw "Camera config inspector not found: $scriptPath"
}

$commandInfo = Resolve-CameraToolCommand `
  -RepoRoot $repoRoot `
  -ToolName "inspect-camera-config" `
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

if ($ListProfiles) {
  $argsList += "--list-profiles"
}

if ($Validate) {
  $argsList += "--validate"
}

if ($AsJson) {
  $argsList += "--json"
}

Invoke-CameraTool -CommandInfo $commandInfo -Arguments $argsList
exit $LASTEXITCODE
