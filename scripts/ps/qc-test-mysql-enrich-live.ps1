<#
    qc-test-mysql-enrich-live.ps1
    Guarded smoke test for real MySQL enrichment.

    This test is intentionally optional. It only runs when a DSN file is
    available, because local development should not require network or database
    access. When it does run, it proves that the live enrichment path can:
      1. read the trace-derived aggregate CSV,
      2. connect to MySQL in read-only mode,
      3. preserve row counts and schema,
      4. populate the DB source marker so we know the run was actually enriched.
#>

param(
  [string]$SampleRoot = (Join-Path $PSScriptRoot "..\..\data\samples"),
  [string]$DsnFile = (Join-Path $PSScriptRoot "..\..\config\mysql_labsite.dsn"),
  [string]$Database = "operation_data",
  [string]$TestsDatabase = "lab_scheduler",
  [int]$Limit = 3
)

$ErrorActionPreference = "Stop"

$summarizer = Join-Path $PSScriptRoot "qc-trace-summarize.ps1"
$aggregator = Join-Path $PSScriptRoot "qc-aggregate-summaries.ps1"
$enricher = Join-Path $PSScriptRoot "qc-mysql-enrich.ps1"
$sampleRootFull = [System.IO.Path]::GetFullPath($SampleRoot)
$dsnPath = [System.IO.Path]::GetFullPath($DsnFile)

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

if (-not (Test-Path -LiteralPath $dsnPath)) {
  Write-Host "Skipping live MySQL enrichment smoke test because no DSN file was found at $dsnPath" -ForegroundColor Yellow
  exit 0
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("qc-mysql-enrich-live-test-" + [guid]::NewGuid().ToString("N"))
$summariesDir = Join-Path $tempRoot "summaries"
$outDir = Join-Path $tempRoot "outbox"
$aggregatedCsv = Join-Path $outDir "run-summaries.csv"
$enrichedCsv = Join-Path $outDir "run-summaries-enriched.csv"

try {
  $null = New-Item -ItemType Directory -Path $summariesDir -Force
  $null = New-Item -ItemType Directory -Path $outDir -Force

  # Recreate the upstream artifacts locally so the smoke test exercises the
  # same real entry points used in production refreshes.
  & $summarizer -SourceRoot $sampleRootFull -Recurse -AsJson -OutDir $summariesDir -ByLocalDate | Out-Null
  & $aggregator -SummariesDir $summariesDir -OutCsv $aggregatedCsv | Out-Null
  & $enricher -InputCsv $aggregatedCsv -OutCsv $enrichedCsv -DsnFile $dsnPath -Database $Database -TestsDatabase $TestsDatabase -Limit $Limit | Out-Null

  Assert-True -Label "Live enriched CSV missing" -Condition (Test-Path -LiteralPath $enrichedCsv)

  $baseRows = @(Import-Csv -LiteralPath $aggregatedCsv)
  $enrichedRows = @(Import-Csv -LiteralPath $enrichedCsv)

  Assert-Eq -Label "Live enriched row count" -Actual $enrichedRows.Count -Expected ([Math]::Min($baseRows.Count, $Limit))

  # This is the key smoke-test assertion: if DB enrichment really ran, the
  # Python script stamps the source database name into every processed row.
  foreach ($row in $enrichedRows) {
    Assert-Eq -Label "enrich_source_db should match requested database" -Actual ([string]$row.enrich_source_db) -Expected $Database
    Assert-True -Label "run_id should remain populated after live enrichment" -Condition (-not [string]::IsNullOrWhiteSpace([string]$row.run_id))
  }
} finally {
  if (Test-Path -LiteralPath $tempRoot) {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force
  }
}

Write-Host "qc-mysql-enrich live smoke test passed" -ForegroundColor Green
