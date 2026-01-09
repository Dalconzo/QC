<#
  cameras/build-exe.ps1
  Build a self-contained Windows executable for save-error-clip using PyInstaller.

  Requirements: Python 3.10+, pip; internet to install pyinstaller if missing.

  Output: cameras\dist\save-error-clip.exe
  Optional: place ffmpeg.exe side-by-side with the exe for reliable trimming.
#>
param(
  [switch]$Clean,
  [switch]$Install
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Ensure-PyInstaller {
  try {
    $v = & pyinstaller --version 2>$null
    if ($LASTEXITCODE -eq 0) { return $true }
  } catch {}
  if (-not $Install) { return $false }
  Write-Host 'Installing pyinstaller ...' -ForegroundColor Yellow
  & python -m pip install --user pyinstaller | Write-Output
  try {
    $v = & pyinstaller --version 2>$null
    return $LASTEXITCODE -eq 0
  } catch { return $false }
}

if (-not (Ensure-PyInstaller)) {
  Write-Error 'PyInstaller is not available. Re-run with -Install to auto-install.'
  exit 1
}

Push-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
try {
  if ($Clean) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist, save-error-clip.spec | Out-Null
  }
  pyinstaller --noconfirm --onefile --name save-error-clip save-error-clip.py
  Write-Host 'Binary built:' -ForegroundColor Green
  Get-Item dist\save-error-clip.exe | Select-Object FullName, Length, LastWriteTime | Format-List
  Write-Host "Tip: place ffmpeg.exe next to the EXE for trimming without PATH." -ForegroundColor Gray
} finally {
  Pop-Location
}

