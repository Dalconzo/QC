<#
  Foreground Scheduled Task entry point for the central replay host.
  Task Scheduler owns this process and can restart it after failures.
#>

[CmdletBinding()]
param(
  [string]$RepoRoot = "",
  [string]$ServerConfig = "",
  [string]$ServerLocalConfig = "",
  [string]$CameraConfig = "",
  [string]$CameraLocalConfig = "",
  [string]$ServerLog = "",
  [string]$PythonExecutable = ""
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RepoRoot) {
  $RepoRoot = Split-Path -Parent $scriptDir
}
$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)

. (Join-Path $scriptDir "central-replay-task-common.ps1")
. (Join-Path $scriptDir "camera-env.ps1")

$paths = Resolve-CentralReplayTaskPaths @PSBoundParameters
Assert-CentralReplayTaskFiles -Paths $paths

$logDirectory = Split-Path -Parent $paths.ServerLog
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

$serverScript = Join-Path $RepoRoot "cameras\central-replay-server.py"
if (-not (Test-Path -LiteralPath $serverScript -PathType Leaf)) {
  throw "Central replay server script not found: $serverScript"
}

$pythonCommand = if ($paths.PythonExecutable) {
  [string[]]@($paths.PythonExecutable)
} else {
  Get-CameraPythonCommand -RepoRoot $RepoRoot
}
$arguments = @(
  "--server-config", $paths.ServerConfig,
  "--server-local-config", $paths.ServerLocalConfig,
  "--config", $paths.CameraConfig,
  "--local-config", $paths.CameraLocalConfig,
  "--log-path", $paths.ServerLog
)

Invoke-CameraPython -PythonCommand $pythonCommand -ScriptPath $serverScript -Arguments $arguments
exit $LASTEXITCODE
