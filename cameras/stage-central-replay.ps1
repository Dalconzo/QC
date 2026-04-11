<#
  cameras/stage-central-replay.ps1

  Thin PowerShell wrapper for the Python staging tool that prepares completed
  local replay runs for the future central LAN replay service.
#>

[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$LocalConfig = "",
    [string]$RunsRoot = "",
    [string]$StagingRoot = "",
    [int]$Limit = 0,
    [switch]$Restage,
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

$scriptPath = Join-Path $scriptDir "stage-central-replay.py"
$argsList = @(
    $scriptPath,
    "--config", ([System.IO.Path]::GetFullPath($Config)),
    "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig))
)

if ($RunsRoot) {
    $argsList += "--runs-root"
    $argsList += ([System.IO.Path]::GetFullPath($RunsRoot))
}
if ($StagingRoot) {
    $argsList += "--staging-root"
    $argsList += ([System.IO.Path]::GetFullPath($StagingRoot))
}
if ($Limit -gt 0) {
    $argsList += "--limit"
    $argsList += $Limit.ToString()
}
if ($Restage) {
    $argsList += "--restage"
}
if ($AsJson) {
    $argsList += "--json"
}

& $python.Source @argsList
exit $LASTEXITCODE
