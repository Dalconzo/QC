<#
  cameras/test-camera-workstation.ps1

  Workstation preflight/self-test for the local Hamilton camera stack.

  This is the operator-facing "are we ready?" command before or after rollout.
  It validates config, checks local storage paths, verifies manifest health,
  optionally probes the configured camera, and can bring up the replay app long
  enough to confirm the local site responds.
#>

[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$LocalConfig = "",
    [string]$ProfileId = "",
    [string]$RunsRoot = "",
    [string]$ProbeOutput = "",
    [Nullable[int]]$ReplayPort = $null,
    [switch]$ProbeCamera,
    [switch]$StartReplay,
    [int]$ReplayWaitSec = 10,
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

function Invoke-PythonJson {
    param(
        [string]$ScriptPath,
        [string[]]$Arguments
    )

    $raw = & $python.Source $ScriptPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: python $ScriptPath $($Arguments -join ' ')"
    }
    return $raw | ConvertFrom-Json
}

$inspectScript = Join-Path $scriptDir "inspect-camera-config.py"
$inspectArgs = @(
    "--config", ([System.IO.Path]::GetFullPath($Config)),
    "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig)),
    "--json"
)
if ($ProfileId) {
    $inspectArgs += "--profile"
    $inspectArgs += $ProfileId
}
$effective = Invoke-PythonJson -ScriptPath $inspectScript -Arguments $inspectArgs
$configRoot = if ($effective.config) { $effective.config } else { $effective }
$selectedProfile = if ($effective.selected_profile) { $effective.selected_profile } else { $null }

$resolvedRunsRoot = if ($RunsRoot) { [System.IO.Path]::GetFullPath($RunsRoot) } else { [string]$configRoot.storage.runs_root }
$resolvedReplayPort = if ($ReplayPort -ne $null) { [int]$ReplayPort } else { [int]$configRoot.replay.port }
$resolvedReplayHost = if ([string]::IsNullOrWhiteSpace([string]$configRoot.replay.host)) { "127.0.0.1" } else { [string]$configRoot.replay.host }
$resolvedReplayUrl = "http://{0}:{1}" -f $resolvedReplayHost, $resolvedReplayPort

$runsRootCheck = @{
    path = $resolvedRunsRoot
    exists = $false
    writable = $false
}
New-Item -ItemType Directory -Force -Path $resolvedRunsRoot | Out-Null
$runsRootCheck.exists = Test-Path -LiteralPath $resolvedRunsRoot
$probeWritePath = Join-Path $resolvedRunsRoot ".camera-preflight-write-test"
try {
    Set-Content -LiteralPath $probeWritePath -Value "ok" -Encoding ASCII
    Remove-Item -LiteralPath $probeWritePath -Force
    $runsRootCheck.writable = $true
} catch {
    $runsRootCheck.writable = $false
}

$manifestScript = Join-Path $scriptDir "inspect-run-manifests.py"
$manifestPayload = Invoke-PythonJson -ScriptPath $manifestScript -Arguments @(
    "--config", ([System.IO.Path]::GetFullPath($Config)),
    "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig)),
    "--runs-root", $resolvedRunsRoot,
    "--json"
)

$cameraProbePayload = $null
if ($ProbeCamera) {
    $probeScript = Join-Path $scriptDir "test-camera-source.py"
    $probeArgs = @(
        "--config", ([System.IO.Path]::GetFullPath($Config)),
        "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig)),
        "--json"
    )
    if ($ProfileId) {
        $probeArgs += "--profile"
        $probeArgs += $ProfileId
    }
    if ($ProbeOutput) {
        $probeArgs += "--output"
        $probeArgs += ([System.IO.Path]::GetFullPath($ProbeOutput))
    }
    try {
        $cameraProbePayload = Invoke-PythonJson -ScriptPath $probeScript -Arguments $probeArgs
    } catch {
        $cameraProbePayload = @{
            ok = $false
            errors = @($_.Exception.Message)
        }
    }
}

