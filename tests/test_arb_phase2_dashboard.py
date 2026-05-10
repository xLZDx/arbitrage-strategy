"""
Phase 2 dashboard tests — opportunities feed + cumulative PnL.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.dashboard.app_arb import create_app
from src.storage import arb_store
from src.utils import config


SEED_TABLE = "opportunities"


def _cleanup():
    arb_store.close()
    d = arb_store.table_dir(SEED_TABLE)
    if d.exists():
        shutil.rmtree(d)


def setup_function(_):
    _cleanup()


def teardown_function(_):
    _cleanup()


def _row(ts, pair, decision, net_bps=10.0, pnl=0.05):
    return {
        "ts": ts, "pair": pair,
        "bybit_mid": 80000.0, "bybit_bid": 79999.5, "bybit_ask": 80000.5,
        "dex_mid": 79900.0,
        "spread_bps": 12.5, "gross_bps": 12.5, "direction": "bybit_high",
        "weighted_obi": 0.1, "obi_delta": 0.01, "cancellation_rate": 0.05,
        "gas_gwei": 0.006, "gas_cost_bps": 0.65,
        "bybit_fee_bps": 10.0, "dex_fee_bps": 5.0,
        "slippage_haircut_bps": 5.0,
        "expected_net_bps": net_bps,
        "notional_usd": 50.0, "theoretical_pnl_usd": pnl,
        "decision": decision, "reason": "passes_threshold" if decision == "GO" else "below_min_net_bps",
        "eth_price_used": 3000.0,
    }


def _seed():
    arb_store.write_records(SEED_TABLE, [
        _row("2026-05-10T12:00:00+00:00", "BTCUSDT", "GO", 12.0, 0.06),
        _row("2026-05-10T12:00:01+00:00", "BTCUSDT", "GO", 8.0, 0.04),
        _row("2026-05-10T12:00:02+00:00", "BTCUSDT", "SKIP", -2.0, -0.01),
    ], pair="BTCUSDT")
    arb_store.write_records(SEED_TABLE, [
        _row("2026-05-10T12:00:00+00:00", "ETHUSDT", "GO", 15.0, 0.075),
        _row("2026-05-10T12:00:03+00:00", "ETHUSDT", "SKIP", 4.0, 0.02),
    ], pair="ETHUSDT")


# --- /api/arb/opportunities -----------------------------------------------


def test_opportunities_empty_when_no_data() -> None:
    client = create_app().test_client()
    r = client.get("/api/arb/opportunities")
    assert r.status_code == 200
    body = r.get_json()
    assert body["opportunities"] == []


def test_opportunities_returns_rows_desc_by_ts() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/opportunities?n=10")
    body = r.get_json()
    rows = body["opportunities"]
    assert len(rows) == 5
    timestamps = [r["ts"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_opportunities_filter_by_decision_go() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/opportunities?decision=GO")
    rows = r.get_json()["opportunities"]
    assert all(row["decision"] == "GO" for row in rows)
    assert len(rows) == 3


def test_opportunities_filter_by_decision_skip() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/opportunities?decision=SKIP")
    rows = r.get_json()["opportunities"]
    assert all(row["decision"] == "SKIP" for row in rows)
    assert len(rows) == 2


def test_opportunities_filter_by_pair() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/opportunities?pair=BTCUSDT")
    rows = r.get_json()["opportunities"]
    assert all(row["pair"] == "BTCUSDT" for row in rows)
    assert len(rows) == 3


def test_opportunities_n_clamped() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/opportunities?n=99999")
    rows = r.get_json()["opportunities"]
    assert len(rows) <= 500  # clamped


def test_opportunities_unknown_pair_filter_ignored() -> None:
    """Unknown pair filter is dropped silently (returns all rows)."""
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/opportunities?pair=XRPUSDT")
    rows = r.get_json()["opportunities"]
    assert len(rows) == 5  # unfiltered


# --- /api/arb/pnl_simulated -----------------------------------------------


def test_pnl_simulated_empty() -> None:
    client = create_app().test_client()
    r = client.get("/api/arb/pnl_simulated")
    assert r.status_code == 200
    body = r.get_json()
    assert body["cumulative"] == 0.0
    assert body["go_count"] == 0
    assert body["skip_count"] == 0
    assert body["by_pair"] == []


def test_pnl_simulated_summary() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/pnl_simulated")
    body = r.get_json()
    # GO: 0.06 + 0.04 + 0.075 = 0.175
    assert abs(body["cumulative"] - 0.175) < 1e-6
    assert body["go_count"] == 3
    assert body["skip_count"] == 2


def test_pnl_simulated_by_pair() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/pnl_simulated")
    body = r.get_json()
    by_pair = {row["pair"]: row for row in body["by_pair"]}
    assert "BTCUSDT" in by_pair
    assert "ETHUSDT" in by_pair
    btc = by_pair["BTCUSDT"]
    assert btc["go_count"] == 2
    assert btc["skip_count"] == 1
    assert abs(btc["cumulative_usd"] - 0.10) < 1e-6
    assert btc["avg_go_net_bps"] == 10.0  # avg(12, 8)
    eth = by_pair["ETHUSDT"]
    assert eth["go_count"] == 1
    assert eth["skip_count"] == 1
    assert abs(eth["cumulative_usd"] - 0.075) < 1e-6


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
