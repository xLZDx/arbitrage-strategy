"""
Phase 4 — ops scaffolding tests (inventory_manager, health).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.ops import health as ops_health
from src.ops.inventory_manager import (
    ALERT_THRESHOLD, HALT_THRESHOLD, InventoryManager,
)
from src.risk import limits as risk
from src.sim.inventory import Inventory
from src.utils import config


def setup_function(_):
    risk.halt_clear()


def teardown_function(_):
    risk.halt_clear()


# --- InventoryManager ----------------------------------------------------


def test_inventory_manager_no_alert_when_balanced() -> None:
    mgr = InventoryManager()
    inv = Inventory.with_balanced_seed(500.0)
    assert mgr.check(inv) is None


def test_inventory_manager_warns_above_alert_threshold() -> None:
    mgr = InventoryManager()
    inv = Inventory.with_initial_usd(500.0)
    inv.adjust("bybit", "USDT", -300.0)  # imbalance ≈ 0.43
    alert = mgr.check(inv)
    assert alert is not None
    assert alert.severity in ("WARN", "HALT")


def test_inventory_manager_halt_severity_above_25_pct() -> None:
    mgr = InventoryManager()
    inv = Inventory.with_initial_usd(500.0)
    inv.adjust("bybit", "USDT", -400.0)  # bybit=$100 dex=$500, imbalance=0.667
    alert = mgr.check(inv)
    assert alert is not None
    assert alert.imbalance > HALT_THRESHOLD
    assert alert.severity == "HALT"


def test_inventory_manager_warn_severity_between_thresholds() -> None:
    """Build an inventory where ALERT < imbalance < HALT."""
    mgr = InventoryManager()
    # bybit $400, dex $500: imbalance = 100/900 ≈ 0.111 — too low.
    # bybit $300, dex $500: imbalance = 200/800 = 0.25 — exactly HALT threshold.
    # bybit $350, dex $500: imbalance = 150/850 ≈ 0.176 — too low.
    # bybit $325, dex $500: imbalance = 175/825 ≈ 0.212 — between thresholds.
    inv = Inventory()
    inv.bybit["USDT"] = 325.0
    inv.dex["USDC"] = 500.0
    alert = mgr.check(inv)
    assert alert is not None
    assert ALERT_THRESHOLD <= alert.imbalance <= HALT_THRESHOLD
    assert alert.severity == "WARN"


def test_inventory_manager_cooldown_blocks_repeat() -> None:
    mgr = InventoryManager()
    inv = Inventory.with_initial_usd(500.0)
    inv.adjust("bybit", "USDT", -400.0)
    a1 = mgr.check(inv)
    a2 = mgr.check(inv)  # immediate retry
    assert a1 is not None
    assert a2 is None  # blocked by cooldown


# --- health snapshot ------------------------------------------------------


def test_services_snapshot_returns_known_services() -> None:
    snap = ops_health.services_snapshot()
    names = {s["name"] for s in snap["services"]}
    assert names == {"ingestion", "dashboard"}


def test_services_snapshot_includes_halt_state() -> None:
    snap = ops_health.services_snapshot()
    assert "halt_active" in snap
    assert snap["halt_active"] is False


def test_services_snapshot_reflects_halt_set() -> None:
    risk.halt_set("ops snapshot test")
    snap = ops_health.services_snapshot()
    assert snap["halt_active"] is True
    assert "ops snapshot test" in (snap["halt_reason"] or "")


def test_services_snapshot_dead_pid_reports_alive_false() -> None:
    pid_file = config.PIDS_DIR / "ingestion.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(9_999_999))  # very unlikely PID
    try:
        snap = ops_health.services_snapshot()
        ingest = next(s for s in snap["services"] if s["name"] == "ingestion")
        assert ingest["pid"] == 9_999_999
        assert ingest["alive"] is False
    finally:
        pid_file.unlink(missing_ok=True)


def test_services_snapshot_self_pid_alive() -> None:
    pid_file = config.PIDS_DIR / "ingestion.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    try:
        snap = ops_health.services_snapshot()
        ingest = next(s for s in snap["services"] if s["name"] == "ingestion")
        assert ingest["alive"] is True
    finally:
        pid_file.unlink(missing_ok=True)


def test_services_snapshot_table_freshness_present() -> None:
    snap = ops_health.services_snapshot()
    # Keys exist for every monitored table; values may be None when no data
    expected = {"obi_snapshots", "dex_quotes", "gas_history",
                "opportunities", "sim_trades"}
    assert set(snap["table_freshness_s"].keys()) == expected


def _run_all() -> int:
    failures: list[tuple[str, str]] = []
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            setup_function(None)
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
        finally:
            teardown_function(None)
    print()
    if failures:
        print(f"{len(failures)} / {len(tests)} FAILED")
        return 1
    print(f"{len(tests)} / {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
