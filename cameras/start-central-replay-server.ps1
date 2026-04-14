<#
  cameras/start-central-replay-server.ps1

  Launch the central replay browse server on the host machine.

  By default this starts the server in the background, waits for the configured
  health endpoint, and optionally opens the browser.
#>

[CmdletBinding()]
param(
    [string]$ServerConfig = "",
    [string]$ServerLocalConfig = "",
    [string]$CameraConfig = "",
    [string]$CameraLocalConfig = "",
    [string]$UploadRoot = "",
    [string]$CatalogPath = "",
    [string]$BindHost = "",
    [Nullable[int]]$Port = $null,
    [string]$ServerLog = "",
    [string]$SiteName = "",
    [string]$HealthPath = "",
    [switch]$Background,
    [switch]$OpenBrowser,
    [int]$WaitSec = 15
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

if (-not $ServerConfig) {
    $ServerConfig = Join-Path $repoRoot "config\central-replay-server.json"
}
if (-not $ServerLocalConfig) {
    $ServerLocalConfig = Join-Path $repoRoot "config\central-replay-server.local.json"
}
if (-not $CameraConfig) {
    $CameraConfig = Join-Path $repoRoot "config\camera-recorder.json"
}
if (-not $CameraLocalConfig) {
    $CameraLocalConfig = Join-Path $repoRoot "config\camera-recorder.local.json"
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python is not available in PATH."
}

$serverScript = Join-Path $scriptDir "central-replay-server.py"
if (-not (Test-Path -LiteralPath $serverScript)) {
    throw "Central replay server script not found: $serverScript"
}

$inspectArgs = @(
    $serverScript,
    "--server-config", ([System.IO.Path]::GetFullPath($ServerConfig)),
    "--server-local-config", ([System.IO.Path]::GetFullPath($ServerLocalConfig)),
    "--config", ([System.IO.Path]::GetFullPath($CameraConfig)),
    "--local-config", ([System.IO.Path]::GetFullPath($CameraLocalConfig)),
    "--print-config",
    "--json"
)
if ($UploadRoot) {
    $inspectArgs += @("--upload-root", $UploadRoot)
}
if ($CatalogPath) {
    $inspectArgs += @("--catalog-path", $CatalogPath)
}
if ($BindHost) {
    $inspectArgs += @("--host", $BindHost)
}
if ($Port -ne $null) {
    $inspectArgs += @("--port", $Port)
}
if ($ServerLog) {
    $inspectArgs += @("--log-path", ([System.IO.Path]::GetFullPath($ServerLog)))
}
if ($SiteName) {
    $inspectArgs += @("--site-name", $SiteName)
}
if ($HealthPath) {
    $inspectArgs += @("--health-path", $HealthPath)
}

$configJson = & python @inspectArgs
if ($LASTEXITCODE -ne 0) {
    throw "Failed to resolve central replay server config."
}

$effectivePayload = $configJson | ConvertFrom-Json
$runtime = $effectivePayload.runtime

$resolvedHost = [string]$runtime.host
$resolvedPort = [int]$runtime.port
$resolvedHealthPath = [string]$runtime.healthcheck_path
$resolvedLogPath = [string]$runtime.log_path

$logDir = Split-Path -Parent $resolvedLogPath
if ($logDir) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
$stdoutLog = [System.IO.Path]::Combine($logDir, (([System.IO.Path]::GetFileNameWithoutExtension($resolvedLogPath)) + ".stdout.log"))
$stderrLog = [System.IO.Path]::Combine($logDir, (([System.IO.Path]::GetFileNameWithoutExtension($resolvedLogPath)) + ".stderr.log"))

$argsList = @(
    $serverScript,
    "--server-config", ([System.IO.Path]::GetFullPath($ServerConfig)),
    "--server-local-config", ([System.IO.Path]::GetFullPath($ServerLocalConfig)),
    "--config", ([System.IO.Path]::GetFullPath($CameraConfig)),
    "--local-config", ([System.IO.Path]::GetFullPath($CameraLocalConfig))
)
if ($UploadRoot) {
    $argsList += @("--upload-root", $UploadRoot)
}
if ($CatalogPath) {
    $argsList += @("--catalog-path", $CatalogPath)
}
if ($BindHost) {
    $argsList += @("--host", $BindHost)
}
if ($Port -ne $null) {
    $argsList += @("--port", $Port)
}
if ($ServerLog) {
    $argsList += @("--log-path", ([System.IO.Path]::GetFullPath($ServerLog)))
}
if ($SiteName) {
    $argsList += @("--site-name", $SiteName)
}
if ($HealthPath) {
    $argsList += @("--health-path", $HealthPath)
}

function Get-ServerBaseUrl {
    param(
        [string]$HostName,
        [int]$PortNumber
    )

    $browserHost = if ($HostName -eq "0.0.0.0") { "127.0.0.1" } else { $HostName }
    return "http://{0}:{1}" -f $browserHost, $PortNumber
}

function Test-CentralReplayServer {
    param(
        [string]$BaseUrl,
        [string]$Path
    )

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ($BaseUrl + $Path) -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    } catch {
        return $false
    }
}

function Wait-CentralReplayServer {
    param(
        [string]$BaseUrl,
        [string]$Path,
        [int]$TimeoutSec
    )

    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSec))
    while ((Get-Date) -lt $deadline) {
        if (Test-CentralReplayServer -BaseUrl $BaseUrl -Path $Path) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

$baseUrl = Get-ServerBaseUrl -HostName $resolvedHost -PortNumber $resolvedPort

Write-Host "Starting central replay server..."
Write-Host "URL: $baseUrl"
Write-Host "Health: $($baseUrl + $resolvedHealthPath)"

if ($Background) {
    if (-not (Test-CentralReplayServer -BaseUrl $baseUrl -Path $resolvedHealthPath)) {
        $proc = Start-Process -FilePath $python.Source -ArgumentList $argsList -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
        Write-Host ("Central replay server started with PID {0}" -f $proc.Id) -ForegroundColor Green
        if (-not (Wait-CentralReplayServer -BaseUrl $baseUrl -Path $resolvedHealthPath -TimeoutSec $WaitSec)) {
            throw "Central replay server did not become ready within $WaitSec seconds. Check $resolvedLogPath"
        }
    } else {
        Write-Host "Central replay server is already listening; reusing existing host." -ForegroundColor Yellow
    }

    if ($OpenBrowser) {
        Start-Process ($baseUrl + "/") | Out-Null
        Write-Host ("Opened browser at {0}/" -f $baseUrl) -ForegroundColor Green
    }
    exit 0
}

if ($OpenBrowser) {
    Write-Host "Foreground mode keeps the server attached to this window; browser auto-open is ignored." -ForegroundColor Yellow
}

& python @argsList
exit $LASTEXITCODE
