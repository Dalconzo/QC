<#
    qc-build-network-fixture.ps1
    Build a bounded verification fixture from the real network log archive.

    The goal is not to mirror the full share locally. Instead, this script
    copies a small, deterministic subset of real trace files into a local
    fixture root while preserving the canonical Logs\<Machine>\<YYYY-MM-DD>
    layout that the analytics pipeline expects.

    Selection rules:
      1. Pick the latest day for each target machine that actually contains
         method trace files (*_Trace.trc).
      2. Prefer one aborted trace when available so the fixture exercises
         non-happy-path handling.
      3. Prefer one completed trace when available so the fixture includes the
         normal run path.
      4. Prefer one simulation trace when available, then fill the remainder by
         largest file size.

    The script writes a manifest so downstream validation can assert that the
    staged fixture still matches what was selected from the share.
#>

param(
  [string]$SourceRoot = "\\192.168.10.99\home\Logs",
  [string]$OutRoot = (Join-Path $PSScriptRoot "..\..\tmp\network-verification-fixture"),
  [string[]]$Machines = @("H3", "H4", "H6", "H7", "H13", "H14"),
  [int]$MaxFilesPerMachine = 2,
  [long]$MinTraceBytes = 4096,
  [switch]$Force
)

$ErrorActionPreference = "Stop"

function Assert-True {
  param(
    [string]$Label,
    [bool]$Condition
  )

  if (-not $Condition) {
    throw $Label
  }
}

function Get-LatestTraceDay {
  param(
    [string]$MachineRoot,
    [long]$MinBytes
  )

  # The share holds one directory per day. We only want a day that contains
  # actual method traces, not an empty date folder or a directory that only
  # contains tiny stub files.
  $dayDirs = Get-ChildItem -LiteralPath $MachineRoot -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending
  foreach ($dayDir in $dayDirs) {
    $traceCount = @(Get-ChildItem -LiteralPath $dayDir.FullName -Filter '*_Trace.trc' -File -ErrorAction SilentlyContinue | Where-Object { $_.Length -ge $MinBytes }).Count
    if ($traceCount -gt 0) {
      return $dayDir
    }
  }

  return $null
}

function Test-TraceMarker {
  param(
    [string]$Path,
    [string]$Pattern
  )

  return [bool](Select-String -LiteralPath $Path -Pattern $Pattern -Quiet)
}

function Get-TraceCandidates {
  param(
    [string]$Machine,
    [System.IO.DirectoryInfo]$DayDir,
    [long]$MinBytes
  )

  $files = Get-ChildItem -LiteralPath $DayDir.FullName -Filter '*_Trace.trc' -File -ErrorAction SilentlyContinue | Where-Object { $_.Length -ge $MinBytes }
  foreach ($file in ($files | Sort-Object -Property @(
    @{ Expression = 'Length'; Descending = $true },
    @{ Expression = 'Name'; Descending = $false }
  ))) {
    [pscustomobject]@{
      machine         = $Machine
      day             = $DayDir.Name
      source_path     = $file.FullName
      name            = $file.Name
      size_bytes      = $file.Length
      has_abort       = Test-TraceMarker -Path $file.FullName -Pattern 'Abort method - complete'
      is_simulation   = Test-TraceMarker -Path $file.FullName -Pattern 'SetSimulation - progress; simulate mode\s*=\s*1'
      relative_path   = (Join-Path (Join-Path $Machine $DayDir.Name) $file.Name)
    }
  }
}

function Add-UniqueSelection {
  param(
    [System.Collections.Generic.List[object]]$Selected,
    $Candidate
  )

  if ($null -eq $Candidate) {
    return
  }

  $alreadyPresent = $Selected | Where-Object { $_.source_path -eq $Candidate.source_path } | Select-Object -First 1
  if (-not $alreadyPresent) {
    $Selected.Add($Candidate)
  }
}

