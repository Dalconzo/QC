Set-StrictMode -Version 2.0

function Resolve-CentralReplayTaskPaths {
  param(
    [string]$RepoRoot,
    [string]$ServerConfig,
    [string]$ServerLocalConfig,
    [string]$CameraConfig,
    [string]$CameraLocalConfig,
    [string]$ServerLog,
    [string]$PythonExecutable
  )

  if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
  }
  $RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)

  $defaults = @{
    RepoRoot = $RepoRoot
    ServerConfig = Join-Path $RepoRoot "config\central-replay-server.json"
    ServerLocalConfig = Join-Path $RepoRoot "config\central-replay-server.local.json"
    CameraConfig = Join-Path $RepoRoot "config\camera-recorder.json"
    CameraLocalConfig = Join-Path $RepoRoot "config\camera-recorder.local.json"
    ServerLog = Join-Path $RepoRoot "logs\central-replay-server.log"
    Runner = Join-Path $RepoRoot "cameras\run-central-replay-server-task.ps1"
    PythonExecutable = ""
  }

  foreach ($name in @("ServerConfig", "ServerLocalConfig", "CameraConfig", "CameraLocalConfig", "ServerLog")) {
    $value = Get-Variable -Name $name -ValueOnly
    if ($value) {
      $defaults[$name] = [System.IO.Path]::GetFullPath($value)
    }
  }
  if ($PythonExecutable) {
    $defaults.PythonExecutable = [System.IO.Path]::GetFullPath($PythonExecutable)
  }
  return $defaults
}

function ConvertTo-CentralReplayTaskArgument {
  param([Parameter(Mandatory = $true)][string]$Value)
  return '"{0}"' -f ($Value -replace '"', '\"')
}

function New-CentralReplayTaskActionArguments {
  param([Parameter(Mandatory = $true)][hashtable]$Paths)

  $arguments = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-WindowStyle", "Hidden",
    "-File", (ConvertTo-CentralReplayTaskArgument $Paths.Runner),
    "-RepoRoot", (ConvertTo-CentralReplayTaskArgument $Paths.RepoRoot),
    "-ServerConfig", (ConvertTo-CentralReplayTaskArgument $Paths.ServerConfig),
    "-ServerLocalConfig", (ConvertTo-CentralReplayTaskArgument $Paths.ServerLocalConfig),
    "-CameraConfig", (ConvertTo-CentralReplayTaskArgument $Paths.CameraConfig),
    "-CameraLocalConfig", (ConvertTo-CentralReplayTaskArgument $Paths.CameraLocalConfig),
    "-ServerLog", (ConvertTo-CentralReplayTaskArgument $Paths.ServerLog)
  )
  if ($Paths.PythonExecutable) {
    $arguments += @("-PythonExecutable", (ConvertTo-CentralReplayTaskArgument $Paths.PythonExecutable))
  }
  return $arguments -join " "
}

function Assert-CentralReplayTaskFiles {
  param([Parameter(Mandatory = $true)][hashtable]$Paths)

  foreach ($name in @("ServerConfig", "CameraConfig", "Runner")) {
    if (-not (Test-Path -LiteralPath $Paths[$name] -PathType Leaf)) {
      throw "Required central replay file is missing: $($Paths[$name])"
    }
  }
  if ($Paths.PythonExecutable -and -not (Test-Path -LiteralPath $Paths.PythonExecutable -PathType Leaf)) {
    throw "Configured Python executable is missing: $($Paths.PythonExecutable)"
  }

  $logDirectory = Split-Path -Parent $Paths.ServerLog
  if (-not $logDirectory) {
    throw "ServerLog must include a parent directory."
  }
}
