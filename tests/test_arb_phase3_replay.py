"""
Phase 3 — replay simulator tests.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.sim.replay import (
    PARTIAL_FILL_PCT, ReplayResult, _legs_for, _obi_alignment,
    realized_fill_pct, realized_slippage, replay,
)


def _opp(decision="GO", direction="bybit_high", spread_bps=20.0,
         expected_net_bps=10.0, slippage_haircut_bps=5.0,
         pair="BTCUSDT", notional_usd=50.0, weighted_obi=0.1,
         gas_gwei=0.006, eth_price_used=3000.0,
         bybit_fee_bps=10.0, dex_fee_bps=5.0,
         bybit_mid=80000.0, dex_mid=79840.0, ts="2026-05-10T12:00:00+00:00"):
    return {
        "ts": ts, "pair": pair, "decision": decision, "direction": direction,
        "spread_bps": spread_bps, "expected_net_bps": expected_net_bps,
        "slippage_haircut_bps": slippage_haircut_bps,
        "notional_usd": notional_usd, "weighted_obi": weighted_obi,
        "gas_gwei": gas_gwei, "eth_price_used": eth_price_used,
        "bybit_fee_bps": bybit_fee_bps, "dex_fee_bps": dex_fee_bps,
        "bybit_mid": bybit_mid, "dex_mid": dex_mid,
    }


# --- realized_slippage -----------------------------------------------------


def test_realized_slippage_zero_when_haircut_zero() -> None:
    rng = random.Random(0)
    assert realized_slippage(0.0, rng) == 0.0


def test_realized_slippage_in_noise_band() -> None:
    rng = random.Random(0)
    haircut = 10.0
    samples = [realized_slippage(haircut, rng) for _ in range(200)]
    # noise = 0.30 → expected band [7, 13]
    assert all(7.0 <= s <= 13.0 for s in samples)
    mean = sum(samples) / len(samples)
    # Mode = 10, triangular roughly centered, mean within tolerance
    assert 9.0 < mean < 11.0


# --- realized_fill_pct -----------------------------------------------------


def test_fill_pct_returns_full_or_partial() -> None:
    rng = random.Random(0)
    samples = [realized_fill_pct(rng) for _ in range(500)]
    assert all(s in (1.0, PARTIAL_FILL_PCT) for s in samples)


def test_fill_pct_more_partials_when_obi_against() -> None:
    rng_a = random.Random(42)
    rng_b = random.Random(42)
    n = 400
    aligned = sum(1 for _ in range(n) if realized_fill_pct(rng_a, obi_alignment=1.0) < 1.0)
    against = sum(1 for _ in range(n) if realized_fill_pct(rng_b, obi_alignment=-1.0) < 1.0)
    assert against > aligned, f"misaligned should produce more partials, got aligned={aligned} against={against}"


# --- _obi_alignment --------------------------------------------------------


def test_obi_alignment_positive_when_obi_positive() -> None:
    assert _obi_alignment(0.5, "bybit_high") == 1.0
    assert _obi_alignment(0.5, "dex_high") == 1.0


def test_obi_alignment_negative_when_obi_negative() -> None:
    assert _obi_alignment(-0.5, "bybit_high") == -1.0


def test_obi_alignment_neutral_at_zero() -> None:
    # zero OBI → treat as misaligned (conservative)
    assert _obi_alignment(0.0, "bybit_high") == -1.0


# --- _legs_for -------------------------------------------------------------


def test_legs_for_bybit_high_btc() -> None:
    """Sell BTC on Bybit (debit BTC, credit USDT); buy cbBTC on DEX."""
    legs = _legs_for("BTCUSDT", "bybit_high", notional_usd=50.0, fill_pct=1.0)
    venues_assets = {(v, a): d for v, a, d in legs}
    assert venues_assets[("bybit", "BTC")] == -50.0
    assert venues_assets[("bybit", "USDT")] == +50.0
    assert venues_assets[("dex", "USDC")] == -50.0
    assert venues_assets[("dex", "cbBTC")] == +50.0


def test_legs_for_dex_high_eth() -> None:
    legs = _legs_for("ETHUSDT", "dex_high", notional_usd=50.0, fill_pct=1.0)
    venues_assets = {(v, a): d for v, a, d in legs}
    assert venues_assets[("bybit", "USDT")] == -50.0
    assert venues_assets[("bybit", "ETH")] == +50.0
    assert venues_assets[("dex", "WETH")] == -50.0
    assert venues_assets[("dex", "USDC")] == +50.0


def test_legs_for_partial_fill() -> None:
    legs = _legs_for("BTCUSDT", "bybit_high", notional_usd=50.0, fill_pct=0.5)
    venues_assets = {(v, a): d for v, a, d in legs}
    assert venues_assets[("bybit", "USDT")] == +25.0


# --- replay end-to-end -----------------------------------------------------


def test_replay_skips_non_go() -> None:
    opps = [_opp(decision="SKIP"), _opp(decision="SKIP")]
    r = replay(opps, initial_usd_per_side=500.0, rng=random.Random(0))
    assert r.n_trades == 0


def test_replay_writes_one_trade_per_go() -> None:
    opps = [_opp(decision="GO"), _opp(decision="SKIP"), _opp(decision="GO")]
    r = replay(opps, initial_usd_per_side=500.0, rng=random.Random(0))
    assert r.n_trades == 2


def test_replay_deterministic_under_same_seed() -> None:
    opps = [_opp(decision="GO", ts=f"2026-05-10T12:00:0{i}+00:00") for i in range(5)]
    r1 = replay(opps, initial_usd_per_side=500.0, rng=random.Random(42))
    r2 = replay(opps, initial_usd_per_side=500.0, rng=random.Random(42))
    assert len(r1.trades) == len(r2.trades)
    for a, b in zip(r1.trades, r2.trades):
        assert a.realized_pnl_usd == b.realized_pnl_usd
        assert a.realized_slippage_bps == b.realized_slippage_bps
        assert a.fill_pct == b.fill_pct


def test_replay_different_seeds_diverge() -> None:
    opps = [_opp(decision="GO", ts=f"2026-05-10T12:00:0{i}+00:00") for i in range(20)]
    r1 = replay(opps, initial_usd_per_side=500.0, rng=random.Random(1))
    r2 = replay(opps, initial_usd_per_side=500.0, rng=random.Random(2))
    pnl1 = [t.realized_pnl_usd for t in r1.trades]
    pnl2 = [t.realized_pnl_usd for t in r2.trades]
    assert pnl1 != pnl2  # at least one differs


def test_replay_inventory_eventually_rejects() -> None:
    """With $50 notional and only $100 bankroll, we run out fast."""
    opps = [_opp(decision="GO", direction="bybit_high",
                 ts=f"2026-05-10T12:00:0{i}+00:00") for i in range(10)]
    r = replay(opps, initial_usd_per_side=100.0, rng=random.Random(0))
    # Each bybit_high trade debits $50 from dex USDC.
    # After 2 trades, USDC = 0 → 3rd should reject.
    assert r.n_inventory_rejected > 0


def test_replay_pnl_aggregates() -> None:
    """Cumulative PnL == sum of per-trade realized PnL."""
    opps = [_opp(decision="GO", spread_bps=30.0, expected_net_bps=15.0,
                  ts=f"2026-05-10T12:00:0{i}+00:00") for i in range(5)]
    r = replay(opps, initial_usd_per_side=1000.0, rng=random.Random(0))
    total = sum(t.realized_pnl_usd for t in r.trades)
    assert abs(r.cumulative_pnl_usd - total) < 1e-9


def test_replay_hit_rate_in_zero_one() -> None:
    opps = [_opp(decision="GO", ts=f"2026-05-10T12:00:0{i}+00:00") for i in range(20)]
    r = replay(opps, initial_usd_per_side=1000.0, rng=random.Random(0))
    assert 0.0 <= r.hit_rate <= 1.0


def test_replay_sharpe_none_when_under_two_trades() -> None:
    opps = [_opp(decision="GO")]
    r = replay(opps, initial_usd_per_side=500.0, rng=random.Random(0))
    assert r.sharpe() is None


def test_replay_equity_curve_length_matches_trades() -> None:
    opps = [_opp(decision="GO", ts=f"2026-05-10T12:00:0{i}+00:00") for i in range(7)]
    r = replay(opps, initial_usd_per_side=500.0, rng=random.Random(0))
    assert len(r.equity_curve) == r.n_trades


def test_replay_unknown_pair_skipped() -> None:
    opps = [_opp(pair="XRPUSDT")]   # not in PAIR_LEGS
    r = replay(opps, initial_usd_per_side=500.0, rng=random.Random(0))
    assert r.n_trades == 0


def test_replay_zero_notional_skipped() -> None:
    opps = [_opp(notional_usd=0.0)]
    r = replay(opps, initial_usd_per_side=500.0, rng=random.Random(0))
    assert r.n_trades == 0


def test_replay_inventory_rejection_zeros_pnl() -> None:
    """Regression: when inventory blocks a trade, realized_pnl_usd MUST be 0
    (we didn't trade), not 'what we would have made' which would inflate PnL."""
    from src.sim.inventory import Inventory
    inv = Inventory()  # empty inventory → every trade rejected
    opps = [_opp(decision="GO", spread_bps=100.0, expected_net_bps=80.0,
                  ts=f"2026-05-10T12:00:0{i}+00:00") for i in range(3)]
    r = replay(opps, initial_usd_per_side=0.0,
                rng=random.Random(0), inventory=inv)
    assert r.n_trades == 3
    assert r.n_filled == 0
    assert r.n_inventory_rejected == 3
    assert r.cumulative_pnl_usd == 0.0
    for t in r.trades:
        assert t.realized_pnl_usd == 0.0
        assert t.fill_pct == 0.0
        assert not t.inventory_ok


def test_replay_balanced_seed_executes_first_trade() -> None:
    """With the default balanced seed, the first GO trade actually fills
    (not inventory-rejected). Regression for the unbalanced-seed bug."""
    opps = [_opp(decision="GO", direction="bybit_high",
                  ts="2026-05-10T12:00:00+00:00")]
    r = replay(opps, initial_usd_per_side=500.0, rng=random.Random(0))
    assert r.n_trades == 1
    assert r.n_filled == 1
    assert r.trades[0].inventory_ok


def test_replay_equity_grows_with_realized_pnl() -> None:
    """Regression for the MTM bug (was reporting $13M on $1k portfolio
    because inventory mistakenly multiplied USD-equivalent by price).
    After fix: equity should grow by realized_pnl_usd per filled trade."""
    opps = [_opp(decision="GO", direction="bybit_high" if i % 2 == 0 else "dex_high",
                  spread_bps=30.0, expected_net_bps=12.0,
                  ts=f"2026-05-10T12:00:{i:02d}+00:00") for i in range(10)]
    r = replay(opps, initial_usd_per_side=500.0, rng=random.Random(42))
    starting = 1000.0  # 500 each side
    final_equity = r.equity_curve[-1][1]
    # Final equity should be starting + cumulative_pnl, within rounding
    assert abs(final_equity - (starting + r.cumulative_pnl_usd)) < 0.01, \
        f"final={final_equity}, starting={starting}, pnl={r.cumulative_pnl_usd}"
    # And the order of magnitude must be sane (not millions)
    assert 999.0 <= final_equity <= 1010.0, f"equity blew up: {final_equity}"


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
