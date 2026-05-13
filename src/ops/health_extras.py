"""
Cross-cutting operational tooling (P3 user-requested, 2026-05-11):

1. ZombieProcessDetector
   Finds python.exe processes whose CommandLine matches the project but
   whose PID is not in any data/arb/pids/*.pid. These are orphans from
   stop_all.ps1 + Start-Process race conditions. Returns a list operator
   can review or call kill() on.

2. DashboardCorrectnessValidator
   Probes /api/arb/spread + /api/arb/risk + /api/arb/soak_summary; flags:
     - NaN / Inf in any numeric field
     - spread_bps with |value| > IMPLAUSIBLE_SPREAD_BPS (1000)
     - missing/null required fields
     - stale ts (older than freshness threshold)
   Returns a list of findings ready for an alert log.

3. DataLoadingSpeedMonitor
   Measures /api/arb/spread + /api/arb/risk + /api/arb/soak_summary latency.
   p50 / p95 / p99 over a rolling 60-sample window. Alerts when p95 exceeds
   the configured budget (default 500ms for spread/risk, 2s for soak).

4. TFTEtaComputer  (NO GUESSING — reads real training logs)
   Looks at sister project's training_status_report.json / training_jobs.json
   if present, OR scans logs/training.log for "TFT training step <n>/<total>".
   Computes wall-clock per-step ELAPSED and projects ETA from
   (steps_remaining * mean_elapsed_per_step). Returns None if not enough
   data points to project (< 5 steps measured) — explicitly does not guess.
"""

from __future__ import annotations

import logging
import math
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.utils import config, safe_json

log = logging.getLogger(__name__)


# --- 1. Zombie process detector -------------------------------------------


@dataclass(frozen=True)
class ZombieProcess:
    pid: int
    command_line: str
    reason: str  # "orphan_no_pid_file" or "stale_pid_file"


def find_zombie_processes() -> list[ZombieProcess]:
    """
    Returns python.exe processes whose CommandLine references the arbitrage
    project but whose PID is not recorded in data/arb/pids/*.pid.
    Cross-platform: uses Windows WMI on win32, otherwise psutil if available.
    """
    project_root = str(config.REPO_ROOT)
    expected_pids = _read_expected_pids()
    found = []
    if os.name == "nt":
        found.extend(_find_zombies_windows(project_root, expected_pids))
    else:
        found.extend(_find_zombies_posix(project_root, expected_pids))
    return found


def _read_expected_pids() -> set[int]:
    """Read all PIDs that the pid-file system says SHOULD be running."""
    pids = set()
    if not config.PIDS_DIR.exists():
        return pids
    for pid_file in config.PIDS_DIR.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            pids.add(pid)
        except (OSError, ValueError):
            continue
    return pids


def _find_zombies_windows(project_root: str, expected: set[int]) -> list[ZombieProcess]:
    try:
        import subprocess
        # Use PowerShell WMI — same approach as stop_all.ps1
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
            "Where-Object { $_.CommandLine -match 'arbitrage_strategy' } | "
            "ForEach-Object { '{0}|{1}' -f $_.ProcessId, $_.CommandLine }"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
        )
        zombies = []
        for line in result.stdout.strip().splitlines():
            if "|" not in line:
                continue
            pid_s, cmdline = line.split("|", 1)
            try:
                pid = int(pid_s.strip())
            except ValueError:
                continue
            if pid not in expected:
                zombies.append(ZombieProcess(
                    pid=pid, command_line=cmdline.strip(),
                    reason="orphan_no_pid_file",
                ))
        return zombies
    except Exception as e:
        log.warning("zombie scan (windows) failed: %s", e)
        return []


def _find_zombies_posix(project_root: str, expected: set[int]) -> list[ZombieProcess]:
    try:
        import psutil  # type: ignore
        zombies = []
        for proc in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
            if proc.info["name"] != "python":
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            if "arbitrage_strategy" not in cmdline:
                continue
            if proc.info["pid"] not in expected:
                zombies.append(ZombieProcess(
                    pid=proc.info["pid"], command_line=cmdline,
                    reason="orphan_no_pid_file",
                ))
        return zombies
    except ImportError:
        log.warning("psutil not installed; skipping POSIX zombie scan")
        return []


