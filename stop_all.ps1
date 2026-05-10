# stop_all.ps1 — arbitrage_strategy
# Phase 0: empty. Service stops registered as Phases add services.

param(
    [switch]$Verbose
)

$ErrorActionPreference = "Continue"
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] arbitrage_strategy stop_all.ps1 (Phase 0 stub)"

$PidDir = Join-Path $RepoRoot "data\arb\pids"
if (Test-Path $PidDir) {
    Get-ChildItem $PidDir -Filter "*.pid" | ForEach-Object {
        $pidFile = $_.FullName
        $procId = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($procId -and ($procId -match '^\d+$')) {
            try {
                Stop-Process -Id [int]$procId -Force -ErrorAction SilentlyContinue
                Write-Host "  stopped PID $procId ($($_.BaseName))"
            } catch {}
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] No services to stop yet (Phase 0). Done."
