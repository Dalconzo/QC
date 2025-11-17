<# 
    qc-trace-summarize.ps1
    Produces summary metadata for Hamilton trace (*.trc) files.
    Reads each file once (streaming) to keep memory low even for large traces.
    Default output is a table; pass -AsJson to emit JSONL for downstream tools.
#>

param(
  [string]$SourceRoot = (Get-Location).Path,
  [string]$Filter = "*.trc",
  [switch]$Recurse,
  [switch]$AsJson,
  [switch]$IncludeEvents,  # when set, emit per-file event preview (first 10 error lines)
  [int]$MaxPreview = 10,
  [string]$OutDir,         # when provided, append JSONL to this directory grouped by date
  [switch]$ByLocalDate     # group output files by run_local_date instead of UTC date
)

$script:TestMachines = @("H14","H13","H7")
$script:NormalizedRoot = [System.IO.Path]::GetFullPath($SourceRoot)

function Get-MachineFromPath([string]$Path) {
  $parts = $Path -split '[\\/]'
  $idx = [Array]::IndexOf($parts, "Logs")
  if ($idx -ge 0 -and $idx + 1 -lt $parts.Length) {
    return $parts[$idx + 1]
  }
  # fallback: use the last path segment (immediate parent directory name for DirectoryName inputs)
  if ($parts.Length -ge 1) {
    return $parts[$parts.Length - 1]
  }
  return ""
}

