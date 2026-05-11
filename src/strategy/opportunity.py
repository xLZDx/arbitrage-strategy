"""
Rule-based arbitrage opportunity detector (Phase 2, no execution).

Pure logic: given the latest Bybit microstructure (best bid/ask + OBI),
DEX mid-price, and gas reading, decide whether an arbitrage setup
exists and compute the expected net PnL in bps.

Every observation produces an Opportunity row that gets logged to
data/arb/db/opportunities/. Rows where decision == "GO" are the labelable
candidates for Phase 6 HistGBT training (success label = realized net bps
within window > MIN_NET_BPS).

Decision flow:
  1. Compute gross_bps = (bybit_mid - dex_mid) / mid_price * 10_000.
  2. Direction:
       gross > 0 → bybit_high (sell on Bybit, buy on DEX).
       gross < 0 → dex_high   (buy on Bybit, sell on DEX).
  3. Subtract estimated costs:
       - Bybit taker fee   (10 bps, conservative)
       - DEX pool fee      (cfg.fee_bps)
       - Gas cost in bps   (gas_units × gas_price × ETH_USD / notional)
       - Slippage haircut  (dynamic, OBI-driven)
  4. If net_bps >= MIN_NET_BPS  → GO with reason "passes_threshold".
     Else                       → SKIP with reason describing the binding constraint.

This module has NO side effects: no I/O, no clock. Easy to test deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Literal

from src.utils import config

DirectionT = Literal["bybit_high", "dex_high"]
DecisionT = Literal["GO", "SKIP"]

# Re-export from config so callers can override per-trade. Maker is the
# default cost basis when ARB_PREFER_MAKER=1 (drops Bybit fee 10x).
BYBIT_TAKER_FEE_BPS = config.BYBIT_TAKER_FEE_BPS
BYBIT_MAKER_FEE_BPS = config.BYBIT_MAKER_FEE_BPS

# Default per-swap gas units for a Uniswap V3 router call on Base.
# Replaced by realized values in Phase 5 once we have execution data.
DEFAULT_DEX_GAS_UNITS = 180_000

# Reference ETH price for converting gas (gwei) → USD.
# Phase 1 stub; replaced with live ETHUSDT mid in Phase 6+ feature pipe.
ETH_PRICE_USD_FALLBACK = 3000.0

# Sanity ceiling on spread (bps). Anything wider screams "bad pool data,
# wrong address, or decimal-orientation bug" — never act on it. Real arb
# spreads on majors are 1-50 bps; long-tail can hit 100-500 bps; > 1000 bps
# is implausible enough to be 99.99% bug. Caught the AERO-pool address bug
# 2026-05-11 (returned garbage data showing -4 trillion bps spread).
IMPLAUSIBLE_SPREAD_BPS = 1000.0


@dataclass(frozen=True)
class Opportunity:
    ts: str                 # UTC ISO8601
    pair: str               # e.g. "BTCUSDT"
    bybit_mid: float
    bybit_bid: float
    bybit_ask: float
    dex_mid: float
    spread_bps: float       # signed; positive = bybit higher than dex
    gross_bps: float        # abs(spread_bps)
    direction: DirectionT
    weighted_obi: float
    obi_delta: float
    cancellation_rate: float
    gas_gwei: float
    gas_cost_bps: float
    bybit_fee_bps: float
    dex_fee_bps: float
    slippage_haircut_bps: float
    expected_net_bps: float
    notional_usd: float
    theoretical_pnl_usd: float
    decision: DecisionT
    reason: str
    eth_price_used: float


def estimate_gas_cost_bps(
    gas_total_gwei: float,
    notional_usd: float,
    eth_price_usd: float = ETH_PRICE_USD_FALLBACK,
    gas_units: int = DEFAULT_DEX_GAS_UNITS,
) -> float:
    """
    Convert gas (in gwei) to bps of a notional trade.

    cost_eth = gas_units * gas_total_gwei * 1e-9
    cost_usd = cost_eth * eth_price_usd
    cost_bps = cost_usd / notional_usd * 10_000
    """
    if notional_usd <= 0:
        return float("inf")
    cost_eth = gas_units * gas_total_gwei * 1e-9
    cost_usd = cost_eth * eth_price_usd
    return cost_usd / notional_usd * 10_000.0


def estimate_slippage_haircut_bps(
    weighted_obi: float,
    cancellation_rate: float,
) -> float:
    """
    Heuristic dynamic slippage haircut from OBI signals.

    - Strong OBI alignment with our trade direction → smaller haircut (book is
      receptive; we'll fill near top).
    - High cancellation rate → larger haircut (artificial liquidity; orders
      may evaporate).
    - Always capped at MAX_SLIPPAGE_BPS_ABSOLUTE.

    Phase 6 replaces this with a learned regression on realized slippage.
    For Phase 2 it's a simple defensible default.
    """
    base = 5.0  # bps baseline
    obi_adjustment = abs(weighted_obi) * 5.0  # up to +5 bps when imbalanced
    spoof_penalty = cancellation_rate * 15.0  # up to +15 bps when fully suspect
    haircut = base + obi_adjustment + spoof_penalty
    return min(haircut, config.MAX_SLIPPAGE_BPS_ABSOLUTE)


def detect_opportunity(
    *,
    ts: str,
    pair: str,
    bybit_bid: float,
    bybit_ask: float,
    dex_mid: float,
    weighted_obi: float,
    obi_delta: float,
    cancellation_rate: float,
    gas_total_gwei: float,
    pool_fee_bps: float,
    notional_usd: float = config.BANKROLL_PER_SIDE_USD * config.PER_TRADE_CAP_PCT / 100.0,
    eth_price_usd: float = ETH_PRICE_USD_FALLBACK,
    bybit_fee_bps: float | None = None,
) -> Opportunity:
    """
    bybit_fee_bps: override the Bybit fee used in cost calc. Defaults to
    BYBIT_MAKER_FEE_BPS if config.PREFER_MAKER else BYBIT_TAKER_FEE_BPS.
    """
    if bybit_fee_bps is None:
        bybit_fee_bps = (BYBIT_MAKER_FEE_BPS if config.PREFER_MAKER
                          else BYBIT_TAKER_FEE_BPS)
    """
    Pure decision function. Always returns an Opportunity record (never None);
    decision="GO" or "SKIP". Every call must produce a row so the dataset
    captures veto reasons too — Phase 6 needs negative samples.
    """
    bybit_mid = (bybit_bid + bybit_ask) / 2.0
    if bybit_mid <= 0 or dex_mid <= 0:
        return Opportunity(
            ts=ts, pair=pair, bybit_mid=bybit_mid, bybit_bid=bybit_bid,
            bybit_ask=bybit_ask, dex_mid=dex_mid,
            spread_bps=0.0, gross_bps=0.0, direction="bybit_high",
            weighted_obi=weighted_obi, obi_delta=obi_delta,
            cancellation_rate=cancellation_rate,
            gas_gwei=gas_total_gwei, gas_cost_bps=0.0,
            bybit_fee_bps=bybit_fee_bps, dex_fee_bps=pool_fee_bps,
            slippage_haircut_bps=0.0, expected_net_bps=0.0,
            notional_usd=notional_usd, theoretical_pnl_usd=0.0,
            decision="SKIP", reason="non_positive_mid",
            eth_price_used=eth_price_usd,
        )

    spread_bps = (bybit_mid - dex_mid) / bybit_mid * 10_000.0
    gross_bps = abs(spread_bps)
    direction: DirectionT = "bybit_high" if spread_bps > 0 else "dex_high"

    gas_cost_bps = estimate_gas_cost_bps(gas_total_gwei, notional_usd,
                                          eth_price_usd=eth_price_usd)
    slippage_bps = estimate_slippage_haircut_bps(weighted_obi, cancellation_rate)
    total_cost_bps = (bybit_fee_bps + pool_fee_bps + gas_cost_bps + slippage_bps)
    expected_net_bps = round(gross_bps - total_cost_bps, 4)
    theoretical_pnl_usd = round(notional_usd * expected_net_bps / 10_000.0, 6)

    decision: DecisionT = "GO"
    reason = "passes_threshold"
    if gross_bps > IMPLAUSIBLE_SPREAD_BPS:
        decision = "SKIP"
        reason = "implausible_spread"
    elif gross_bps < total_cost_bps:
        decision = "SKIP"
        reason = "negative_after_costs"
    elif expected_net_bps < config.MIN_NET_BPS:
        decision = "SKIP"
        reason = "below_min_net_bps"
    elif cancellation_rate > 0.6:
        decision = "SKIP"
        reason = "spoofing_detected"

    return Opportunity(
        ts=ts, pair=pair, bybit_mid=round(bybit_mid, 6),
        bybit_bid=bybit_bid, bybit_ask=bybit_ask, dex_mid=round(dex_mid, 6),
        spread_bps=round(spread_bps, 4), gross_bps=round(gross_bps, 4),
        direction=direction,
        weighted_obi=weighted_obi, obi_delta=obi_delta,
        cancellation_rate=cancellation_rate,
        gas_gwei=gas_total_gwei,
        gas_cost_bps=round(gas_cost_bps, 4),
        bybit_fee_bps=bybit_fee_bps,
        dex_fee_bps=pool_fee_bps,
        slippage_haircut_bps=round(slippage_bps, 4),
        expected_net_bps=expected_net_bps,
        notional_usd=notional_usd,
        theoretical_pnl_usd=theoretical_pnl_usd,
        decision=decision,
        reason=reason,
        eth_price_used=eth_price_usd,
    )


def opportunity_to_row(op: Opportunity) -> dict:
    """Adapter for arb_store.write_records()."""
    return asdict(op)
