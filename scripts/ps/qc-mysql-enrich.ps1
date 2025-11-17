<#
  qc-mysql-enrich.ps1
  Wrapper to run qc-enrich-summaries.py with safe defaults and enforce read-only MySQL access.

  Usage examples:
    ./qc-mysql-enrich.ps1 -InputCsv .\outbox\run-summaries.csv -DsnFile .\mysql_labsite.dsn -Database lab -OutCsv .\outbox\run-summaries-enriched.csv -Limit 200
#>

param(
  [string]$InputCsv = ".\outbox\run-summaries.csv",
  [string]$OutCsv = ".\outbox\run-summaries-enriched.csv",
  [string]$DsnFile = ".\config\mysql_labsite.dsn",
  [string]$Database,
  [string]$TestsDatabase,
  [string]$RuntimeTable,
  [int]$Limit = 200,
  [switch]$EnsureConnector
)

function Ensure-PythonConnector {
  param([switch]$Install)
  $python = Get-Command python -ErrorAction SilentlyContinue
  if (-not $python) {
    Write-Error "Python is not available in PATH. Install Python 3.9+ to run enrichment."
    return $false
  }
  try {
    $ver = & python -c "import mysql.connector, sys; sys.stdout.write(mysql.connector.__version__)" 2>$null
    if ($ver) { return $true }
  } catch {}
  if ($Install) {
    Write-Host "Installing mysql-connector-python ..." -ForegroundColor Yellow
    & python -m pip install --user mysql-connector-python | Write-Output
    try {
      $ver = & python -c "import mysql.connector, sys; sys.stdout.write(mysql.connector.__version__)" 2>$null
      if ($ver) { return $true }
    } catch {}
  }
  return $false
}

if (-not (Test-Path -LiteralPath $InputCsv)) {
  Write-Error "Input CSV not found: $InputCsv"
  exit 1
}

$ok = Ensure-PythonConnector -Install:$EnsureConnector
if (-not $ok) {
  Write-Warning "mysql-connector-python is not available; skipping DB queries. The script will still write output with empty enrichment fields."
}

$argsList = @()
$argsList += "--input-csv"; $argsList += (Resolve-Path -LiteralPath $InputCsv).Path
$argsList += "--out-csv";   $argsList += $OutCsv
$argsList += "--dsn-file";  $argsList += (Resolve-Path -LiteralPath $DsnFile).Path
if ($Database) { $argsList += "--database"; $argsList += $Database }
if ($TestsDatabase) { $argsList += "--tests-database"; $argsList += $TestsDatabase }
if ($RuntimeTable) { $argsList += "--runtime-table"; $argsList += $RuntimeTable }
if ($Limit -gt 0) { $argsList += "--limit"; $argsList += $Limit }

Write-Host "Running enrichment (read-only) ..." -ForegroundColor Cyan
& python .\qc-enrich-summaries.py @argsList
if ($LASTEXITCODE -ne 0) {
  Write-Warning "Enrichment exited with code $LASTEXITCODE. Output may be partial or unenriched."
}