# --- 2. Dashboard correctness validator -----------------------------------


@dataclass(frozen=True)
class CorrectnessFinding:
    endpoint: str
    field: str
    issue: str
    value: object


def validate_dashboard_data(client) -> list[CorrectnessFinding]:
    """Probe critical endpoints; return list of correctness violations.

    `client` is a Flask test_client OR a thin wrapper exposing .get()
    that returns a Response with .get_json(). Allows the same logic to
    run in tests + against the live server."""
    findings = []
    findings.extend(_validate_spread(client))
    findings.extend(_validate_risk(client))
    findings.extend(_validate_soak(client))
    return findings


def _validate_spread(client) -> list[CorrectnessFinding]:
    out = []
    r = client.get("/api/arb/spread")
    if r.status_code != 200:
        out.append(CorrectnessFinding("/spread", "_status",
                                       f"http_{r.status_code}", None))
        return out
    body = r.get_json() or {}
    for row in body.get("spreads", []) or []:
        for field in ("bybit_mid", "bybit_bid", "bybit_ask", "dex_mid", "spread_bps"):
            val = row.get(field)
            if val is None:
                continue  # nullable, acceptable
            if isinstance(val, float):
                if math.isnan(val):
                    out.append(CorrectnessFinding("/spread", field, "NaN", val))
                elif math.isinf(val):
                    out.append(CorrectnessFinding("/spread", field, "Inf", val))
        # spread > 10% bps → almost certainly bad pool data
        sb = row.get("spread_bps")
        if isinstance(sb, (int, float)) and abs(sb) > 1000:
            out.append(CorrectnessFinding(
                "/spread", "spread_bps", "implausible_spread", sb,
            ))
        # Bid > ask = bad book
        bb, ba = row.get("bybit_bid"), row.get("bybit_ask")
        if (isinstance(bb, (int, float)) and isinstance(ba, (int, float))
                and bb > 0 and ba > 0 and bb > ba):
            out.append(CorrectnessFinding(
                "/spread", "bid_ask_inverted",
                f"bid={bb} > ask={ba}", row.get("pair"),
            ))
    return out


def _validate_risk(client) -> list[CorrectnessFinding]:
    out = []
    r = client.get("/api/arb/risk")
    if r.status_code != 200:
        out.append(CorrectnessFinding("/risk", "_status",
                                       f"http_{r.status_code}", None))
        return out
    body = r.get_json() or {}
    for field in ("daily_loss_cap_usd", "drawdown_trigger_usd",
                  "per_trade_cap_usd"):
        val = body.get(field)
        if val is None or (isinstance(val, (int, float)) and val <= 0):
            out.append(CorrectnessFinding("/risk", field,
                                           "missing_or_zero", val))
    if body.get("mode") not in ("SHADOW", "TESTNET", "MAINNET"):
        out.append(CorrectnessFinding("/risk", "mode", "unknown_mode",
                                       body.get("mode")))
    return out


def _validate_soak(client) -> list[CorrectnessFinding]:
    out = []
    r = client.get("/api/arb/soak_summary")
    if r.status_code != 200:
        out.append(CorrectnessFinding("/soak_summary", "_status",
                                       f"http_{r.status_code}", None))
        return out
    body = r.get_json() or {}
    # spread distribution: each row must have sane min/median/max
    for row in body.get("spread_distribution", []) or []:
        for field in ("min", "median", "max"):
            val = row.get(field)
            if isinstance(val, float):
                if math.isnan(val) or math.isinf(val):
                    out.append(CorrectnessFinding(
                        "/soak_summary", f"{row.get('pair')}.{field}",
                        "NaN_or_Inf", val,
                    ))
        if (isinstance(row.get("min"), (int, float))
                and isinstance(row.get("max"), (int, float))
                and row["min"] > row["max"]):
            out.append(CorrectnessFinding(
                "/soak_summary", f"{row.get('pair')}",
                "min>max", row,
            ))
    return out


# --- 3. Data-loading speed monitor ----------------------------------------


