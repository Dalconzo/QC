<#
  qc-aggregate-summaries.ps1
  Reads JSONL summaries produced by qc-trace-summarize.ps1 (-OutDir) and
  consolidates them into a single CSV plus simple helper metric exports.
#>

param(
  [string]$SummariesDir = ".\\summaries",
  [string]$OutCsv = ".\\outbox\\run-summaries.csv",
  [switch]$WriteHelperMetrics
)

$sumRoot = [System.IO.Path]::GetFullPath($SummariesDir)
if (-not (Test-Path -LiteralPath $sumRoot)) {
  Write-Error "Summaries directory not found: $sumRoot"
  exit 1
}

$outCsvPath = [System.IO.Path]::GetFullPath($OutCsv)
$outDir = [System.IO.Path]::GetDirectoryName($outCsvPath)
if (-not (Test-Path -LiteralPath $outDir)) {
  $null = New-Item -ItemType Directory -Path $outDir -Force
}

$records = New-Object System.Collections.Generic.List[pscustomobject]
$files = Get-ChildItem -Path $sumRoot -Filter 'runs-*.jsonl' -File -ErrorAction SilentlyContinue | Sort-Object FullName
if (-not $files) {
  Write-Host "No JSONL summaries found under $sumRoot" -ForegroundColor Yellow
  exit 0
}

foreach ($file in $files) {
  Write-Host ("Reading " + $file.FullName) -ForegroundColor DarkGray
  foreach ($line in (Get-Content -Path $file.FullName)) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    try {
      $obj = $line | ConvertFrom-Json
      $records.Add([pscustomobject]@{
        run_id        = $obj.run_id
        machine       = $obj.machine
        environment   = $obj.environment
        run_local_date= $obj.run_local_date
        method        = $obj.method
        user          = $obj.user
        status        = $obj.status
        duration_min  = $obj.duration_min
        error_lines   = $obj.error_lines
        warning_lines = $obj.warning_lines
        dialog_count  = $obj.dialog_count
        is_simulation = $obj.is_simulation
        file          = $obj.file
        name          = $obj.name
        size_bytes    = $obj.size_bytes
        checksum      = $obj.checksum
        checksum_valid= $obj.checksum_valid
        start_utc     = $obj.start_utc
        end_utc       = $obj.end_utc
      })
    } catch {
      Write-Warning ("Failed to parse JSON in {0}: {1}" -f $file.FullName, $_)
    }
  }
}

if ($records.Count -eq 0) {
  Write-Host "No records parsed; nothing to write." -ForegroundColor Yellow
  exit 0
}

$records | Export-Csv -Path $outCsvPath -NoTypeInformation -Encoding UTF8
Write-Host ("Wrote consolidated CSV: " + $outCsvPath) -ForegroundColor Green

if ($WriteHelperMetrics) {
  $metricsDir = Join-Path $outDir 'metrics'
  if (-not (Test-Path -LiteralPath $metricsDir)) { $null = New-Item -ItemType Directory -Path $metricsDir -Force }

  # Per-machine/day run counts
  $records |
    Group-Object machine, run_local_date |
    ForEach-Object {
      $groupRows = @($_.Group)
      $first = $groupRows[0]
      [pscustomobject]@{
        machine       = $first.machine
        run_local_date= $first.run_local_date
        run_count     = $_.Count
      }
    } |
    Sort-Object machine, run_local_date |
    Export-Csv -Path (Join-Path $metricsDir 'runs_per_machine_day.csv') -NoTypeInformation -Encoding UTF8

  # Abort rate per machine/day
  $abort = $records | Group-Object machine, run_local_date |
    ForEach-Object {
      $groupRows = @($_.Group)
      $first = $groupRows[0]
      $machine = $first.machine
      $date = $first.run_local_date
      $total = $_.Count
      $aborted = @($groupRows | Where-Object { $_.status -eq 'Aborted' }).Count
      [pscustomobject]@{
        machine        = $machine
        run_local_date = $date
        total_runs     = $total
        aborted_runs   = $aborted
        abort_rate     = if ($total -gt 0) { [math]::Round(($aborted / $total) * 100, 2) } else { 0 }
      }
    } |
    Sort-Object machine, run_local_date
  $abort | Export-Csv -Path (Join-Path $metricsDir 'abort_rate_per_machine_day.csv') -NoTypeInformation -Encoding UTF8

  # Median duration per machine/day
  $duration = $records |
    Where-Object { $_.duration_min -ne $null } |
    Group-Object machine, run_local_date |
    ForEach-Object {
      $groupRows = @($_.Group)
      $first = $groupRows[0]
      $machine = $first.machine
      $date = $first.run_local_date
      $vals = $_.Group | ForEach-Object { [double]$_.duration_min } | Sort-Object
      $n = $vals.Count
      $median = if ($n -eq 0) { $null } elseif ($n % 2 -eq 1) { $vals[[int]([math]::Floor($n/2))] } else { ([double]$vals[$n/2 - 1] + [double]$vals[$n/2]) / 2 }
      [pscustomobject]@{
        machine        = $machine
        run_local_date = $date
        median_duration_min = if ($median -ne $null) { [math]::Round($median,2) } else { $null }
      }
    } |
    Sort-Object machine, run_local_date
  $duration | Export-Csv -Path (Join-Path $metricsDir 'median_duration_per_machine_day.csv') -NoTypeInformation -Encoding UTF8

  Write-Host ("Wrote helper metrics to " + $metricsDir) -ForegroundColor Green
}
