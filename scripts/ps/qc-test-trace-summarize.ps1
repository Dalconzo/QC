<# 
    qc-test-trace-summarize.ps1
    Lightweight regression checks for qc-trace-summarize.ps1.
    Covers machine resolution from trace contents and canonical Logs\<Machine>\ paths.
#>

param(
  [string]$SampleRoot = (Join-Path $PSScriptRoot "..\..\data\samples")
)

$ErrorActionPreference = "Stop"

$summarizer = Join-Path $PSScriptRoot "qc-trace-summarize.ps1"
$sampleRootFull = [System.IO.Path]::GetFullPath($SampleRoot)

if (-not (Test-Path -LiteralPath $summarizer)) {
  throw "Summarizer script not found: $summarizer"
}

if (-not (Test-Path -LiteralPath $sampleRootFull)) {
  throw "Sample root not found: $sampleRootFull"
}

function Invoke-Summarizer([string]$Root) {
  $lines = & $summarizer -SourceRoot $Root -Recurse -AsJson
  if (-not $lines) {
    throw "Summarizer returned no output for $Root"
  }
  return @($lines | ConvertFrom-Json)
}

function Get-SummaryByName($Summaries, [string]$Name) {
  $match = $Summaries | Where-Object { $_.name -eq $Name } | Select-Object -First 1
  if (-not $match) {
    throw "Missing summary for $Name"
  }
  return $match
}

function Assert-Eq([string]$Label, $Actual, $Expected) {
  if ($Actual -ne $Expected) {
    throw "$Label expected '$Expected' but got '$Actual'"
  }
}

$knownSerialSample = "HamiltonStar_Vibrant_TBDS_NZPS_12col_Ver1.4.1_74c6417c93394663a3119e13e4a7f77e_Trace.trc"
$unknownSerialSample = "HamiltonStar_Vibrant_TBDS_NZPS_12col_Ver1.4.1_d2046f0a60364136bc646cd5eb6b0f28_Trace.trc"

$sampleSummaries = Invoke-Summarizer -Root $sampleRootFull
$knownFromTrace = Get-SummaryByName -Summaries $sampleSummaries -Name $knownSerialSample
$unknownFromTrace = Get-SummaryByName -Summaries $sampleSummaries -Name $unknownSerialSample

Assert-Eq -Label "Known serial fallback machine" -Actual $knownFromTrace.machine -Expected "H7"
Assert-Eq -Label "Known serial fallback environment" -Actual $knownFromTrace.environment -Expected "Test"
Assert-Eq -Label "Unknown serial without canonical path" -Actual $unknownFromTrace.machine -Expected ""

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("qc-trace-summarize-test-" + [guid]::NewGuid().ToString("N"))

try {
  $overrideDir = Join-Path $tempRoot "Logs\H13\2025-10-02"
  $fallbackDir = Join-Path $tempRoot "Logs\H14\2025-10-08"
  $null = New-Item -ItemType Directory -Path $overrideDir -Force
  $null = New-Item -ItemType Directory -Path $fallbackDir -Force

  Copy-Item -LiteralPath (Join-Path $sampleRootFull $knownSerialSample) -Destination (Join-Path $overrideDir $knownSerialSample)
  Copy-Item -LiteralPath (Join-Path $sampleRootFull $unknownSerialSample) -Destination (Join-Path $fallbackDir $unknownSerialSample)

  $pathSummaries = Invoke-Summarizer -Root $tempRoot
  $knownFromPath = Get-SummaryByName -Summaries $pathSummaries -Name $knownSerialSample
  $unknownFromPath = Get-SummaryByName -Summaries $pathSummaries -Name $unknownSerialSample

  Assert-Eq -Label "Canonical folder overrides trace serial" -Actual $knownFromPath.machine -Expected "H13"
  Assert-Eq -Label "Canonical folder environment" -Actual $knownFromPath.environment -Expected "Test"
  Assert-Eq -Label "Unknown serial defaults to canonical folder" -Actual $unknownFromPath.machine -Expected "H14"
  Assert-Eq -Label "Unknown serial canonical environment" -Actual $unknownFromPath.environment -Expected "Test"
} finally {
  if (Test-Path -LiteralPath $tempRoot) {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force
  }
}

Write-Host "qc-trace-summarize regression checks passed" -ForegroundColor Green
