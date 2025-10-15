<# 
    qc-trace-patterns.ps1
    Scans Hamilton trace logs (*.trc), counts occurrences of expected patterns,
    and records any previously unseen patterns to a log for review.
    Patterns are defined as "<Source> : <Action>" up to the first semicolon.
#>

param(
  [string]$Root = ".",
  [string]$ExpectedPatternsPath = ".\expected-patterns.json",
  [string]$UnknownLogPath = ".\qc-unknown-patterns.log",
  [switch]$Recurse,
  [int]$SampleLimit = 3,
  [switch]$DeltaOnly  # when set, highlight only patterns not seen in prior unknown log
)

function Get-DefaultExpectedPatterns {
  @(
    "SYSTEM : Analyze method - start",
    "SYSTEM : Analyze method - complete",
    "SYSTEM : Start method - start",
    "SYSTEM : Start method - progress",
    "SYSTEM : Execute method - complete",
    "SYSTEM : End method - start",
    "SYSTEM : End method - complete",
    "SYSTEM : Abort method - complete",
    "SYSTEM : File checksum - written",
    "SYSTEM : Custom Dialog - start",
    "SYSTEM : Custom Dialog - complete",
    "SYSTEM : HSLHamHeaterShakerLib - StopShaker - start",
    "SYSTEM : HSLHamHeaterShakerLib - StopShaker - complete",
    "SYSTEM : HSLHamHeaterShakerLib - StopTempCtrl - start",
    "SYSTEM : HSLHamHeaterShakerLib - StopTempCtrl - complete",
    "Microlabr STAR : Communication - progress",
    "Microlabr STAR : Start method command - start",
    "Microlabr STAR : Start method command - progress",
    "Microlabr STAR : Start method command - complete",
    "Microlabr STAR : Firmware Command (Single Step) - start",
    "Microlabr STAR : Firmware Command (Single Step) - complete",
    "Microlabr STAR : CO-RE 96 Head Tip Eject (Single Step) - start",
    "Microlabr STAR : CO-RE 96 Head Tip Eject (Single Step) - complete",
    "Microlabr STAR : CO-RE 96 Head Tip Pick Up (Single Step) - start",
    "Microlabr STAR : CO-RE 96 Head Tip Pick Up (Single Step) - complete",
    "Microlabr STAR : 1000ul Channel Tip Eject (Single Step) - start",
    "Microlabr STAR : 1000ul Channel Tip Eject (Single Step) - complete",
    "Microlabr STAR : Lock/Unlock Front Cover (Single Step) - start",
    "Microlabr STAR : Lock/Unlock Front Cover (Single Step) - complete",
    "Microlabr STAR : Abort command - start",
    "Microlabr STAR : Abort command - complete",
    "Microlabr STAR : End method command - start",
    "Microlabr STAR : End method command - complete",
    "Microlabr STAR : Clean up instrument - progress",
    "USER : Trace - complete"
  )
}

function Load-ExpectedPatterns([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) {
    $defaults = Get-DefaultExpectedPatterns
    $json = $defaults | ConvertTo-Json -Depth 3
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $dir = [System.IO.Path]::GetDirectoryName($fullPath)
    if (-not (Test-Path -LiteralPath $dir)) {
      $null = New-Item -Path $dir -ItemType Directory -Force
    }
    Set-Content -Path $fullPath -Value $json -Encoding UTF8
    Write-Host "Created default expected patterns file at $fullPath" -ForegroundColor Yellow
    return $defaults
  }
  try {
    $content = Get-Content -Path $Path -Raw -ErrorAction Stop
    $data = $content | ConvertFrom-Json
    return [string[]]$data
  } catch {
    throw "Failed to load expected patterns from '$Path': $_"
  }
}

$rootPath = [System.IO.Path]::GetFullPath($Root)
$expectedPath = [System.IO.Path]::GetFullPath($ExpectedPatternsPath)
$unknownLogPath = [System.IO.Path]::GetFullPath($UnknownLogPath)
$sampleLimit = [Math]::Max(1, $SampleLimit)

$expectedPatterns = Load-ExpectedPatterns -Path $expectedPath
$sourceNormalizeRegex = '^Microlab.+\sSTAR$'

function Normalize-Source([string]$Source) {
  $trimmed = $Source.Trim()
  if ($trimmed -match '^Microlab.+\sSTAR$') {
    return 'Microlabr STAR'
  }
  return $trimmed
}
$expectedSet = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$expectedCounts = [ordered]@{}
foreach ($pattern in $expectedPatterns) {
  if ($expectedSet.Add($pattern)) {
    $expectedCounts[$pattern] = 0
  }
}

$unknownPatterns = @{}
$previouslySeen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
if (Test-Path -LiteralPath $unknownLogPath) {
  try {
    # Extract prior patterns from the unknown log
    $regex = '^Pattern:\s*(.+)$'
    Select-String -Path $unknownLogPath -Pattern $regex |
      ForEach-Object { $previouslySeen.Add($_.Matches[0].Groups[1].Value.Trim()) | Out-Null }
  } catch {
    Write-Warning "Failed to parse prior unknown log: $_"
  }
}

