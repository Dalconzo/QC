<#
  cameras/install-camera-workstation.ps1

  Bootstrap one Hamilton workstation for local camera capture and replay.

  The goal is to collapse the current multi-step rollout into one operator
  command that can:
  - discover or accept workstation-local overrides
  - write camera-recorder.local.json for that machine
  - create the local output/log folders
  - validate the merged config
  - install the local replay shortcuts
  - install and optionally start the daemon Scheduled Task
#>

[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$LocalConfig = "",
    [string]$ProfileId = "default",
    [string]$CameraSource = "",
    [string]$CameraLabel = "",
    [string]$HamiltonLogDir = "",
    [string]$RunsRoot = "",
    [string]$RecorderLogDir = "",
    [string]$FfmpegPath = "",
    [Nullable[int]]$ReplayPort = $null,
    [switch]$InstallFfmpeg,
    [switch]$ListDevices,
    [switch]$SkipShortcuts,
    [switch]$DesktopOnly,
    [switch]$SkipDaemonTask,
    [switch]$RunDaemonNow
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

function Resolve-DefaultPath {
    param(
        [string]$CurrentValue,
        [string]$RelativePath
    )

    if ($CurrentValue) {
        return [System.IO.Path]::GetFullPath($CurrentValue)
    }

    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $RelativePath))
}

function Get-PythonCommand {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "Python is not available in PATH."
    }
    return $python
}

function Get-WingetCommand {
    return Get-Command winget -ErrorAction SilentlyContinue
}

function Find-FfmpegPath {
    param(
        [string]$ExplicitPath
    )

    $candidates = @()
    if ($ExplicitPath) {
        $candidates += [System.IO.Path]::GetFullPath($ExplicitPath)
    }

    $candidates += @(
        (Join-Path $scriptDir "ffmpeg.exe"),
        (Join-Path $scriptDir "dist\ffmpeg.exe")
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }

    $ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($ffmpeg) {
        return $ffmpeg.Source
    }

    return ""
}

function Ensure-FfmpegPath {
    param(
        [string]$ExplicitPath,
        [switch]$AllowInstall
    )

    $resolved = Find-FfmpegPath -ExplicitPath $ExplicitPath
    if ($resolved) {
        return $resolved
    }

    if (-not $AllowInstall) {
        return ""
    }

    $winget = Get-WingetCommand
    if (-not $winget) {
        throw "ffmpeg was not found and winget is not available. Install ffmpeg manually or rerun with -FfmpegPath."
    }

    Write-Host "ffmpeg was not found. Installing it with winget ..." -ForegroundColor Cyan
    & $winget.Source install --id Gyan.FFmpeg -e --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed to install ffmpeg."
    }

    $resolved = Find-FfmpegPath -ExplicitPath $ExplicitPath
    if (-not $resolved) {
        throw "ffmpeg installation completed, but ffmpeg is still not visible in PATH. Open a new shell or rerun with -FfmpegPath."
    }

    return $resolved
}

function Get-EffectiveConfig {
    param(
        [string]$BaseConfig,
        [string]$OverrideConfig,
        [string]$SelectedProfile
    )

    $inspectScript = Join-Path $scriptDir "inspect-camera-config.py"
    $argsList = @(
        $inspectScript,
        "--config", $BaseConfig,
        "--local-config", $OverrideConfig,
        "--json"
    )

    if ($SelectedProfile) {
        $argsList += "--profile"
        $argsList += $SelectedProfile
    }

    $configJson = & python @argsList
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read effective camera config."
    }

    return $configJson | ConvertFrom-Json
}

function Write-LocalOverride {
    param(
        [string]$Path,
        [hashtable]$Payload
    )

    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }

    $json = $Payload | ConvertTo-Json -Depth 10
    Set-Content -LiteralPath $Path -Value $json -Encoding UTF8
}

if (-not $Config) {
    $Config = Resolve-DefaultPath -CurrentValue "" -RelativePath "config\camera-recorder.json"
} else {
    $Config = [System.IO.Path]::GetFullPath($Config)
}

if (-not $LocalConfig) {
    $LocalConfig = Resolve-DefaultPath -CurrentValue "" -RelativePath "config\camera-recorder.local.json"
} else {
    $LocalConfig = [System.IO.Path]::GetFullPath($LocalConfig)
}

$python = Get-PythonCommand
$ffmpegResolved = Ensure-FfmpegPath -ExplicitPath $FfmpegPath -AllowInstall:$InstallFfmpeg

if ($ListDevices) {
    $recorderScript = Join-Path $scriptDir "camera-recorder.py"
    $argsList = @(
        $recorderScript,
        "--config", $Config,
        "--local-config", $LocalConfig
    )
    if ($ffmpegResolved) {
        $argsList += "--ffmpeg"
        $argsList += $ffmpegResolved
    }
    $argsList += "--list-devices"
    & python @argsList
    exit $LASTEXITCODE
}

$effective = Get-EffectiveConfig -BaseConfig $Config -OverrideConfig $LocalConfig -SelectedProfile $ProfileId
$selectedProfile = if ($effective.selected_profile) { $effective.selected_profile } else { $null }

$effectiveCameraSource = $CameraSource
if (-not $effectiveCameraSource) {
    if ($selectedProfile -and $selectedProfile.source) {
        $effectiveCameraSource = [string]$selectedProfile.source
    } else {
        $effectiveCameraSource = "0"
    }
}

