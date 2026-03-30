<#
  cameras/uninstall-local-camera-tools.ps1

  Remove the workstation shortcuts created by install-local-camera-tools.ps1.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$desktopDir = [Environment]::GetFolderPath("Desktop")
$startMenuPrograms = [Environment]::GetFolderPath("Programs")
$startMenuDir = Join-Path $startMenuPrograms "Hamilton Camera"

$desktopShortcuts = @(
    "Hamilton Replay.lnk",
    "Hamilton Latest Run.lnk",
    "Hamilton Camera Status.lnk"
)

$startMenuShortcuts = @(
    "Hamilton Replay.lnk",
    "Hamilton Latest Run.lnk",
    "Hamilton Camera Status.lnk",
    "Start Hamilton Camera Daemon.lnk",
    "Stop Hamilton Camera Daemon.lnk"
)

foreach ($name in $desktopShortcuts) {
    $path = Join-Path $desktopDir $name
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}

foreach ($name in $startMenuShortcuts) {
    $path = Join-Path $startMenuDir $name
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}

if (Test-Path -LiteralPath $startMenuDir) {
    $remaining = Get-ChildItem -LiteralPath $startMenuDir -Force -ErrorAction SilentlyContinue
    if (-not $remaining) {
        Remove-Item -LiteralPath $startMenuDir -Force
    }
}

Write-Host "Removed Hamilton camera workstation shortcuts." -ForegroundColor Green
