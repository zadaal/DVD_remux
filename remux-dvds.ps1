# remux-dvds.ps1
# Losslessly remux DVD VIDEO_TS folders to MKV (no re-encode).
# Video stays MPEG-2, audio stays AC-3 -- only the container changes.
#
# USAGE:
#   1. Install ffmpeg and make sure it's on your PATH (test: ffmpeg -version)
#   2. Open PowerShell in the folder that CONTAINS your DVD folders.
#   3. Run:  .\remux-dvds.ps1
#      (If blocked:  powershell -ExecutionPolicy Bypass -File .\remux-dvds.ps1 )

# --- Settings ---------------------------------------------------------------
$Source = Get-Location                       # current directory
$Dest   = '\\diskstation\video\family\family\zada sampled\videos\02-mpeg2'
# ---------------------------------------------------------------------------

if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }

# Find every folder that has a VIDEO_TS subfolder
$videoDirs = Get-ChildItem -Path $Source -Directory -Recurse |
    Where-Object { Test-Path (Join-Path $_.FullName 'VIDEO_TS') }

if (-not $videoDirs) { Write-Host "No VIDEO_TS folders found under $Source"; exit }

foreach ($dir in $videoDirs) {
    $name    = $dir.Name
    $videoTs = Join-Path $dir.FullName 'VIDEO_TS'
    $outFile = Join-Path $Dest "$name.mkv"

    Write-Host "`n=== $name ==="

    if (Test-Path $outFile) { Write-Host "  skip (already exists): $outFile"; continue }

    # Group title VOBs by VTS set (VTS_01_1.VOB ... VTS_01_9.VOB), ignore _0 (menus)
    $vobs = Get-ChildItem -Path $videoTs -Filter 'VTS_*_[1-9].VOB'
    if (-not $vobs) { Write-Host "  no title VOBs found, skipping"; continue }

    $bestSet = $vobs |
        Group-Object { ($_.Name -split '_')[1] } |
        Sort-Object { ($_.Group | Measure-Object Length -Sum).Sum } -Descending |
        Select-Object -First 1

    $ordered = $bestSet.Group | Sort-Object Name
    $concat  = 'concat:' + (($ordered | ForEach-Object { $_.FullName }) -join '|')

    Write-Host ("  remuxing {0} VOB(s) -> {1}" -f $ordered.Count, $outFile)

    ffmpeg -hide_banner -loglevel warning -stats -i $concat -map 0 -c copy "$outFile"

    if ($LASTEXITCODE -eq 0) { Write-Host "  done" } else { Write-Host "  FFMPEG ERROR ($LASTEXITCODE)" }
}

Write-Host "`nAll done."