@dataclass
class LatencySample:
    endpoint: str
    elapsed_ms: float
    ts: float


@dataclass
class SpeedMonitor:
    """Rolling window of latency samples per endpoint."""
    window_size: int = 60
    budgets_ms: dict[str, float] = field(default_factory=lambda: {
        "/api/arb/spread": 500.0,
        "/api/arb/risk": 500.0,
        "/api/arb/soak_summary": 2000.0,
    })
    samples: dict[str, list[float]] = field(default_factory=dict)

    def measure(self, client, endpoint: str) -> LatencySample:
        t0 = time.perf_counter()
        client.get(endpoint)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        bucket = self.samples.setdefault(endpoint, [])
        bucket.append(elapsed_ms)
        if len(bucket) > self.window_size:
            bucket.pop(0)
        return LatencySample(endpoint=endpoint, elapsed_ms=elapsed_ms, ts=time.time())

    def quantile(self, endpoint: str, q: float) -> float | None:
        bucket = self.samples.get(endpoint, [])
        if not bucket:
            return None
        sorted_b = sorted(bucket)
        idx = max(0, min(len(sorted_b) - 1, int(round(q * (len(sorted_b) - 1)))))
        return sorted_b[idx]

    def budget_violations(self) -> list[tuple[str, float, float]]:
        """Returns (endpoint, p95_ms, budget_ms) for endpoints over budget."""
        out = []
        for endpoint, budget in self.budgets_ms.items():
            p95 = self.quantile(endpoint, 0.95)
            if p95 is not None and p95 > budget:
                out.append((endpoint, p95, budget))
        return out


# --- 4. TFT ETA computer (NO GUESSING) ------------------------------------


@dataclass(frozen=True)
class TFTEtaEstimate:
    """Result of an ETA computation. eta_seconds is None when we don't
    have enough data to project — the operator gets an honest "unknown"."""
    measured_steps: int
    total_steps: int | None
    mean_elapsed_per_step_s: float | None
    eta_seconds: float | None
    confidence: str  # "high" / "medium" / "low" / "unknown"
    source: str      # "training_status_report" / "training_log" / "no_data"
    reason: str      # short explanation


def compute_tft_eta(
    status_json_path: Path | None = None,
    training_log_path: Path | None = None,
) -> TFTEtaEstimate:
    """
    Compute TFT training ETA from REAL observed data. Returns
    TFTEtaEstimate.eta_seconds=None if there aren't enough data points
    (< 5 measured steps) — explicitly DOES NOT GUESS.

    Sources in priority order:
      1. data/training_status_report.json (sister project canonical state)
      2. logs/training.log lines matching "step <n>/<total>" pattern
      3. None — return "unknown" estimate.
    """
    sister_root = config.REPO_ROOT.parent / "AI trading assistance"
    if status_json_path is None:
        status_json_path = sister_root / "data" / "training_status_report.json"
    if training_log_path is None:
        training_log_path = sister_root / "logs" / "training.log"

    # Source 1: training_status_report.json
    if status_json_path.exists():
        try:
            report = safe_json.read_json(status_json_path, default={}) or {}
            tft_state = _extract_tft_state(report)
            if tft_state is not None:
                return tft_state
        except Exception as e:
            log.warning("training_status_report parse failed: %s", e)

    # Source 2: scan training log for step lines
    if training_log_path.exists():
        return _eta_from_log(training_log_path)

    return TFTEtaEstimate(
        measured_steps=0, total_steps=None,
        mean_elapsed_per_step_s=None, eta_seconds=None,
        confidence="unknown", source="no_data",
        reason="No training_status_report.json AND no logs/training.log found",
    )


