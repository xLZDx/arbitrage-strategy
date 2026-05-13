"""
Phase 2 regression tests — opportunity detector.

Pure-logic tests: deterministic. Verify decision matrix against hand-computed
bps, edge cases (negative spreads, zero mid, extreme cancellation rate),
and helper math.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.strategy.opportunity import (
    BYBIT_TAKER_FEE_BPS, DEFAULT_DEX_GAS_UNITS, ETH_PRICE_USD_FALLBACK,
    Opportunity, detect_opportunity, estimate_gas_cost_bps,
    estimate_slippage_haircut_bps, opportunity_to_row,
)
from src.utils import config


# --- estimate_gas_cost_bps -------------------------------------------------


def test_gas_cost_zero_when_gas_zero() -> None:
    assert estimate_gas_cost_bps(0.0, 1000.0) == 0.0


def test_gas_cost_inf_when_notional_zero() -> None:
    assert estimate_gas_cost_bps(1.0, 0.0) == float("inf")
    assert estimate_gas_cost_bps(1.0, -10.0) == float("inf")


def test_gas_cost_base_l2_realistic() -> None:
    """Base typical: 0.006 gwei × 180k gas × $3000/ETH = $0.0032
       on $50 notional → 0.0032/50 * 10000 = 0.65 bps."""
    cost_bps = estimate_gas_cost_bps(
        gas_total_gwei=0.006, notional_usd=50.0,
        eth_price_usd=3000.0, gas_units=DEFAULT_DEX_GAS_UNITS,
    )
    assert 0.5 < cost_bps < 1.0, f"got {cost_bps}"


def test_gas_cost_eth_mainnet_realistic() -> None:
    """Eth mainnet typical: 30 gwei × 180k gas × $3000/ETH = $16.2
       on $50 notional → 16.2/50 * 10000 = 3240 bps. Validates the bps
       sensitivity that justifies Q1's L2-not-mainnet decision."""
    cost_bps = estimate_gas_cost_bps(
        gas_total_gwei=30.0, notional_usd=50.0,
        eth_price_usd=3000.0, gas_units=DEFAULT_DEX_GAS_UNITS,
    )
    assert cost_bps > 1000, f"mainnet should be unviable at $50 notional, got {cost_bps}"


# --- estimate_slippage_haircut_bps -----------------------------------------


def test_slippage_haircut_floor() -> None:
    """Zero OBI imbalance + zero cancellation → just baseline 5 bps."""
    h = estimate_slippage_haircut_bps(weighted_obi=0.0, cancellation_rate=0.0)
    assert h == 5.0


def test_slippage_haircut_obi_adds() -> None:
    """OBI = 1 → +5 bps adjustment → total 10 bps."""
    h = estimate_slippage_haircut_bps(weighted_obi=1.0, cancellation_rate=0.0)
    assert h == 10.0
    h2 = estimate_slippage_haircut_bps(weighted_obi=-1.0, cancellation_rate=0.0)
    assert h2 == 10.0  # uses absolute value


def test_slippage_haircut_spoof_dominates() -> None:
    """High cancellation rate → close to ceiling."""
    h = estimate_slippage_haircut_bps(weighted_obi=0.0, cancellation_rate=1.0)
    assert h == min(20.0, config.MAX_SLIPPAGE_BPS_ABSOLUTE)


def test_slippage_haircut_capped() -> None:
    """Combined extremes are bounded by MAX_SLIPPAGE_BPS_ABSOLUTE.
    Natural max with current weights = 5 + 5 + 15 = 25 bps (under the 30 cap).
    Test verifies haircut stays in expected range AND below cap."""
    h = estimate_slippage_haircut_bps(weighted_obi=1.0, cancellation_rate=1.0)
    assert 24.0 <= h <= config.MAX_SLIPPAGE_BPS_ABSOLUTE


# --- detect_opportunity --------------------------------------------------


def _kwargs(**overrides):
    base = dict(
        ts="2026-05-10T12:00:00+00:00",
        pair="BTCUSDT",
        bybit_bid=80000.0,
        bybit_ask=80001.0,
        dex_mid=79999.0,  # 1 USD below bybit_mid (80000.5)
        weighted_obi=0.0,
        obi_delta=0.0,
        cancellation_rate=0.0,
        gas_total_gwei=0.006,
        pool_fee_bps=5.0,
        notional_usd=50.0,
        eth_price_usd=3000.0,
    )
    base.update(overrides)
    return base


def test_detect_returns_opportunity_record() -> None:
    op = detect_opportunity(**_kwargs())
    assert isinstance(op, Opportunity)
    assert op.pair == "BTCUSDT"


