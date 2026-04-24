function Get-CameraPythonCommand {
  param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot
  )

  function Test-PythonCandidate {
    param(
      [string]$ExecutablePath,
      [string[]]$Arguments = @()
    )

    if (-not $ExecutablePath) {
      return $false
    }
    if (-not (Test-Path -LiteralPath $ExecutablePath)) {
      return $false
    }
    try {
      & $ExecutablePath @Arguments --version *> $null
      return ($LASTEXITCODE -eq 0)
    } catch {
      return $false
    }
  }

  $repoPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
  if (Test-PythonCandidate -ExecutablePath $repoPython) {
    return ,([string[]]@($repoPython))
  }

  $searchRoots = @(
    "C:\Program Files",
    (Join-Path $env:LOCALAPPDATA "Programs\Python")
  )
  foreach ($root in $searchRoots) {
    if (-not (Test-Path -LiteralPath $root)) {
      continue
    }
    try {
      $installedPython = Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "Python*" } |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName "python.exe" } |
        Where-Object { Test-PythonCandidate -ExecutablePath $_ } |
        Select-Object -First 1
      if ($installedPython) {
        return ,([string[]]@([string]$installedPython))
      }
    } catch {
    }
  }

  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($python -and (Test-PythonCandidate -ExecutablePath $python.Source)) {
    return ,([string[]]@($python.Source))
  }

  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) {
    if (Test-PythonCandidate -ExecutablePath $py.Source -Arguments @("-3")) {
      return ,([string[]]@($py.Source, "-3"))
    }
  }

  throw "A usable Python interpreter is not available in PATH."
}

function Read-CameraJsonObject {
  param(
    [string]$Path
  )

  if (-not $Path) {
    return @{}
  }
  if (-not (Test-Path -LiteralPath $Path)) {
    return @{}
  }

  $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
  if (-not $raw.Trim()) {
    return @{}
  }

  $payload = $raw | ConvertFrom-Json -ErrorAction Stop
  if ($null -ne $payload -and -not ($payload -is [System.Array])) {
    return $payload
  }
  return @{}
}

function Get-CameraCompatibilityModeFromConfig {
  param(
    [string]$ConfigPath,
    [string]$LocalConfigPath
  )

  $localConfig = Read-CameraJsonObject -Path $LocalConfigPath
  if ($localConfig.workstation -and $localConfig.workstation.compatibility_mode) {
    return [string]$localConfig.workstation.compatibility_mode
  }

  $baseConfig = Read-CameraJsonObject -Path $ConfigPath
  if ($baseConfig.workstation -and $baseConfig.workstation.compatibility_mode) {
    return [string]$baseConfig.workstation.compatibility_mode
  }

  return Get-DetectedCompatibilityMode
}

function Invoke-CameraPython {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$PythonCommand,
    [Parameter(Mandatory = $true)]
    [string]$ScriptPath,
    [string[]]$Arguments = @()
  )

  $pythonExe = $PythonCommand[0]
  $pythonArgs = @()
  if ($PythonCommand.Count -gt 1) {
    $pythonArgs = $PythonCommand[1..($PythonCommand.Count - 1)]
  }

  & $pythonExe @pythonArgs $ScriptPath @Arguments
}

function Get-CameraPackagedToolPath {
  param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,
    [Parameter(Mandatory = $true)]
    [string]$ToolName
  )

  $distRoot = Join-Path $RepoRoot "cameras\dist\legacy-runtime"
  return Join-Path $distRoot ($ToolName + ".exe")
}

function Resolve-CameraToolCommand {
  param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,
    [Parameter(Mandatory = $true)]
    [string]$ToolName,
    [Parameter(Mandatory = $true)]
    [string]$ScriptPath,
    [string]$ConfigPath = "",
    [string]$LocalConfigPath = ""
  )

  $compatibilityMode = Get-CameraCompatibilityModeFromConfig -ConfigPath $ConfigPath -LocalConfigPath $LocalConfigPath
  $compatibilityMode = [string]$compatibilityMode
  if (-not $compatibilityMode.Trim()) {
    $compatibilityMode = Get-DetectedCompatibilityMode
  }
  $compatibilityMode = $compatibilityMode.Trim().ToLowerInvariant()

  $packagedPath = Get-CameraPackagedToolPath -RepoRoot $RepoRoot -ToolName $ToolName
  if (($compatibilityMode -eq "legacy-windows" -or -not (Test-Path -LiteralPath $ScriptPath)) -and (Test-Path -LiteralPath $packagedPath)) {
    return @{
      mode = "packaged"
      tool_name = $ToolName
      compatibility_mode = $compatibilityMode
      file_path = $packagedPath
      prefix_arguments = @()
      script_path = $ScriptPath
    }
  }

  $pythonCommand = Get-CameraPythonCommand -RepoRoot $RepoRoot
  return @{
    mode = "python"
    tool_name = $ToolName
    compatibility_mode = $compatibilityMode
    file_path = $pythonCommand[0]
    prefix_arguments = if ($pythonCommand.Count -gt 1) { @($pythonCommand[1..($pythonCommand.Count - 1)], $ScriptPath) } else { @($ScriptPath) }
    script_path = $ScriptPath
  }
}

function Invoke-CameraTool {
  param(
    [Parameter(Mandatory = $true)]
    [hashtable]$CommandInfo,
    [string[]]$Arguments = @()
  )

  & $CommandInfo.file_path @($CommandInfo.prefix_arguments) @Arguments
}

function Get-CameraOsInfo {
  $version = [Environment]::OSVersion.Version
  $caption = ""
  try {
    $os = Get-WmiObject Win32_OperatingSystem -ErrorAction SilentlyContinue
    if ($os) {
      $caption = [string]$os.Caption
      if ($os.Version) {
        $parsed = $null
        if ([System.Version]::TryParse([string]$os.Version, [ref]$parsed)) {
          $version = $parsed
        }
      }
    }
  } catch {
  }

  return @{
    caption = $caption
    version = $version
    major = [int]$version.Major
    minor = [int]$version.Minor
    build = [int]$version.Build
  }
}

function Get-DetectedCompatibilityMode {
  $osInfo = Get-CameraOsInfo
  if ($osInfo.major -lt 10) {
    return "legacy-windows"
  }
  return "modern"
}

function Get-CameraTaskSchedulerBackend {
  param(
    [string]$CompatibilityMode = ""
  )

  $effectiveMode = [string]$CompatibilityMode
  if (-not $effectiveMode) {
    $effectiveMode = Get-DetectedCompatibilityMode
  }

  if ($effectiveMode -eq "legacy-windows") {
    return "schtasks"
  }

  $cmdlet = Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue
  if ($cmdlet) {
    return "scheduledtasks"
  }

  return "schtasks"
}
