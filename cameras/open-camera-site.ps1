<#
  cameras/open-camera-site.ps1

  One-click launcher for the local Hamilton camera site.

  This is the operator-facing entry point we want on the desktop: start the
  local replay server if needed, wait for it to answer, then open the default
  browser to the main camera site.
#>

[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$LocalConfig = "",
    [string]$RunsRoot = "",
    [string]$BindHost = "",
    [Nullable[int]]$Port = $null,
    [string]$ReplayLog = "",
    [int]$WaitSec = 15,
    [switch]$LiveView
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

if ($LiveView) {
    $argsList += "-LiveView"
}

& powershell @argsList
exit $LASTEXITCODE