Write-Host "Scanning root: $rootPath" -ForegroundColor Gray
$searchParams = @{
  Path        = $rootPath
  Filter      = "*.trc"
  File        = $true
  ErrorAction = 'SilentlyContinue'
}
if ($Recurse) { $searchParams.Recurse = $true }

$files = Get-ChildItem @searchParams | Sort-Object FullName
$fileCount = $files.Count
Write-Host "Discovered $fileCount trace file(s)." -ForegroundColor Gray

if (-not $files) {
  Write-Host "No .trc files found under $rootPath"
  exit 0
}

$lineRegex = '^(?<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})>\s*(?<source>[^:]+):\s*(?<event>[^;]+)'

foreach ($file in $files) {
  Write-Host ("Processing: {0}" -f $file.FullName) -ForegroundColor DarkGray
  $lineNumber = 0
  $sr = [System.IO.StreamReader]::new($file.FullName)
  try {
    while (-not $sr.EndOfStream) {
      $line = $sr.ReadLine()
      $lineNumber++
      if ([string]::IsNullOrWhiteSpace($line)) { continue }
      if ($line -notmatch $lineRegex) { continue }

      $source = Normalize-Source -Source $Matches.source
      $event  = $Matches.event.Trim()
      $patternKey = "$source : $event"

      if ($expectedSet.Contains($patternKey)) {
        $expectedCounts[$patternKey]++
      } else {
        if (-not $unknownPatterns.ContainsKey($patternKey)) {
          $unknownPatterns[$patternKey] = [pscustomobject]@{
            Count   = 0
            Samples = New-Object System.Collections.Generic.List[pscustomobject]
          }
        }
        $entry = $unknownPatterns[$patternKey]
        $entry.Count++
        if ($entry.Samples.Count -lt $sampleLimit) {
          $entry.Samples.Add([pscustomobject]@{
            File = $file.FullName
            Line = $lineNumber
            Text = $line.Trim()
          })
        }
      }
    }
  } finally {
    $sr.Dispose()
  }
}

Write-Host ""
Write-Host "=== Expected Pattern Summary ===" -ForegroundColor Cyan
$expectedCounts.GetEnumerator() |
  Sort-Object Value -Descending |
  ForEach-Object {
    "{0,-70} {1,8}" -f $_.Key, $_.Value
  } | Write-Output

if ($unknownPatterns.Count -gt 0) {
  Write-Host ""
  Write-Host "=== Unrecognized Patterns Detected ===" -ForegroundColor Yellow
  $unknownPatterns.GetEnumerator() |
    Sort-Object { $_.Value.Count } -Descending |
    ForEach-Object {
      "{0} (Count={1})" -f $_.Key, $_.Value.Count
    } | Write-Output

  # Compute delta vs historical unknowns
  $newOnly = @{}
  foreach ($kvp in $unknownPatterns.GetEnumerator()) {
    if (-not $previouslySeen.Contains($kvp.Key)) { $newOnly[$kvp.Key] = $kvp.Value }
  }

  if ($DeltaOnly) {
    Write-Host ""; Write-Host ("Newly observed patterns: {0}" -f $newOnly.Count) -ForegroundColor Cyan
    $newOnly.GetEnumerator() |
      Sort-Object { $_.Value.Count } -Descending |
      ForEach-Object { "{0} (Count={1})" -f $_.Key, $_.Value.Count } | Write-Output
  }

  $sb = New-Object System.Text.StringBuilder
  $null = $sb.AppendLine("===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====")
  $toLog = if ($DeltaOnly) { $newOnly } else { $unknownPatterns }
  foreach ($kvp in ($toLog.GetEnumerator() | Sort-Object { $_.Value.Count } -Descending)) {
    $pattern = $kvp.Key
    $data = $kvp.Value
    $null = $sb.AppendLine("Pattern: $pattern")
    $null = $sb.AppendLine("Count: $($data.Count)")
    foreach ($sample in $data.Samples) {
      $null = $sb.AppendLine("  Sample: $($sample.File):$($sample.Line)")
      $null = $sb.AppendLine("           $($sample.Text)")
    }
    $null = $sb.AppendLine("")
  }
  [System.IO.File]::AppendAllText($unknownLogPath, $sb.ToString(), [System.Text.Encoding]::UTF8)
  Write-Host ""
  Write-Host "Logged new patterns to $unknownLogPath" -ForegroundColor Yellow

  Write-Host ""
  Write-Host "Tip: To whitelist patterns, add them to expected-patterns.json (exact match after normalization)." -ForegroundColor Gray
} else {
  Write-Host ""
  Write-Host "No new patterns detected." -ForegroundColor Green
}
