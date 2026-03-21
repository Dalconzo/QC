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
  [switch]$EnsureConnector,
  [switch]$SkipDb
)

function Ensure-PythonConnector {
  param([switch]$Install)

  # The wrapper owns dependency checks so the Python script can stay focused on
  # data enrichment. If the connector is missing, we downgrade to skip-db mode
  # instead of failing the trace-first pipeline.
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

# Keep DB access optional. Missing connector support should not block the
# enrichment step from producing a schema-complete CSV for downstream tools.
$ok = Ensure-PythonConnector -Install:$EnsureConnector
if (-not $ok) {
  Write-Warning "mysql-connector-python is not available; skipping DB queries. The script will still write output with empty enrichment fields."
  $SkipDb = $true
}

$scriptPath = Join-Path $PSScriptRoot "..\py\qc-enrich-summaries.py"
$scriptPath = [System.IO.Path]::GetFullPath($scriptPath)
if (-not (Test-Path -LiteralPath $scriptPath)) {
  Write-Error "Python enrichment script not found: $scriptPath"
  exit 1
}

# Build the Python argument list explicitly so the wrapper can enforce the
# contract around absolute paths and skip-db fallbacks.
$argsList = @()
$argsList += "--input-csv"; $argsList += (Resolve-Path -LiteralPath $InputCsv).Path
$argsList += "--out-csv";   $argsList += ([System.IO.Path]::GetFullPath($OutCsv))
if ($SkipDb) {
  $argsList += "--skip-db"
} elseif (Test-Path -LiteralPath $DsnFile) {
  $argsList += "--dsn-file";  $argsList += (Resolve-Path -LiteralPath $DsnFile).Path
} else {
  Write-Warning "DSN file not found: $DsnFile. Proceeding without DB lookups."
  $argsList += "--skip-db"
}
if ($Database -and -not $SkipDb) { $argsList += "--database"; $argsList += $Database }
if ($TestsDatabase -and -not $SkipDb) { $argsList += "--tests-database"; $argsList += $TestsDatabase }
if ($RuntimeTable) { $argsList += "--runtime-table"; $argsList += $RuntimeTable }
if ($Limit -gt 0) { $argsList += "--limit"; $argsList += $Limit }

Write-Host "Running enrichment ..." -ForegroundColor Cyan
& python $scriptPath @argsList
if ($LASTEXITCODE -ne 0) {
  Write-Warning "Enrichment exited with code $LASTEXITCODE. Output may be partial or unenriched."
}
