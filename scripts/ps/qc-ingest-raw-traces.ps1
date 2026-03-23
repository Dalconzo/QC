<#
    qc-ingest-raw-traces.ps1
    Wrapper around the SQLite-backed raw trace ingest.

    This script exists so the raw-ingest step looks and feels like the rest of
    the PowerShell-first pipeline, while the actual compressed payload storage
    lives in Python's built-in sqlite3 module for portability.

    The store is intentionally split into two lanes:
    - run_trace: method traces used by run analytics
    - usbcomm: lower-level HxUsbComm firmware/USB traffic logs

    Keeping those streams separate now makes later retention and analytics
    decisions much easier.
#>

param(
  [string]$SourceRoot = "Z:\Logs",
  [string]$DatabasePath = (Join-Path $PSScriptRoot "..\..\archive\raw-trace-store\raw-traces.sqlite"),
  [ValidateSet("All", "RunTrace", "UsbComm")]
  [string]$Stream = "All",
  [string]$OutManifestCsv = (Join-Path $PSScriptRoot "..\..\outbox\raw-trace-ingest-manifest.csv"),
  [string]$OutManifestJsonl,
  [int]$MaxFiles = 0,
  [int]$MaxFilesPerMachine = 0,
  [ValidateRange(0, 9)]
  [int]$CompressionLevel = 6,
  [string]$BatchLabel,
  [switch]$Recurse,
  [switch]$VerboseProgress
)

$ErrorActionPreference = "Stop"

function Resolve-StreamArgument {
  param(
    [string]$Name
  )

  switch ($Name) {
    "All"      { return "all" }
    "RunTrace" { return "run_trace" }
    "UsbComm"  { return "usbcomm" }
    default    { throw "Unsupported stream selection: $Name" }
  }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
  Write-Error "Python is not available in PATH. Install Python 3.9+ to run raw trace ingest."
  exit 1
}

$scriptPath = Join-Path $PSScriptRoot "..\py\qc-ingest-raw-traces.py"
$scriptPath = [System.IO.Path]::GetFullPath($scriptPath)
if (-not (Test-Path -LiteralPath $scriptPath)) {
  Write-Error "Python ingest script not found: $scriptPath"
  exit 1
}

$argsList = @()
$argsList += "--source-root"; $argsList += ([System.IO.Path]::GetFullPath($SourceRoot))
$argsList += "--database-path"; $argsList += ([System.IO.Path]::GetFullPath($DatabasePath))
$argsList += "--stream"; $argsList += (Resolve-StreamArgument -Name $Stream)
if ($OutManifestCsv) {
  $argsList += "--out-manifest-csv"; $argsList += ([System.IO.Path]::GetFullPath($OutManifestCsv))
}
if ($OutManifestJsonl) {
  $argsList += "--out-manifest-jsonl"; $argsList += ([System.IO.Path]::GetFullPath($OutManifestJsonl))
}
if ($MaxFiles -gt 0) {
  $argsList += "--max-files"; $argsList += $MaxFiles
}
if ($MaxFilesPerMachine -gt 0) {
  $argsList += "--max-files-per-machine"; $argsList += $MaxFilesPerMachine
}
$argsList += "--compression-level"; $argsList += $CompressionLevel
if ($BatchLabel) {
  # Batch labels make it easier to tie later retention decisions back to the
  # ingest run that proved a file was durably captured.
  $argsList += "--batch-label"; $argsList += $BatchLabel
}
if ($Recurse) {
  $argsList += "--recurse"
}
if ($VerboseProgress) {
  $argsList += "--verbose"
}

Write-Host "Running raw trace ingest ..." -ForegroundColor Cyan
& python $scriptPath @argsList
if ($LASTEXITCODE -ne 0) {
  Write-Error "Raw trace ingest failed with exit code $LASTEXITCODE"
  exit $LASTEXITCODE
}
