<# qc-edge-tracewatch-ps5.ps1  (Windows PowerShell 5.1)
   Adds structured logging:
     - events for Created/Changed/Renamed
     - exclusive-open result, file size, lastwrite
     - checksum detection (shows last non-blank line in DEBUG)
     - copy→outbox and upload outcome (status code)
     - midnight/dev sweep summaries
#>

param(
  [string]$Root = "C:\Program Files (x86)\HAMILTON\LogFiles",
  [string]$Api  = "https://collector.local:8443/ingest",
  [string]$Token = "change-me",
  [string]$Outbox = "C:\QC\outbox",
  [string]$StateDir = "C:\QC\state",
  [int]$SettleSeconds = 4,
  [int]$RetryIntervalSeconds = 20,
  [int]$LookbackDays = 2,
  [int]$DevIntervalMinutes = 0,      # >0 => periodic sweep for dev
  [int]$TailBytes = 131072,
  [string]$ChecksumRegex = 'File checksum - written;.*\$\$checksum=[0-9A-Fa-f]+\$\$length=\d+\$+\s*$',
  [string]$LogPath = "C:\QC\logs\tracewatch.log",
  [ValidateSet('INFO','DEBUG')]
  [string]$LogLevel = "INFO"
)

# ---- Setup ---------------------------------------------------------------
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Net.Http
$null = New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
$null = New-Item -ItemType Directory -Force -Path $Outbox | Out-Null
$null = New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$StatePath = Join-Path $StateDir "uploaded.jsonl"
$AgentVer = "tracewatch-ps5-0.2.0"

$script:ChecksumRegex = $ChecksumRegex
$script:TailBytes = $TailBytes  # tail read window in bytes

# ---- Logging helpers -----------------------------------------------------
$__LOG_LEVEL = @{ 'DEBUG'=1; 'INFO'=2 }
$__CUR_LEVEL = $__LOG_LEVEL[$LogLevel]

function Rotate-Log {
  try {
    if (Test-Path $LogPath) {
      $fi = Get-Item $LogPath
      if ($fi.Length -gt 5MB) {
        for ($i=3; $i -ge 1; $i--) {
          $old = "$LogPath.$i"
          $new = "$LogPath." + ($i+1)
          if (Test-Path $old) { Move-Item -Force $old $new }
        }
        Move-Item -Force $LogPath "$LogPath.1"
      }
    }
  } catch { }
}

function Write-Log([string]$Level,[string]$Event,[string]$Msg) {
  if ($__LOG_LEVEL[$Level] -lt $__CUR_LEVEL) { return }
  $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss.fff")
  $line = "$ts [$Level] [$Event] $Msg"
  try {
    Rotate-Log
    Add-Content -Path $LogPath -Value $line
  } catch { }
  # Also echo to console when running interactively
  if (-not $env:SESSIONNAME) { } else { Write-Output $line }
}

Write-Log INFO "START" "TraceWatch agent starting; Root='$Root' Api='$Api' Outbox='$Outbox' DevIntervalMinutes=$DevIntervalMinutes LogLevel=$LogLevel"

# ---- HTTP client ---------------------------------------------------------
$handler = New-Object System.Net.Http.HttpClientHandler
$client  = New-Object System.Net.Http.HttpClient($handler)
if ($Token -and $Token -ne "change-me") {
  $client.DefaultRequestHeaders.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue("Bearer",$Token)
}

# ---- State helpers -------------------------------------------------------
function Get-UploadedSet {
  $set = New-Object 'System.Collections.Generic.HashSet[string]'
  if (Test-Path $StatePath) {
    Get-Content $StatePath -ErrorAction SilentlyContinue | ForEach-Object {
      if ($_ -match '"key":"([^"]+)"') { [void]$set.Add($Matches[1]) }
    }
  }
  return $set
}
function Make-Key([string]$Path,[string]$Sha,[datetime]$LastWriteUtc) {
  return "$Sha|$($Path.ToLower())|$($LastWriteUtc.ToString('o'))"
}

# ---- File I/O helpers ----------------------------------------------------
function Safe-Exclusive([string]$Path) {
  try {
    $fs = [System.IO.File]::Open($Path,'Open','Read','None')
    $fs.Close()
    return $true
  } catch {
    return $false
  }
}

