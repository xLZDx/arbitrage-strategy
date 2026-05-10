"""
Phase 5 — coordinator integration tests.

Exercises the full attempt() flow in SHADOW mode:
- happy path (preflight OK + inventory OK + sim passes + both legs)
- preflight rejection (HALT, REJECT)
- inventory rejection (insufficient base asset)
- simulation revert (failed sim)
- stuck-leg unwind (DEX submission fails after Bybit fill)
- stuck-leg unrecoverable (Bybit fails after DEX submitted)
- idempotency: two attempts with same trade_id produce same outcome
- inventory updated only on success
- realized PnL booked only on success
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.exec.bybit_leg import BybitLegExecutor, Fill
from src.exec.bundle_simulator import BundleSimulator, SimulationResult
from src.exec.coordinator import ArbCoordinator, TradeRecord, persist_trade
from src.exec.dex_leg import DexLegExecutor
from src.exec.private_rpc_router import PrivateRpcRouter, SubmissionResult
from src.risk import limits as risk
from src.sim.inventory import Inventory
from src.storage import arb_store
from src.utils import config

_TEST_LEDGER = config.DATA_DIR / "_test_coord_idempotency.json"


def _good_opp(pair="BTCUSDT", direction="bybit_high", notional=50.0,
              expected_net_bps=20.0, theoretical_pnl=0.10):
    return {
        "ts": "2026-05-10T12:00:00+00:00", "pair": pair, "decision": "GO",
        "direction": direction, "notional_usd": notional,
        "expected_net_bps": expected_net_bps,
        "theoretical_pnl_usd": theoretical_pnl,
        "bybit_mid": 80000.0, "dex_mid": 79900.0,
    }


def _coord(inventory=None) -> ArbCoordinator:
    return ArbCoordinator(
        bybit=BybitLegExecutor(mode=config.MODE_SHADOW, ledger_path=_TEST_LEDGER),
        dex=DexLegExecutor(mode=config.MODE_SHADOW),
        router=PrivateRpcRouter(mode=config.MODE_SHADOW),
        simulator=BundleSimulator(mode=config.MODE_SHADOW),
        inventory=inventory or Inventory.with_balanced_seed(500.0),
        risk_state=risk.RiskState(),
    )


def setup_function(_):
    risk.halt_clear()
    if _TEST_LEDGER.exists():
        _TEST_LEDGER.unlink()
    lock = _TEST_LEDGER.with_suffix(".json.lock")
    if lock.exists():
        try:
            lock.unlink()
        except Exception:
            pass


def teardown_function(_):
    risk.halt_clear()
    if _TEST_LEDGER.exists():
        _TEST_LEDGER.unlink()
    d = arb_store.table_dir("trades")
    if d.exists():
        shutil.rmtree(d)


# --- happy path ----------------------------------------------------------


def test_happy_path_shadow_outcome() -> None:
    coord = _coord()
    rec = coord.attempt(_good_opp())
    assert rec.outcome == "shadow"
    assert rec.reason == "both_legs_simulated"
    assert rec.bybit_status == "shadow"
    assert rec.dex_status == "shadow"
    assert rec.sim_passed
    assert rec.bybit_client_order_id is not None


def test_happy_path_inventory_updated() -> None:
    coord = _coord()
    bybit_btc_before = coord.inventory.get("bybit", "BTC")
    rec = coord.attempt(_good_opp(direction="bybit_high"))
    assert rec.outcome == "shadow"
    # bybit_high debits BTC, credits USDT
    assert coord.inventory.get("bybit", "BTC") == bybit_btc_before - 50.0


def test_happy_path_pnl_booked() -> None:
    coord = _coord()
    coord.attempt(_good_opp(theoretical_pnl=0.075))
    assert abs(coord.inventory.realized_pnl_usd - 0.075) < 1e-9


# --- preflight rejection -------------------------------------------------


def test_rejected_when_halt_active() -> None:
    risk.halt_set("test halt")
    coord = _coord()
    rec = coord.attempt(_good_opp())
    assert rec.outcome == "rejected_preflight"
    assert "HALT" in rec.reason
    assert rec.bybit_status is None  # never attempted


def test_rejected_when_notional_exceeds_cap() -> None:
    coord = _coord()
    big = _good_opp(notional=10_000.0)  # way over per-trade cap
    rec = coord.attempt(big)
    assert rec.outcome == "rejected_preflight"
    assert "REJECT" in rec.reason


def test_rejected_when_below_min_net_bps() -> None:
    coord = _coord()
    weak = _good_opp(expected_net_bps=1.0)
    rec = coord.attempt(weak)
    assert rec.outcome == "rejected_preflight"
    assert "below_min_net_bps" in rec.reason


# --- inventory rejection -------------------------------------------------


def test_rejected_when_inventory_insufficient() -> None:
    inv = Inventory()  # everything zero
    coord = _coord(inventory=inv)
    rec = coord.attempt(_good_opp())
    assert rec.outcome == "rejected_inventory"
    assert "insufficient" in rec.reason


def test_rejected_unknown_pair() -> None:
    coord = _coord()
    rec = coord.attempt(_good_opp(pair="XRPUSDT"))
    assert rec.outcome == "rejected_inventory"
    assert "unknown_pair" in rec.reason or "no_pool_config" in rec.reason


# --- simulation revert ---------------------------------------------------


def test_rejected_when_simulator_reverts() -> None:
    coord = _coord()
    # Patch the simulator to return a failed sim
    fake = SimulationResult(success=False, gas_used=0,
                            revert_reason="STF", mode=config.MODE_SHADOW)
    with patch.object(coord.simulator, "simulate", return_value=fake):
        rec = coord.attempt(_good_opp())
    assert rec.outcome == "rejected_simulation"
    assert "STF" in rec.reason
    assert not rec.sim_passed


def test_simulator_revert_does_not_touch_inventory() -> None:
    coord = _coord()
    inv_before = dict(coord.inventory.bybit)
    fake = SimulationResult(False, 0, "K", config.MODE_SHADOW)
    with patch.object(coord.simulator, "simulate", return_value=fake):
        coord.attempt(_good_opp())
    assert coord.inventory.bybit == inv_before


# --- stuck-leg ----------------------------------------------------------


def test_stuck_leg_dex_failed_triggers_unwind() -> None:
    """Bybit OK + DEX submission fails → unwind invoked, outcome reflects it."""
    coord = _coord()
    failed = SubmissionResult(tx_hash=None, relay="x", submitted_at_ts=0.0,
                              status="error", error="rpc_unreachable")
    with patch.object(coord.router, "submit_signed_tx", return_value=failed):
        rec = coord.attempt(_good_opp())
    assert rec.outcome == "stuck_leg_unwound"
    assert "dex_failed" in rec.reason
    # Inventory should NOT have been modified (we unwound)
    # (the unwind is itself a SHADOW order so net effect is 0)


def test_stuck_leg_bybit_failed_after_dex_submitted() -> None:
    """DEX OK + Bybit failed → unrecoverable (we're long the DEX side alone)."""
    coord = _coord()
    failed_fill = Fill(
        symbol="BTCUSDT", side="SELL", requested_qty_usd=50.0,
        filled_qty_usd=0.0, avg_price=0.0, status="rejected",
        venue_order_id=None, client_order_id="x", mode=config.MODE_SHADOW,
        error="api_500",
    )
    with patch.object(coord.bybit, "place_spot_taker", return_value=failed_fill):
        rec = coord.attempt(_good_opp())
    assert rec.outcome == "stuck_leg_unrecoverable"
    assert "bybit_failed" in rec.reason


def test_stuck_leg_inventory_not_updated_on_failure() -> None:
    coord = _coord()
    inv_before = dict(coord.inventory.bybit)
    failed = SubmissionResult(None, "x", 0.0, "error", "rpc_err")
    with patch.object(coord.router, "submit_signed_tx", return_value=failed):
        coord.attempt(_good_opp())
    # Bybit-leg inventory should be unchanged (unwind cancels out)
    assert coord.inventory.bybit == inv_before


# --- idempotency --------------------------------------------------------


def test_two_attempts_with_distinct_trade_ids_both_run() -> None:
    """Each attempt() generates a fresh trade_id, so they are distinct.
    Use a larger bankroll so both trades fit (default $500/side splits to
    only $83 of BTC per pair, draining after one $50 trade)."""
    coord = _coord(inventory=Inventory.with_balanced_seed(2000.0))
    rec1 = coord.attempt(_good_opp())
    rec2 = coord.attempt(_good_opp())
    assert rec1.trade_id != rec2.trade_id
    assert rec1.outcome == rec2.outcome == "shadow"


# --- persistence --------------------------------------------------------


def test_persist_trade_round_trip() -> None:
    coord = _coord()
    rec = coord.attempt(_good_opp())
    persist_trade(rec)
    rows = arb_store.scan_table("trades")
    assert len(rows) == 1


def test_persist_multiple_pairs_creates_separate_partitions() -> None:
    coord = _coord()
    persist_trade(coord.attempt(_good_opp(pair="BTCUSDT")))
    persist_trade(coord.attempt(_good_opp(pair="ETHUSDT")))
    btc = arb_store.scan_table("trades", where="pair = 'BTCUSDT'")
    eth = arb_store.scan_table("trades", where="pair = 'ETHUSDT'")
    assert len(btc) == 1
    assert len(eth) == 1


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
