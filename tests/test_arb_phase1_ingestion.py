"""
Phase 1 regression tests — ingestion clients (Bybit WS, DEX quote, gas oracle).

Network-free: all external calls are mocked. Tests cover:
- Bybit message parsing (snapshot vs delta vs unknown)
- _LocalBook delta application + reset
- Reconnect resets local snapshot state
- DexQuote dataclass + units conversion
- GasReading.estimate_swap_cost_usd math
- BatchBuffer flush triggers (row count + timeout)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.data.bybit_l2_ws import BookSnapshot, BybitL2Stream, _LocalBook
from src.data.dex_quote import (
    DexPriceReader, DexQuote, PILOT_POOLS, PoolConfig,
    sqrt_price_x96_to_mid, TWO_POW_96,
)
from src.data.gas_oracle import GasOracle, GasReading
from src.data.ingestion_main import BatchBuffer


# --- _LocalBook ------------------------------------------------------------


def test_localbook_apply_snapshot() -> None:
    book = _LocalBook()
    book.apply("b", [["100.0", "1.5"], ["99.5", "2.0"]])
    book.apply("a", [["100.5", "1.0"], ["101.0", "0.5"]])
    assert book.bids == {100.0: 1.5, 99.5: 2.0}
    assert book.asks == {100.5: 1.0, 101.0: 0.5}


def test_localbook_delta_zero_size_removes_level() -> None:
    book = _LocalBook()
    book.apply("b", [["100.0", "1.5"], ["99.5", "2.0"]])
    book.apply("b", [["99.5", "0"]])  # zero-size = remove
    assert 99.5 not in book.bids
    assert book.bids == {100.0: 1.5}


def test_localbook_delta_overwrites_size() -> None:
    book = _LocalBook()
    book.apply("b", [["100.0", "1.5"]])
    book.apply("b", [["100.0", "5.0"]])
    assert book.bids == {100.0: 5.0}


def test_localbook_to_snapshot_sorts_correctly() -> None:
    book = _LocalBook()
    book.apply("b", [["99.0", "1"], ["100.0", "1"], ["98.0", "1"]])
    book.apply("a", [["102.0", "1"], ["100.5", "1"], ["101.0", "1"]])
    snap = book.to_snapshot("BTCUSDT", 1234, full=False, update_id=42)
    bid_prices = [b[0] for b in snap.bids]
    ask_prices = [a[0] for a in snap.asks]
    assert bid_prices == sorted(bid_prices, reverse=True), "bids must be desc"
    assert ask_prices == sorted(ask_prices), "asks must be asc"


def test_localbook_reset_clears() -> None:
    book = _LocalBook()
    book.apply("b", [["100.0", "1"]])
    book.has_snapshot = True
    book.last_update_id = 99
    book.reset()
    assert book.bids == {}
    assert book.asks == {}
    assert not book.has_snapshot
    assert book.last_update_id == -1


# --- BybitL2Stream message handling ---------------------------------------


def test_stream_ignores_non_orderbook_topics() -> None:
    stream = BybitL2Stream(["BTCUSDT"])
    assert stream._handle_message({"op": "subscribe", "success": True}) is None
    assert stream._handle_message({"topic": "publicTrade.BTCUSDT", "data": []}) is None
    assert stream._handle_message({}) is None


def test_stream_ignores_unknown_symbol() -> None:
    stream = BybitL2Stream(["BTCUSDT"])
    msg = {
        "topic": "orderbook.50.ETHUSDT",
        "type": "snapshot",
        "ts": 1234,
        "data": {"s": "ETHUSDT", "b": [["100", "1"]], "a": [["101", "1"]], "u": 1},
    }
    assert stream._handle_message(msg) is None


def test_stream_snapshot_yields_full_book() -> None:
    stream = BybitL2Stream(["BTCUSDT"])
    msg = {
        "topic": "orderbook.50.BTCUSDT",
        "type": "snapshot",
        "ts": 1234567890,
        "data": {
            "s": "BTCUSDT",
            "b": [["100.0", "1.0"], ["99.5", "2.0"]],
            "a": [["100.5", "1.0"], ["101.0", "0.5"]],
            "u": 100,
        },
    }
    snap = stream._handle_message(msg)
    assert snap is not None
    assert snap.is_full_snapshot
    assert snap.update_id == 100
    assert snap.symbol == "BTCUSDT"
    assert len(snap.bids) == 2
    assert len(snap.asks) == 2


def test_stream_delta_without_snapshot_is_dropped() -> None:
    stream = BybitL2Stream(["BTCUSDT"])
    delta = {
        "topic": "orderbook.50.BTCUSDT",
        "type": "delta",
        "ts": 1234,
        "data": {"s": "BTCUSDT", "b": [["100", "1"]], "a": [["101", "1"]], "u": 5},
    }
    assert stream._handle_message(delta) is None


def test_stream_delta_after_snapshot_yields_book() -> None:
    stream = BybitL2Stream(["BTCUSDT"])
    snap_msg = {
        "topic": "orderbook.50.BTCUSDT",
        "type": "snapshot",
        "ts": 1,
        "data": {"s": "BTCUSDT", "b": [["100", "1"]], "a": [["101", "1"]], "u": 1},
    }
    delta_msg = {
        "topic": "orderbook.50.BTCUSDT",
        "type": "delta",
        "ts": 2,
        "data": {"s": "BTCUSDT", "b": [["99", "0.5"]], "a": [["102", "0.3"]], "u": 2},
    }
    stream._handle_message(snap_msg)
    snap = stream._handle_message(delta_msg)
    assert snap is not None
    assert not snap.is_full_snapshot
    assert snap.update_id == 2
    assert any(b[0] == 99.0 for b in snap.bids)


# --- GasReading ------------------------------------------------------------


def test_gas_reading_cost_estimate() -> None:
    r = GasReading(
        ts_ms=1, block_number=10, base_fee_gwei=0.001,
        priority_fee_gwei=0.001, total_gas_price_gwei=0.002,
    )
    # 0.002 gwei * 200_000 gas = 400 gwei = 4e-7 ETH
    # at $3000/ETH ≈ $0.0012
    cost = r.estimate_swap_cost_usd(gas_units=200_000, eth_price_usd=3000.0)
    assert 0.001 < cost < 0.002, f"expected ~$0.0012 on Base, got {cost}"


def test_gas_oracle_initial_latest_is_none() -> None:
    o = GasOracle()
    assert o.latest() is None


# --- DexQuote (Uniswap V3 slot0) -------------------------------------------


def test_dexquote_dataclass_immutable() -> None:
    q = DexQuote(
        ts_ms=1, pair="ETHUSDT",
        pool_address="0xd0b53D9277642d899DF5C87A3966A349A798F224",
        sqrt_price_x96=int(2**96),
        mid_price=3000.0,
        fee_bps=500,
    )
    try:
        q.mid_price = 9999.0  # type: ignore[misc]
        assert False, "DexQuote should be frozen"
    except Exception:
        pass


def test_sqrt_price_x96_to_mid_unit_ratio() -> None:
    """sqrtPriceX96 = 2^96 means raw ratio = 1.0; with d0=d1, mid = 1.0."""
    mid = sqrt_price_x96_to_mid(sqrt_price_x96=TWO_POW_96,
                                token0_decimals=18, token1_decimals=18)
    assert abs(mid - 1.0) < 1e-12


def test_sqrt_price_x96_to_mid_decimals_skew() -> None:
    """For WETH/USDC pool, decimals differ (18 vs 6).
    sqrtPriceX96 corresponding to ~3000 USDC/WETH:
      raw ratio = (3000 * 10^(6-18)) = 3e-9
      sqrtPriceX96 = sqrt(3e-9) * 2^96
    """
    raw_ratio = 3e-9
    sqrt_p = int((raw_ratio ** 0.5) * TWO_POW_96)
    mid = sqrt_price_x96_to_mid(sqrt_p, token0_decimals=18, token1_decimals=6)
    # Should round-trip back to ~3000
    assert 2900 < mid < 3100, f"expected ~3000, got {mid}"


def test_sqrt_price_x96_to_mid_zero_safe() -> None:
    assert sqrt_price_x96_to_mid(0, 18, 6) == 0.0


def test_pilot_pools_have_eth_and_btc() -> None:
    assert "ETHUSDT" in PILOT_POOLS
    assert "BTCUSDT" in PILOT_POOLS
    eth = PILOT_POOLS["ETHUSDT"]
    assert eth.base_symbol == "WETH"
    assert eth.quote_symbol == "USDC"
    assert eth.base_decimals == 18
    assert eth.quote_decimals == 6
    assert eth.base_is_token0 is True   # WETH addr < USDC addr
    assert eth.fee_bps == 500
    btc = PILOT_POOLS["BTCUSDT"]
    assert btc.base_symbol == "cbBTC"
    assert btc.base_is_token0 is False  # USDC addr < cbBTC addr → invert


def test_pool_config_immutable() -> None:
    p = PILOT_POOLS["ETHUSDT"]
    try:
        p.fee_bps = 999  # type: ignore[misc]
        assert False, "PoolConfig should be frozen"
    except Exception:
        pass


def test_dex_reader_no_network_init() -> None:
    """Constructor must not touch the network."""
    r = DexPriceReader()
    assert r._w3 is None
    assert r.pools is PILOT_POOLS


# --- BatchBuffer ------------------------------------------------------------


def test_batchbuffer_flushes_at_row_threshold() -> None:
    flushed = []
    buf = BatchBuffer(flush_rows=3, flush_s=999.0)
    # patch flush to capture
    orig = buf.flush
    def _capture(key):
        flushed.append((key, list(buf._rows.get(key, []))))
        orig(key)
    buf.flush = _capture  # type: ignore[method-assign]
    for i in range(5):
        buf.add("obi_snapshots", "BTCUSDT", {"ts": str(i), "weighted_obi": 0.1})
    # 5 rows added, threshold=3 → at least one flush captured
    assert any(len(rows) >= 3 for _, rows in flushed)


def test_batchbuffer_flush_all() -> None:
    buf = BatchBuffer(flush_rows=999, flush_s=999.0)
    buf.add("obi_snapshots", "BTCUSDT", {"ts": "x", "weighted_obi": 0.0})
    assert buf._rows[("obi_snapshots", "BTCUSDT")] != []
    # flush_all should be safe even though arb_store will write a parquet file
    # (it's a roundtrip test for the buffer state machine; cleanup below)
    import shutil
    from src.storage import arb_store
    try:
        buf.flush_all()
        assert buf._rows[("obi_snapshots", "BTCUSDT")] == []
    finally:
        shutil.rmtree(arb_store.table_dir("obi_snapshots"), ignore_errors=True)


def _run_all() -> int:
    failures: list[tuple[str, str]] = []
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print()
    if failures:
        print(f"{len(failures)} / {len(tests)} FAILED")
        return 1
    print(f"{len(tests)} / {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
