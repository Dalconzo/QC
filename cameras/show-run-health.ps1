<#
  cameras/show-run-health.ps1

  Thin PowerShell wrapper around inspect-run-manifests.py so operators can
  diagnose stale replay manifests without dropping into Python directly.
#>

[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$LocalConfig = "",
    [string]$RunsRoot = "",
    [ValidateSet("none", "all-stale", "missing-video", "missing-trace")]
    [string]$Cleanup = "none",
    [switch]$Delete,
    [string]$QuarantineDir = "",
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

$scriptPath = Join-Path $scriptDir "inspect-run-manifests.py"
$argsList = @(
    $scriptPath,
    "--config", ([System.IO.Path]::GetFullPath($Config)),
    "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig)),
    "--cleanup", $Cleanup
)

if ($RunsRoot) {
    $argsList += "--runs-root"
    $argsList += ([System.IO.Path]::GetFullPath($RunsRoot))
}
if ($Delete) {
    $argsList += "--delete"
}
if ($QuarantineDir) {
    $argsList += "--quarantine-dir"
    $argsList += ([System.IO.Path]::GetFullPath($QuarantineDir))
}
if ($AsJson) {
    $argsList += "--json"
}

& $python.Source @argsList
exit $LASTEXITCODE
