"""
Cross-venue inventory tracker.

Two sides for CEX-DEX statistical arb:
  - bybit:  USDT, plus base assets (BTC, ETH, SOL) held as spot positions
  - dex:    USDC, plus base assets (cbBTC, WETH, wSOL) held as wallet balances

Each simulated arbitrage trade has TWO legs (Bybit + DEX). Both legs adjust
inventory atomically; if either leg would push a side below 0 (or below a
configured minimum), the trade is rejected.

Phase 3 = simulator-only. Phase 5 wires this into live executor with the
same interface, plus periodic rebalance alerts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

VenueT = Literal["bybit", "dex"]
ASSETS_BY_VENUE: dict[VenueT, tuple[str, ...]] = {
    "bybit": ("USDT", "BTC", "ETH", "SOL"),
    "dex":   ("USDC", "cbBTC", "WETH", "wSOL"),
}

# Map Bybit pair → (bybit_base_asset, dex_base_asset). Used to translate
# "BTCUSDT" buy into the right legs.
PAIR_LEGS: dict[str, tuple[str, str]] = {
    "BTCUSDT": ("BTC", "cbBTC"),
    "ETHUSDT": ("ETH", "WETH"),
    "SOLUSDT": ("SOL", "wSOL"),
}


@dataclass
class Inventory:
    """
    Mutable balance ledger across venues.

    Phase 3 stores balances as USD-equivalent values (the simulator runs
    end-to-end in USD notional). A separate realized_pnl_usd accumulator
    captures arb profit, since balanced two-leg trades net to zero in the
    venue ledgers but DO produce net portfolio gain (the spread captured).
    """
    bybit: dict[str, float] = field(default_factory=dict)
    dex: dict[str, float] = field(default_factory=dict)
    realized_pnl_usd: float = 0.0

    @classmethod
    def with_initial_usd(cls, usd_per_side: float) -> "Inventory":
        """USD-only starting state. Note: real CEX-DEX arb needs base assets too;
        first 'sell BTC on Bybit' leg from this state will be inventory-rejected.
        Use with_balanced_seed() for realistic backtests."""
        inv = cls()
        for asset in ASSETS_BY_VENUE["bybit"]:
            inv.bybit[asset] = 0.0
        for asset in ASSETS_BY_VENUE["dex"]:
            inv.dex[asset] = 0.0
        inv.bybit["USDT"] = float(usd_per_side)
        inv.dex["USDC"] = float(usd_per_side)
        return inv

    @classmethod
    def with_balanced_seed(
        cls,
        usd_per_side: float,
        usd_split_pct: float = 50.0,
    ) -> "Inventory":
        """
        Realistic CEX-DEX starting inventory: usd_split_pct of usd_per_side
        in stable, the rest distributed equally across base assets (in
        USD-equivalent units, not native units — Phase 3 simplification).

        For each pair in PAIR_LEGS, allocates equal USD-equivalent on each side.
        """
        inv = cls.with_initial_usd(0.0)
        usd_per_side = float(usd_per_side)
        stable = usd_per_side * (usd_split_pct / 100.0)
        non_stable = usd_per_side - stable
        n_pairs = len(PAIR_LEGS)
        per_asset = (non_stable / n_pairs) if n_pairs else 0.0

        inv.bybit["USDT"] = stable
        inv.dex["USDC"] = stable
        for bybit_base, dex_base in PAIR_LEGS.values():
            inv.bybit[bybit_base] = per_asset
            inv.dex[dex_base] = per_asset
        return inv

    def get(self, venue: VenueT, asset: str) -> float:
        store = self.bybit if venue == "bybit" else self.dex
        return store.get(asset, 0.0)

    def adjust(self, venue: VenueT, asset: str, delta: float) -> None:
        store = self.bybit if venue == "bybit" else self.dex
        store[asset] = store.get(asset, 0.0) + delta

    def can_apply(self, legs: list[tuple[VenueT, str, float]]) -> tuple[bool, str]:
        """
        legs: list of (venue, asset, delta) tuples.
        Returns (ok, reason). All-or-nothing check.
        """
        for venue, asset, delta in legs:
            new_balance = self.get(venue, asset) + delta
            if new_balance < 0.0:
                return False, f"insufficient_{venue}_{asset}_{new_balance:.6f}"
        return True, "ok"

    def apply(self, legs: list[tuple[VenueT, str, float]]) -> tuple[bool, str]:
        ok, reason = self.can_apply(legs)
        if not ok:
            return False, reason
        for venue, asset, delta in legs:
            self.adjust(venue, asset, delta)
        return True, "ok"

    def book_pnl(self, amount_usd: float) -> None:
        """Adds realized arbitrage PnL to the accumulator (positive or negative)."""
        self.realized_pnl_usd += float(amount_usd)

    def total_usd(
        self,
        bybit_prices: dict[str, float] | None = None,
        dex_prices: dict[str, float] | None = None,
    ) -> float:
        """
        Phase 3: balances are stored as USD-equivalent values; arb-spread
        profit is captured in realized_pnl_usd separately because balanced
        two-leg trades net to zero in the per-venue ledgers.
        Price arguments are accepted for forward-compatibility with Phase 5+.
        """
        return (sum(self.bybit.values()) + sum(self.dex.values())
                + self.realized_pnl_usd)

    def imbalance_ratio(
        self,
        bybit_prices: dict[str, float] | None = None,
        dex_prices: dict[str, float] | None = None,
    ) -> float:
        """
        Returns abs(bybit_value - dex_value) / total_value in [0, 1].
        Phase 4 risk module triggers HALT above 0.25 (per RISK.md).
        Uses USD-equivalent balances directly (see total_usd note).
        """
        bybit_value = sum(self.bybit.values())
        dex_value = sum(self.dex.values())
        total = bybit_value + dex_value
        if total <= 0.0:
            return 0.0
        return abs(bybit_value - dex_value) / total
