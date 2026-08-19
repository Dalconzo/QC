<#
  cameras/test-start-recorder-gate.ps1

  Lightweight smoke test for the Hamilton recorder startup gate.

  The gate default now lives in shared config rather than hardcoded wrapper
  parameters, so this test verifies the effective config first and then checks
  that the recorder exits cleanly before touching camera hardware when an
  explicit startup gate never appears.
#>

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "camera-env.ps1")

$pythonCommand = Get-CameraPythonCommand -RepoRoot $repoRoot
$scriptPath = Join-Path $PSScriptRoot "start-recorder.ps1"
$inspectPath = Join-Path $PSScriptRoot "inspect-camera-config.py"
if (-not (Test-Path -LiteralPath $scriptPath) -or -not (Test-Path -LiteralPath $inspectPath)) {
  throw "Required camera scripts are missing."
}

try {
  $configJson = Invoke-CameraPython `
    -PythonCommand $pythonCommand `
    -ScriptPath $inspectPath `
    -Arguments @("--config", (Join-Path $repoRoot "config\camera-recorder.json"), "--json")
} catch {
  throw "Failed to inspect camera config: $($_.Exception.Message)"
}
if ($LASTEXITCODE -ne 0) {
  throw "Failed to inspect camera config."
}

$config = $configJson | ConvertFrom-Json
if ($config.config.hamilton.process_name -ne "HxRun.exe") {
  throw "Effective config does not default the Hamilton process gate to HxRun.exe"
}

$outDir = "C:\QC\tmp\camera-gate-default-test"
if (Test-Path -LiteralPath $outDir) {
  Remove-Item -LiteralPath $outDir -Recurse -Force
}

# Use an explicit fake process for the runtime check. The shared config default
# is asserted above, and the fake process keeps this smoke test stable even on
# a workstation where HxRun.exe happens to be running.
& powershell -NoProfile -File $scriptPath `
  -Source 0 `
  -OutDir $outDir `
  -Label gate-default `
  -StartWhenExe definitely_missing_process.exe `
  -StartupTimeoutSec 1 `
  -PollSec 0.25 `
  -VerboseRecorder

$exitCode = $LASTEXITCODE
if ($exitCode -ne 3) {
  throw "Expected recorder to exit with code 3 when the startup gate is unsatisfied, got $exitCode"
}

Write-Host "Recorder startup gate smoke test passed." -ForegroundColor Green
