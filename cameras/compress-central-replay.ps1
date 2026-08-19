[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$UploadRoot,
    [switch]$Execute,
    [int]$MaxFiles = 10,
    [double]$MinAgeHours = 24,
    [double]$MinSizeMb = 100,
    [double]$MaxSizeMb = 0,
    [int]$Crf = 30,
    [string]$Preset = "veryfast",
    [string]$Ffmpeg = "ffmpeg",
    [string]$Ffprobe = "ffprobe"
)

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "camera-env.ps1")
$pythonCommand = if ($env:CAMERA_PYTHON) {
    [string[]]@($env:CAMERA_PYTHON)
} else {
    Get-CameraPythonCommand -RepoRoot $repoRoot
}
$scriptPath = Join-Path $PSScriptRoot "compress-central-replay.py"
$arguments = @(
    "--upload-root", $UploadRoot,
    "--max-files", $MaxFiles,
    "--min-age-hours", $MinAgeHours,
    "--min-size-mb", $MinSizeMb,
    "--max-size-mb", $MaxSizeMb,
    "--crf", $Crf,
    "--preset", $Preset,
    "--ffmpeg", $Ffmpeg,
    "--ffprobe", $Ffprobe
)
if ($Execute) { $arguments += "--execute" }

Invoke-CameraPython -PythonCommand $pythonCommand -ScriptPath $scriptPath -Arguments $arguments
exit $LASTEXITCODE
