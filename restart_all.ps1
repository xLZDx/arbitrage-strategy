# restart_all.ps1 — arbitrage_strategy
# Phase 0: empty. Service registrations land in Phases 1, 2, 5.
#
# Conventions matching sister project:
#   - Each service launched in its own process
#   - PIDs tracked in data/arb/pids/<service>.pid
#   - Logs to logs/arb_<service>_<date>.jsonl
#   - Always run from repo root: D:\test 2\arbitrage_strategy

param(
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] arbitrage_strategy restart_all.ps1 (Phase 0 stub)"

# --- Stop everything first ---
& "$RepoRoot\stop_all.ps1"

# --- Service launches (extended per phase) ---

# Phase 1: ingestion process
# Start-Process -FilePath "$RepoRoot\venv\Scripts\python.exe" `
#               -ArgumentList "-m", "src.data.ingestion_main" `
#               -WorkingDirectory $RepoRoot `
#               -WindowStyle Hidden `
#               -RedirectStandardOutput "$RepoRoot\logs\arb_ingestion_$(Get-Date -Format 'yyyy-MM-dd').jsonl"

# Phase 2: opportunity detector

# Phase 5: executor

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] No services registered yet (Phase 0). Done."
