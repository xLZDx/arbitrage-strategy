"""
Phase 5 unit tests — bybit_leg, dex_leg, private_rpc_router,
bundle_simulator, flashbots_executor. SHADOW mode only — no network.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.data.dex_quote import PILOT_POOLS
from src.exec.bybit_leg import BybitLegExecutor, Fill, make_client_order_id
from src.exec.bundle_simulator import BundleSimulator, SimulationResult
from src.exec.dex_leg import DexLegExecutor, PreparedSwap
from src.exec.flashbots_executor import FlashbotsExecutor
from src.exec.private_rpc_router import PrivateRpcRouter, SubmissionResult
from src.utils import config


_TEST_LEDGER = config.DATA_DIR / "_test_bybit_idempotency.json"


def setup_function(_):
    if _TEST_LEDGER.exists():
        _TEST_LEDGER.unlink()
    lock = _TEST_LEDGER.with_suffix(".json.lock")
    if lock.exists():
        try:
            lock.unlink()
        except Exception:
            pass


def teardown_function(_):
    if _TEST_LEDGER.exists():
        _TEST_LEDGER.unlink()


# --- bybit_leg -----------------------------------------------------------


def test_make_client_order_id_deterministic() -> None:
    a = make_client_order_id("trade-x", "bybit")
    b = make_client_order_id("trade-x", "bybit")
    c = make_client_order_id("trade-y", "bybit")
    assert a == b
    assert a != c
    assert a.startswith("arb-bybit-")
    assert len(a) <= 36


def test_bybit_shadow_returns_synthetic_fill() -> None:
    ex = BybitLegExecutor(mode=config.MODE_SHADOW, ledger_path=_TEST_LEDGER)
    fill = ex.place_spot_taker(
        symbol="BTCUSDT", side="SELL", qty_usd=50.0,
        trade_id="t1", last_price=80000.0,
    )
    assert isinstance(fill, Fill)
    assert fill.status == "shadow"
    assert fill.filled_qty_usd == 50.0
    assert fill.fill_pct == 1.0
    assert fill.avg_price == 80000.0


def test_bybit_idempotency_replay_returns_cached() -> None:
    ex = BybitLegExecutor(mode=config.MODE_SHADOW, ledger_path=_TEST_LEDGER)
    f1 = ex.place_spot_taker("BTCUSDT", "SELL", 50.0, "trade-id-A", 80000.0)
    f2 = ex.place_spot_taker("BTCUSDT", "SELL", 50.0, "trade-id-A", 80000.0)
    assert f1.client_order_id == f2.client_order_id
    assert f1.status == f2.status


def test_bybit_idempotency_persists_across_instances() -> None:
    """A new executor with the same ledger path must replay prior fills."""
    ex1 = BybitLegExecutor(mode=config.MODE_SHADOW, ledger_path=_TEST_LEDGER)
    f1 = ex1.place_spot_taker("BTCUSDT", "SELL", 50.0, "trade-X", 80000.0)
    ex2 = BybitLegExecutor(mode=config.MODE_SHADOW, ledger_path=_TEST_LEDGER)
    f2 = ex2.place_spot_taker("BTCUSDT", "SELL", 50.0, "trade-X", 80000.0)
    assert f1.client_order_id == f2.client_order_id


def test_bybit_mainnet_requires_gate_env_var() -> None:
    """Mainnet refuses to instantiate without ARB_MAINNET_GATE=1."""
    os.environ.pop("ARB_MAINNET_GATE", None)
    try:
        BybitLegExecutor(mode=config.MODE_MAINNET,
                         api_key="x", api_secret="y",
                         ledger_path=_TEST_LEDGER)
    except RuntimeError as e:
        assert "ARB_MAINNET_GATE" in str(e)
        return
    assert False, "expected RuntimeError"


def test_bybit_testnet_requires_credentials() -> None:
    for var in ("BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET"):
        os.environ.pop(var, None)
    try:
        BybitLegExecutor(mode=config.MODE_TESTNET, ledger_path=_TEST_LEDGER)
    except RuntimeError as e:
        assert "credentials missing" in str(e)
        return
    assert False, "expected RuntimeError"


# --- dex_leg -------------------------------------------------------------


def test_dex_shadow_build_swap_sane_amounts() -> None:
    ex = DexLegExecutor(mode=config.MODE_SHADOW)
    cfg = PILOT_POOLS["ETHUSDT"]
    swap = ex.build_swap(
        pair="ETHUSDT", direction="buy",  # USDC -> WETH
        notional_usd=50.0, live_mid_price=3000.0, pool_cfg=cfg,
    )
    assert isinstance(swap, PreparedSwap)
    assert swap.is_shadow
    assert swap.src_token == "USDC"
    assert swap.dst_token == "WETH"
    # USDC has 6 decimals → 50 USDC = 50e6 wei
    assert swap.amount_in_wei == 50_000_000
    # Expected out: 50 / 3000 ≈ 0.01667 WETH; with 30 bps haircut ≈ 0.01662
    expected_min = 0.01667 * 0.997 * 1e18
    assert 0.99 * expected_min < swap.amount_out_min_wei < 1.01 * expected_min


def test_dex_shadow_sell_direction() -> None:
    ex = DexLegExecutor(mode=config.MODE_SHADOW)
    cfg = PILOT_POOLS["BTCUSDT"]
    swap = ex.build_swap(
        pair="BTCUSDT", direction="sell",  # cbBTC -> USDC
        notional_usd=50.0, live_mid_price=80000.0, pool_cfg=cfg,
    )
    assert swap.src_token == "cbBTC"
    assert swap.dst_token == "USDC"
    # 50 USD / 80000 = 0.000625 cbBTC; 8 decimals → 62500
    assert swap.amount_in_wei == 62500


def test_dex_shadow_deadline_in_future() -> None:
    import time as _time
    ex = DexLegExecutor(mode=config.MODE_SHADOW)
    cfg = PILOT_POOLS["ETHUSDT"]
    swap = ex.build_swap("ETHUSDT", "buy", 50.0, 3000.0, cfg)
    assert swap.deadline_unix > _time.time()


def test_dex_invalid_direction_raises() -> None:
    ex = DexLegExecutor(mode=config.MODE_SHADOW)
    cfg = PILOT_POOLS["ETHUSDT"]
    try:
        ex.build_swap("ETHUSDT", "invalid", 50.0, 3000.0, cfg)
    except ValueError as e:
        assert "buy" in str(e) or "sell" in str(e)
        return
    assert False, "expected ValueError"


def test_dex_mainnet_needs_gate() -> None:
    os.environ.pop("ARB_MAINNET_GATE", None)
    try:
        DexLegExecutor(mode=config.MODE_MAINNET)
    except RuntimeError as e:
        assert "ARB_MAINNET_GATE" in str(e)
        return
    assert False


# --- private_rpc_router --------------------------------------------------


def test_router_shadow_returns_mock_hash() -> None:
    r = PrivateRpcRouter(mode=config.MODE_SHADOW)
    out = r.submit_signed_tx("0x" + "ab" * 100)
    assert out.status == "shadow"
    assert out.tx_hash and out.tx_hash.startswith("0x") and len(out.tx_hash) == 66


def test_router_default_relay_per_mode() -> None:
    assert PrivateRpcRouter(mode=config.MODE_MAINNET).relay_url == \
        "https://rpc.flashbots.net/fast"
    assert PrivateRpcRouter(mode=config.MODE_TESTNET).relay_url == \
        "https://sepolia.base.org"
    assert PrivateRpcRouter(mode=config.MODE_SHADOW).relay_url == \
        "shadow://no-submission"


# --- bundle_simulator ----------------------------------------------------


def test_simulator_shadow_passes() -> None:
    sim = BundleSimulator(mode=config.MODE_SHADOW)
    cfg = PILOT_POOLS["ETHUSDT"]
    swap = DexLegExecutor(mode=config.MODE_SHADOW).build_swap(
        "ETHUSDT", "buy", 50.0, 3000.0, cfg)
    res = sim.simulate(swap)
    assert isinstance(res, SimulationResult)
    assert res.passed
    assert res.gas_used > 0


def test_simulator_extracts_revert_reason() -> None:
    sim = BundleSimulator(mode=config.MODE_SHADOW)
    err = Exception("execution reverted: STF")
    reason = sim._extract_revert_reason(err)
    assert "STF" in reason


def test_simulator_handles_unknown_error() -> None:
    sim = BundleSimulator(mode=config.MODE_SHADOW)
    reason = sim._extract_revert_reason(ConnectionError("boom"))
    assert "ConnectionError" in reason


# --- flashbots_executor --------------------------------------------------


def test_flashbots_shadow_round_trip() -> None:
    fb = FlashbotsExecutor(router=PrivateRpcRouter(mode=config.MODE_SHADOW))
    cfg = PILOT_POOLS["ETHUSDT"]
    swap = DexLegExecutor(mode=config.MODE_SHADOW).build_swap(
        "ETHUSDT", "buy", 50.0, 3000.0, cfg)
    out = fb.sign_and_submit(swap)
    assert out.status == "shadow"
    assert out.tx_hash and out.tx_hash.startswith("0x")


def test_flashbots_testnet_without_key_returns_error() -> None:
    os.environ.pop("BASE_WALLET_PRIVATE_KEY", None)
    fb = FlashbotsExecutor(router=PrivateRpcRouter(mode=config.MODE_TESTNET))
    cfg = PILOT_POOLS["ETHUSDT"]
    # Need shadow swap to avoid build raising; use SHADOW dex builder
    swap = DexLegExecutor(mode=config.MODE_SHADOW).build_swap(
        "ETHUSDT", "buy", 50.0, 3000.0, cfg)
    out = fb.sign_and_submit(swap)
    assert out.status == "error"
    assert "BASE_WALLET_PRIVATE_KEY" in (out.error or "")


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
