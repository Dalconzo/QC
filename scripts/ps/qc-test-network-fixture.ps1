<#
    qc-test-network-fixture.ps1
    Validate the analytics pipeline against a bounded fixture built from the
    real network log archive.

    This test is intentionally broader than the tiny checked-in samples. It
    verifies that the full summarize -> aggregate -> enrich flow still works on
    representative production/test traces copied from the live share while
    remaining small enough to run quickly on a workstation.
#>

param(
  [string]$FixtureRoot = (Join-Path $PSScriptRoot "..\..\tmp\network-verification-fixture"),
  [string]$DsnFile = (Join-Path $PSScriptRoot "..\..\config\mysql_labsite.dsn"),
  [string]$Database = "operation_data",
  [string]$TestsDatabase = "lab_scheduler"
)

$ErrorActionPreference = "Stop"

$summarizer = Join-Path $PSScriptRoot "qc-trace-summarize.ps1"
$aggregator = Join-Path $PSScriptRoot "qc-aggregate-summaries.ps1"
$enricher = Join-Path $PSScriptRoot "qc-mysql-enrich.ps1"
$fixtureRootFull = [System.IO.Path]::GetFullPath($FixtureRoot)
$logsRoot = Join-Path $fixtureRootFull "Logs"
$manifestPath = Join-Path $fixtureRootFull "fixture-manifest.json"

function Assert-Eq {
  param(
    [string]$Label,
    $Actual,
    $Expected
  )

  if ($Actual -ne $Expected) {
    throw "$Label expected '$Expected' but got '$Actual'"
  }
}

function Assert-True {
  param(
    [string]$Label,
    [bool]$Condition
  )

  if (-not $Condition) {
    throw "$Label failed"
  }
}

Assert-True -Label "Fixture root missing" -Condition (Test-Path -LiteralPath $fixtureRootFull)
Assert-True -Label "Fixture Logs root missing" -Condition (Test-Path -LiteralPath $logsRoot)
Assert-True -Label "Fixture manifest missing" -Condition (Test-Path -LiteralPath $manifestPath)

$manifestRows = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifestRows -isnot [System.Array]) {
  $manifestRows = @($manifestRows)
}
Assert-True -Label "Fixture manifest empty" -Condition ($manifestRows.Count -gt 0)

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("qc-network-fixture-test-" + [guid]::NewGuid().ToString("N"))
$summariesDir = Join-Path $tempRoot "summaries"
$outDir = Join-Path $tempRoot "outbox"
$aggregatedCsv = Join-Path $outDir "run-summaries.csv"
$skipDbCsv = Join-Path $outDir "run-summaries-enriched.csv"
$liveDbCsv = Join-Path $outDir "run-summaries-enriched-live.csv"

try {
  $null = New-Item -ItemType Directory -Path $summariesDir -Force
  $null = New-Item -ItemType Directory -Path $outDir -Force

  # The fixture already contains canonical Logs\<Machine>\<Date> paths, so the
  # normal pipeline should be able to consume it without any special casing.
  & $summarizer -SourceRoot $logsRoot -Filter '*_Trace.trc' -Recurse -AsJson -OutDir $summariesDir -ByLocalDate | Out-Null
  $summaryFiles = @(Get-ChildItem -LiteralPath $summariesDir -Filter 'runs-*.jsonl' -File -ErrorAction SilentlyContinue)
  Assert-True -Label "Summaries JSONL files missing" -Condition ($summaryFiles.Count -gt 0)

  $summaries = @(
    $summaryFiles |
      Sort-Object Name |
      ForEach-Object { Get-Content -LiteralPath $_.FullName } |
      Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
      ConvertFrom-Json
  )

  Assert-Eq -Label "Summary row count" -Actual $summaries.Count -Expected $manifestRows.Count

  foreach ($summary in $summaries) {
    $manifest = $manifestRows | Where-Object { $_.relative_path -eq $summary.relative_path } | Select-Object -First 1
    Assert-True -Label "Missing manifest row for $($summary.relative_path)" -Condition ($null -ne $manifest)
    Assert-Eq -Label "Machine from path for $($summary.name)" -Actual ([string]$summary.machine) -Expected ([string]$manifest.machine)
    Assert-True -Label "Missing method for $($summary.name)" -Condition (-not [string]::IsNullOrWhiteSpace([string]$summary.method))
    Assert-True -Label "Missing user for $($summary.name)" -Condition (-not [string]::IsNullOrWhiteSpace([string]$summary.user))
  }

  $machines = @($summaries | Select-Object -ExpandProperty machine -Unique)
  $environments = @($summaries | Select-Object -ExpandProperty environment -Unique)
  $statuses = @($summaries | Select-Object -ExpandProperty status -Unique)

  Assert-True -Label "Fixture should cover multiple machines" -Condition ($machines.Count -ge 4)
  Assert-True -Label "Fixture should include Test runs" -Condition ($environments -contains "Test")
  Assert-True -Label "Fixture should include Production runs" -Condition ($environments -contains "Production")
  Assert-True -Label "Fixture should include an Aborted run" -Condition ($statuses -contains "Aborted")
  Assert-True -Label "Fixture should include a Completed run" -Condition ($statuses -contains "Completed")

  & $aggregator -SummariesDir $summariesDir -OutCsv $aggregatedCsv -WriteHelperMetrics | Out-Null
  Assert-True -Label "Aggregated CSV missing" -Condition (Test-Path -LiteralPath $aggregatedCsv)
  $aggregatedRows = @(Import-Csv -LiteralPath $aggregatedCsv)
  Assert-Eq -Label "Aggregated row count" -Actual $aggregatedRows.Count -Expected $summaries.Count

  & $enricher -InputCsv $aggregatedCsv -OutCsv $skipDbCsv -SkipDb | Out-Null
  Assert-True -Label "Skip-db enriched CSV missing" -Condition (Test-Path -LiteralPath $skipDbCsv)
  $skipDbRows = @(Import-Csv -LiteralPath $skipDbCsv)
  Assert-Eq -Label "Skip-db row count" -Actual $skipDbRows.Count -Expected $aggregatedRows.Count

  if (Test-Path -LiteralPath $DsnFile) {
    & $enricher -InputCsv $aggregatedCsv -OutCsv $liveDbCsv -DsnFile $DsnFile -Database $Database -TestsDatabase $TestsDatabase | Out-Null
    Assert-True -Label "Live-db enriched CSV missing" -Condition (Test-Path -LiteralPath $liveDbCsv)

    $liveDbRows = @(Import-Csv -LiteralPath $liveDbCsv)
    Assert-Eq -Label "Live-db row count" -Actual $liveDbRows.Count -Expected $aggregatedRows.Count

    foreach ($row in $liveDbRows) {
      Assert-Eq -Label "Live enrich_source_db for $($row.run_id)" -Actual ([string]$row.enrich_source_db) -Expected $Database
    }
  } else {
    Write-Warning "Skipping live-db verification because DSN file is missing: $DsnFile"
  }
} finally {
  if (Test-Path -LiteralPath $tempRoot) {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force
  }
}

Write-Host "qc network fixture pipeline checks passed" -ForegroundColor Green
