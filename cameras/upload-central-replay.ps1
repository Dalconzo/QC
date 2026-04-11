[CmdletBinding()]
param(
    [string]$Config = "",
    [string]$LocalConfig = "",
    [string]$StagingRoot = "",
    [string]$UploadRoot = "",
    [string]$BatchId = "",
    [int]$Limit = 0,
    [switch]$AsJson
)

$pythonScript = Join-Path $PSScriptRoot "upload-central-replay.py"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Config) {
    $Config = Join-Path $repoRoot "config\camera-recorder.json"
}
if (-not $LocalConfig) {
    $LocalConfig = Join-Path $repoRoot "config\camera-recorder.local.json"
}

$resolvedConfig = (Resolve-Path $Config).Path
$resolvedLocalConfig = $LocalConfig
try {
    $resolvedLocalConfig = (Resolve-Path $LocalConfig -ErrorAction Stop).Path
}
catch {
    $resolvedLocalConfig = $LocalConfig
}

$args = @(
    $pythonScript,
    "--config", $resolvedConfig,
    "--local-config", $resolvedLocalConfig
)

if ($StagingRoot) {
    $resolvedStagingRoot = $StagingRoot
    try {
        $resolvedStagingRoot = (Resolve-Path $StagingRoot -ErrorAction Stop).Path
    }
    catch {
        $resolvedStagingRoot = $StagingRoot
    }
    $args += @("--staging-root", $resolvedStagingRoot)
}
if ($UploadRoot) {
    $resolvedUploadRoot = $UploadRoot
    try {
        $resolvedUploadRoot = (Resolve-Path $UploadRoot -ErrorAction Stop).Path
    }
    catch {
        $resolvedUploadRoot = $UploadRoot
    }
    $args += @("--upload-root", $resolvedUploadRoot)
}
if ($BatchId) {
    $args += @("--batch-id", $BatchId)
}
if ($Limit -gt 0) {
    $args += @("--limit", $Limit)
}
if ($AsJson) {
    $args += "--json"
}

& python @args
exit $LASTEXITCODE
