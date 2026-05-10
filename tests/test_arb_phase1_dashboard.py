"""
Phase 1 regression tests — dashboard blueprint + standalone app.

Uses Flask test client. Seeds data/arb/db/ with a few rows so the
endpoints have something to return.
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


SEED_TABLES = ("obi_snapshots", "dex_quotes", "gas_history")


def _cleanup_seed():
    arb_store.close()
    for t in SEED_TABLES:
        d = arb_store.table_dir(t)
        if d.exists():
            shutil.rmtree(d)


def setup_function(_):
    _cleanup_seed()


def teardown_function(_):
    _cleanup_seed()


def _seed_data():
    arb_store.write_records(
        "obi_snapshots",
        [
            {"ts": "2026-05-10T12:00:00+00:00", "pair": "BTCUSDT",
             "weighted_obi": 0.12, "obi_delta": 0.01, "cancellation_rate": 0.0,
             "best_bid": 81000.0, "best_ask": 81001.0,
             "bid_volume": 1.0, "ask_volume": 1.0, "levels_used": 10,
             "update_id": 1, "is_full_snapshot": True},
            {"ts": "2026-05-10T12:00:01+00:00", "pair": "BTCUSDT",
             "weighted_obi": 0.15, "obi_delta": 0.03, "cancellation_rate": 0.0,
             "best_bid": 81002.0, "best_ask": 81003.0,
             "bid_volume": 1.1, "ask_volume": 0.9, "levels_used": 10,
             "update_id": 2, "is_full_snapshot": False},
        ],
        pair="BTCUSDT",
    )
    arb_store.write_records(
        "obi_snapshots",
        [{"ts": "2026-05-10T12:00:00+00:00", "pair": "ETHUSDT",
          "weighted_obi": -0.05, "obi_delta": 0.0, "cancellation_rate": 0.0,
          "best_bid": 2358.0, "best_ask": 2358.5,
          "bid_volume": 5.0, "ask_volume": 5.5, "levels_used": 10,
          "update_id": 1, "is_full_snapshot": True}],
        pair="ETHUSDT",
    )
    arb_store.write_records(
        "dex_quotes",
        [{"ts": "2026-05-10T12:00:00+00:00", "pair": "BTCUSDT",
          "pool_address": "0x4e96", "sqrt_price_x96": "1234567890",
          "mid_price": 80999.0, "fee_bps": 500, "source": "uniswap_v3_slot0"}],
        pair="BTCUSDT",
    )
    arb_store.write_records(
        "gas_history",
        [{"ts": "2026-05-10T12:00:00+00:00",
          "block_number": 12345678, "base_fee_gwei": 0.005,
          "priority_fee_gwei": 0.001, "total_gas_price_gwei": 0.006}],
        pair="BASE",
    )


# --- /api/arb/health -------------------------------------------------------


def test_health_returns_200_and_mode() -> None:
    client = create_app().test_client()
    r = client.get("/api/arb/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok"
    assert body["mode"] in (config.MODE_SHADOW, config.MODE_TESTNET, config.MODE_MAINNET)
    assert body["pilot_pairs"] == list(config.PILOT_PAIRS)
    assert "halt_active" in body


def test_ingestion_liveness_check_dead_pid() -> None:
    """A stale PID file pointing at a non-existent process must report False.
    Regression: Windows path was using file-mtime heuristic which falsely
    reported running. Caught during Phase 1 restart_all smoke test 2026-05-10.
    """
    from src.dashboard.arb_blueprint import _is_ingestion_running
    pid_file = config.PIDS_DIR / "ingestion.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    # Use a PID that is virtually guaranteed to not exist (high reserved range).
    fake_pid = 9999999
    pid_file.write_text(str(fake_pid))
    try:
        alive, pid = _is_ingestion_running()
        assert alive is False, f"expected dead, got alive={alive} pid={pid}"
        assert pid == fake_pid
    finally:
        pid_file.unlink(missing_ok=True)


def test_ingestion_liveness_check_no_pid_file() -> None:
    from src.dashboard.arb_blueprint import _is_ingestion_running
    pid_file = config.PIDS_DIR / "ingestion.pid"
    pid_file.unlink(missing_ok=True)
    alive, pid = _is_ingestion_running()
    assert alive is False
    assert pid is None


def test_ingestion_liveness_check_self_pid() -> None:
    """If the PID file points at OUR process, the check should report alive."""
    from src.dashboard.arb_blueprint import _is_ingestion_running
    pid_file = config.PIDS_DIR / "ingestion.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    try:
        alive, pid = _is_ingestion_running()
        assert alive is True
        assert pid == os.getpid()
    finally:
        pid_file.unlink(missing_ok=True)


# --- /api/arb/pairs --------------------------------------------------------


def test_pairs_returns_pilot_list() -> None:
    client = create_app().test_client()
    r = client.get("/api/arb/pairs")
    assert r.status_code == 200
    assert r.get_json()["pairs"] == list(config.PILOT_PAIRS)


# --- /api/arb/spread -------------------------------------------------------


def test_spread_empty_when_no_data() -> None:
    client = create_app().test_client()
    r = client.get("/api/arb/spread")
    assert r.status_code == 200
    body = r.get_json()
    assert body["spreads"] == []


def test_spread_computes_bps_correctly() -> None:
    _seed_data()
    client = create_app().test_client()
    r = client.get("/api/arb/spread")
    assert r.status_code == 200
    body = r.get_json()
    spreads = {s["pair"]: s for s in body["spreads"]}
    # BTC: bybit_mid = (81002+81003)/2 = 81002.5; dex = 80999
    # (81002.5 - 80999) / 81002.5 * 10000 ≈ 0.43 bps
    assert "BTCUSDT" in spreads
    btc = spreads["BTCUSDT"]
    assert btc["bybit_mid"] == 81002.5
    assert btc["dex_mid"] == 80999.0
    assert 0 < btc["spread_bps"] < 1.0
    # ETH: dex_mid is None → spread_bps None
    assert spreads["ETHUSDT"]["dex_mid"] is None
    assert spreads["ETHUSDT"]["spread_bps"] is None


# --- /api/arb/obi/<pair> ---------------------------------------------------


def test_obi_unknown_pair_returns_404() -> None:
    client = create_app().test_client()
    r = client.get("/api/arb/obi/XRPUSDT")
    assert r.status_code == 404


def test_obi_returns_chronological_snapshots() -> None:
    _seed_data()
    client = create_app().test_client()
    r = client.get("/api/arb/obi/BTCUSDT?n=10")
    assert r.status_code == 200
    body = r.get_json()
    assert body["pair"] == "BTCUSDT"
    snaps = body["snapshots"]
    assert len(snaps) == 2
    # chronological order (oldest first) for sparkline
    assert snaps[0]["ts"] < snaps[1]["ts"]
    assert snaps[0]["weighted_obi"] == 0.12


def test_obi_n_param_clamped() -> None:
    """n=0 → clamped to 1; n=99999 → clamped to 1000."""
    _seed_data()
    client = create_app().test_client()
    r = client.get("/api/arb/obi/BTCUSDT?n=0")
    assert r.status_code == 200
    # We have only 2 rows; n=0 clamps up to 1, returns 1 row
    assert len(r.get_json()["snapshots"]) == 1


# --- /api/arb/gas ----------------------------------------------------------


def test_gas_empty_when_no_data() -> None:
    client = create_app().test_client()
    r = client.get("/api/arb/gas")
    assert r.status_code == 200
    body = r.get_json()
    assert body.get("gas") is None


def test_gas_returns_latest() -> None:
    _seed_data()
    client = create_app().test_client()
    r = client.get("/api/arb/gas")
    assert r.status_code == 200
    body = r.get_json()
    assert body["gas"]["block_number"] == 12345678
    assert body["gas"]["total_gas_price_gwei"] == 0.006


# --- index page ------------------------------------------------------------


def test_index_renders_html() -> None:
    client = create_app().test_client()
    r = client.get("/")
    assert r.status_code == 200
    assert b"arbitrage_strategy" in r.data
    assert b"/api/arb/spread" in r.data


def test_healthz() -> None:
    client = create_app().test_client()
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}


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
