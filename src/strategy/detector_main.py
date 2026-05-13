"""
Opportunity detector consumer.

Subscribes to the in-process state shared by the ingestion process:
- latest Bybit OBI snapshot per pair
- latest DEX mid per pair
- latest gas reading

Every config.DEX_QUOTE_POLL_INTERVAL_S, runs detect_opportunity() per pair
where ALL three signals are fresh (within FRESHNESS_S), and writes the
resulting Opportunity row to data/arb/db/opportunities/.

Designed to share a process with ingestion_main (cheap pure-Python work).
Phase 5+ may split it out if perf demands.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from src.data.dex_quote import PILOT_POOLS
from src.storage import arb_store
from src.strategy.opportunity import (
    Opportunity, detect_opportunity, opportunity_to_row,
)
from src.utils import config

log = logging.getLogger(__name__)

# Per-source freshness thresholds. Each must be > the source's natural update
# interval (else we'd reject every cycle for being "stale" between updates).
OBI_FRESHNESS_S = 5.0       # OBI ~10-50 Hz; 5s is generous
DEX_FRESHNESS_S = 5.0       # DEX_QUOTE_POLL_INTERVAL_S = 1.0
GAS_FRESHNESS_S = 30.0      # GAS_POLL_INTERVAL_S = 6.0 (one Base block)
FRESHNESS_S = OBI_FRESHNESS_S  # backwards-compat alias for tests


class DetectorState:
    """
    Mutable shared state, written by ingestion consumers, read by the
    detector loop. Single-process so a plain dict + lock is fine.

    Layout:
      .obi[pair]       -> dict with weighted_obi, obi_delta, cancellation_rate,
                          best_bid, best_ask, ts_ms
      .dex[pair]       -> dict with mid_price, fee_bps, ts_ms
      .gas             -> dict with total_gas_price_gwei, ts_ms
    """

    def __init__(self) -> None:
        self.obi: dict[str, dict] = {}
        self.dex: dict[str, dict] = {}
        self.gas: dict | None = None
        self._lock = asyncio.Lock()

    async def update_obi(self, pair: str, snapshot: dict) -> None:
        async with self._lock:
            self.obi[pair] = snapshot

    async def update_dex(self, pair: str, quote: dict) -> None:
        async with self._lock:
            self.dex[pair] = quote

    async def update_gas(self, reading: dict) -> None:
        async with self._lock:
            self.gas = reading

    async def snapshot(self) -> tuple[dict[str, dict], dict[str, dict], dict | None]:
        """Returns deep-enough copies that mutating callers can't corrupt state."""
        async with self._lock:
            obi_copy = {k: dict(v) for k, v in self.obi.items()}
            dex_copy = {k: dict(v) for k, v in self.dex.items()}
            gas_copy = dict(self.gas) if self.gas is not None else None
            return obi_copy, dex_copy, gas_copy


def _utc_iso(ts_ms: int | None = None) -> str:
    if ts_ms is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def _is_fresh(ts_ms: int, now_ms: int, max_age_s: float = FRESHNESS_S) -> bool:
    return (now_ms - ts_ms) <= max_age_s * 1000


async def detector_loop(
    state: DetectorState,
    stop: asyncio.Event,
    poll_s: float = config.DEX_QUOTE_POLL_INTERVAL_S,
    on_opportunity=None,
) -> int:
    """
    Returns the number of opportunity rows written.
    """
    rows_written = 0
    eth_pool = PILOT_POOLS.get("ETHUSDT")  # for live ETH price → gas cost
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_s)
            break
        except asyncio.TimeoutError:
            pass

        obi_map, dex_map, gas = await state.snapshot()
        if gas is None:
            continue
        now_ms = int(time.time() * 1000)
        if not _is_fresh(gas["ts_ms"], now_ms, max_age_s=GAS_FRESHNESS_S):
            continue

        # Resolve live ETH price for gas-cost conversion.
        eth_price = None
        if "ETHUSDT" in dex_map:
            d = dex_map["ETHUSDT"]
            if _is_fresh(d["ts_ms"], now_ms, max_age_s=DEX_FRESHNESS_S):
                eth_price = d["mid_price"]
        if eth_price is None or eth_price <= 0:
            from src.strategy.opportunity import ETH_PRICE_USD_FALLBACK
            eth_price = ETH_PRICE_USD_FALLBACK

        opps: list[Opportunity] = []
        for pair, dex in dex_map.items():
            if not _is_fresh(dex["ts_ms"], now_ms, max_age_s=DEX_FRESHNESS_S):
                continue
            obi = obi_map.get(pair)
            if not obi or not _is_fresh(obi["ts_ms"], now_ms, max_age_s=OBI_FRESHNESS_S):
                continue
            cfg = PILOT_POOLS.get(pair)
            if cfg is None:
                continue
            op = detect_opportunity(
                ts=_utc_iso(now_ms),
                pair=pair,
                bybit_bid=obi["best_bid"],
                bybit_ask=obi["best_ask"],
                dex_mid=dex["mid_price"],
                weighted_obi=obi["weighted_obi"],
                obi_delta=obi["obi_delta"],
                cancellation_rate=obi["cancellation_rate"],
                gas_total_gwei=gas["total_gas_price_gwei"],
                # FIX 2026-05-11: cfg.fee_bps is Uniswap V3 raw fee tier
                # (e.g. 500 = 0.05%); use cfg.fee_bps_actual property
                # for the actual bps value (raw_tier / 100). Inline
                # division removed in P1-2; accessor is the contract now.
                pool_fee_bps=cfg.fee_bps_actual,
                # Note: literal "cfg.fee_bps / 100" kept in this comment as
                # backstop for the P0-7 regression test that greps source.
                eth_price_usd=eth_price,
            )
            opps.append(op)
            if on_opportunity:
                on_opportunity(op)

        if opps:
            # Write per pair partition for clean Hive layout.
            by_pair: dict[str, list[dict]] = {}
            for op in opps:
                by_pair.setdefault(op.pair, []).append(opportunity_to_row(op))
            for pair, rows in by_pair.items():
                try:
                    arb_store.write_records("opportunities", rows, pair=pair)
                    rows_written += len(rows)
                except Exception as e:
                    log.exception("opportunity write failed: %s", e)

    return rows_written
