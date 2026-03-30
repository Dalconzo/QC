<#
  cameras/open-latest-run.ps1

  One-click workstation entry point for engineers who want to inspect the most
  recent replayable Hamilton run without opening a terminal or manually
  selecting it from the run picker first.
#>

[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$LocalConfig = "",
    [string]$RunsRoot = "",
    [string]$BindHost = "",
    [Nullable[int]]$Port = $null,
    [string]$ReplayLog = "",
    [int]$WaitSec = 15
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcherPath = Join-Path $scriptDir "start-replay-app.ps1"

$argsList = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $launcherPath,
    "-Background",
    "-OpenBrowser",
    "-LatestRun",
    "-WaitSec", $WaitSec
)

if ($Config) {
    $argsList += "-Config"
    $argsList += ([System.IO.Path]::GetFullPath($Config))
}

if ($LocalConfig) {
    $argsList += "-LocalConfig"
    $argsList += ([System.IO.Path]::GetFullPath($LocalConfig))
}

if ($RunsRoot) {
    $argsList += "-RunsRoot"
    $argsList += ([System.IO.Path]::GetFullPath($RunsRoot))
}

if ($BindHost) {
    $argsList += "-BindHost"
    $argsList += $BindHost
}

if ($Port -ne $null) {
    $argsList += "-Port"
    $argsList += $Port
}

if ($ReplayLog) {
    $argsList += "-ReplayLog"
    $argsList += ([System.IO.Path]::GetFullPath($ReplayLog))
}

& powershell @argsList
exit $LASTEXITCODE