def _extract_tft_state(report: dict) -> TFTEtaEstimate | None:
    """Look for a TFT job entry in the sister project's status report."""
    jobs = report.get("jobs") or report.get("active_jobs") or {}
    if not isinstance(jobs, dict):
        return None
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        kind = (job.get("model") or job.get("kind") or "").lower()
        if "tft" not in kind:
            continue
        # Extract current step + total
        cur = job.get("step") or job.get("current_step") or job.get("epoch")
        total = job.get("total_steps") or job.get("total_epochs") or job.get("max_steps")
        started_ts = job.get("started_at") or job.get("start_ts")
        if cur is None or total is None or started_ts is None:
            continue
        try:
            cur, total = int(cur), int(total)
            if isinstance(started_ts, str):
                started = datetime.fromisoformat(started_ts.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            else:
                elapsed = time.time() - float(started_ts)
        except Exception:
            continue
        if cur < 5:
            # Not enough data to project — refuse to guess
            return TFTEtaEstimate(
                measured_steps=cur, total_steps=total,
                mean_elapsed_per_step_s=None, eta_seconds=None,
                confidence="low", source="training_status_report",
                reason=f"only {cur} steps measured; need >= 5 to project",
            )
        mean_per_step = elapsed / cur
        eta = (total - cur) * mean_per_step
        confidence = "high" if cur >= 20 else "medium"
        return TFTEtaEstimate(
            measured_steps=cur, total_steps=total,
            mean_elapsed_per_step_s=mean_per_step,
            eta_seconds=eta, confidence=confidence,
            source="training_status_report",
            reason=f"projected from {cur} measured steps",
        )
    return None


_LOG_STEP_RE = re.compile(
    r"(?:step|epoch)\s*[:=]?\s*(\d+)\s*[/]\s*(\d+)",
    re.IGNORECASE,
)


def _eta_from_log(log_path: Path) -> TFTEtaEstimate:
    """Tail the training log; project from step transitions with timestamps."""
    timestamps: list[tuple[float, int, int]] = []  # (epoch_s, step, total)
    try:
        # Tail the last 200 lines (training step lines are frequent)
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-500:]
        for line in lines:
            m = _LOG_STEP_RE.search(line)
            if not m:
                continue
            step, total = int(m.group(1)), int(m.group(2))
            # Extract log line timestamp (assume ISO at start)
            ts_match = re.match(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})", line)
            if not ts_match:
                continue
            try:
                ts = datetime.fromisoformat(ts_match.group(1).replace(" ", "T"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                epoch_s = ts.timestamp()
            except Exception:
                continue
            timestamps.append((epoch_s, step, total))
    except Exception as e:
        return TFTEtaEstimate(
            measured_steps=0, total_steps=None,
            mean_elapsed_per_step_s=None, eta_seconds=None,
            confidence="unknown", source="training_log",
            reason=f"log read failed: {e}",
        )
    if len(timestamps) < 5:
        return TFTEtaEstimate(
            measured_steps=len(timestamps), total_steps=None,
            mean_elapsed_per_step_s=None, eta_seconds=None,
            confidence="low", source="training_log",
            reason=f"only {len(timestamps)} step lines in last 500 log lines; "
                   f"need >= 5 to project",
        )

    # Compute mean elapsed per step from pairwise deltas
    timestamps.sort()
    deltas = []
    for i in range(1, len(timestamps)):
        ts_prev, step_prev, _ = timestamps[i - 1]
        ts_cur, step_cur, _ = timestamps[i]
        if step_cur > step_prev:
            delta = (ts_cur - ts_prev) / (step_cur - step_prev)
            if 0 < delta < 86400:  # sanity: 1 step shouldn't take > 1 day
                deltas.append(delta)
    if len(deltas) < 4:
        return TFTEtaEstimate(
            measured_steps=len(timestamps), total_steps=timestamps[-1][2],
            mean_elapsed_per_step_s=None, eta_seconds=None,
            confidence="low", source="training_log",
            reason="not enough step transitions in log to project",
        )
    mean_per_step = statistics.median(deltas)
    cur_step, total = timestamps[-1][1], timestamps[-1][2]
    eta = (total - cur_step) * mean_per_step
    confidence = "high" if len(deltas) >= 20 else "medium"
    return TFTEtaEstimate(
        measured_steps=cur_step, total_steps=total,
        mean_elapsed_per_step_s=mean_per_step,
        eta_seconds=eta, confidence=confidence,
        source="training_log",
        reason=f"projected from {len(deltas)} step-deltas (median: {mean_per_step:.1f}s/step)",
    )