# --- P0-7 regression: dex_fee unit confusion (was 100x bug) ---------------


def test_pool_fee_raw_tier_500_passed_as_bps_skips_everything() -> None:
    """REGRESSION for the 2026-05-11 100x dex_fee bug.
    Uniswap V3 raw fee tier 500 means 0.05% = 5 bps. If a future caller
    passes 500 directly into detect_opportunity (treating it as bps), the
    cost stack inflates by ~100x and every spread gets SKIP-negative_after_costs.
    Production callers (detector_main.py) MUST convert via cfg.fee_bps / 100.0."""
    # Clear-profit spread (~100 bps gross) that would normally GO at 5 bps fee
    op_correct = detect_opportunity(**_kwargs(dex_mid=79200.0, pool_fee_bps=5.0))
    assert op_correct.decision == "GO", (
        f"5 bps pool fee MUST allow GO on 100 bps spread; "
        f"got SKIP reason={op_correct.reason}, "
        f"expected_net_bps={op_correct.expected_net_bps}"
    )

    # SAME spread, but caller forgot to convert: passes 500 as bps directly
    op_wrong = detect_opportunity(**_kwargs(dex_mid=79200.0, pool_fee_bps=500.0))
    assert op_wrong.decision == "SKIP", (
        f"Raw V3 fee tier 500 passed as bps MUST SKIP (cost ~516 bps > gross 100 bps); "
        f"got GO with expected_net_bps={op_wrong.expected_net_bps}. "
        f"This is the exact pre-fix bug — regression detected."
    )
    assert op_wrong.dex_fee_bps == 500.0, (
        "The inflation should be visible in the persisted record so audit "
        "tools can detect the mis-call after the fact."
    )
    # The bug is invisible in the SKIP reason if we don't surface it explicitly;
    # but the magnitude is detectable: cost-after-fee should exceed gross.
    cost_total = (op_wrong.bybit_fee_bps + op_wrong.dex_fee_bps
                  + op_wrong.gas_cost_bps + op_wrong.slippage_haircut_bps)
    assert cost_total > op_wrong.gross_bps, (
        f"With raw tier as bps, total cost ({cost_total:.1f}) MUST exceed "
        f"gross ({op_wrong.gross_bps:.1f}) — that's the bug signature."
    )


def test_pool_fee_3000_raw_tier_also_blocked() -> None:
    """The 0.30% tier (3000 raw) similarly explodes the cost stack if mis-passed."""
    op_correct = detect_opportunity(**_kwargs(dex_mid=79200.0, pool_fee_bps=30.0))
    op_wrong = detect_opportunity(**_kwargs(dex_mid=79200.0, pool_fee_bps=3000.0))
    assert op_correct.expected_net_bps > op_wrong.expected_net_bps + 2000


def test_detector_main_does_the_fee_conversion() -> None:
    """Regression: detector_main.py MUST convert cfg.fee_bps from Uniswap
    raw fee tier to actual bps before passing to detect_opportunity. The
    contract is satisfied by EITHER:
      - inline division: cfg.fee_bps / 100
      - the dedicated accessor: cfg.fee_bps_actual (added in P1-2)
    Both are acceptable; the test fails only if neither appears."""
    import inspect
    from src.strategy import detector_main
    source = inspect.getsource(detector_main)
    has_inline_div = ("cfg.fee_bps / 100" in source
                      or "cfg.fee_bps/100" in source)
    has_accessor = "cfg.fee_bps_actual" in source
    assert has_inline_div or has_accessor, (
        "detector_main.py must convert cfg.fee_bps from raw Uniswap tier "
        "to actual bps. Use cfg.fee_bps_actual (preferred) or inline "
        "cfg.fee_bps / 100. The unit conversion is the entire fix for "
        "the 2026-05-11 100x cost overcount bug."
    )


def test_pool_config_fee_bps_actual_accessor() -> None:
    """REGRESSION P1-2: PoolConfig.fee_bps_actual returns the bps value
    (raw_tier / 100), preventing the unit-confusion landmine."""
    from src.data.dex_quote import PILOT_POOLS
    eth_cfg = PILOT_POOLS["ETHUSDT"]
    assert eth_cfg.fee_bps == 500  # raw Uniswap tier
    assert eth_cfg.fee_bps_actual == 5.0  # actual bps
    assert eth_cfg.uniswap_fee_tier == 500  # alias confirmation


def test_detect_zero_mid_returns_skip() -> None:
    op = detect_opportunity(**_kwargs(bybit_bid=0.0, bybit_ask=0.0))
    assert op.decision == "SKIP"
    assert op.reason == "non_positive_mid"