$replayPayload = @{
    started = $false
    ready = $false
    url = $resolvedReplayUrl
}
if ($StartReplay) {
    $startScript = Join-Path $scriptDir "start-replay-app.ps1"
    & powershell -NoProfile -ExecutionPolicy Bypass -File $startScript `
        -Config ([System.IO.Path]::GetFullPath($Config)) `
        -LocalConfig ([System.IO.Path]::GetFullPath($LocalConfig)) `
        -RunsRoot $resolvedRunsRoot `
        -Port $resolvedReplayPort `
        -Background `
        -WaitSec $ReplayWaitSec | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Replay app failed to start during preflight."
    }
    $replayPayload.started = $true
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ($resolvedReplayUrl + "/api/runs") -TimeoutSec 3
        $replayPayload.ready = ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300)
    } catch {
        $replayPayload.ready = $false
        $replayPayload.error = $_.Exception.Message
    }
}

$daemonTaskStatus = @{
    task_name = [string]$configRoot.daemon.task_name
    installed = $false
}
try {
    $task = Get-ScheduledTask -TaskName $configRoot.daemon.task_name -ErrorAction Stop
    $daemonTaskStatus.installed = $true
    $daemonTaskStatus.state = [string]$task.State
} catch {
    $daemonTaskStatus.installed = $false
}

$payload = [ordered]@{
    config = @{
        base = [string]$effective.config_path
        local = [string]$effective.local_override_path
        local_override_exists = [bool]$effective.local_override_exists
    }
    validation = $effective.validation
    selected_profile = $selectedProfile
    runs_root = $runsRootCheck
    manifests = $manifestPayload.summary
    daemon_task = $daemonTaskStatus
    replay = $replayPayload
}

if ($cameraProbePayload) {
    $payload.camera_probe = $cameraProbePayload
}

if ($AsJson) {
    $payload | ConvertTo-Json -Depth 8
    $hasFailure = $false
    if ($payload.validation.errors.Count -gt 0) { $hasFailure = $true }
    if (-not $payload.runs_root.writable) { $hasFailure = $true }
    if ($cameraProbePayload -and (-not $cameraProbePayload.ok)) { $hasFailure = $true }
    if ($StartReplay -and (-not $payload.replay.ready)) { $hasFailure = $true }
    exit $(if ($hasFailure) { 1 } else { 0 })
}

Write-Host "Camera workstation preflight" -ForegroundColor Cyan
Write-Host "Base config: $($payload.config.base)"
Write-Host "Local override: $($payload.config.local) (exists=$($payload.config.local_override_exists))"
Write-Host "Runs root: $($payload.runs_root.path)"
Write-Host "Runs root writable: $($payload.runs_root.writable)"
Write-Host "Manifest summary: ready=$($payload.manifests.ready_count) stale=$($payload.manifests.stale_count)"
Write-Host "Daemon task installed: $($payload.daemon_task.installed)"
if ($payload.daemon_task.state) {
    Write-Host "Daemon task state: $($payload.daemon_task.state)"
}
if ($selectedProfile) {
    Write-Host "Selected profile: $($selectedProfile.id) [$($selectedProfile.label)] -> $($selectedProfile.source)"
}
if ($payload.validation.warnings) {
    Write-Host "Warnings:" -ForegroundColor Yellow
    foreach ($warning in $payload.validation.warnings) {
        Write-Host "  - $warning"
    }
}
if ($payload.validation.errors) {
    Write-Host "Errors:" -ForegroundColor Red
    foreach ($error in $payload.validation.errors) {
        Write-Host "  - $error"
    }
}
if ($cameraProbePayload) {
    Write-Host "Camera probe ok: $($cameraProbePayload.ok)"
    if ($cameraProbePayload.output_path) {
        Write-Host "Probe frame: $($cameraProbePayload.output_path)"
    }
    if ($cameraProbePayload.errors) {
        foreach ($item in $cameraProbePayload.errors) {
            Write-Host "  probe error: $item" -ForegroundColor Red
        }
    }
}
if ($StartReplay) {
    Write-Host "Replay site ready: $($payload.replay.ready) at $($payload.replay.url)"
    if ($payload.replay.error) {
        Write-Host "  replay error: $($payload.replay.error)" -ForegroundColor Red
    }
}

$hasFailure = $false
if ($payload.validation.errors.Count -gt 0) { $hasFailure = $true }
if (-not $payload.runs_root.writable) { $hasFailure = $true }
if ($cameraProbePayload -and (-not $cameraProbePayload.ok)) { $hasFailure = $true }
if ($StartReplay -and (-not $payload.replay.ready)) { $hasFailure = $true }
exit $(if ($hasFailure) { 1 } else { 0 })
