<#
  cameras/build-camera-runtime.ps1

  Build the packaged camera workstation runtime used by legacy Windows hosts.

  Output layout:
    cameras\dist\legacy-runtime\inspect-camera-config.exe
    cameras\dist\legacy-runtime\inspect-run-manifests.exe
    cameras\dist\legacy-runtime\test-camera-source.exe
    cameras\dist\legacy-runtime\camera-recorder.exe
    cameras\dist\legacy-runtime\camera-daemon.exe

  This is intended to be run on a supported build machine, then copied to the
  offline workstation together with the repo snapshot.
#>

param(
  [switch]$Clean,
  [switch]$Install
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
. (Join-Path $scriptDir "camera-env.ps1")

$pythonCommand = Get-CameraPythonCommand -RepoRoot $repoRoot
$pythonExe = $pythonCommand[0]
$pythonArgs = @()
if ($pythonCommand.Count -gt 1) {
  $pythonArgs = $pythonCommand[1..($pythonCommand.Count - 1)]
}

function Test-PyInstaller {
  try {
    & $pythonExe @pythonArgs -m PyInstaller --version *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

if (-not (Test-PyInstaller)) {
  if (-not $Install) {
    throw "PyInstaller is not available for the selected Python runtime. Re-run with -Install on the build machine."
  }

  Write-Host "Installing PyInstaller into the active camera build runtime ..." -ForegroundColor Cyan
  & $pythonExe @pythonArgs -m pip install pyinstaller
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to install PyInstaller."
  }
}

$distRoot = Join-Path $scriptDir "dist\legacy-runtime"
$buildRoot = Join-Path $scriptDir "build\legacy-runtime"

if ($Clean) {
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $distRoot
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $buildRoot
}

New-Item -ItemType Directory -Force -Path $distRoot | Out-Null
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null

$targets = @(
  @{ Name = "inspect-camera-config"; Script = "inspect-camera-config.py" },
  @{ Name = "inspect-run-manifests"; Script = "inspect-run-manifests.py" },
  @{ Name = "test-camera-source"; Script = "test-camera-source.py" },
  @{ Name = "camera-recorder"; Script = "camera-recorder.py" },
  @{ Name = "camera-daemon"; Script = "camera-daemon.py" }
)

Push-Location $scriptDir
try {
  foreach ($target in $targets) {
    $scriptPath = Join-Path $scriptDir $target.Script
    if (-not (Test-Path -LiteralPath $scriptPath)) {
      throw "Missing runtime source script: $scriptPath"
    }

    Write-Host ("Building {0} ..." -f $target.Name) -ForegroundColor Cyan
    & $pythonExe @pythonArgs -m PyInstaller `
      --noconfirm `
      --clean `
      --onefile `
      --name $target.Name `
      --distpath $distRoot `
      --workpath $buildRoot `
      --specpath $buildRoot `
      --paths $scriptDir `
      $scriptPath

    if ($LASTEXITCODE -ne 0) {
      throw "PyInstaller failed while building $($target.Name)."
    }
  }
} finally {
  Pop-Location
}

Write-Host "Legacy camera runtime built:" -ForegroundColor Green
Get-ChildItem -LiteralPath $distRoot -Filter *.exe | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
