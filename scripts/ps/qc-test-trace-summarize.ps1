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

function Assert-True([string]$Label, [bool]$Condition) {
  if (-not $Condition) {
    throw "$Label failed"
  }
}

function Test-IsoTimestamp([string]$Value) {
  if ([string]::IsNullOrWhiteSpace($Value)) {
    return $false
  }
  $parsed = [datetimeoffset]::MinValue
  return [datetimeoffset]::TryParse($Value, [ref]$parsed)
}

function Assert-SummaryContract($Summary) {
  $requiredStrings = @(
    "run_id",
    "file",
    "name",
    "relative_path",
    "environment",
    "run_local_date",
    "start_utc",
    "start_local",
    "end_utc",
    "end_local",
    "user",
    "method",
    "status"
  )

  foreach ($propertyName in $requiredStrings) {
    $value = [string]$Summary.$propertyName
    Assert-True -Label "$($Summary.name) missing $propertyName" -Condition (-not [string]::IsNullOrWhiteSpace($value))
  }

  Assert-True -Label "$($Summary.name) invalid environment" -Condition (@("Test", "Production") -contains [string]$Summary.environment)
  Assert-True -Label "$($Summary.name) invalid status" -Condition (@("Completed", "Aborted") -contains [string]$Summary.status)
  Assert-True -Label "$($Summary.name) invalid start_utc" -Condition (Test-IsoTimestamp -Value ([string]$Summary.start_utc))
  Assert-True -Label "$($Summary.name) invalid start_local" -Condition (Test-IsoTimestamp -Value ([string]$Summary.start_local))
  Assert-True -Label "$($Summary.name) invalid end_utc" -Condition (Test-IsoTimestamp -Value ([string]$Summary.end_utc))
  Assert-True -Label "$($Summary.name) invalid end_local" -Condition (Test-IsoTimestamp -Value ([string]$Summary.end_local))
  Assert-True -Label "$($Summary.name) invalid run_local_date" -Condition ([string]$Summary.run_local_date -match '^\d{4}-\d{2}-\d{2}$')
  Assert-True -Label "$($Summary.name) negative duration" -Condition ([double]$Summary.duration_min -ge 0)
  Assert-True -Label "$($Summary.name) negative error_lines" -Condition ([int]$Summary.error_lines -ge 0)
  Assert-True -Label "$($Summary.name) negative warning_lines" -Condition ([int]$Summary.warning_lines -ge 0)
  Assert-True -Label "$($Summary.name) negative dialog_count" -Condition ([int]$Summary.dialog_count -ge 0)

  $startUtc = [datetimeoffset]::Parse([string]$Summary.start_utc)
  $endUtc = [datetimeoffset]::Parse([string]$Summary.end_utc)
  Assert-True -Label "$($Summary.name) end before start" -Condition ($endUtc -ge $startUtc)

  if ([string]::IsNullOrWhiteSpace([string]$Summary.machine)) {
    Assert-Eq -Label "$($Summary.name) run_id without machine" -Actual ([string]$Summary.run_id) -Expected ([string]$Summary.name)
  } else {
    Assert-True -Label "$($Summary.name) invalid machine format" -Condition ([string]$Summary.machine -match '^H\d+$')
    Assert-Eq -Label "$($Summary.name) run_id with machine" -Actual ([string]$Summary.run_id) -Expected ("$($Summary.machine):$($Summary.name)")
    $expectedEnvironment = if (@("H14", "H13", "H7") -contains [string]$Summary.machine) { "Test" } else { "Production" }
    Assert-Eq -Label "$($Summary.name) machine/environment mismatch" -Actual ([string]$Summary.environment) -Expected $expectedEnvironment
  }
}

$knownSerialSample = "HamiltonStar_Vibrant_TBDS_NZPS_12col_Ver1.4.1_74c6417c93394663a3119e13e4a7f77e_Trace.trc"
$unknownSerialSample = "HamiltonStar_Vibrant_TBDS_NZPS_12col_Ver1.4.1_d2046f0a60364136bc646cd5eb6b0f28_Trace.trc"

$sampleSummaries = Invoke-Summarizer -Root $sampleRootFull
foreach ($summary in $sampleSummaries) {
  Assert-SummaryContract -Summary $summary
}
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
  foreach ($summary in $pathSummaries) {
    Assert-SummaryContract -Summary $summary
  }
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
