"""
Replay simulator.

Reads opportunities from data/arb/db/opportunities/ in chronological order,
executes the GO ones against a simulated inventory, and computes realized
PnL per trade including:

- Bybit taker fee  (op.bybit_fee_bps)
- DEX pool fee     (op.dex_fee_bps)
- Realized gas     (gas_gwei * gas_units * eth_price)
- Realized slippage:
    drawn from a triangular distribution centered on op.slippage_haircut_bps
    with width controlled by SLIPPAGE_NOISE_PCT. This is a stand-in until
    Phase 5 produces real fills we can fit a model on.
- Partial-fill probability:
    if partial: only PARTIAL_FILL_PCT of the notional fills, rest reverts.
    Assumed inversely related to OBI alignment with trade direction.

Output: rows written to data/arb/db/sim_trades/, PLUS aggregate metrics
(equity curve, hit rate, average realized vs theoretical bps gap).

Sharpe ratio computed at the end gates the project per CLAUDE.md
kill criterion: < 1.0 over 1 week of replay → pause for venue/pair rethink.

Determinism: ALL randomness is seeded via the rng argument. Same opportunities
+ same seed → exact same trades.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable

from src.sim.inventory import Inventory, PAIR_LEGS, VenueT
from src.strategy.opportunity import DEFAULT_DEX_GAS_UNITS

# Realized slippage = haircut * triangular(low, mode=1.0, high)
# with low=1-NOISE, high=1+NOISE. So expected E[multiplier]=1.0.
SLIPPAGE_NOISE_PCT = 0.30

# Probability of partial fill, per trade. In Phase 5 this gets replaced
# with a model conditioned on OBI / venue depth.
PARTIAL_FILL_PROB = 0.05
PARTIAL_FILL_PCT = 0.5  # if partial: this fraction of notional fills

# Minimum tick on Bybit fees if op record was missing them (defensive).
DEFAULT_BYBIT_TAKER_BPS = 10.0
DEFAULT_DEX_POOL_BPS = 5.0


@dataclass
class SimTrade:
    """Realized result of one simulated execution."""
    ts: str
    pair: str
    decision: str
    direction: str
    notional_usd: float
    spread_bps: float
    expected_net_bps: float
    realized_slippage_bps: float
    realized_gas_usd: float
    realized_pnl_usd: float
    realized_net_bps: float
    fill_pct: float          # 1.0 = full fill, < 1.0 = partial
    inventory_ok: bool
    inventory_reason: str
    bybit_usdt_after: float
    dex_usdc_after: float
    portfolio_usd_after: float


@dataclass
class ReplayResult:
    trades: list[SimTrade] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    starting_equity_usd: float = 0.0

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def n_filled(self) -> int:
        return sum(1 for t in self.trades if t.inventory_ok and t.fill_pct > 0)

    @property
    def n_inventory_rejected(self) -> int:
        return sum(1 for t in self.trades if not t.inventory_ok)

    @property
    def cumulative_pnl_usd(self) -> float:
        return sum(t.realized_pnl_usd for t in self.trades)

    @property
    def hit_rate(self) -> float:
        filled = [t for t in self.trades if t.inventory_ok and t.fill_pct > 0]
        if not filled:
            return 0.0
        wins = sum(1 for t in filled if t.realized_pnl_usd > 0)
        return wins / len(filled)

    @property
    def avg_realized_net_bps(self) -> float:
        filled = [t for t in self.trades if t.inventory_ok and t.fill_pct > 0]
        if not filled:
            return 0.0
        return sum(t.realized_net_bps for t in filled) / len(filled)

    def sharpe(self, periods_per_year: float = 252.0) -> float | None:
        """Annualized Sharpe of per-trade returns. None if < 2 trades."""
        rets = [t.realized_pnl_usd for t in self.trades if t.inventory_ok and t.fill_pct > 0]
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        if var <= 0:
            return None
        std = math.sqrt(var)
        return (mean / std) * math.sqrt(periods_per_year)


def realized_slippage(
    haircut_bps: float,
    rng: random.Random,
    noise_pct: float = SLIPPAGE_NOISE_PCT,
) -> float:
    """Triangular sample with mode = haircut, range ±noise."""
    if haircut_bps <= 0.0:
        return 0.0
    low = haircut_bps * (1.0 - noise_pct)
    high = haircut_bps * (1.0 + noise_pct)
    return rng.triangular(low=low, high=high, mode=haircut_bps)


def realized_fill_pct(
    rng: random.Random,
    obi_alignment: float = 1.0,
) -> float:
    """
    Returns 1.0 (full) most of the time, PARTIAL_FILL_PCT occasionally.
    Higher partial-fill probability when OBI is misaligned with our direction.

    obi_alignment: +1 means OBI agrees with our trade (favorable book), -1
    means OBI is against us (book about to flip away). Used as a multiplier.
    """
    base_p = PARTIAL_FILL_PROB
    if obi_alignment < 0:
        base_p = min(0.25, base_p * (1.0 - obi_alignment))  # up to 5x more likely
    return PARTIAL_FILL_PCT if rng.random() < base_p else 1.0


def _legs_for(pair: str, direction: str, notional_usd: float, fill_pct: float
              ) -> list[tuple[VenueT, str, float]]:
    """
    Return the inventory legs to apply for one trade leg.

    direction:
      "bybit_high" → sell on Bybit, buy on DEX
                     (debit BYBIT base asset, credit BYBIT USDT,
                      debit DEX USDC,        credit DEX base asset)
      "dex_high"   → buy on Bybit, sell on DEX (mirror)

    notional_usd: USD value of the trade leg (= notional_usd * fill_pct)
    Quantities of base asset are not modeled at unit precision in Phase 3;
    we use USD-equivalents for the simulator's bookkeeping.
    """
    bybit_base, dex_base = PAIR_LEGS[pair]
    fill_notional = notional_usd * fill_pct
    if direction == "bybit_high":
        return [
            ("bybit", bybit_base, -fill_notional),
            ("bybit", "USDT",     +fill_notional),
            ("dex",   "USDC",     -fill_notional),
            ("dex",   dex_base,   +fill_notional),
        ]
    elif direction == "dex_high":
        return [
            ("bybit", "USDT",     -fill_notional),
            ("bybit", bybit_base, +fill_notional),
            ("dex",   dex_base,   -fill_notional),
            ("dex",   "USDC",     +fill_notional),
        ]
    else:
        return []


def replay(
    opportunities: Iterable[dict],
    initial_usd_per_side: float,
    rng: random.Random | None = None,
    inventory: Inventory | None = None,
) -> ReplayResult:
    """
    Run the simulator over an iterable of opportunity dicts.

    opportunities: rows from data/arb/db/opportunities/ as dicts. Must include
        ts, pair, decision, direction, spread_bps, expected_net_bps,
        bybit_fee_bps, dex_fee_bps, gas_gwei, slippage_haircut_bps,
        notional_usd, weighted_obi.
    inventory: optional pre-seeded Inventory. Default = with_balanced_seed
        (50% stable + 50% base assets per side) — required for CEX-DEX arb
        to actually execute trades in either direction.
    """
    rng = rng or random.Random(0)
    inv = inventory if inventory is not None else \
        Inventory.with_balanced_seed(initial_usd_per_side)
    starting = initial_usd_per_side * 2
    result = ReplayResult(starting_equity_usd=starting)

    for op in opportunities:
        if op.get("decision") != "GO":
            continue
        pair = op["pair"]
        if pair not in PAIR_LEGS:
            continue

        notional = float(op.get("notional_usd", 0.0))
        if notional <= 0:
            continue
        direction = op.get("direction", "bybit_high")

        # Realized slippage + partial fill
        haircut = float(op.get("slippage_haircut_bps", 0.0))
        slippage_bps = realized_slippage(haircut, rng)
        obi_alignment = _obi_alignment(op.get("weighted_obi", 0.0), direction)
        fill_pct = realized_fill_pct(rng, obi_alignment)

        # Realized gas in USD (use opportunity's eth_price_used if present)
        gas_gwei = float(op.get("gas_gwei", 0.0))
        eth_price = float(op.get("eth_price_used", 3000.0))
        realized_gas = (DEFAULT_DEX_GAS_UNITS * gas_gwei * 1e-9) * eth_price

        # Cost stack — note bybit & dex fees scale with fill_pct
        bybit_fee_bps = float(op.get("bybit_fee_bps", DEFAULT_BYBIT_TAKER_BPS))
        dex_fee_bps   = float(op.get("dex_fee_bps", DEFAULT_DEX_POOL_BPS))
        gross_bps = abs(float(op.get("spread_bps", 0.0)))
        cost_bps = bybit_fee_bps + dex_fee_bps + slippage_bps
        cost_usd = (cost_bps / 10_000.0) * notional * fill_pct + realized_gas

        gross_usd = (gross_bps / 10_000.0) * notional * fill_pct
        realized_pnl = gross_usd - cost_usd
        realized_net_bps = ((realized_pnl / notional) * 10_000.0) if notional > 0 else 0.0

        # Inventory check — if rejected, the trade doesn't actually happen.
        # Realized PnL must be 0 (we didn't trade), not "what we would have made"
        # (which would double-count vs. theoretical_pnl_usd in opportunities table).
        legs = _legs_for(pair, direction, notional, fill_pct)
        inv_ok, inv_reason = inv.can_apply(legs)
        if inv_ok:
            inv.apply(legs)
            inv.book_pnl(realized_pnl)
        else:
            realized_pnl = 0.0
            realized_net_bps = 0.0
            slippage_bps = 0.0
            realized_gas = 0.0
            fill_pct = 0.0

        # Mark-to-market — for the equity curve we use a simple proxy:
        # USDT + USDC + base assets at last spread mid.
        bybit_mid = float(op.get("bybit_mid", 0.0))
        dex_mid = float(op.get("dex_mid", 0.0))
        bybit_base, dex_base = PAIR_LEGS[pair]
        bybit_prices = {bybit_base: bybit_mid}
        dex_prices = {dex_base: dex_mid}
        portfolio_usd = inv.total_usd(bybit_prices, dex_prices)

        trade = SimTrade(
            ts=op["ts"],
            pair=pair,
            decision="GO",
            direction=direction,
            notional_usd=notional,
            spread_bps=float(op.get("spread_bps", 0.0)),
            expected_net_bps=float(op.get("expected_net_bps", 0.0)),
            realized_slippage_bps=round(slippage_bps, 4),
            realized_gas_usd=round(realized_gas, 6),
            realized_pnl_usd=round(realized_pnl, 6),
            realized_net_bps=round(realized_net_bps, 4),
            fill_pct=round(fill_pct, 3),
            inventory_ok=inv_ok,
            inventory_reason=inv_reason,
            bybit_usdt_after=round(inv.get("bybit", "USDT"), 6),
            dex_usdc_after=round(inv.get("dex", "USDC"), 6),
            portfolio_usd_after=round(portfolio_usd, 6),
        )
        result.trades.append(trade)
        result.equity_curve.append((op["ts"], portfolio_usd))

    return result


def _obi_alignment(weighted_obi: float, direction: str) -> float:
    """
    Returns +1 if OBI agrees with trade direction (favorable),
            -1 if OBI is against the trade.
    For "bybit_high" we sell on Bybit → favorable if asks are thin
    (OBI > 0 means bids dominate → asks thin → favorable for selling there).
    For "dex_high" we buy on Bybit → favorable if bids are thin
    (OBI < 0 means asks dominate → bids thin → unfavorable for buying →
    actually wait, we buy with USDT against asks; thin bids don't matter
    for buying. So buying is favorable when asks are thin → OBI > 0).
    Both directions favored by OBI > 0 in this simple model. Phase 5 may
    refine.
    """
    return 1.0 if weighted_obi > 0 else -1.0
