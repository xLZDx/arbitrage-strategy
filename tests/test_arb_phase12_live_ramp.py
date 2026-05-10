"""
Phase 12 — live ramp guard tests.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.exec.live_ramp import (
    DRILL_LOG_PATH, LiveRampGuard, RampReadiness,
)
from src.storage import arb_store
from src.utils import config


_DRILL_BACKUP = Path(str(DRILL_LOG_PATH) + ".bak")


def setup_function(_):
    os.environ.pop("ARB_MAINNET_GATE", None)
    if DRILL_LOG_PATH.exists():
        DRILL_LOG_PATH.replace(_DRILL_BACKUP)
    pt = arb_store.table_dir("paper_trades")
    if pt.exists():
        shutil.rmtree(pt)


def teardown_function(_):
    os.environ.pop("ARB_MAINNET_GATE", None)
    if DRILL_LOG_PATH.exists():
        DRILL_LOG_PATH.unlink()
    if _DRILL_BACKUP.exists():
        _DRILL_BACKUP.replace(DRILL_LOG_PATH)
    pt = arb_store.table_dir("paper_trades")
    if pt.exists():
        shutil.rmtree(pt)


def _seed_drill_now() -> None:
    DRILL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DRILL_LOG_PATH.write_text(
        f'{{"ts": "{datetime.now(timezone.utc).isoformat()}", "result": "PASS"}}\n',
        encoding="utf-8",
    )


def _seed_paper_soak(days_span: int, gap_pct: float) -> None:
    """Seed Phase 11 paper_trades table covering N calendar days with the
    requested aggregate |gap| pct."""
    rows = []
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    coord_per = 0.10
    abs_gap_per = coord_per * (gap_pct / 100.0)
    for d in range(days_span + 1):  # inclusive of last day
        ts = base.replace(day=1 + d).isoformat()
        rows.append({
            "ts": ts, "pair": "BTCUSDT", "direction": "bybit_high",
            "notional_usd": 50.0,
            "coordinator_outcome": "shadow",
            "coordinator_pnl_estimate": coord_per,
            "sim_realized_pnl_usd": coord_per - abs_gap_per,
            "pnl_gap_usd": abs_gap_per,
            "pnl_gap_bps": (abs_gap_per / 50.0) * 10_000.0,
            "sim_realized_slippage_bps": 5.0,
            "sim_realized_gas_usd": 0.003,
            "sim_fill_pct": 1.0,
        })
    arb_store.write_records("paper_trades", rows, pair="BTCUSDT")


# --- gating ---------------------------------------------------------------


def test_refuses_when_gate_unset() -> None:
    g = LiveRampGuard(bankroll_per_side_usd=2000.0)
    r = g.check()
    assert not r.ready
    assert any("ARB_MAINNET_GATE" in x for x in r.reasons)


def test_refuses_when_no_drill() -> None:
    os.environ["ARB_MAINNET_GATE"] = "1"
    g = LiveRampGuard(bankroll_per_side_usd=2000.0)
    r = g.check()
    assert not r.ready
    assert any("drill" in x for x in r.reasons)


def test_refuses_when_drill_stale() -> None:
    os.environ["ARB_MAINNET_GATE"] = "1"
    DRILL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DRILL_LOG_PATH.write_text("old\n")
    # Backdate by 48h
    old = time.time() - (48 * 3600)
    os.utime(DRILL_LOG_PATH, (old, old))
    g = LiveRampGuard(bankroll_per_side_usd=2000.0)
    r = g.check()
    assert not r.ready
    assert any("drill" in x for x in r.reasons)


def test_refuses_when_paper_soak_missing() -> None:
    os.environ["ARB_MAINNET_GATE"] = "1"
    _seed_drill_now()
    g = LiveRampGuard(bankroll_per_side_usd=2000.0)
    r = g.check()
    assert not r.ready
    assert any("paper_trades" in x or "soak" in x for x in r.reasons)


def test_refuses_when_paper_soak_too_short() -> None:
    os.environ["ARB_MAINNET_GATE"] = "1"
    _seed_drill_now()
    _seed_paper_soak(days_span=2, gap_pct=10.0)
    g = LiveRampGuard(bankroll_per_side_usd=2000.0)
    r = g.check()
    assert not r.ready
    assert any("days" in x for x in r.reasons)


def test_refuses_when_gap_above_15_pct() -> None:
    os.environ["ARB_MAINNET_GATE"] = "1"
    _seed_drill_now()
    _seed_paper_soak(days_span=8, gap_pct=25.0)  # 25% gap
    g = LiveRampGuard(bankroll_per_side_usd=2000.0)
    r = g.check()
    assert not r.ready
    assert any("gap" in x for x in r.reasons)


def test_refuses_when_bankroll_is_q4_placeholder() -> None:
    os.environ["ARB_MAINNET_GATE"] = "1"
    _seed_drill_now()
    _seed_paper_soak(days_span=8, gap_pct=5.0)
    g = LiveRampGuard(bankroll_per_side_usd=500.0)  # placeholder
    r = g.check()
    assert not r.ready
    assert any("placeholder" in x for x in r.reasons)


def test_ready_when_all_conditions_met() -> None:
    os.environ["ARB_MAINNET_GATE"] = "1"
    _seed_drill_now()
    _seed_paper_soak(days_span=8, gap_pct=5.0)
    g = LiveRampGuard(bankroll_per_side_usd=2000.0)
    r = g.check()
    assert r.ready, f"expected ready, reasons: {r.reasons}"


def test_assert_ready_raises_on_failure() -> None:
    g = LiveRampGuard(bankroll_per_side_usd=2000.0)
    try:
        g.assert_ready()
    except RuntimeError as e:
        assert "refused" in str(e).lower()
        return
    assert False


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