function Get-RelativePath([string]$FullPath) {
  $root = $script:NormalizedRoot
  if ($FullPath.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
    return $FullPath.Substring($root.Length).TrimStart('\','/')
  }
  return $FullPath
}

function New-TraceSummary {
  param(
    [System.IO.FileInfo]$File
  )

  $machine = Get-MachineFromPath -Path $File.DirectoryName
  $environment = if ($script:TestMachines -contains $machine) { "Test" } else { "Production" }

  $startUtc = $null
  $endUtc = $null
  $lastTimestamp = $null
  $isSimulation = $false

  $summary = [ordered]@{
    run_id        = $null
    file           = $File.FullName
    name           = $File.Name
    size_bytes     = $File.Length
    relative_path  = Get-RelativePath -FullPath $File.FullName
    machine        = $machine
    environment    = $environment
    run_local_date = $null
    start_utc      = $null
    start_local    = $null
    end_utc        = $null
    end_local      = $null
    duration_min   = $null
    user           = $null
    method         = $null
    status         = "Completed"
    checksum       = $null
    checksum_valid = $null
    error_lines    = 0
    warning_lines  = 0
    dialog_count   = 0
    is_simulation  = $false
  }

  $preview = @()

  $sr = [System.IO.StreamReader]::new($File.FullName)
  try {
    while (-not $sr.EndOfStream) {
      $line = $sr.ReadLine()

      if (-not $line) { continue }

      if ($line -match '^(?<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})>') {
        $ts = [datetime]::ParseExact($Matches.ts, "yyyy-MM-dd HH:mm:ss", $null).ToUniversalTime()
        if (-not $startUtc) { $startUtc = $ts }
        $lastTimestamp = $ts
      }

      if (-not $summary.method -and $line -match 'Method file\s+(?<method>.+)$') {
        $summary.method = $Matches.method.Trim()
      }

      if (-not $summary.user -and $line -match 'User name:\s*(?<user>.+)$') {
        $summary.user = $Matches.user.Trim()
      }

      if ($line -match 'Abort method - complete') {
        $summary.status = "Aborted"
      }

      if ($line -match 'Custom Dialog - start;') {
        $summary.dialog_count++
      }

      if ($line -match 'File checksum - written; (?<rest>.+)$') {
        $rest = $Matches.rest
        if ($rest -match 'checksum=([0-9A-Fa-f]+)') {
          $summary.checksum = $Matches[1]
        }
        if ($rest -match 'valid=(\d+)') {
          $summary.checksum_valid = [int]$Matches[1]
        }
        if ($rest -match 'time=([^$]+)') {
          $endLocal = [datetime]::ParseExact($Matches[1], "yyyy-MM-dd HH:mm", $null)
          $endUtc = $endLocal.ToUniversalTime()
        }
      }

      if ($line -match 'SetSimulation - progress' -and $line -match 'simulate mode\s*=\s*1') {
        $isSimulation = $true
      }

      if ($line -match '(?i)\berror\b') {
        $summary.error_lines++
        if ($IncludeEvents -and $preview.Count -lt $MaxPreview) {
          $preview += $line
        }
      } elseif ($line -match '(?i)\bwarn(ing)?\b') {
        $summary.warning_lines++
        if ($IncludeEvents -and $preview.Count -lt $MaxPreview) {
          $preview += $line
        }
      }
    }
  } finally {
    $sr.Dispose()
  }

  if ($lastTimestamp) {
    if (-not $endUtc) {
      $endUtc = $lastTimestamp
    } elseif ($lastTimestamp -gt $endUtc) {
      $endUtc = $lastTimestamp
    }
  }

  if ($startUtc -and $endUtc) {
    if ($endUtc -lt $startUtc) {
      $endUtc = $startUtc
    }
    $summary.duration_min = [math]::Round(($endUtc - $startUtc).TotalMinutes,2)
  }

  if ($startUtc) {
    $summary.start_utc = $startUtc.ToString("o")
    $startLocal = $startUtc.ToLocalTime()
    $summary.start_local = $startLocal.ToString("o")
    $summary.run_local_date = $startLocal.ToString("yyyy-MM-dd")
  }

  if ($endUtc) {
    $summary.end_utc = $endUtc.ToString("o")
    $endLocal = $endUtc.ToLocalTime()
    $summary.end_local = $endLocal.ToString("o")
  }

  $summary.is_simulation = $isSimulation

  # derive run_id (stable per file/machine)
  $summary.run_id = if ($summary.machine) { "$($summary.machine):$($summary.name)" } else { $summary.name }

  return [pscustomobject]@{
    Summary = $summary
    Preview = $preview
  }
}

$search = @{
  Path        = $SourceRoot
  Filter      = $Filter
  File        = $true
  ErrorAction = 'SilentlyContinue'
}
if ($Recurse) { $search.Recurse = $true }

$files = Get-ChildItem @search | Sort-Object LastWriteTime

$outputs = @()
foreach ($file in $files) {
  $result = New-TraceSummary -File $file
  $summary = [pscustomobject]$result.Summary

  if ($AsJson) {
    $outputs += ($summary | ConvertTo-Json -Compress)
  } else {
    $outputs += $summary
  }

  if ($IncludeEvents -and $result.Preview.Count -gt 0 -and -not $AsJson) {
    Write-Output ("  Preview errors/warnings:")
    $result.Preview | ForEach-Object { Write-Output ("    " + $_) }
  }
}

if ($AsJson) {
  $outputs | ForEach-Object { Write-Output $_ }
} else {
  $outputs
}

# Optional: write JSONL to summaries folder for nightly aggregation
if ($OutDir) {
  $outRoot = [System.IO.Path]::GetFullPath($OutDir)
  if (-not (Test-Path -LiteralPath $outRoot)) {
    $null = New-Item -ItemType Directory -Path $outRoot -Force
  }
  foreach ($line in $outputs) {
    $obj = $null
    if ($AsJson) {
      try { $obj = $line | ConvertFrom-Json } catch { continue }
    } else {
      $obj = $line
    }
    if (-not $obj) { continue }
    $dateKey = if ($ByLocalDate -and $obj.run_local_date) { $obj.run_local_date } elseif ($obj.start_utc) { ([datetime]::Parse($obj.start_utc)).ToString('yyyy-MM-dd') } else { (Get-Date).ToString('yyyy-MM-dd') }
    $outFile = Join-Path $outRoot ("runs-" + $dateKey + ".jsonl")
    $json = ($obj | ConvertTo-Json -Compress)
    Add-Content -Path $outFile -Value $json
  }
  Write-Host ("Wrote JSONL summaries to: " + (Get-ChildItem -Path $outRoot -Filter 'runs-*.jsonl' | Select-Object -ExpandProperty FullName | Sort-Object | Select-Object -Last 1)) -ForegroundColor Green
}
