<#
    qc-test-mysql-enrich.ps1
    Regression checks for the DB-optional enrichment path.

    This test intentionally uses -SkipDb so it can run on any workstation
    without MySQL access. The goal is to prove that enrichment preserves the
    aggregated rows and produces a stable, schema-complete CSV even when the
    database is unavailable or intentionally bypassed.
#>

param(
  [string]$SampleRoot = (Join-Path $PSScriptRoot "..\..\data\samples")
)

$ErrorActionPreference = "Stop"

$summarizer = Join-Path $PSScriptRoot "qc-trace-summarize.ps1"
$aggregator = Join-Path $PSScriptRoot "qc-aggregate-summaries.ps1"
$enricher = Join-Path $PSScriptRoot "qc-mysql-enrich.ps1"
$sampleRootFull = [System.IO.Path]::GetFullPath($SampleRoot)

if (-not (Test-Path -LiteralPath $summarizer)) {
  throw "Summarizer script not found: $summarizer"
}

if (-not (Test-Path -LiteralPath $aggregator)) {
  throw "Aggregator script not found: $aggregator"
}

if (-not (Test-Path -LiteralPath $enricher)) {
  throw "Enrichment wrapper not found: $enricher"
}

if (-not (Test-Path -LiteralPath $sampleRootFull)) {
  throw "Sample root not found: $sampleRootFull"
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

function Get-PropertyNames($Object) {
  return @($Object.PSObject.Properties | ForEach-Object { $_.Name })
}

$expectedColumns = @(
  "run_id",
  "machine",
  "environment",
  "run_local_date",
  "method",
  "user",
  "status",
  "duration_min",
  "error_lines",
  "warning_lines",
  "dialog_count",
  "is_simulation",
  "file",
  "name",
  "size_bytes",
  "checksum",
  "checksum_valid",
  "start_utc",
  "end_utc",
  "instrument_status_start",
  "occupied_during_run",
  "assay",
  "assay_guess",
  "plate_id",
  "enrich_source_db"
)

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("qc-mysql-enrich-test-" + [guid]::NewGuid().ToString("N"))
$summariesDir = Join-Path $tempRoot "summaries"
$outDir = Join-Path $tempRoot "outbox"
$aggregatedCsv = Join-Path $outDir "run-summaries.csv"
$enrichedCsv = Join-Path $outDir "run-summaries-enriched.csv"

try {
  $null = New-Item -ItemType Directory -Path $summariesDir -Force
  $null = New-Item -ItemType Directory -Path $outDir -Force

  # Build realistic upstream inputs so the enrichment test covers the same CSV
  # shape produced by the normal trace-first pipeline.
  & $summarizer -SourceRoot $sampleRootFull -Recurse -AsJson -OutDir $summariesDir -ByLocalDate | Out-Null
  & $aggregator -SummariesDir $summariesDir -OutCsv $aggregatedCsv | Out-Null
  & $enricher -InputCsv $aggregatedCsv -OutCsv $enrichedCsv -SkipDb | Out-Null

  Assert-True -Label "Aggregated CSV missing" -Condition (Test-Path -LiteralPath $aggregatedCsv)
  Assert-True -Label "Enriched CSV missing" -Condition (Test-Path -LiteralPath $enrichedCsv)

  $baseRows = @(Import-Csv -LiteralPath $aggregatedCsv)
  $enrichedRows = @(Import-Csv -LiteralPath $enrichedCsv)

  Assert-Eq -Label "Enriched row count" -Actual $enrichedRows.Count -Expected $baseRows.Count

  $actualColumns = if ($enrichedRows.Count -gt 0) { Get-PropertyNames -Object $enrichedRows[0] } else { @() }
  Assert-Eq -Label "Enriched CSV column count" -Actual $actualColumns.Count -Expected $expectedColumns.Count
  for ($i = 0; $i -lt $expectedColumns.Count; $i++) {
    Assert-Eq -Label "Enriched CSV column[$i]" -Actual $actualColumns[$i] -Expected $expectedColumns[$i]
  }

  for ($i = 0; $i -lt $baseRows.Count; $i++) {
    $baseRow = $baseRows[$i]
    $enrichedRow = $enrichedRows[$i]

    # Skip-db mode must preserve the aggregated record as-is and only append the
    # enrichment contract columns after it.
    foreach ($column in @(
      "run_id","machine","environment","run_local_date","method","user","status",
      "duration_min","error_lines","warning_lines","dialog_count","is_simulation",
      "file","name","size_bytes","checksum","checksum_valid","start_utc","end_utc"
    )) {
      Assert-Eq -Label "Base column $column for row $i" -Actual ([string]$enrichedRow.$column) -Expected ([string]$baseRow.$column)
    }

    Assert-True -Label "occupied_during_run should be 0 or blank for row $i" -Condition ([string]$enrichedRow.occupied_during_run -in @("", "0"))
    Assert-Eq -Label "instrument_status_start blank for row $i" -Actual ([string]$enrichedRow.instrument_status_start) -Expected ""
    Assert-Eq -Label "assay blank for row $i" -Actual ([string]$enrichedRow.assay) -Expected ""
    Assert-Eq -Label "assay_guess blank for row $i" -Actual ([string]$enrichedRow.assay_guess) -Expected ""
    Assert-Eq -Label "plate_id blank for row $i" -Actual ([string]$enrichedRow.plate_id) -Expected ""
    Assert-Eq -Label "enrich_source_db blank for row $i" -Actual ([string]$enrichedRow.enrich_source_db) -Expected ""
  }
} finally {
  if (Test-Path -LiteralPath $tempRoot) {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force
  }
}

Write-Host "qc-mysql-enrich regression checks passed" -ForegroundColor Green
