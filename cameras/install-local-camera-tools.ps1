<#
  cameras/install-local-camera-tools.ps1

  Install workstation shortcuts for the local Hamilton camera workflow. This
  keeps the prototype usable by engineers without asking them to remember the
  underlying PowerShell commands.
#>

[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$LocalConfig = "",
    [switch]$DesktopOnly
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

$desktopDir = [Environment]::GetFolderPath("Desktop")
$startMenuPrograms = [Environment]::GetFolderPath("Programs")
$startMenuDir = Join-Path $startMenuPrograms "Hamilton Camera"

$targets = @(
    @{
        Name = "Hamilton Replay.lnk"
        Directory = $desktopDir
        Script = "start-replay-app.ps1"
        Arguments = @("-Background", "-OpenBrowser")
        Description = "Open the local Hamilton replay app."
    },
    @{
        Name = "Hamilton Latest Run.lnk"
        Directory = $desktopDir
        Script = "open-latest-run.ps1"
        Arguments = @()
        Description = "Open the most recent replayable Hamilton run."
    },
    @{
        Name = "Hamilton Live View.lnk"
        Directory = $desktopDir
        Script = "start-replay-app.ps1"
        Arguments = @("-Background", "-OpenBrowser", "-LiveView")
        Description = "Open the local Hamilton live camera preview."
    },
    @{
        Name = "Hamilton Camera Status.lnk"
        Directory = $desktopDir
        Script = "show-camera-daemon-status.ps1"
        Arguments = @()
        Description = "Show the local Hamilton camera daemon status."
    }
)

if (-not $DesktopOnly) {
    $targets += @(
        @{
            Name = "Hamilton Replay.lnk"
            Directory = $startMenuDir
            Script = "start-replay-app.ps1"
            Arguments = @("-Background", "-OpenBrowser")
            Description = "Open the local Hamilton replay app."
        },
        @{
            Name = "Hamilton Latest Run.lnk"
            Directory = $startMenuDir
            Script = "open-latest-run.ps1"
            Arguments = @()
            Description = "Open the most recent replayable Hamilton run."
        },
        @{
            Name = "Hamilton Live View.lnk"
            Directory = $startMenuDir
            Script = "start-replay-app.ps1"
            Arguments = @("-Background", "-OpenBrowser", "-LiveView")
            Description = "Open the local Hamilton live camera preview."
        },
        @{
            Name = "Hamilton Camera Status.lnk"
            Directory = $startMenuDir
            Script = "show-camera-daemon-status.ps1"
            Arguments = @()
            Description = "Show the local Hamilton camera daemon status."
        },
        @{
            Name = "Start Hamilton Camera Daemon.lnk"
            Directory = $startMenuDir
            Script = "start-camera-daemon.ps1"
            Arguments = @()
            Description = "Start the workstation camera daemon."
        },
        @{
            Name = "Stop Hamilton Camera Daemon.lnk"
            Directory = $startMenuDir
            Script = "stop-camera-daemon.ps1"
            Arguments = @()
            Description = "Stop the workstation camera daemon cleanly."
        }
    )
}

if (-not $DesktopOnly) {
    New-Item -ItemType Directory -Force -Path $startMenuDir | Out-Null
}

$shell = New-Object -ComObject WScript.Shell

foreach ($target in $targets) {
    New-Item -ItemType Directory -Force -Path $target.Directory | Out-Null

    $shortcutPath = Join-Path $target.Directory $target.Name
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = "powershell.exe"

    $argList = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $scriptDir $target.Script),
        "-Config", ([System.IO.Path]::GetFullPath($Config)),
        "-LocalConfig", ([System.IO.Path]::GetFullPath($LocalConfig))
    ) + $target.Arguments
    $shortcut.Arguments = [string]::Join(" ", ($argList | ForEach-Object {
        if ($_ -match "\s") { '"' + $_ + '"' } else { $_ }
    }))
    $shortcut.WorkingDirectory = $repoRoot
    $shortcut.Description = $target.Description
    $shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,220"
    $shortcut.Save()
}

Write-Host "Installed Hamilton camera workstation shortcuts." -ForegroundColor Green
if (-not $DesktopOnly) {
    Write-Host "Desktop: $desktopDir"
    Write-Host "Start Menu: $startMenuDir"
} else {
    Write-Host "Desktop: $desktopDir"
}