$effectiveCameraLabel = $CameraLabel
if (-not $effectiveCameraLabel) {
    if ($selectedProfile -and $selectedProfile.label) {
        $effectiveCameraLabel = [string]$selectedProfile.label
    } else {
        $effectiveCameraLabel = "Default Camera"
    }
}

$effectiveHamiltonLogDir = if ($HamiltonLogDir) {
    [System.IO.Path]::GetFullPath($HamiltonLogDir)
} elseif ($effective.config.hamilton.log_dir) {
    [string]$effective.config.hamilton.log_dir
} else {
    "C:\Program Files (x86)\HAMILTON\LogFiles"
}

$effectiveRunsRoot = if ($RunsRoot) {
    [System.IO.Path]::GetFullPath($RunsRoot)
} elseif ($effective.config.storage.runs_root) {
    [string]$effective.config.storage.runs_root
} else {
    [System.IO.Path]::GetFullPath((Join-Path $repoRoot "cameras\video_clips"))
}

$effectiveRecorderLogDir = if ($RecorderLogDir) {
    [System.IO.Path]::GetFullPath($RecorderLogDir)
} elseif ($effective.config.storage.recorder_log_dir) {
    [string]$effective.config.storage.recorder_log_dir
} else {
    [System.IO.Path]::GetFullPath((Join-Path $repoRoot "logs"))
}

$effectiveDaemonLogPath = [System.IO.Path]::GetFullPath((Join-Path $effectiveRecorderLogDir "camera-daemon.log"))
$effectiveDaemonStatusPath = [System.IO.Path]::GetFullPath((Join-Path $effectiveRecorderLogDir "camera-daemon-status.json"))
$effectiveDaemonPidPath = [System.IO.Path]::GetFullPath((Join-Path $effectiveRecorderLogDir "camera-daemon.pid"))
$effectiveDaemonStopPath = [System.IO.Path]::GetFullPath((Join-Path $scriptDir "camera-daemon.stop"))
$effectiveRecorderStopPath = [System.IO.Path]::GetFullPath((Join-Path $scriptDir "cameras.recorder.stop"))
$effectiveReplayLogPath = [System.IO.Path]::GetFullPath((Join-Path $effectiveRecorderLogDir "camera-replay.log"))

$override = @{
    hamilton = @{
        log_dir = $effectiveHamiltonLogDir
    }
    storage = @{
        runs_root = $effectiveRunsRoot
        recorder_log_dir = $effectiveRecorderLogDir
    }
    recorder = @{
        default_profile = $ProfileId
        stop_file = $effectiveRecorderStopPath
    }
    replay = @{
        log_path = $effectiveReplayLogPath
    }
    live = @{
        default_profile = $ProfileId
    }
    daemon = @{
        task_name = "HamiltonCameraRecorderDaemon"
        stop_file = $effectiveDaemonStopPath
        pid_file = $effectiveDaemonPidPath
        status_path = $effectiveDaemonStatusPath
        log_path = $effectiveDaemonLogPath
    }
    profiles = @(
        @{
            id = $ProfileId
            label = $effectiveCameraLabel
            source = $effectiveCameraSource
            ffmpeg_path = $ffmpegResolved
        }
    )
}

if ($ffmpegResolved) {
    $override.recorder.ffmpeg_path = $ffmpegResolved
}

if ($ReplayPort -ne $null) {
    $override.replay = @{
        port = [int]$ReplayPort
    }
}

Write-Host "Writing workstation-local camera override ..." -ForegroundColor Cyan
Write-LocalOverride -Path $LocalConfig -Payload $override

New-Item -ItemType Directory -Force -Path $effectiveRunsRoot | Out-Null
New-Item -ItemType Directory -Force -Path $effectiveRecorderLogDir | Out-Null

$validateScript = Join-Path $scriptDir "show-camera-config.ps1"
Write-Host "Validating effective camera config ..." -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass -File $validateScript -Config $Config -LocalConfig $LocalConfig -Validate
if ($LASTEXITCODE -ne 0) {
    throw "Camera config validation failed."
}

if (-not $SkipShortcuts) {
    $shortcutScript = Join-Path $scriptDir "install-local-camera-tools.ps1"
    Write-Host "Installing workstation shortcuts ..." -ForegroundColor Cyan
    $shortcutArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $shortcutScript,
        "-Config", $Config,
        "-LocalConfig", $LocalConfig
    )
    if ($DesktopOnly) {
        $shortcutArgs += "-DesktopOnly"
    }
    & powershell @shortcutArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install workstation shortcuts."
    }
}

if (-not $SkipDaemonTask) {
    $taskScript = Join-Path $scriptDir "install-camera-daemon-task.ps1"
    Write-Host "Installing camera daemon Scheduled Task ..." -ForegroundColor Cyan
    $taskArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $taskScript,
        "-Config", $Config,
        "-LocalConfig", $LocalConfig
    )
    if ($RunDaemonNow) {
        $taskArgs += "-RunNow"
    }
    & powershell @taskArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install camera daemon Scheduled Task."
    }
}

Write-Host "Hamilton camera workstation bootstrap is complete." -ForegroundColor Green
Write-Host "Local override: $LocalConfig"
Write-Host "Camera source: $effectiveCameraSource"
Write-Host "Runs root: $effectiveRunsRoot"
Write-Host "Recorder logs: $effectiveRecorderLogDir"
if ($ffmpegResolved) {
    Write-Host "ffmpeg: $ffmpegResolved"
} else {
    Write-Host "ffmpeg: not found. Install with -InstallFfmpeg, provide -FfmpegPath, or let the recorder fall back to OpenCV."
}
