<#
    qc-inventory-usbcomm-traces.ps1
    Inventory HxUsbComm trace files as a separate ingest stream.

    These files are not method-run traces. They log lower-level USB / firmware
    traffic, so they should not be mixed into the run-summary dataset. This
    script builds a compact manifest that can feed a dedicated storage path
    later, such as a separate DuckDB/SQLite table or a compressed raw archive.
#>

param(
  [string]$SourceRoot = "Z:\Logs",
  [string]$OutCsv = (Join-Path $PSScriptRoot "..\..\outbox\usbcomm-inventory.csv"),
  [string]$OutJsonl,
  [switch]$Recurse,
  [int]$MaxFiles = 0,
  [int]$MaxFilesPerMachine = 0
)

$ErrorActionPreference = "Stop"

$normalizedRoot = [System.IO.Path]::GetFullPath($SourceRoot)

function Get-MachineFromPath {
  param(
    [string]$Path
  )

  $parts = $Path -split '[\\/]'
  $idx = [Array]::IndexOf($parts, "Logs")
  if ($idx -ge 0 -and $idx + 1 -lt $parts.Length) {
    return $parts[$idx + 1]
  }

  foreach ($part in $parts) {
    if ($part -match '^(?i)H\d+$') {
      return $part.ToUpperInvariant()
    }
  }

  return ""
}

function Get-RelativePath {
  param(
    [string]$FullPath
  )

  if ($FullPath.StartsWith($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    return $FullPath.Substring($normalizedRoot.Length).TrimStart('\', '/')
  }

  return $FullPath
}

$search = @{
  Path        = $SourceRoot
  Filter      = "HxUsbComm*.trc"
  File        = $true
  ErrorAction = "SilentlyContinue"
}
if ($Recurse) {
  $search.Recurse = $true
}

$files = Get-ChildItem @search | Sort-Object FullName
if ($MaxFilesPerMachine -gt 0) {
  # Per-machine sampling is more useful for smoke tests because a global limit
  # would otherwise stop in the alphabetically earliest machine directory.
  $files = @(
    $files |
      Group-Object { Get-MachineFromPath -Path $_.DirectoryName } |
      Sort-Object Name |
      ForEach-Object {
        $_.Group | Sort-Object FullName | Select-Object -First $MaxFilesPerMachine
      }
  )
} elseif ($MaxFiles -gt 0) {
  $files = @($files | Select-Object -First $MaxFiles)
}

$rows = foreach ($file in $files) {
  # The inventory stores only stable metadata and a content hash. Raw-content
  # ingestion belongs in the dedicated usbcomm storage task, not in the
  # run-summary pipeline.
  $hash = Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256
  $machine = Get-MachineFromPath -Path $file.DirectoryName

  [pscustomobject]@{
    log_stream          = "usbcomm"
    machine             = $machine
    log_local_date      = if ($file.Directory.Name -match '^\d{4}-\d{2}-\d{2}$') { $file.Directory.Name } else { "" }
    file                = $file.FullName
    name                = $file.Name
    relative_path       = Get-RelativePath -FullPath $file.FullName
    size_bytes          = $file.Length
    last_write_time_utc = $file.LastWriteTimeUtc.ToString("o")
    sha256              = $hash.Hash
  }
}

$outCsvFull = [System.IO.Path]::GetFullPath($OutCsv)
$outCsvDir = Split-Path -Parent $outCsvFull
if (-not (Test-Path -LiteralPath $outCsvDir)) {
  $null = New-Item -ItemType Directory -Path $outCsvDir -Force
}

$rows | Export-Csv -LiteralPath $outCsvFull -NoTypeInformation -Encoding UTF8
Write-Host "Wrote usbcomm inventory CSV: $outCsvFull" -ForegroundColor Green

if ($OutJsonl) {
  $outJsonlFull = [System.IO.Path]::GetFullPath($OutJsonl)
  $outJsonlDir = Split-Path -Parent $outJsonlFull
  if (-not (Test-Path -LiteralPath $outJsonlDir)) {
    $null = New-Item -ItemType Directory -Path $outJsonlDir -Force
  }

  $rows | ForEach-Object { $_ | ConvertTo-Json -Compress } | Set-Content -LiteralPath $outJsonlFull
  Write-Host "Wrote usbcomm inventory JSONL: $outJsonlFull" -ForegroundColor Green
}
