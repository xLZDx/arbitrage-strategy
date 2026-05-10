"""
Service health snapshot.

Reports liveness of all arbitrage-strategy processes:
  - ingestion (data/arb/pids/ingestion.pid)
  - dashboard (data/arb/pids/dashboard.pid)
  - HALT flag status
  - last data write per table (freshness)

Designed to be called from /api/arb/health (already exists for ingestion only)
and from a future /api/monitor/services aggregator. Keeping it cheap (file
existence + stat) so it can be polled at >1Hz without load.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from src.risk import limits
from src.storage import arb_store
from src.utils import config


@dataclass(frozen=True)
class ServiceState:
    name: str
    pid: int | None
    alive: bool
    last_seen_age_s: float | None  # seconds since PID file mtime; None if no file


def _alive(pid: int) -> bool:
    if pid is None:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _service_state(name: str) -> ServiceState:
    pid_file = config.PIDS_DIR / f"{name}.pid"
    if not pid_file.exists():
        return ServiceState(name=name, pid=None, alive=False, last_seen_age_s=None)
    try:
        pid = int(pid_file.read_text().strip())
    except Exception:
        return ServiceState(name=name, pid=None, alive=False, last_seen_age_s=None)
    age = time.time() - pid_file.stat().st_mtime
    return ServiceState(name=name, pid=pid, alive=_alive(pid),
                        last_seen_age_s=round(age, 1))


def services_snapshot() -> dict:
    """Snapshot of every known service. Cheap; safe to poll often."""
    services = [_service_state("ingestion"), _service_state("dashboard")]
    last_writes = {}
    for table in ("obi_snapshots", "dex_quotes", "gas_history",
                  "opportunities", "sim_trades"):
        last_writes[table] = _last_write_age_s(table)
    return {
        "services": [
            {"name": s.name, "pid": s.pid, "alive": s.alive,
             "last_seen_age_s": s.last_seen_age_s}
            for s in services
        ],
        "halt_active": limits.halt_active(),
        "halt_reason": limits.halt_reason(),
        "mode": config.EXECUTION_MODE,
        "table_freshness_s": last_writes,
    }


def _last_write_age_s(table: str) -> float | None:
    if not arb_store.table_exists(table):
        return None
    glob = list((arb_store.table_dir(table)).rglob("*.parquet"))
    if not glob:
        return None
    youngest = max(p.stat().st_mtime for p in glob)
    return round(time.time() - youngest, 1)
