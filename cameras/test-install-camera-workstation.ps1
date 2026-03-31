<#
  cameras/test-install-camera-workstation.ps1

  Smoke test for the one-command workstation bootstrap flow.

  This avoids real desktop/task-scheduler changes by skipping shortcut and task
  installation, but still verifies that the bootstrap script can write a local
  override, create the working folders, and produce a valid effective config.
#>

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

$tmpRoot = Join-Path $repoRoot "tmp\camera-bootstrap-test"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $tmpRoot
New-Item -ItemType Directory -Force -Path $tmpRoot | Out-Null

$hamiltonDir = Join-Path $tmpRoot "hamilton"
$runsRoot = Join-Path $tmpRoot "runs"
$logDir = Join-Path $tmpRoot "logs"
New-Item -ItemType Directory -Force -Path $hamiltonDir | Out-Null

$configPath = Join-Path $tmpRoot "camera-recorder.json"
$localPath = Join-Path $tmpRoot "camera-recorder.local.json"

$baseConfig = @{
    hamilton = @{
        log_dir = $hamiltonDir
        process_name = "HxRun.exe"
    }
    storage = @{
        runs_root = (Join-Path $tmpRoot "base-runs")
        recorder_log_dir = (Join-Path $tmpRoot "base-logs")
    }
    replay = @{
        port = 5059
        log_path = (Join-Path $tmpRoot "replay.log")
    }
    daemon = @{
        task_name = "HamiltonCameraBootstrapSmoke"
        stop_file = (Join-Path $tmpRoot "daemon.stop")
        pid_file = (Join-Path $tmpRoot "daemon.pid")
        status_path = (Join-Path $tmpRoot "daemon-status.json")
        log_path = (Join-Path $tmpRoot "daemon.log")
    }
    profiles = @(
        @{
            id = "default"
            label = "Base Camera"
            source = "0"
        }
    )
} | ConvertTo-Json -Depth 6

Set-Content -LiteralPath $configPath -Value $baseConfig -Encoding UTF8

$bootstrap = Join-Path $scriptDir "install-camera-workstation.ps1"
& powershell -NoProfile -ExecutionPolicy Bypass -File $bootstrap `
    -Config $configPath `
    -LocalConfig $localPath `
    -ProfileId workstation `
    -CameraSource 'dshow:video="Smoke Camera"' `
    -CameraLabel 'Smoke Camera' `
    -HamiltonLogDir $hamiltonDir `
    -RunsRoot $runsRoot `
    -RecorderLogDir $logDir `
    -ReplayPort 5060 `
    -SkipShortcuts `
    -SkipDaemonTask

if ($LASTEXITCODE -ne 0) {
    throw "Bootstrap script returned exit code $LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $localPath)) {
    throw "Local override was not written: $localPath"
}

if (-not (Test-Path -LiteralPath $runsRoot)) {
    throw "Runs root was not created: $runsRoot"
}

if (-not (Test-Path -LiteralPath $logDir)) {
    throw "Recorder log dir was not created: $logDir"
}

$localConfig = Get-Content -LiteralPath $localPath | ConvertFrom-Json
if ($localConfig.recorder.default_profile -ne "workstation") {
    throw "Unexpected default profile in local override."
}

if ($localConfig.profiles[0].source -ne 'dshow:video=Smoke Camera') {
    throw "Unexpected camera source in local override."
}

$validate = Join-Path $scriptDir "show-camera-config.ps1"
& powershell -NoProfile -ExecutionPolicy Bypass -File $validate -Config $configPath -LocalConfig $localPath -Validate
if ($LASTEXITCODE -ne 0) {
    throw "Merged config did not validate after bootstrap."
}

Write-Host "camera workstation bootstrap smoke test passed" -ForegroundColor Green
