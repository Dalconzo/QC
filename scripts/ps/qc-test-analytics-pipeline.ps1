<#
    qc-test-analytics-pipeline.ps1
    One-command health check for the trace analytics pipeline.

    This wrapper intentionally delegates to the lower-level regression scripts
    instead of reimplementing their assertions. That keeps each stage-specific
    contract in one place while still giving operators a single command they can
    run to answer the practical question: "Is the analytics pipeline healthy?"

    Stages covered:
      1. Trace summarizer regression checks
      2. Aggregation regression checks
      3. DB-optional enrichment regression checks
      4. Guarded live MySQL enrichment smoke check

    The live MySQL check is optional by design. If the DSN is absent, that
    sub-test skips without failing the trace-first pipeline.
#>

param(
  [string]$SampleRoot = (Join-Path $PSScriptRoot "..\..\data\samples"),
  [string]$DsnFile = (Join-Path $PSScriptRoot "..\..\config\mysql_labsite.dsn"),
  [string]$Database = "operation_data",
  [string]$TestsDatabase = "lab_scheduler"
)

$ErrorActionPreference = "Stop"

$traceTest = Join-Path $PSScriptRoot "qc-test-trace-summarize.ps1"
$aggregateTest = Join-Path $PSScriptRoot "qc-test-aggregate-summaries.ps1"
$skipDbTest = Join-Path $PSScriptRoot "qc-test-mysql-enrich.ps1"
$liveDbTest = Join-Path $PSScriptRoot "qc-test-mysql-enrich-live.ps1"

foreach ($scriptPath in @($traceTest, $aggregateTest, $skipDbTest, $liveDbTest)) {
  if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Required pipeline test script not found: $scriptPath"
  }
}

function Invoke-Stage {
  param(
    [string]$Label,
    [string]$ScriptPath,
    [hashtable]$Parameters = @{}
  )

  Write-Host ("[" + $Label + "] starting") -ForegroundColor Cyan

  # Using the call operator keeps the wrapper transparent: each child script
  # controls its own assertions and exit behavior, and any failure bubbles up.
  & $ScriptPath @Parameters

  Write-Host ("[" + $Label + "] passed") -ForegroundColor Green
}

$sampleRootFull = [System.IO.Path]::GetFullPath($SampleRoot)
$dsnPath = [System.IO.Path]::GetFullPath($DsnFile)

Invoke-Stage -Label "summarizer" -ScriptPath $traceTest -Parameters @{ SampleRoot = $sampleRootFull }
Invoke-Stage -Label "aggregate" -ScriptPath $aggregateTest -Parameters @{ SampleRoot = $sampleRootFull }
Invoke-Stage -Label "enrich-skip-db" -ScriptPath $skipDbTest -Parameters @{ SampleRoot = $sampleRootFull }
Invoke-Stage -Label "enrich-live-db" -ScriptPath $liveDbTest -Parameters @{
  SampleRoot = $sampleRootFull
  DsnFile = $dsnPath
  Database = $Database
  TestsDatabase = $TestsDatabase
}

Write-Host "qc analytics pipeline checks passed" -ForegroundColor Green
