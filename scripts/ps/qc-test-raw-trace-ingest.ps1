<#
    qc-test-raw-trace-ingest.ps1
    Regression check for the compressed raw trace store.

    The goal is to prove three behaviors:
    1. Full trace bytes are stored in SQLite in compressed form.
    2. Run traces and HxUsbComm traces occupy separate storage lanes.
    3. Duplicate payloads in the same lane are deduplicated by content hash
       while still preserving every observed source path in trace_sources.
    4. Every ingest run is recorded as a batch, with one event row per observed
       file so later retention logic can audit what happened.
#>

param()

$ErrorActionPreference = "Stop"

function Assert-True([string]$Label, [bool]$Condition) {
  if (-not $Condition) {
    throw $Label
  }
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$sampleRoot = Join-Path $repoRoot "data\samples"
$tmpRoot = Join-Path $repoRoot "tmp\raw-trace-ingest-test"
$inputRoot = Join-Path $tmpRoot "Logs"
$manifestCsv = Join-Path $tmpRoot "raw-trace-ingest-manifest.csv"
$manifestJsonl = Join-Path $tmpRoot "raw-trace-ingest-manifest.jsonl"
$databasePath = Join-Path $tmpRoot "raw-traces.sqlite"
$ingestScript = Join-Path $PSScriptRoot "qc-ingest-raw-traces.ps1"

if (Test-Path -LiteralPath $tmpRoot) {
  Remove-Item -LiteralPath $tmpRoot -Recurse -Force
}

$null = New-Item -ItemType Directory -Path (Join-Path $inputRoot "H7\2025-10-02") -Force
$null = New-Item -ItemType Directory -Path (Join-Path $inputRoot "H7\2025-10-03") -Force
$null = New-Item -ItemType Directory -Path (Join-Path $inputRoot "H8\2025-10-02") -Force

$sampleFiles = @(Get-ChildItem -LiteralPath $sampleRoot -Filter "*_Trace.trc" -File | Sort-Object Name)
Assert-True -Label "Expected sample trace files" -Condition ($sampleFiles.Count -ge 3)

# Copy the real sample traces into a canonical Logs tree so the raw store can
# capture machine/date metadata from path structure during the regression run.
Copy-Item -LiteralPath $sampleFiles[0].FullName -Destination (Join-Path $inputRoot "H7\2025-10-02\$($sampleFiles[0].Name)")
Copy-Item -LiteralPath $sampleFiles[1].FullName -Destination (Join-Path $inputRoot "H7\2025-10-03\$($sampleFiles[1].Name)")
Copy-Item -LiteralPath $sampleFiles[2].FullName -Destination (Join-Path $inputRoot "H8\2025-10-02\$($sampleFiles[2].Name)")

# Duplicate one run-trace payload under a different source path to prove the
# store keeps one payload row but two source-location rows for that lane.
Copy-Item -LiteralPath $sampleFiles[0].FullName -Destination (Join-Path $inputRoot "H8\2025-10-02\duplicate-$($sampleFiles[0].Name)")

# Reuse the same bytes as a synthetic usbcomm trace. The content is identical
# to the first run trace on purpose so the test proves stream separation.
Copy-Item -LiteralPath $sampleFiles[0].FullName -Destination (Join-Path $inputRoot "H7\2025-10-02\HxUsbComm20251002.trc")

& powershell -NoProfile -File $ingestScript `
  -SourceRoot $inputRoot `
  -DatabasePath $databasePath `
  -Stream All `
  -OutManifestCsv $manifestCsv `
  -OutManifestJsonl $manifestJsonl `
  -BatchLabel "regression" `
  -Recurse

Assert-True -Label "Raw trace database missing" -Condition (Test-Path -LiteralPath $databasePath)
Assert-True -Label "Manifest CSV missing" -Condition (Test-Path -LiteralPath $manifestCsv)
Assert-True -Label "Manifest JSONL missing" -Condition (Test-Path -LiteralPath $manifestJsonl)

$manifestRows = @(Import-Csv -LiteralPath $manifestCsv)
Assert-True -Label "Expected five observed source files in manifest" -Condition ($manifestRows.Count -eq 5)

$pythonCheck = @'
import csv
import gzip
import hashlib
import sqlite3
import sys

db_path, manifest_path = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db_path)

trace_count = con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
source_count = con.execute("SELECT COUNT(*) FROM trace_sources").fetchone()[0]
batch_count = con.execute("SELECT COUNT(*) FROM ingest_batches").fetchone()[0]
event_count = con.execute("SELECT COUNT(*) FROM ingest_events").fetchone()[0]
run_count = con.execute("SELECT COUNT(*) FROM traces WHERE stream_type='run_trace'").fetchone()[0]
usb_count = con.execute("SELECT COUNT(*) FROM traces WHERE stream_type='usbcomm'").fetchone()[0]

assert trace_count == 4, f"expected 4 unique stored payloads, got {trace_count}"
assert source_count == 5, f"expected 5 observed source rows, got {source_count}"
assert batch_count == 1, f"expected 1 ingest batch, got {batch_count}"
assert event_count == 5, f"expected 5 ingest events, got {event_count}"
assert run_count == 3, f"expected 3 unique run-trace payloads, got {run_count}"
assert usb_count == 1, f"expected 1 usbcomm payload, got {usb_count}"

duplicate_sources = con.execute(
    """
    SELECT COUNT(*)
    FROM trace_sources
    WHERE stream_type='run_trace'
      AND trace_id = (
        SELECT trace_id
        FROM traces
        WHERE stream_type='run_trace'
        ORDER BY trace_id
        LIMIT 1
      )
    """
).fetchone()[0]
assert duplicate_sources >= 1

row = con.execute(
    """
    SELECT sha256, size_bytes, compressed_size_bytes, content_gzip
    FROM traces
    WHERE stream_type='run_trace'
    ORDER BY trace_id
    LIMIT 1
    """
).fetchone()
assert row is not None, "missing run_trace payload"
sha256_value, size_bytes, compressed_size_bytes, payload = row
raw = gzip.decompress(payload)
assert len(raw) == size_bytes, "decompressed payload size mismatch"
assert hashlib.sha256(raw).hexdigest().upper() == sha256_value, "payload hash mismatch"
assert compressed_size_bytes == len(payload), "compressed size metadata mismatch"

batch_row = con.execute(
    """
    SELECT stream_filter, recurse, observed_files, new_trace_payloads, new_source_locations, batch_label
    FROM ingest_batches
    """
).fetchone()
assert batch_row == ("all", 1, 5, 4, 5, "regression"), f"unexpected batch summary: {batch_row!r}"

event_row = con.execute(
    """
    SELECT stream_type, source_path, trace_inserted, source_inserted, is_duplicate_content
    FROM ingest_events
    WHERE stream_type='usbcomm'
    LIMIT 1
    """
).fetchone()
assert event_row is not None, "missing usbcomm ingest event"
assert event_row[2:] == (1, 1, 0), f"unexpected usbcomm event flags: {event_row!r}"

con.close()

with open(manifest_path, "r", encoding="utf-8-sig", newline="") as handle:
    rows = list(csv.DictReader(handle))

assert len(rows) == 5, f"expected 5 manifest rows, got {len(rows)}"
assert any(row["stream_type"] == "usbcomm" for row in rows), "manifest missing usbcomm row"
assert sum(1 for row in rows if row["is_duplicate_content"] == "1") >= 1, "expected at least one duplicate payload marker"
'@

@"
$pythonCheck
"@ | python - $databasePath $manifestCsv

Write-Host "Raw trace ingest regression checks passed." -ForegroundColor Green
