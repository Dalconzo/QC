[CmdletBinding()]
param(
    [string]$RunsRoot = "C:\QC\cameras\video_clips",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 5050
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$appPath = Join-Path $PSScriptRoot "replay-app.py"

Write-Host "Starting Hamilton replay app..."
Write-Host "Runs root: $RunsRoot"
Write-Host "URL: http://$BindHost`:$Port"

python $appPath --runs-root $RunsRoot --host $BindHost --port $Port
