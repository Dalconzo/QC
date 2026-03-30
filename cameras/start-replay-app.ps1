[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$LocalConfig = "",
    [string]$RunsRoot = "",
    [string]$BindHost = "",
    [Nullable[int]]$Port = $null
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

$appPath = Join-Path $scriptDir "replay-app.py"

Write-Host "Starting Hamilton replay app..."
if ($RunsRoot) {
    Write-Host "Runs root override: $RunsRoot"
}
if ($BindHost -or $Port -ne $null) {
    Write-Host "Replay bind override requested."
}

$argsList = @(
    $appPath,
    "--config", ([System.IO.Path]::GetFullPath($Config)),
    "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig))
)

if ($RunsRoot) {
    $argsList += "--runs-root"
    $argsList += ([System.IO.Path]::GetFullPath($RunsRoot))
}

if ($BindHost) {
    $argsList += "--host"
    $argsList += $BindHost
}

if ($Port -ne $null) {
    $argsList += "--port"
    $argsList += $Port
}

python @argsList
