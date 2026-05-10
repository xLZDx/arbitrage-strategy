"""
Paper-trade hardener (Phase 11).

Wraps the coordinator so every detected GO opportunity is run through the
SHADOW pipeline AND the Phase-3 replay model, then both numbers are
compared. The simulated-vs-paper PnL gap is the headline metric: per the
plan §5 Phase 11 exit criterion, the gap must stay within ±15% over a
7-day soak before live capital lands in Phase 12.

Outputs land in data/arb/db/paper_trades/ — one row per opportunity-pair
showing both numbers + the gap.
"""

from __future__ import annotations

import logging
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from src.exec.coordinator import ArbCoordinator, TradeRecord
from src.sim.replay import (
    realized_fill_pct, realized_slippage, _legs_for, _obi_alignment,
)
from src.strategy.opportunity import DEFAULT_DEX_GAS_UNITS

log = logging.getLogger(__name__)


@dataclass
class PaperTradeRecord:
    ts: str
    pair: str
    direction: str
    notional_usd: float
    coordinator_outcome: str           # from ArbCoordinator
    coordinator_pnl_estimate: float    # what attempt() believed
    sim_realized_pnl_usd: float        # what the Phase-3 simulator says
    pnl_gap_usd: float                 # coord_estimate - sim_realized
    pnl_gap_bps: float                 # gap normalized to notional
    sim_realized_slippage_bps: float
    sim_realized_gas_usd: float
    sim_fill_pct: float


@dataclass
class PaperTradeRunner:
    """
    Runs each GO opportunity through both the coordinator and the simulator,
    computes the gap, persists the result.
    """
    coordinator: ArbCoordinator
    rng_seed: int = 0
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.rng_seed)

    def run_opportunity(self, opp: dict) -> PaperTradeRecord:
        rec = self.coordinator.attempt(opp)
        sim_pnl, sim_slip, sim_gas, sim_fill = self._simulator_estimate(opp)
        coord_estimate = float(opp.get("theoretical_pnl_usd", 0.0))
        gap_usd = coord_estimate - sim_pnl
        notional = max(0.01, float(opp.get("notional_usd", 0.0)))
        gap_bps = (gap_usd / notional) * 10_000.0
        return PaperTradeRecord(
            ts=opp.get("ts", datetime.now(timezone.utc).isoformat()),
            pair=opp.get("pair", "?"),
            direction=opp.get("direction", "bybit_high"),
            notional_usd=float(opp.get("notional_usd", 0.0)),
            coordinator_outcome=rec.outcome,
            coordinator_pnl_estimate=round(coord_estimate, 6),
            sim_realized_pnl_usd=round(sim_pnl, 6),
            pnl_gap_usd=round(gap_usd, 6),
            pnl_gap_bps=round(gap_bps, 4),
            sim_realized_slippage_bps=round(sim_slip, 4),
            sim_realized_gas_usd=round(sim_gas, 6),
            sim_fill_pct=round(sim_fill, 3),
        )

    def run_batch(self, opps: Iterable[dict]) -> list[PaperTradeRecord]:
        return [self.run_opportunity(op) for op in opps]

    def _simulator_estimate(self, opp: dict) -> tuple[float, float, float, float]:
        """
        Same math as src/sim/replay.py but inlined so this module can run
        without needing the full ReplayResult plumbing.
        """
        notional = float(opp.get("notional_usd", 0.0))
        if notional <= 0:
            return 0.0, 0.0, 0.0, 0.0
        haircut = float(opp.get("slippage_haircut_bps", 0.0))
        slippage_bps = realized_slippage(haircut, self._rng)
        obi_align = _obi_alignment(float(opp.get("weighted_obi", 0.0)),
                                     opp.get("direction", "bybit_high"))
        fill = realized_fill_pct(self._rng, obi_alignment=obi_align)

        gas_gwei = float(opp.get("gas_gwei", 0.0))
        eth_price = float(opp.get("eth_price_used", 3000.0))
        gas_usd = (DEFAULT_DEX_GAS_UNITS * gas_gwei * 1e-9) * eth_price

        bybit_fee_bps = float(opp.get("bybit_fee_bps", 10.0))
        dex_fee_bps   = float(opp.get("dex_fee_bps", 5.0))
        gross_bps = abs(float(opp.get("spread_bps", 0.0)))
        cost_bps = bybit_fee_bps + dex_fee_bps + slippage_bps
        cost_usd = (cost_bps / 10_000.0) * notional * fill + gas_usd
        gross_usd = (gross_bps / 10_000.0) * notional * fill
        return gross_usd - cost_usd, slippage_bps, gas_usd, fill


def gap_summary(records: list[PaperTradeRecord]) -> dict:
    """Aggregate the gap stats across a batch of paper-trade records.
    Returns whether the soak is within the Phase-11 ±15% tolerance."""
    if not records:
        return {"n": 0, "abs_gap_pct": 0.0, "within_15_pct": True}
    total_coord = sum(abs(r.coordinator_pnl_estimate) for r in records)
    total_gap = sum(abs(r.pnl_gap_usd) for r in records)
    gap_pct = (total_gap / total_coord * 100.0) if total_coord > 0 else 0.0
    return {
        "n": len(records),
        "total_coord_pnl_usd": round(total_coord, 4),
        "total_abs_gap_usd": round(total_gap, 4),
        "abs_gap_pct": round(gap_pct, 2),
        "within_15_pct": gap_pct <= 15.0,
        "mean_gap_bps": round(
            sum(r.pnl_gap_bps for r in records) / len(records), 4
        ),
    }
