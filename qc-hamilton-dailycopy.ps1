<# 
    qc-hamilton-dailycopy.ps1
    Copies Hamilton trace logs to a shared network drive and writes
    rotating run logs under Z:\Logs\<MachineName>.
#>

param(
  [string]$SourceRoot = "C:\Program Files (x86)\HAMILTON\LogFiles",
  [string]$NetworkRoot = "Z:\Logs",
  [string]$LogRoot = "Z:\Logs",
  [string]$MachineName = $env:COMPUTERNAME,
  [int]$DaysBack = 0,
  [string[]]$Extensions = @("*.trc"),
  [int]$MaxLogBytes = 5MB,
  [int]$LogRetention = 5,
  [switch]$DryRun
)

$script:LogFile = $null
$script:MaxLogBytes = [long]$MaxLogBytes
$script:LogRetention = [Math]::Max(1, $LogRetention)

function Normalize-FullPath([string]$Path) {
  return ([System.IO.Path]::GetFullPath($Path))
}

function Initialize-Logging([string]$LogDirectory, [string]$BaseName) {
  if (-not (Test-Path -LiteralPath $LogDirectory)) {
    $null = New-Item -Path $LogDirectory -ItemType Directory -Force
  }
  $script:LogFile = Join-Path $LogDirectory ("{0}.log" -f $BaseName)
}

function Rotate-Log {
  if (-not $script:LogFile -or -not (Test-Path -LiteralPath $script:LogFile)) { return }
  try {
    $fi = Get-Item -LiteralPath $script:LogFile -ErrorAction Stop
  } catch {
    return
  }
  if ($fi.Length -lt $script:MaxLogBytes) { return }

  if ($script:LogRetention -le 1) {
    try { Clear-Content -Path $script:LogFile -ErrorAction SilentlyContinue } catch {}
    return
  }

  $maxIndex = $script:LogRetention - 1
  for ($i = $maxIndex; $i -ge 1; $i--) {
    $old = "{0}.{1}" -f $script:LogFile, $i
    $new = "{0}.{1}" -f $script:LogFile, ($i + 1)
    if (Test-Path -LiteralPath $old) {
      if ($i -eq $maxIndex) {
        Remove-Item -LiteralPath $old -Force -ErrorAction SilentlyContinue
      } else {
        Move-Item -LiteralPath $old -Destination $new -Force
      }
    }
  }
  Move-Item -LiteralPath $script:LogFile -Destination ("{0}.1" -f $script:LogFile) -Force
}

function Write-Log([string]$Message) {
  $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  $line = "$ts $Message"
  Write-Output $line
  try {
    Rotate-Log
    $lineWithNewline = $line + [Environment]::NewLine
    [System.IO.File]::AppendAllText($script:LogFile, $lineWithNewline, [System.Text.Encoding]::UTF8)
  } catch {
    Write-Output "$ts WARN Logging to '$script:LogFile' failed: $_"
  }
}

function Matches-Extension($fileName, [string[]]$patterns) {
  if (-not $patterns -or $patterns.Count -eq 0) { return $true }
  foreach ($pattern in $patterns) {
    if ($fileName -like $pattern) { return $true }
  }
  return $false
}

$sourceRoot = Normalize-FullPath $SourceRoot
$networkRoot = Normalize-FullPath $NetworkRoot
$logRoot = Normalize-FullPath $LogRoot

if (-not (Test-Path -LiteralPath $sourceRoot)) {
  throw "Source root '$sourceRoot' does not exist."
}

if (-not (Test-Path -LiteralPath $networkRoot)) {
  throw "Network root '$networkRoot' is unavailable."
}

Initialize-Logging -LogDirectory (Join-Path $logRoot $MachineName) -BaseName "dailycopy"

$targetDate = (Get-Date).AddDays(-$DaysBack).Date
$targetDateLabel = $targetDate.ToString("yyyy-MM-dd")
$machineFolder = Join-Path $networkRoot $MachineName
$targetRoot = Join-Path $machineFolder $targetDateLabel

if (-not $DryRun) {
  $null = New-Item -Path $targetRoot -ItemType Directory -Force
}

Write-Log "Collecting logs from '$sourceRoot' written on $targetDateLabel"
Write-Log "Staging destination: '$targetRoot' (machine '$MachineName')"

$files = Get-ChildItem -Path $sourceRoot -Recurse -File -ErrorAction SilentlyContinue |
         Where-Object {
           $_.LastWriteTime.Date -eq $targetDate -and (Matches-Extension $_.Name $Extensions)
         } |
         Sort-Object FullName

if (-not $files) {
  Write-Log "No matching files found for $targetDateLabel."
  return
}

$copied = 0
$skipped = 0
$failed = 0

foreach ($file in $files) {
  $relative = $file.FullName.Substring($sourceRoot.Length).TrimStart('\','/')
  if (-not $relative) { $relative = $file.Name }
  $destPath = Join-Path $targetRoot $relative
  $destFolder = Split-Path -Parent $destPath

  if (-not $DryRun) {
    if (-not (Test-Path -LiteralPath $destFolder)) {
      $null = New-Item -Path $destFolder -ItemType Directory -Force
    }
  }

  $shouldCopy = $true
  if (Test-Path -LiteralPath $destPath) {
    $destInfo = Get-Item -LiteralPath $destPath
    if ($destInfo.Length -eq $file.Length -and
        [Math]::Abs(($destInfo.LastWriteTime - $file.LastWriteTime).TotalSeconds) -le 1) {
      $shouldCopy = $false
    }
  }

  if (-not $shouldCopy) {
    $skipped++
    continue
  }

  if ($DryRun) {
    Write-Log "DRYRUN would copy '$($file.FullName)' -> '$destPath'"
    $copied++
    continue
  }

  try {
    Copy-Item -LiteralPath $file.FullName -Destination $destPath -Force
    (Get-Item -LiteralPath $destPath).LastWriteTime = $file.LastWriteTime
    $copied++
  } catch {
    Write-Log "ERROR copying '$($file.FullName)': $_"
    $failed++
  }
}

Write-Log "Copy complete. Copied=$copied Skipped=$skipped Failed=$failed"

if ($failed -gt 0) {
  exit 1
}