function Select-FixtureRecords {
  param(
    [object[]]$Candidates,
    [int]$Limit
  )

  $selected = New-Object 'System.Collections.Generic.List[object]'

  # Lead with explicit behavior coverage before falling back to file size.
  Add-UniqueSelection -Selected $selected -Candidate ($Candidates | Where-Object { $_.has_abort } | Sort-Object size_bytes -Descending | Select-Object -First 1)
  Add-UniqueSelection -Selected $selected -Candidate ($Candidates | Where-Object { -not $_.has_abort } | Sort-Object size_bytes -Descending | Select-Object -First 1)
  Add-UniqueSelection -Selected $selected -Candidate ($Candidates | Where-Object { $_.is_simulation } | Sort-Object size_bytes -Descending | Select-Object -First 1)

  foreach ($candidate in ($Candidates | Sort-Object -Property @(
    @{ Expression = 'has_abort'; Descending = $true },
    @{ Expression = 'is_simulation'; Descending = $true },
    @{ Expression = 'size_bytes'; Descending = $true }
  ))) {
    if ($selected.Count -ge $Limit) {
      break
    }
    Add-UniqueSelection -Selected $selected -Candidate $candidate
  }

  return @($selected | Select-Object -First $Limit)
}

$sourceRootFull = $SourceRoot.TrimEnd('\')
$outRootFull = [System.IO.Path]::GetFullPath($OutRoot)
$logsRoot = Join-Path $outRootFull "Logs"
$manifestPath = Join-Path $outRootFull "fixture-manifest.json"
$manifestCsvPath = Join-Path $outRootFull "fixture-manifest.csv"

Assert-True -Label "Source root not found: $sourceRootFull" -Condition (Test-Path -LiteralPath $sourceRootFull)

if ((Test-Path -LiteralPath $outRootFull) -and -not $Force) {
  throw "Fixture output already exists: $outRootFull. Re-run with -Force to rebuild it."
}

if (Test-Path -LiteralPath $outRootFull) {
  Remove-Item -LiteralPath $outRootFull -Recurse -Force
}

$null = New-Item -ItemType Directory -Path $logsRoot -Force
$manifestRows = @()

foreach ($machine in $Machines) {
  $machineRoot = Join-Path $sourceRootFull $machine
  if (-not (Test-Path -LiteralPath $machineRoot)) {
    Write-Warning "Skipping missing machine directory: $machineRoot"
    continue
  }

  $dayDir = Get-LatestTraceDay -MachineRoot $machineRoot -MinBytes $MinTraceBytes
  if ($null -eq $dayDir) {
    Write-Warning "Skipping $machine because no qualifying *_Trace.trc files were found."
    continue
  }

  $candidates = @(Get-TraceCandidates -Machine $machine -DayDir $dayDir -MinBytes $MinTraceBytes)
  $selected = @(Select-FixtureRecords -Candidates $candidates -Limit $MaxFilesPerMachine)

  foreach ($record in $selected) {
    $destinationPath = Join-Path $logsRoot $record.relative_path
    $destinationDir = Split-Path -Parent $destinationPath
    $null = New-Item -ItemType Directory -Path $destinationDir -Force
    Copy-Item -LiteralPath $record.source_path -Destination $destinationPath -Force

    $hash = Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256

    $manifestRows += [pscustomobject]@{
      machine          = $record.machine
      day              = $record.day
      name             = $record.name
      relative_path    = $record.relative_path
      source_path      = $record.source_path
      fixture_path     = $destinationPath
      size_bytes       = $record.size_bytes
      sha256           = $hash.Hash
      has_abort        = $record.has_abort
      is_simulation    = $record.is_simulation
      selection_reason = if ($record.has_abort) { "aborted coverage" } elseif ($record.is_simulation) { "simulation coverage" } else { "completed coverage / largest trace" }
    }
  }
}

Assert-True -Label "Fixture selection produced no files." -Condition ($manifestRows.Count -gt 0)

$manifestRows | Sort-Object machine, day, name | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath
$manifestRows | Sort-Object machine, day, name | Export-Csv -LiteralPath $manifestCsvPath -NoTypeInformation -Encoding UTF8

Write-Host "Built network verification fixture:" -ForegroundColor Green
Write-Host "  Logs root: $logsRoot"
Write-Host "  Manifest : $manifestPath"
Write-Host "  Rows     : $($manifestRows.Count)"
