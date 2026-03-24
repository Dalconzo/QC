<#
  cameras/test-start-recorder-gate.ps1

  Lightweight smoke test for the Hamilton recorder startup gate. This verifies
  that the PowerShell wrapper defaults to the real Run Manager process name
  (`HxRun.exe`) and that the Python recorder exits cleanly before touching the
  camera backend when an explicit startup gate is not satisfied.

  This test is intentionally hardware-free. It checks the wrapper source for
  the `HxRun.exe` default, then uses a fake process name and a short startup
  timeout, treating recorder exit code 3 as the expected success condition for
  the timeout path.
#>

$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "start-recorder.ps1"
if (-not (Test-Path -LiteralPath $scriptPath)) {
  throw "Recorder wrapper not found: $scriptPath"
}

$scriptText = Get-Content -LiteralPath $scriptPath -Raw
if ($scriptText -notmatch '\[string\]\$StartWhenExe\s*=\s*"HxRun\.exe"') {
  throw "Recorder wrapper does not default StartWhenExe to HxRun.exe"
}

$outDir = "C:\QC\tmp\camera-gate-default-test"
if (Test-Path -LiteralPath $outDir) {
  Remove-Item -LiteralPath $outDir -Recurse -Force
}

# Use an explicit fake process for the runtime check. The wrapper default is
# asserted above from source, and the fake process keeps this smoke test stable
# even on a workstation where HxRun.exe happens to be running.
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
