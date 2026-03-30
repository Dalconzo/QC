<#
  cameras/start-replay-app.ps1

  Launch the local Hamilton replay app for workstation use.

  For rollout we want two operator-friendly modes:
  - foreground debug mode while we are still stabilizing the prototype
  - background mode that starts the local server, waits for it to respond, and
    optionally opens the browser to the run picker or latest ready run
#>

[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$LocalConfig = "",
    [string]$RunsRoot = "",
    [string]$BindHost = "",
    [Nullable[int]]$Port = $null,
    [string]$ReplayLog = "",
    [switch]$Background,
    [switch]$OpenBrowser,
    [switch]$LatestRun,
    [switch]$LiveView,
    [int]$WaitSec = 15
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

$inspectScript = Join-Path $scriptDir "inspect-camera-config.py"
$inspectArgs = @(
    $inspectScript,
    "--config", ([System.IO.Path]::GetFullPath($Config)),
    "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig)),
    "--json"
)
$configJson = & python @inspectArgs
if ($LASTEXITCODE -ne 0) {
    throw "Failed to read effective camera config."
}

$effectiveConfig = $configJson | ConvertFrom-Json
$resolvedRunsRoot = if ($RunsRoot) { [System.IO.Path]::GetFullPath($RunsRoot) } else { [string]$effectiveConfig.storage.runs_root }
$resolvedHost = if ($BindHost) { $BindHost } else { [string]$effectiveConfig.replay.host }
$resolvedHost = if ([string]::IsNullOrWhiteSpace($resolvedHost)) { "127.0.0.1" } else { $resolvedHost }
$resolvedPort = if ($Port -ne $null) { [int]$Port } else { [int]$effectiveConfig.replay.port }
$resolvedReplayLog = if ($ReplayLog) { [System.IO.Path]::GetFullPath($ReplayLog) } else { [string]$effectiveConfig.replay.log_path }

if (-not $resolvedReplayLog) {
    throw "Replay log path is empty."
}

$replayLogDir = Split-Path -Parent $resolvedReplayLog
if ($replayLogDir) {
    New-Item -ItemType Directory -Force -Path $replayLogDir | Out-Null
}
$resolvedReplayErrorLog = [System.IO.Path]::Combine(
    $replayLogDir,
    (([System.IO.Path]::GetFileNameWithoutExtension($resolvedReplayLog)) + ".stderr.log")
)

$appPath = Join-Path $scriptDir "replay-app.py"
$argsList = @(
    $appPath,
    "--config", ([System.IO.Path]::GetFullPath($Config)),
    "--local-config", ([System.IO.Path]::GetFullPath($LocalConfig))
)

if ($resolvedRunsRoot) {
    $argsList += "--runs-root"
    $argsList += $resolvedRunsRoot
}

if ($resolvedHost) {
    $argsList += "--host"
    $argsList += $resolvedHost
}

if ($resolvedPort -ne 0) {
    $argsList += "--port"
    $argsList += $resolvedPort
}

function Get-ReplayBaseUrl {
    param(
        [string]$HostName,
        [int]$PortNumber
    )

    return "http://{0}:{1}" -f $HostName, $PortNumber
}

function Test-ReplayServer {
    param([string]$BaseUrl)

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ($BaseUrl + "/api/runs") -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    } catch {
        return $false
    }
}

function Wait-ReplayServer {
    param(
        [string]$BaseUrl,
        [int]$TimeoutSec
    )

    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSec))
    while ((Get-Date) -lt $deadline) {
        if (Test-ReplayServer -BaseUrl $BaseUrl) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Get-LatestRunUrl {
    param([string]$BaseUrl)

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ($BaseUrl + "/api/runs/latest") -TimeoutSec 2
        $payload = $response.Content | ConvertFrom-Json
        if ($payload.item -and $payload.item.run_id) {
            return ($BaseUrl + "/?run_id=" + [System.Uri]::EscapeDataString([string]$payload.item.run_id))
        }
    } catch {
    }
    return $BaseUrl + "/"
}

function Get-LiveViewUrl {
    param([string]$BaseUrl)

    return $BaseUrl + "/?mode=live"
}

$baseUrl = Get-ReplayBaseUrl -HostName $resolvedHost -PortNumber $resolvedPort

Write-Host "Starting Hamilton replay app..."
Write-Host "Runs root: $resolvedRunsRoot"
Write-Host "URL: $baseUrl"

if ($Background) {
    if (-not (Test-ReplayServer -BaseUrl $baseUrl)) {
        $startInfo = @{
            FilePath = $python.Source
            ArgumentList = $argsList
            WindowStyle = "Hidden"
            RedirectStandardOutput = $resolvedReplayLog
            RedirectStandardError = $resolvedReplayErrorLog
            PassThru = $true
        }
        $proc = Start-Process @startInfo
        Write-Host ("Replay app background process started with PID {0}" -f $proc.Id) -ForegroundColor Green
        if (-not (Wait-ReplayServer -BaseUrl $baseUrl -TimeoutSec $WaitSec)) {
            throw "Replay app did not become ready within $WaitSec seconds. Check $resolvedReplayLog"
        }
    } else {
        Write-Host "Replay app is already listening; reusing existing local server." -ForegroundColor Yellow
    }

    if ($OpenBrowser) {
        $targetUrl = if ($LiveView) {
            Get-LiveViewUrl -BaseUrl $baseUrl
        } elseif ($LatestRun) {
            Get-LatestRunUrl -BaseUrl $baseUrl
        } else {
            $baseUrl + "/"
        }
        Start-Process $targetUrl | Out-Null
        Write-Host "Opened browser at $targetUrl" -ForegroundColor Green
    }
    exit 0
}

if ($OpenBrowser) {
    Write-Host "Foreground mode keeps the replay server attached to this window; browser auto-open is ignored." -ForegroundColor Yellow
}

& python @argsList
exit $LASTEXITCODE
