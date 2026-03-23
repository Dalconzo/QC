<#
    qc-test-raw-trace-ingest-live.ps1
    Bounded live smoke test for the compressed raw trace store.

    This check uses real network-derived data, but keeps the workload small:
    - run traces come from the existing bounded network verification fixture
    - usbcomm traces come directly from the share with a per-machine cap

    The goal is to prove that both storage lanes work end to end in one SQLite
    database without attempting a full-share ingest.
#>

param(
  [string]$FixtureRoot = (Join-Path $PSScriptRoot "..\..\tmp\network-verification-fixture"),
  [string]$UsbCommSourceRoot = "\\192.168.10.99\home\Logs",
  [int]$UsbCommFilesPerMachine = 1
)

$ErrorActionPreference = "Stop"

function Assert-True([string]$Label, [bool]$Condition) {
  if (-not $Condition) {
    throw $Label
  }
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$fixtureRootFull = [System.IO.Path]::GetFullPath($FixtureRoot)
$fixtureLogsRoot = Join-Path $fixtureRootFull "Logs"
$tmpRoot = Join-Path $repoRoot "tmp\raw-trace-live-smoke"
$databasePath = Join-Path $tmpRoot "raw-traces.sqlite"
$runManifest = Join-Path $tmpRoot "run-trace-manifest.csv"
$usbManifest = Join-Path $tmpRoot "usbcomm-manifest.csv"
$ingestScript = Join-Path $PSScriptRoot "qc-ingest-raw-traces.ps1"

Assert-True -Label "Network fixture root missing: $fixtureRootFull" -Condition (Test-Path -LiteralPath $fixtureRootFull)
Assert-True -Label "Network fixture Logs root missing: $fixtureLogsRoot" -Condition (Test-Path -LiteralPath $fixtureLogsRoot)
Assert-True -Label "UsbComm source root missing: $UsbCommSourceRoot" -Condition (Test-Path -LiteralPath $UsbCommSourceRoot)

if (Test-Path -LiteralPath $tmpRoot) {
  Remove-Item -LiteralPath $tmpRoot -Recurse -Force
}
$null = New-Item -ItemType Directory -Path $tmpRoot -Force

# First ingest the bounded real run traces from the local fixture.
& powershell -NoProfile -File $ingestScript `
  -SourceRoot $fixtureLogsRoot `
  -DatabasePath $databasePath `
  -Stream RunTrace `
  -OutManifestCsv $runManifest `
  -Recurse

# Then add a bounded sample of real usbcomm logs from the share so the same
# database proves both lanes can coexist cleanly.
& powershell -NoProfile -File $ingestScript `
  -SourceRoot $UsbCommSourceRoot `
  -DatabasePath $databasePath `
  -Stream UsbComm `
  -OutManifestCsv $usbManifest `
  -Recurse `
  -MaxFilesPerMachine $UsbCommFilesPerMachine

Assert-True -Label "Raw trace live smoke database missing" -Condition (Test-Path -LiteralPath $databasePath)
Assert-True -Label "Run-trace manifest missing" -Condition (Test-Path -LiteralPath $runManifest)
Assert-True -Label "UsbComm manifest missing" -Condition (Test-Path -LiteralPath $usbManifest)

$pythonCheck = @'
import csv
import sqlite3
import sys

db_path, run_manifest_path, usb_manifest_path = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(db_path)

trace_count = con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
source_count = con.execute("SELECT COUNT(*) FROM trace_sources").fetchone()[0]
batch_count = con.execute("SELECT COUNT(*) FROM ingest_batches").fetchone()[0]
event_count = con.execute("SELECT COUNT(*) FROM ingest_events").fetchone()[0]
run_count = con.execute("SELECT COUNT(*) FROM traces WHERE stream_type='run_trace'").fetchone()[0]
usb_count = con.execute("SELECT COUNT(*) FROM traces WHERE stream_type='usbcomm'").fetchone()[0]
size_totals = con.execute(
    "SELECT COALESCE(SUM(size_bytes), 0), COALESCE(SUM(compressed_size_bytes), 0) FROM traces"
).fetchone()

assert trace_count > 0, "expected stored traces"
assert source_count >= trace_count, "source rows should be at least trace rows"
assert batch_count == 2, f"expected 2 ingest batches, got {batch_count}"
assert run_count > 0, "expected run_trace rows"
assert usb_count > 0, "expected usbcomm rows"
assert size_totals[1] < size_totals[0], "expected compressed payloads to be smaller than original payloads in aggregate"

con.close()

with open(run_manifest_path, "r", encoding="utf-8-sig", newline="") as handle:
    run_rows = list(csv.DictReader(handle))
with open(usb_manifest_path, "r", encoding="utf-8-sig", newline="") as handle:
    usb_rows = list(csv.DictReader(handle))

assert len(run_rows) > 0, "run-trace manifest should not be empty"
assert len(usb_rows) > 0, "usbcomm manifest should not be empty"
assert event_count == len(run_rows) + len(usb_rows), "event ledger should match observed file count across both batches"
assert all(row["stream_type"] == "run_trace" for row in run_rows), "run manifest should contain only run_trace rows"
assert all(row["stream_type"] == "usbcomm" for row in usb_rows), "usb manifest should contain only usbcomm rows"
'@

@"
$pythonCheck
"@ | python - $databasePath $runManifest $usbManifest

Write-Host "Raw trace live smoke checks passed." -ForegroundColor Green
