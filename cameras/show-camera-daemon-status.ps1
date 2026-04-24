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
. (Join-Path $scriptDir "camera-env.ps1")

if (-not $Config) {
  $Config = Join-Path $repoRoot "config\camera-recorder.json"
}

if (-not $LocalConfig) {
  $LocalConfig = Join-Path $repoRoot "config\camera-recorder.local.json"
}

$inspectScript = Join-Path $scriptDir "inspect-camera-config.py"
$inspectCommand = Resolve-CameraToolCommand `
  -RepoRoot $repoRoot `
  -ToolName "inspect-camera-config" `
  -ScriptPath $inspectScript `
  -ConfigPath ([System.IO.Path]::GetFullPath($Config)) `
  -LocalConfigPath ([System.IO.Path]::GetFullPath($LocalConfig))

$configJson = Invoke-CameraTool -CommandInfo $inspectCommand -Arguments @(
  "--config", ([System.IO.Path]::GetFullPath($Config)),
  "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig)),
  "--json"
)
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
