# restart_all.ps1 — arbitrage_strategy
#
# Stops everything via stop_all.ps1, then launches Phase 1 services:
#   - ingestion process (bybit L2 WS + Uniswap V3 + gas)
#   - dashboard standalone Flask app on port 5001
#
# PIDs tracked in data/arb/pids/<service>.pid
# Logs to logs/arb_<service>_<date>.log
#
# Usage: ./restart_all.ps1 [-Verbose]

param(
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
$Python = Join-Path $RepoRoot "venv\Scripts\python.exe"
$LogDir = Join-Path $RepoRoot "logs"
$PidDir = Join-Path $RepoRoot "data\arb\pids"

Set-Location $RepoRoot
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $PidDir | Out-Null

if (-not (Test-Path $Python)) {
    Write-Host "[ERROR] venv missing: $Python" -ForegroundColor Red
    Write-Host "Run: py -3.14 -m venv venv && ./venv/Scripts/python.exe -m pip install --no-cache-dir -e ."
    exit 1
}

$Date = Get-Date -Format "yyyy-MM-dd"
Write-Host "[$(Get-Date -Format 'HH:mm:ss')] arbitrage_strategy restart_all.ps1"

# --- Stop everything first ---
& "$RepoRoot\stop_all.ps1"
Start-Sleep -Milliseconds 300

# --- Launch ingestion ---
$IngestLog = Join-Path $LogDir "arb_ingestion_$Date.log"
$IngestPid = Join-Path $PidDir "ingestion.pid"
Write-Host "  starting ingestion -> $IngestLog"
$ingestProc = Start-Process -FilePath $Python `
    -ArgumentList "-m", "src.data.ingestion_main" `
    -WorkingDirectory $RepoRoot `
    -RedirectStandardOutput $IngestLog `
    -RedirectStandardError "$IngestLog.err" `
    -WindowStyle Hidden `
    -PassThru
$ingestProc.Id | Out-File -Encoding ascii -NoNewline $IngestPid
Write-Host "    PID $($ingestProc.Id)"

# --- Launch dashboard (standalone, port 5001) ---
$DashLog = Join-Path $LogDir "arb_dashboard_$Date.log"
$DashPid = Join-Path $PidDir "dashboard.pid"
Write-Host "  starting dashboard on :5001 -> $DashLog"
$dashProc = Start-Process -FilePath $Python `
    -ArgumentList "-m", "src.dashboard.app_arb", "--port", "5001", "--host", "127.0.0.1" `
    -WorkingDirectory $RepoRoot `
    -RedirectStandardOutput $DashLog `
    -RedirectStandardError "$DashLog.err" `
    -WindowStyle Hidden `
    -PassThru
$dashProc.Id | Out-File -Encoding ascii -NoNewline $DashPid
Write-Host "    PID $($dashProc.Id)"

Start-Sleep -Seconds 2

# --- Smoke check ---
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:5001/api/arb/health" `
        -UseBasicParsing -TimeoutSec 5
    if ($resp.StatusCode -eq 200) {
        $body = $resp.Content | ConvertFrom-Json
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] dashboard up: mode=$($body.mode) ingestion=$($body.ingestion_running) halt=$($body.halt_active)" -ForegroundColor Green
    } else {
        Write-Host "[WARN] dashboard returned HTTP $($resp.StatusCode)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "[WARN] dashboard health check failed: $_" -ForegroundColor Yellow
}

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] all services launched. Open http://127.0.0.1:5001/"
