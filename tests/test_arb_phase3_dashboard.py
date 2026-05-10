"""
Phase 3 dashboard tests — sim_trades + sim_summary endpoints.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.dashboard.app_arb import create_app
from src.storage import arb_store


SEED_TABLE = "sim_trades"


def _cleanup():
    arb_store.close()
    d = arb_store.table_dir(SEED_TABLE)
    if d.exists():
        shutil.rmtree(d)


def setup_function(_):
    _cleanup()


def teardown_function(_):
    _cleanup()


def _trade(ts, pair, pnl=0.05, net_bps=10.0, exp_bps=12.0,
           filled=True, direction="bybit_high"):
    return {
        "ts": ts, "pair": pair, "decision": "GO", "direction": direction,
        "notional_usd": 50.0,
        "spread_bps": 25.0, "expected_net_bps": exp_bps,
        "realized_slippage_bps": 5.0, "realized_gas_usd": 0.003,
        "realized_pnl_usd": pnl, "realized_net_bps": net_bps,
        "fill_pct": 1.0 if filled else 0.0,
        "inventory_ok": filled, "inventory_reason": "ok" if filled else "insufficient_x",
        "bybit_usdt_after": 100.0, "dex_usdc_after": 100.0,
        "portfolio_usd_after": 1000.0,
    }


def _seed():
    arb_store.write_records(SEED_TABLE, [
        _trade("2026-05-10T12:00:00+00:00", "BTCUSDT", pnl=+0.10, net_bps=20.0, exp_bps=15.0),
        _trade("2026-05-10T12:00:01+00:00", "BTCUSDT", pnl=-0.02, net_bps=-4.0, exp_bps=10.0),
        _trade("2026-05-10T12:00:02+00:00", "BTCUSDT", pnl=0.0, net_bps=0.0, filled=False),
    ], pair="BTCUSDT")
    arb_store.write_records(SEED_TABLE, [
        _trade("2026-05-10T12:00:00+00:00", "ETHUSDT", pnl=+0.07, net_bps=14.0, exp_bps=14.0),
    ], pair="ETHUSDT")


# --- /api/arb/sim_trades ---------------------------------------------------


def test_sim_trades_empty_when_no_data() -> None:
    client = create_app().test_client()
    r = client.get("/api/arb/sim_trades")
    assert r.status_code == 200
    assert r.get_json()["trades"] == []


def test_sim_trades_returns_rows_desc() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/sim_trades")
    rows = r.get_json()["trades"]
    assert len(rows) == 4
    timestamps = [row["ts"] for row in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_sim_trades_filter_by_pair() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/sim_trades?pair=ETHUSDT")
    rows = r.get_json()["trades"]
    assert all(row["pair"] == "ETHUSDT" for row in rows)
    assert len(rows) == 1


def test_sim_trades_n_clamped() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/sim_trades?n=99999")
    assert len(r.get_json()["trades"]) <= 500


# --- /api/arb/sim_summary --------------------------------------------------


def test_sim_summary_empty() -> None:
    client = create_app().test_client()
    r = client.get("/api/arb/sim_summary")
    body = r.get_json()
    assert body["n_trades"] == 0
    assert body["cumulative_pnl_usd"] == 0.0
    assert body["by_pair"] == []


def test_sim_summary_aggregates() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/sim_summary")
    body = r.get_json()
    assert body["n_trades"] == 4
    assert body["n_filled"] == 3   # 1 BTC inv-rejected
    assert body["n_inventory_rejected"] == 1
    # Sum: 0.10 - 0.02 + 0 + 0.07 = 0.15
    assert abs(body["cumulative_pnl_usd"] - 0.15) < 1e-6
    # 2 wins (0.10, 0.07) + 1 loss (-0.02) out of 3 filled = 2/3 ≈ 0.667
    assert 0.66 < body["hit_rate"] < 0.67
    # avg realized vs theoretical:
    # realized = (20 - 4 + 14) / 3 = 10.0
    # expected = (15 + 10 + 14) / 3 = 13.0
    # gap = -3.0
    assert abs(body["avg_realized_net_bps"] - 10.0) < 0.01
    assert abs(body["avg_theoretical_net_bps"] - 13.0) < 0.01
    assert abs(body["realized_vs_theoretical_gap_bps"] - (-3.0)) < 0.01


def test_sim_summary_by_pair_breakdown() -> None:
    _seed()
    client = create_app().test_client()
    r = client.get("/api/arb/sim_summary")
    by_pair = {row["pair"]: row for row in r.get_json()["by_pair"]}
    assert "BTCUSDT" in by_pair
    assert "ETHUSDT" in by_pair
    # BTC filled: 0.10 - 0.02 + 0 = 0.08; n_filled = 2 (3rd was rejected)
    assert abs(by_pair["BTCUSDT"]["cumulative_usd"] - 0.08) < 1e-6
    assert by_pair["BTCUSDT"]["n_filled"] == 2
    assert by_pair["ETHUSDT"]["n_filled"] == 1
    assert abs(by_pair["ETHUSDT"]["cumulative_usd"] - 0.07) < 1e-6


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