function Has-Checksum([string]$Path, [ref]$LastLine = [ref]$null, [ref]$EncodingUsed = [ref]$null) {
  # Detect checksum line reliably across encodings; safe for PS 5.1
  try {
    $fs = [System.IO.File]::Open($Path,'Open','Read','ReadWrite')  # allow shared read/write
    $len = $fs.Length
    $text = $null
    $encName = ""

    if ($len -le 2097152) {  # <= 2 MB -> full read with BOM detection
      $sr = New-Object System.IO.StreamReader($fs, $true)  # detect BOM
      $text = $sr.ReadToEnd()
      $encName = $sr.CurrentEncoding.WebName
      $sr.Close()
    } else {
      # Tail read
      $start = [Math]::Max(0, $len - $script:TailBytes)
      $fs.Position = $start
      $buf = New-Object byte[] ($len - $start)
      [void]$fs.Read($buf,0,$buf.Length)

      # Try multiple decoders; pick the one with most printable chars
      $candidates = @(
        [System.Text.Encoding]::UTF8,
        [System.Text.Encoding]::Unicode,              # UTF-16 LE
        [System.Text.Encoding]::BigEndianUnicode,     # UTF-16 BE (unlikely)
        [System.Text.Encoding]::GetEncoding(1252)     # Windows-1252
      )
      $best = ""
      $bestScore = -1
      $bestEnc = $null
      foreach ($enc in $candidates) {
        try {
          $s = $enc.GetString($buf)
          $chars = $s.ToCharArray()
          $printable = ($chars | Where-Object { $c = [int]$_; ($c -ge 9 -and $c -ne 10 -and $c -ne 13 -and $c -ne 0 -and $c -lt 127) }).Count
          $score = $printable / [Math]::Max(1, $chars.Length)
          if ($score -gt $bestScore) { $bestScore = $score; $best = $s; $bestEnc = $enc }
        } catch { }
      }
      $text = $best
      if ($bestEnc -ne $null) { $encName = $bestEnc.WebName } else { $encName = "unknown" }
    }
    $fs.Close()

    if (-not $text) { $LastLine.Value = ""; $EncodingUsed.Value = $encName; return $false }

    # Normalize text
    $text = $text -replace "`0", ""                                # strip NULs
    $lines = $text -split "`r`n|`n|`r"                             # universal newline split
    $lines = $lines | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
    if ($lines.Count -eq 0) { $LastLine.Value = ""; $EncodingUsed.Value = $encName; return $false }

    $last = $lines[-1]
    $LastLine.Value  = $last
    $EncodingUsed.Value = $encName

    # Match checksum at end of last non-blank line
    return ($last -match $ChecksumRegex)
  } catch {
    $LastLine.Value = ""
    $EncodingUsed.Value = ""
    return $false
  }
}


function Copy-ToOutbox([string]$SourcePath) {
  $dest = Join-Path $Outbox ([IO.Path]::GetFileName($SourcePath))
  try {
    Copy-Item -LiteralPath $SourcePath -Destination $dest -Force
    return $dest
  } catch {
    Write-Log WARN "COPY_FAIL" "Failed to copy to outbox: '$SourcePath' → '$dest' ($_ )"
    return $null
  }
}

function Upload-File([string]$Path, [ref]$uploadedSet) {
  try { $sha = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash } catch {
    Write-Log WARN "HASH_FAIL" "SHA256 failed for '$Path' ($_ )"
    return $false
  }
  $fi = Get-Item -LiteralPath $Path
  $key = Make-Key $Path $sha $fi.LastWriteTimeUtc
  if ($uploadedSet.Value.Contains($key)) {
    Write-Log DEBUG "SKIP_DUP" "Already uploaded '$Path' key=$key"
    return $true
  }

  # Multipart
  $mp = New-Object System.Net.Http.MultipartFormDataContent
  $meta = @{
    machine      = $env:COMPUTERNAME
    path         = $Path
    bytes        = $fi.Length
    created_utc  = $fi.CreationTimeUtc.ToString("o")
    modified_utc = $fi.LastWriteTimeUtc.ToString("o")
    sha256       = $sha
    agent_ver    = $AgentVer
  } | ConvertTo-Json -Compress
  $mp.Add((New-Object System.Net.Http.StringContent($meta,[Text.Encoding]::UTF8,"application/json")),"meta")
  $fs = [IO.File]::OpenRead($Path)
  $fc = New-Object System.Net.Http.StreamContent($fs)
  $fc.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("application/octet-stream")
  $mp.Add($fc,"file",[IO.Path]::GetFileName($Path))
  try { $client.DefaultRequestHeaders.Remove("Idempotency-Key") } catch {}
  $client.DefaultRequestHeaders.Add("Idempotency-Key",$sha)

  Write-Log INFO "UPLOAD_ATTEMPT" "'$Path' bytes=$($fi.Length) sha=$sha"
  try {
    $resp = $client.PostAsync($Api,$mp).Result
    $code = [int]$resp.StatusCode
    $fs.Dispose()
    if (-not $resp.IsSuccessStatusCode) {
      Write-Log WARN "UPLOAD_FAIL" "'$Path' HTTP $code"
      return $false
    }
    $rec = @{ key=$key; at_utc=(Get-Date).ToUniversalTime().ToString("o") } | ConvertTo-Json -Compress
    Add-Content -Path $StatePath -Value $rec
    [void]$uploadedSet.Value.Add($key)
    Write-Log INFO "UPLOAD_OK" "'$Path' HTTP $code idempotency=$sha"
    return $true
  } catch {
    try { $fs.Dispose() } catch {}
    Write-Log WARN "UPLOAD_EXC" "'$Path' exception: $_"
    return $false
  }
}
# --- Watcher (PS5.1-safe: use $script: scope, no $using:) ---
$watcher = New-Object System.IO.FileSystemWatcher $Root, "*.trc"
$watcher.IncludeSubdirectories = $true
$watcher.NotifyFilter = [IO.NotifyFilters]'FileName, LastWrite, Size'

