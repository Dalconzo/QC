<# 
    qc-test-aggregate-summaries.ps1
    Regression checks for qc-aggregate-summaries.ps1 using sample traces.
#>

param(
  [string]$SampleRoot = (Join-Path $PSScriptRoot "..\..\data\samples")
)

$ErrorActionPreference = "Stop"

$summarizer = Join-Path $PSScriptRoot "qc-trace-summarize.ps1"
$aggregator = Join-Path $PSScriptRoot "qc-aggregate-summaries.ps1"
$sampleRootFull = [System.IO.Path]::GetFullPath($SampleRoot)

if (-not (Test-Path -LiteralPath $summarizer)) {
  throw "Summarizer script not found: $summarizer"
}

if (-not (Test-Path -LiteralPath $aggregator)) {
  throw "Aggregator script not found: $aggregator"
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
  "end_utc"
)

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("qc-aggregate-summaries-test-" + [guid]::NewGuid().ToString("N"))
$summariesDir = Join-Path $tempRoot "summaries"
$outDir = Join-Path $tempRoot "outbox"
$outCsv = Join-Path $outDir "run-summaries.csv"
$metricsDir = Join-Path $outDir "metrics"

try {
  $null = New-Item -ItemType Directory -Path $summariesDir -Force
  $null = New-Item -ItemType Directory -Path $outDir -Force

  $summaryJson = & $summarizer -SourceRoot $sampleRootFull -Recurse -AsJson -OutDir $summariesDir -ByLocalDate
  if (-not $summaryJson) {
    throw "Summarizer returned no output"
  }
  $summaries = @($summaryJson | ConvertFrom-Json)

  & $aggregator -SummariesDir $summariesDir -OutCsv $outCsv -WriteHelperMetrics | Out-Null

  Assert-True -Label "Consolidated CSV missing" -Condition (Test-Path -LiteralPath $outCsv)

  $csvRows = @(Import-Csv -LiteralPath $outCsv)
  Assert-Eq -Label "CSV row count" -Actual $csvRows.Count -Expected $summaries.Count

  $csvColumns = if ($csvRows.Count -gt 0) { Get-PropertyNames -Object $csvRows[0] } else { @() }
  Assert-Eq -Label "CSV column count" -Actual $csvColumns.Count -Expected $expectedColumns.Count
  for ($i = 0; $i -lt $expectedColumns.Count; $i++) {
    Assert-Eq -Label "CSV column[$i]" -Actual $csvColumns[$i] -Expected $expectedColumns[$i]
  }

  foreach ($row in $csvRows) {
    Assert-True -Label "$($row.name) missing run_id" -Condition (-not [string]::IsNullOrWhiteSpace([string]$row.run_id))
    Assert-True -Label "$($row.name) missing start_utc" -Condition (-not [string]::IsNullOrWhiteSpace([string]$row.start_utc))
    Assert-True -Label "$($row.name) missing end_utc" -Condition (-not [string]::IsNullOrWhiteSpace([string]$row.end_utc))
  }

  $expectedRunCountByGroup = @{}
  $expectedAbortByGroup = @{}
  $durationGroups = @{}

  foreach ($summary in $summaries) {
    $key = "{0}|{1}" -f [string]$summary.machine, [string]$summary.run_local_date

    if (-not $expectedRunCountByGroup.ContainsKey($key)) {
      $expectedRunCountByGroup[$key] = 0
      $expectedAbortByGroup[$key] = 0
      $durationGroups[$key] = New-Object System.Collections.Generic.List[double]
    }

    $expectedRunCountByGroup[$key]++
    if ([string]$summary.status -eq "Aborted") {
      $expectedAbortByGroup[$key]++
    }
    if ($null -ne $summary.duration_min -and [string]$summary.duration_min -ne "") {
      $durationGroups[$key].Add([double]$summary.duration_min)
    }
  }

  $runCountsPath = Join-Path $metricsDir "runs_per_machine_day.csv"
  $abortPath = Join-Path $metricsDir "abort_rate_per_machine_day.csv"
  $medianPath = Join-Path $metricsDir "median_duration_per_machine_day.csv"

  Assert-True -Label "runs_per_machine_day.csv missing" -Condition (Test-Path -LiteralPath $runCountsPath)
  Assert-True -Label "abort_rate_per_machine_day.csv missing" -Condition (Test-Path -LiteralPath $abortPath)
  Assert-True -Label "median_duration_per_machine_day.csv missing" -Condition (Test-Path -LiteralPath $medianPath)

  $runCounts = @(Import-Csv -LiteralPath $runCountsPath)
  $abortRows = @(Import-Csv -LiteralPath $abortPath)
  $medianRows = @(Import-Csv -LiteralPath $medianPath)

  Assert-Eq -Label "runs_per_machine_day row count" -Actual $runCounts.Count -Expected $expectedRunCountByGroup.Count
  Assert-Eq -Label "abort_rate row count" -Actual $abortRows.Count -Expected $expectedRunCountByGroup.Count
  Assert-Eq -Label "median_duration row count" -Actual $medianRows.Count -Expected $durationGroups.Count

  $runCountTotal = [int](($runCounts | Measure-Object -Property run_count -Sum).Sum)
  $abortTotalRuns = [int](($abortRows | Measure-Object -Property total_runs -Sum).Sum)
  $abortTotalAborted = [int](($abortRows | Measure-Object -Property aborted_runs -Sum).Sum)
  $expectedAborted = @($summaries | Where-Object { [string]$_.status -eq "Aborted" }).Count

  Assert-Eq -Label "runs_per_machine_day total runs" -Actual $runCountTotal -Expected $summaries.Count
  Assert-Eq -Label "abort_rate total runs" -Actual $abortTotalRuns -Expected $summaries.Count
  Assert-Eq -Label "abort_rate total aborted" -Actual $abortTotalAborted -Expected $expectedAborted

  foreach ($row in $runCounts) {
    $key = "{0}|{1}" -f [string]$row.machine, [string]$row.run_local_date
    Assert-True -Label "Unexpected run count group $key" -Condition $expectedRunCountByGroup.ContainsKey($key)
    Assert-Eq -Label "Run count for $key" -Actual ([int]$row.run_count) -Expected $expectedRunCountByGroup[$key]
  }

  foreach ($row in $abortRows) {
    $key = "{0}|{1}" -f [string]$row.machine, [string]$row.run_local_date
    Assert-True -Label "Unexpected abort group $key" -Condition $expectedRunCountByGroup.ContainsKey($key)
    Assert-Eq -Label "Abort total_runs for $key" -Actual ([int]$row.total_runs) -Expected $expectedRunCountByGroup[$key]
    Assert-Eq -Label "Abort aborted_runs for $key" -Actual ([int]$row.aborted_runs) -Expected $expectedAbortByGroup[$key]
  }

  foreach ($row in $medianRows) {
    $key = "{0}|{1}" -f [string]$row.machine, [string]$row.run_local_date
    Assert-True -Label "Unexpected median group $key" -Condition $durationGroups.ContainsKey($key)

    $vals = @($durationGroups[$key] | Sort-Object)
    $n = $vals.Count
    $expectedMedian = if ($n -eq 0) { $null } elseif ($n % 2 -eq 1) { $vals[[int]([math]::Floor($n / 2))] } else { ([double]$vals[$n / 2 - 1] + [double]$vals[$n / 2]) / 2 }
    $expectedRounded = if ($null -ne $expectedMedian) { [math]::Round($expectedMedian, 2) } else { $null }
    Assert-Eq -Label "Median duration for $key" -Actual ([double]$row.median_duration_min) -Expected $expectedRounded
  }
} finally {
  if (Test-Path -LiteralPath $tempRoot) {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force
  }
}

Write-Host "qc-aggregate-summaries regression checks passed" -ForegroundColor Green
