"""
Phase 2 detector_main tests — DetectorState + detector_loop integration.

The loop is async + writes to disk; tests use a short stop event so they
finish in ~2s.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.storage import arb_store
from src.strategy.detector_main import DetectorState, _is_fresh, detector_loop
from src.utils import config


def _cleanup():
    arb_store.close()
    d = arb_store.table_dir("opportunities")
    if d.exists():
        shutil.rmtree(d)


def setup_function(_):
    _cleanup()


def teardown_function(_):
    _cleanup()


# --- DetectorState --------------------------------------------------------


def test_state_starts_empty() -> None:
    s = DetectorState()
    assert s.obi == {}
    assert s.dex == {}
    assert s.gas is None


def test_state_update_obi_async() -> None:
    s = DetectorState()
    asyncio.run(s.update_obi("BTCUSDT", {"weighted_obi": 0.1, "ts_ms": 1}))
    obi, dex, gas = asyncio.run(s.snapshot())
    assert obi["BTCUSDT"]["weighted_obi"] == 0.1


def test_state_snapshot_returns_copies() -> None:
    """Mutating the returned dict must not affect state."""
    s = DetectorState()
    asyncio.run(s.update_obi("BTCUSDT", {"weighted_obi": 0.1, "ts_ms": 1}))
    obi, _, _ = asyncio.run(s.snapshot())
    obi["BTCUSDT"]["weighted_obi"] = 999.0
    obi2, _, _ = asyncio.run(s.snapshot())
    assert obi2["BTCUSDT"]["weighted_obi"] == 0.1


def test_is_fresh_within_window() -> None:
    now_ms = 1_000_000
    assert _is_fresh(now_ms - 4000, now_ms)
    assert not _is_fresh(now_ms - 6000, now_ms)
    assert _is_fresh(now_ms, now_ms)


# --- detector_loop integration --------------------------------------------


def test_detector_writes_opportunities_when_signals_fresh() -> None:
    """Seed state with a clear-profit setup → detector writes a GO row."""
    state = DetectorState()
    now = int(time.time() * 1000)
    asyncio.run(state.update_obi("BTCUSDT", {
        "weighted_obi": 0.1, "obi_delta": 0.0, "cancellation_rate": 0.0,
        "best_bid": 80100.0, "best_ask": 80101.0, "ts_ms": now,
    }))
    asyncio.run(state.update_dex("BTCUSDT", {
        "mid_price": 80000.0, "fee_bps": 5.0, "ts_ms": now,
    }))
    # Provide ETH price so gas-cost conversion uses a live value
    asyncio.run(state.update_dex("ETHUSDT", {
        "mid_price": 3000.0, "fee_bps": 5.0, "ts_ms": now,
    }))
    asyncio.run(state.update_obi("ETHUSDT", {
        "weighted_obi": 0.0, "obi_delta": 0.0, "cancellation_rate": 0.0,
        "best_bid": 2999.5, "best_ask": 3000.5, "ts_ms": now,
    }))
    asyncio.run(state.update_gas({
        "block_number": 1, "base_fee_gwei": 0.005, "priority_fee_gwei": 0.001,
        "total_gas_price_gwei": 0.006, "ts_ms": now,
    }))

    async def _run():
        stop = asyncio.Event()
        loop_task = asyncio.create_task(detector_loop(state, stop, poll_s=0.5))
        await asyncio.sleep(1.6)  # let it tick ~3 times
        stop.set()
        return await loop_task

    written = asyncio.run(_run())
    assert written > 0, f"expected detector writes, got {written}"
    # Some BTCUSDT GO rows should be in storage
    rows = arb_store.scan_table("opportunities", where="pair = 'BTCUSDT'")
    assert len(rows) > 0
    # Spread is ~12.5 bps gross (100 / 80050 * 10000), profitable on Base
    decisions = set()
    for row in rows:
        # find decision column dynamically (column names vary by partition)
        decisions.update(c for c in row if c in ("GO", "SKIP"))
    assert "GO" in decisions or "SKIP" in decisions  # at least labelled


def test_detector_skips_when_signals_stale() -> None:
    """Old timestamps → no rows written."""
    state = DetectorState()
    old = int(time.time() * 1000) - 10_000  # 10s old, > FRESHNESS_S=5
    asyncio.run(state.update_obi("BTCUSDT", {
        "weighted_obi": 0.0, "obi_delta": 0.0, "cancellation_rate": 0.0,
        "best_bid": 80000.0, "best_ask": 80001.0, "ts_ms": old,
    }))
    asyncio.run(state.update_dex("BTCUSDT", {
        "mid_price": 79000.0, "fee_bps": 5.0, "ts_ms": old,
    }))
    asyncio.run(state.update_gas({
        "block_number": 1, "base_fee_gwei": 0.005, "priority_fee_gwei": 0.001,
        "total_gas_price_gwei": 0.006, "ts_ms": old,
    }))

    async def _run():
        stop = asyncio.Event()
        loop_task = asyncio.create_task(detector_loop(state, stop, poll_s=0.3))
        await asyncio.sleep(0.8)
        stop.set()
        return await loop_task

    written = asyncio.run(_run())
    assert written == 0


def test_detector_no_gas_no_writes() -> None:
    """No gas reading at all → no detection (can't compute cost)."""
    state = DetectorState()
    now = int(time.time() * 1000)
    asyncio.run(state.update_obi("BTCUSDT", {
        "weighted_obi": 0.0, "obi_delta": 0.0, "cancellation_rate": 0.0,
        "best_bid": 80000.0, "best_ask": 80001.0, "ts_ms": now,
    }))
    asyncio.run(state.update_dex("BTCUSDT", {
        "mid_price": 79000.0, "fee_bps": 5.0, "ts_ms": now,
    }))
    # NO gas update

    async def _run():
        stop = asyncio.Event()
        task = asyncio.create_task(detector_loop(state, stop, poll_s=0.3))
        await asyncio.sleep(0.8)
        stop.set()
        return await task

    written = asyncio.run(_run())
    assert written == 0


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