def test_detect_direction_bybit_high() -> None:
    op = detect_opportunity(**_kwargs(bybit_bid=80100.0, bybit_ask=80101.0,
                                        dex_mid=80000.0))
    assert op.direction == "bybit_high"
    assert op.spread_bps > 0
    assert op.gross_bps > 0


def test_detect_direction_dex_high() -> None:
    op = detect_opportunity(**_kwargs(bybit_bid=79999.0, bybit_ask=80000.0,
                                        dex_mid=80100.0))
    assert op.direction == "dex_high"
    assert op.spread_bps < 0
    assert op.gross_bps > 0


def test_detect_skip_when_below_cost_floor() -> None:
    """Tiny spread that doesn't cover the fees."""
    op = detect_opportunity(**_kwargs(dex_mid=80000.4))  # ~0.06 bps
    assert op.decision == "SKIP"
    # Should bind on negative_after_costs (cost > gross)
    assert op.reason in ("negative_after_costs", "below_min_net_bps")
    assert op.expected_net_bps < config.MIN_NET_BPS


def test_detect_skip_when_below_min_net_threshold() -> None:
    """Profitable but under MIN_NET_BPS (8 bps default)."""
    # Make gross ~10 bps (just barely profitable after costs)
    op = detect_opportunity(**_kwargs(dex_mid=79919.0))  # ~10 bps gross
    if op.decision == "SKIP" and op.reason == "below_min_net_bps":
        assert op.expected_net_bps < config.MIN_NET_BPS
    else:
        # If costs eat it, that's also valid
        assert op.decision == "SKIP"


def test_detect_go_when_clear_profit() -> None:
    """100 bps gross spread on Base — should easily clear all costs."""
    op = detect_opportunity(**_kwargs(dex_mid=79200.0))  # ~100 bps gross
    assert op.decision == "GO"
    assert op.reason == "passes_threshold"
    assert op.expected_net_bps >= config.MIN_NET_BPS
    assert op.theoretical_pnl_usd > 0


def test_detect_skip_on_spoofing() -> None:
    """Even with great spread, high cancellation rate → SKIP."""
    op = detect_opportunity(**_kwargs(dex_mid=79200.0, cancellation_rate=0.8))
    assert op.decision == "SKIP"
    assert op.reason == "spoofing_detected"


def test_detect_pnl_matches_net_bps() -> None:
    """theoretical_pnl_usd = notional * net_bps / 10_000."""
    op = detect_opportunity(**_kwargs(dex_mid=79200.0, notional_usd=100.0))
    if op.decision == "GO":
        expected = round(100.0 * op.expected_net_bps / 10_000.0, 6)
        assert abs(op.theoretical_pnl_usd - expected) < 1e-9


def test_detect_costs_breakdown_sums_correctly() -> None:
    op = detect_opportunity(**_kwargs(dex_mid=79200.0))
    sum_costs = (op.bybit_fee_bps + op.dex_fee_bps + op.gas_cost_bps
                 + op.slippage_haircut_bps)
    assert abs((op.gross_bps - sum_costs) - op.expected_net_bps) < 0.01


def test_detect_records_inputs_verbatim() -> None:
    """Opportunity row preserves all inputs (Phase 6 needs them as features)."""
    op = detect_opportunity(**_kwargs(weighted_obi=0.42, obi_delta=0.05))
    assert op.weighted_obi == 0.42
    assert op.obi_delta == 0.05
    assert op.bybit_fee_bps == BYBIT_TAKER_FEE_BPS
    assert op.dex_fee_bps == 5.0
    assert op.eth_price_used == 3000.0


def test_detect_negative_spread_still_logs() -> None:
    """Negative spread must produce a row (negative samples needed for ML)."""
    op = detect_opportunity(**_kwargs(bybit_bid=80000, bybit_ask=80000,
                                        dex_mid=80100))
    assert op.decision == "SKIP"
    assert op.spread_bps < 0
    # row is still emitted
    assert isinstance(opportunity_to_row(op), dict)


# --- opportunity_to_row ---------------------------------------------------


def test_opportunity_to_row_keys_match_dataclass() -> None:
    op = detect_opportunity(**_kwargs())
    row = opportunity_to_row(op)
    expected = {
        "ts", "pair", "bybit_mid", "bybit_bid", "bybit_ask", "dex_mid",
        "spread_bps", "gross_bps", "direction", "weighted_obi", "obi_delta",
        "cancellation_rate", "gas_gwei", "gas_cost_bps", "bybit_fee_bps",
        "dex_fee_bps", "slippage_haircut_bps", "expected_net_bps",
        "notional_usd", "theoretical_pnl_usd", "decision", "reason",
        "eth_price_used",
    }
    assert set(row.keys()) == expected


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
