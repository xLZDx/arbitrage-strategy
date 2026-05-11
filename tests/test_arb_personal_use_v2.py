"""
Tests for the second batch of personal-use improvements:
- Maker-mode coordinator wiring (maker-first, taker-fallback)
- Uniswap V3 factory.getPool ABI encoding (no network)
- AERO live-pool address re-enabled in PILOT_POOLS
- Multi-relay broadcast wired into FlashbotsExecutor
- Inventory auto-rebalancer plan/apply/log
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.data.dex_quote import AERO_BASE, PILOT_POOLS, USDC_BASE
from src.data.factory_lookup import (
    GET_POOL_SELECTOR, UNISWAP_V3_FACTORY, _pad_address,
    _pad_uint24, find_deepest_pool, get_pool_address,
)
from src.exec.bybit_leg import BybitLegExecutor, Fill
from src.exec.bundle_simulator import BundleSimulator
from src.exec.coordinator import ArbCoordinator
from src.exec.dex_leg import DexLegExecutor
from src.exec.flashbots_executor import FlashbotsExecutor
from src.exec.multi_relay import MultiRelaySubmitter
from src.exec.private_rpc_router import PrivateRpcRouter, SubmissionResult
from src.exec.wallet_signer import WalletSigner
from src.ops.rebalancer import (
    RebalancePlan, TransferLeg, apply_rebalance, plan_rebalance,
    watch_and_rebalance,
)
from src.risk import limits as risk
from src.sim.inventory import Inventory
from src.utils import config


_TEST_LEDGER = config.DATA_DIR / "_test_v2_idempotency.json"


def setup_function(_):
    risk.halt_clear()
    if _TEST_LEDGER.exists():
        _TEST_LEDGER.unlink()


def teardown_function(_):
    risk.halt_clear()
    if _TEST_LEDGER.exists():
        _TEST_LEDGER.unlink()


def _coord(prefer_maker=False, multi_relay=False):
    bybit = BybitLegExecutor(mode=config.MODE_SHADOW, ledger_path=_TEST_LEDGER)
    fb = FlashbotsExecutor(
        router=PrivateRpcRouter(mode=config.MODE_SHADOW),
        signer=WalletSigner(mode=config.MODE_SHADOW),
        use_multi_relay=multi_relay,
    )
    coord = ArbCoordinator(
        bybit=bybit,
        dex=DexLegExecutor(mode=config.MODE_SHADOW),
        router=PrivateRpcRouter(mode=config.MODE_SHADOW),
        simulator=BundleSimulator(mode=config.MODE_SHADOW),
        inventory=Inventory.with_balanced_seed(2000.0),
        risk_state=risk.RiskState(),
        flashbots=fb,
    )
    return coord


def _good_opp(pair="BTCUSDT", direction="bybit_high"):
    return {
        "ts": "2026-05-11T12:00:00+00:00", "pair": pair,
        "decision": "GO", "direction": direction,
        "spread_bps": 25.0, "gross_bps": 25.0,
        "expected_net_bps": 18.0, "theoretical_pnl_usd": 0.10,
        "weighted_obi": 0.1, "obi_delta": 0.0, "cancellation_rate": 0.0,
        "gas_gwei": 0.006, "gas_cost_bps": 0.65,
        "slippage_haircut_bps": 5.0,
        "notional_usd": 50.0,
        "bybit_mid": 80000.0, "bybit_bid": 79999.5, "bybit_ask": 80000.5,
        "dex_mid": 79800.0,
    }


# --- Maker-mode coordinator wiring ---------------------------------------


def test_coordinator_uses_taker_when_prefer_maker_off() -> None:
    os.environ.pop("ARB_PREFER_MAKER", None)
    import importlib
    importlib.reload(config)
    coord = _coord()
    rec = coord.attempt(_good_opp())
    assert rec.outcome == "shadow"
    assert rec.bybit_used_maker is False
    assert rec.bybit_taker_fallback is False


def test_coordinator_uses_maker_when_prefer_maker_on() -> None:
    os.environ["ARB_PREFER_MAKER"] = "1"
    import importlib
    importlib.reload(config)
    try:
        coord = _coord()
        rec = coord.attempt(_good_opp())
        assert rec.outcome == "shadow"
        assert rec.bybit_used_maker is True
        assert rec.bybit_taker_fallback is False  # SHADOW maker fills immediately
    finally:
        os.environ.pop("ARB_PREFER_MAKER", None)
        importlib.reload(config)


def test_coordinator_falls_back_to_taker_on_maker_timeout() -> None:
    """If maker rejects, coordinator must call taker with a distinct trade_id."""
    os.environ["ARB_PREFER_MAKER"] = "1"
    import importlib
    importlib.reload(config)
    try:
        coord = _coord()
        # Make maker reject
        rejected = Fill(
            symbol="BTCUSDT", side="SELL", requested_qty_usd=50.0,
            filled_qty_usd=0.0, avg_price=0.0, status="rejected",
            venue_order_id=None, client_order_id="x",
            mode=config.MODE_SHADOW, error="maker_timeout",
        )
        with patch.object(coord.bybit, "place_spot_maker", return_value=rejected):
            rec = coord.attempt(_good_opp())
        assert rec.bybit_used_maker is True
        assert rec.bybit_taker_fallback is True
        assert rec.outcome == "shadow"
    finally:
        os.environ.pop("ARB_PREFER_MAKER", None)
        importlib.reload(config)


def test_maker_uses_correct_side_of_book() -> None:
    """SELL → quote at the ask; BUY → quote at the bid."""
    os.environ["ARB_PREFER_MAKER"] = "1"
    import importlib
    importlib.reload(config)
    try:
        coord = _coord()
        captured = {}
        orig_maker = coord.bybit.place_spot_maker
        def _spy(symbol, side, qty_usd, trade_id, limit_price, **kw):
            captured["limit_price"] = limit_price
            captured["side"] = side
            return orig_maker(symbol=symbol, side=side, qty_usd=qty_usd,
                              trade_id=trade_id, limit_price=limit_price, **kw)
        coord.bybit.place_spot_maker = _spy  # type: ignore[method-assign]
        opp = _good_opp(direction="bybit_high")  # → SELL on Bybit
        coord.attempt(opp)
        assert captured["side"] == "SELL"
        assert captured["limit_price"] == 80000.5  # the ask
    finally:
        os.environ.pop("ARB_PREFER_MAKER", None)
        importlib.reload(config)


# --- Uniswap V3 factory lookup -------------------------------------------


def test_factory_address_canonical() -> None:
    """Same factory address on Base + Arbitrum + Optimism + mainnet."""
    assert UNISWAP_V3_FACTORY.lower() == "0x33128a8fc17869897dce68ed026d694621f6fdfd"


def test_get_pool_selector_matches_keccak() -> None:
    """selector = keccak256("getPool(address,address,uint24)")[:4]"""
    assert GET_POOL_SELECTOR == bytes.fromhex("1698ee82")


def test_pad_address_returns_32_bytes() -> None:
    out = _pad_address("0x" + "ab" * 20)
    assert len(out) == 32
    assert out[:12] == b"\x00" * 12  # left-padded
    assert out[12:].hex() == "ab" * 20


def test_pad_uint24_in_range() -> None:
    out = _pad_uint24(500)
    assert len(out) == 32
    assert int.from_bytes(out, "big") == 500


def test_pad_uint24_rejects_oversized() -> None:
    try:
        _pad_uint24(2 ** 25)
    except ValueError as e:
        assert "uint24" in str(e)
        return
    assert False


def test_get_pool_returns_none_on_network_error() -> None:
    """No live RPC; should return None silently rather than crash."""
    with patch("src.data.factory_lookup.HTTPProvider",
                side_effect=Exception("no network")):
        result = get_pool_address(AERO_BASE, USDC_BASE, 500,
                                    rpc_url="http://invalid")
    assert result is None


# --- AERO re-enabled with real address ----------------------------------


def test_aero_pool_re_enabled_with_factory_address() -> None:
    """AERO/USDC pool resolved via factory.getPool 2026-05-11."""
    assert "AEROUSDT" in PILOT_POOLS
    cfg = PILOT_POOLS["AEROUSDT"]
    assert cfg.pool_address.lower() == "0xe5b5f522e98b5a2baae212d4da66b865b781db97"
    assert cfg.fee_bps == 500
    assert cfg.base_is_token0 is False  # USDC < AERO


# --- Multi-relay wiring --------------------------------------------------


def test_flashbots_uses_single_relay_by_default() -> None:
    fb = FlashbotsExecutor(
        router=PrivateRpcRouter(mode=config.MODE_SHADOW),
        signer=WalletSigner(mode=config.MODE_SHADOW),
        use_multi_relay=False,
    )
    assert fb.multi_relay is None


def test_flashbots_constructs_multi_relay_when_enabled() -> None:
    fb = FlashbotsExecutor(
        router=PrivateRpcRouter(mode=config.MODE_SHADOW),
        signer=WalletSigner(mode=config.MODE_SHADOW),
        use_multi_relay=True,
    )
    assert fb.multi_relay is not None
    assert isinstance(fb.multi_relay, MultiRelaySubmitter)


def test_flashbots_relay_stats_empty_in_single_mode() -> None:
    fb = FlashbotsExecutor(
        router=PrivateRpcRouter(mode=config.MODE_SHADOW),
        signer=WalletSigner(mode=config.MODE_SHADOW),
        use_multi_relay=False,
    )
    assert fb.relay_stats() == {}


def test_flashbots_multi_relay_records_stats() -> None:
    from src.data.dex_quote import PILOT_POOLS
    fb = FlashbotsExecutor(
        router=PrivateRpcRouter(mode=config.MODE_SHADOW),
        signer=WalletSigner(mode=config.MODE_SHADOW),
        use_multi_relay=True,
    )
    cfg = PILOT_POOLS["ETHUSDT"]
    swap = DexLegExecutor(mode=config.MODE_SHADOW).build_swap(
        "ETHUSDT", "buy", 50.0, 3000.0, cfg)
    res = fb.sign_and_submit(swap)
    assert res.status == "shadow"
    stats = fb.relay_stats()
    assert len(stats) >= 1
    assert all(s["submissions"] >= 1 for s in stats.values())


# --- Inventory rebalancer ------------------------------------------------


def test_rebalance_no_op_when_balanced() -> None:
    inv = Inventory.with_balanced_seed(800.0)
    plan = plan_rebalance(inv)
    assert plan.severity == "NO-OP"
    assert plan.legs == ()


def test_rebalance_no_op_when_below_trigger() -> None:
    inv = Inventory.with_initial_usd(500.0)
    inv.adjust("bybit", "USDT", -50.0)  # imbalance ~5%
    plan = plan_rebalance(inv)
    assert plan.severity == "NO-OP"


def test_rebalance_plans_transfer_when_above_trigger() -> None:
    inv = Inventory.with_initial_usd(500.0)
    inv.adjust("bybit", "USDT", -300.0)  # bybit=$200 dex=$500 → ~43% imbalance
    plan = plan_rebalance(inv, trigger_pct=0.20)
    assert plan.severity in ("WARN", "ACT")
    assert len(plan.legs) == 1
    leg = plan.legs[0]
    assert leg.venue_from == "dex"
    assert leg.venue_to == "bybit"
    assert leg.asset == "USDC"
    assert leg.amount_usd > 0
    assert plan.imbalance_after_estimated < plan.imbalance_before


def test_rebalance_apply_restores_balance() -> None:
    inv = Inventory.with_initial_usd(500.0)
    inv.adjust("bybit", "USDT", -300.0)
    plan = plan_rebalance(inv, trigger_pct=0.20)
    apply_rebalance(inv, plan)
    new_plan = plan_rebalance(inv, trigger_pct=0.20)
    assert new_plan.severity == "NO-OP"


def test_rebalance_severity_act_when_auto_enabled() -> None:
    """Severity reads config.AUTO_REBALANCE at CALL time. Patch the
    attribute directly instead of reloading modules (which invalidates
    isinstance() in other tests' namespaces)."""
    inv = Inventory.with_initial_usd(500.0)
    inv.adjust("bybit", "USDT", -300.0)
    with patch.object(config, "AUTO_REBALANCE", True):
        plan = plan_rebalance(inv, trigger_pct=0.20)
    assert plan.severity == "ACT"


def test_rebalance_handles_no_stable_on_heavy_side() -> None:
    """If the heavy side has no stable to send, plan is no-op."""
    inv = Inventory()
    inv.bybit["BTC"] = 1000.0
    inv.bybit["USDT"] = 0.0
    inv.dex["USDC"] = 100.0
    plan = plan_rebalance(inv, trigger_pct=0.20)
    assert plan.severity == "NO-OP"
    assert "no_stable" in plan.reason


def test_watch_and_rebalance_returns_plan() -> None:
    inv = Inventory.with_initial_usd(500.0)
    inv.adjust("bybit", "USDT", -300.0)
    plan = watch_and_rebalance(inv)
    assert isinstance(plan, RebalancePlan)


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