# Thread-safe "set" for candidate paths
Add-Type -AssemblyName 'System.Collections'
Add-Type -AssemblyName 'System.Core'
$script:candidates = New-Object 'System.Collections.Concurrent.ConcurrentDictionary[string,bool]'

# Expose settle seconds in script scope for the action
$script:SettleSeconds = $SettleSeconds

$action = {
  param($sender, $eventArgs)

  $path = $eventArgs.FullPath
  $evt  = $eventArgs.ChangeType

  if (-not (Test-Path -LiteralPath $path)) { return }

  $fi = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
  Write-Log INFO  "FS_EVENT"  "$evt '$path' bytes=$($fi.Length) lastwrite=$($fi.LastWriteTime.ToString('s'))"

  # Track as candidate
  [void]$script:candidates.TryAdd($path, $true)

  Start-Sleep -Seconds $script:SettleSeconds

  $fi = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
  if ($null -eq $fi) { return }

  # Try exclusive open to infer handle release
  $exclusive = (Safe-Exclusive -Path $path)
  Write-Log DEBUG "EXCLUSIVE" "'$path' exclusive_open=$exclusive size=$($fi.Length)"
  if (-not $exclusive) { return }

  # Check checksum
  $lastLine = ""; $enc = ""
  $done = (Has-Checksum -Path $path -LastLine ([ref]$lastLine) -EncodingUsed ([ref]$enc))
  if ($done) {
    Write-Log INFO  "FINALIZED" "'$path' checksum_detected enc=$enc; last_line='$lastLine'"
  } else {
    Write-Log DEBUG "NOT_FINAL" "'$path' no checksum yet enc=$enc; last_line='$lastLine'"
  }
}

# Re-register handlers using the new $action
$subs = @()
$subs += Register-ObjectEvent -InputObject $watcher -EventName Created -Action $action
$subs += Register-ObjectEvent -InputObject $watcher -EventName Changed -Action $action
$subs += Register-ObjectEvent -InputObject $watcher -EventName Renamed -Action $action
$subs += Register-ObjectEvent -InputObject $watcher -EventName Deleted -Action {
  param($sender, $eventArgs)
  Write-Log DEBUG "FS_EVENT" "Deleted '$($eventArgs.FullPath)'"
}

# ---- Sweeps (dev + midnight) --------------------------------------------
function Sweep-Window([datetime]$StartLocal, [datetime]$EndLocal) {
  $uploaded = Get-UploadedSet
  $files = Get-ChildItem -LiteralPath $Root -Recurse -File -Include *.trc -ErrorAction SilentlyContinue |
           Where-Object { $_.LastWriteTime -ge $StartLocal -and $_.LastWriteTime -lt $EndLocal } |
           Sort-Object LastWriteTime
  Write-Log INFO "SWEEP_START" "window=$($StartLocal.ToString('s')) → $($EndLocal.ToString('s')); candidates=$($files.Count)"
  $ok=0; $fail=0
  foreach ($f in $files) {
  $lastLine=""; $enc=""
    if (Has-Checksum -Path $f.FullName -LastLine ([ref]$lastLine) -EncodingUsed ([ref]$enc)) {
      $copy = Copy-ToOutbox -SourcePath $f.FullName
      if ($null -ne $copy) {
        if (Upload-File -Path $copy -uploadedSet ([ref]$uploaded)) {
          Remove-Item -LiteralPath $copy -Force -ErrorAction SilentlyContinue
          $ok++
        } else { $fail++ }
      } else { $fail++ }
    } else {
    Write-Log DEBUG "SWEEP_SKIP" "'$($f.FullName)' no checksum; enc=$enc; last_line='$lastLine'"
    }
  }
  Write-Log INFO "SWEEP_DONE" "uploaded=$ok failed=$fail"
}

if ($DevIntervalMinutes -gt 0) {
  Write-Log INFO "DEV_LOOP" "Running periodic sweep every $DevIntervalMinutes minute(s)"
  while ($true) {
    $end = Get-Date
    $start = $end.AddMinutes(-$DevIntervalMinutes)
    Sweep-Window -StartLocal $start -EndLocal $end
    Start-Sleep -Seconds ([int]([TimeSpan]::FromMinutes($DevIntervalMinutes)).TotalSeconds)
  }
} else {
  Write-Log INFO "WATCH" "Event-driven watch active; midnight sweep enabled"
  while ($true) {
    $now = Get-Date
    if ($now.Hour -eq 0 -and $now.Minute -ge 10 -and $now.Minute -lt 15) {
      $y0 = (Get-Date -Date $now.Date).AddDays(-1)
      $y1 = $now.Date
      $start = $y0.AddDays(-([Math]::Max(0,$LookbackDays-1)))
      Sweep-Window -StartLocal $start -EndLocal $y1
      Start-Sleep -Seconds 900
    }
    Start-Sleep -Seconds $RetryIntervalSeconds
  }
}
