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
  [int]$MaxPreview = 10
)

function New-TraceSummary {
  param(
    [System.IO.FileInfo]$File
  )

  $summary = [ordered]@{
    file           = $File.FullName
    name           = $File.Name
    size_bytes     = $File.Length
    start_utc      = $null
    end_utc        = $null
    duration_min   = $null
    user           = $null
    method         = $null
    status         = "Completed"
    checksum       = $null
    checksum_valid = $null
    error_lines    = 0
    warning_lines  = 0
    dialog_count   = 0
  }

  $preview = @()
  $lastTimestamp = $null

  $sr = [System.IO.StreamReader]::new($File.FullName)
  try {
    while (-not $sr.EndOfStream) {
      $line = $sr.ReadLine()

      if (-not $line) { continue }

      if ($line -match '^(?<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})>') {
        $ts = [datetime]::ParseExact($Matches.ts, "yyyy-MM-dd HH:mm:ss", $null).ToUniversalTime()
        if (-not $summary.start_utc) { $summary.start_utc = $ts }
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
          $summary.end_utc = $endLocal.ToUniversalTime()
        }
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

  if (-not $summary.end_utc -and $lastTimestamp) {
    $summary.end_utc = $lastTimestamp
  }

  if ($summary.start_utc -and $summary.end_utc) {
    $summary.duration_min = [math]::Round(($summary.end_utc - $summary.start_utc).TotalMinutes,2)
  }

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
