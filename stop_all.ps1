# stop_all.ps1 — arbitrage_strategy
#
# Stops ingestion + dashboard via:
#   1. PID files in data/arb/pids/ (best-effort — PIDs are non-authoritative)
#   2. CommandLine match for any python.exe spawned from this repo's venv
#      running our entry modules (authoritative — catches orphans)
#
# Only touches processes from D:\test 2\arbitrage_strategy\venv\.
# Trading-bot processes (D:\test 2\AI trading assistance\venv\) are left alone.

param(
    [switch]$Verbose
)

$ErrorActionPreference = "Continue"
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] arbitrage_strategy stop_all.ps1"

$PidDir = Join-Path $RepoRoot "data\arb\pids"

# 1. PID-file based stops (fast path)
if (Test-Path $PidDir) {
    Get-ChildItem $PidDir -Filter "*.pid" -ErrorAction SilentlyContinue | ForEach-Object {
        $pidFile = $_.FullName
        $procId = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($procId -and ($procId -match '^\d+$')) {
            try {
                Stop-Process -Id ([int]$procId) -Force -ErrorAction SilentlyContinue
                if ($Verbose) { Write-Host "  PID-file stopped $procId ($($_.BaseName))" }
            } catch {}
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
}

# 2. CommandLine-match orphan reaper (authoritative)
$repoEsc = $RepoRoot -replace '\\', '\\\\'
$matches = Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match "$repoEsc" }
$killed = 0
foreach ($p in $matches) {
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
        if ($Verbose) { Write-Host "  orphan-killed PID $($p.ProcessId)" }
    } catch {}
}

if ($killed -gt 0) {
    Write-Host "  reaped $killed orphan process(es)"
    Start-Sleep -Milliseconds 400
}

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] stop_all done."
