"""
Ingestion process entry point.

Wires together:
  - BybitL2Stream  → ObiTracker per pair → obi_snapshots table
  - DexQuotePoller → dex_quotes table
  - GasOracle      → gas_history table

Run:
  python -m src.data.ingestion_main

Stop with Ctrl+C; SIGTERM also handled. Writes PID to data/arb/pids/ingestion.pid
so restart_all.ps1 can reap it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.data.bybit_l2_ws import BybitL2Stream, BookSnapshot
from src.data.dex_quote import DexPriceReader, DexQuote
from src.data.gas_oracle import GasOracle, GasReading
from src.features.obi import ObiTracker
from src.storage import arb_store
from src.strategy.detector_main import DetectorState, detector_loop
from src.utils import config

log = logging.getLogger("arb.ingestion")


def _utc_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


class BatchBuffer:
    """Buffer rows per (table, pair) and flush every N rows or T seconds."""

    def __init__(self, flush_rows: int = config.ARROW_BATCH_FLUSH_ROWS,
                 flush_s: float = config.ARROW_BATCH_FLUSH_S) -> None:
        self.flush_rows = flush_rows
        self.flush_s = flush_s
        self._rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._last_flush_ts: dict[tuple[str, str], float] = {}

    def add(self, table: str, pair: str, row: dict[str, Any]) -> None:
        key = (table, pair)
        self._rows.setdefault(key, []).append(row)
        self._last_flush_ts.setdefault(key, time.time())
        if (len(self._rows[key]) >= self.flush_rows
                or time.time() - self._last_flush_ts[key] >= self.flush_s):
            self.flush(key)

    def flush(self, key: tuple[str, str]) -> None:
        if not self._rows.get(key):
            return
        table, pair = key
        rows = self._rows[key]
        try:
            arb_store.write_records(table, rows, pair=pair)
        except Exception as e:
            log.exception("flush failed for %s/%s: %s", table, pair, e)
        self._rows[key] = []
        self._last_flush_ts[key] = time.time()

    def flush_all(self) -> None:
        for key in list(self._rows.keys()):
            self.flush(key)


async def _bybit_consumer(buffer: BatchBuffer, stop: asyncio.Event,
                          state: DetectorState | None = None,
                          on_snap=None) -> None:
    trackers = {pair: ObiTracker() for pair in config.PILOT_PAIRS}
    stream = BybitL2Stream(list(config.PILOT_PAIRS))

    async def _run():
        async for snap in stream.run():
            if stop.is_set():
                stream.stop()
                break
            tracker = trackers[snap.symbol]
            obi = tracker.push_book(snap.as_dict())
            best_bid = snap.bids[0][0] if snap.bids else 0.0
            best_ask = snap.asks[0][0] if snap.asks else 0.0
            row = {
                "ts": _utc_iso(snap.ts_ms),
                "pair": snap.symbol,
                "weighted_obi": obi.weighted_obi,
                "obi_delta": obi.obi_delta,
                "cancellation_rate": obi.cancellation_rate,
                "bid_volume": obi.bid_volume,
                "ask_volume": obi.ask_volume,
                "levels_used": obi.levels_used,
                "update_id": snap.update_id,
                "is_full_snapshot": snap.is_full_snapshot,
                "best_bid": best_bid,
                "best_ask": best_ask,
            }
            buffer.add("obi_snapshots", snap.symbol, row)
            if state is not None:
                await state.update_obi(snap.symbol, {
                    "weighted_obi": obi.weighted_obi,
                    "obi_delta": obi.obi_delta,
                    "cancellation_rate": obi.cancellation_rate,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "ts_ms": snap.ts_ms,
                })
            if on_snap:
                on_snap(snap, obi)

    try:
        await _run()
    finally:
        stream.stop()


async def _dex_consumer(buffer: BatchBuffer, stop: asyncio.Event,
                        api_key: str | None,
                        state: DetectorState | None = None) -> None:
    """api_key kept for signature compatibility; Phase 1 uses on-chain slot0()."""
    reader = DexPriceReader()
    await reader.start()

    async def _run():
        async for q in reader.stream():
            if stop.is_set():
                break
            row = {
                "ts": _utc_iso(q.ts_ms),
                "pair": q.pair,
                "pool_address": q.pool_address,
                "sqrt_price_x96": str(q.sqrt_price_x96),  # uint160 too big for int64
                "mid_price": q.mid_price,
                "fee_bps": q.fee_bps,
                "source": q.source,
            }
            buffer.add("dex_quotes", q.pair, row)
            if state is not None:
                await state.update_dex(q.pair, {
                    "mid_price": q.mid_price,
                    "fee_bps": q.fee_bps,
                    "ts_ms": q.ts_ms,
                })

    try:
        await _run()
    finally:
        await reader.stop()


async def _gas_consumer(buffer: BatchBuffer, stop: asyncio.Event,
                        state: DetectorState | None = None) -> None:
    oracle = GasOracle()
    await oracle.start()
    try:
        last_block = -1
        while not stop.is_set():
            r: GasReading | None = oracle.latest()
            if r is not None and r.block_number != last_block:
                row = {
                    "ts": _utc_iso(r.ts_ms),
                    "block_number": r.block_number,
                    "base_fee_gwei": r.base_fee_gwei,
                    "priority_fee_gwei": r.priority_fee_gwei,
                    "total_gas_price_gwei": r.total_gas_price_gwei,
                }
                buffer.add("gas_history", "BASE", row)
                last_block = r.block_number
                if state is not None:
                    await state.update_gas({
                        "block_number": r.block_number,
                        "base_fee_gwei": r.base_fee_gwei,
                        "priority_fee_gwei": r.priority_fee_gwei,
                        "total_gas_price_gwei": r.total_gas_price_gwei,
                        "ts_ms": r.ts_ms,
                    })
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.GAS_POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
    finally:
        await oracle.stop()


async def _flusher(buffer: BatchBuffer, stop: asyncio.Event) -> None:
    """Periodic flush so even slow streams persist within flush_s seconds."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.ARROW_BATCH_FLUSH_S)
        except asyncio.TimeoutError:
            pass
        for key in list(buffer._rows.keys()):
            if buffer._rows.get(key):
                buffer.flush(key)


async def main_async(duration_s: float | None = None,
                     dex_api_key: str | None = None) -> None:
    """Run all ingestion consumers. duration_s=None = run forever."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("ingestion starting (mode=%s, duration=%s)",
             config.EXECUTION_MODE, duration_s)

    pid_file = config.PIDS_DIR / "ingestion.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    stop = asyncio.Event()
    buffer = BatchBuffer()
    state = DetectorState()

    def _signal(*_):
        log.info("signal received, stopping...")
        stop.set()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, _signal)

    tasks = [
        asyncio.create_task(_bybit_consumer(buffer, stop, state=state), name="bybit"),
        asyncio.create_task(_dex_consumer(buffer, stop, dex_api_key, state=state), name="dex"),
        asyncio.create_task(_gas_consumer(buffer, stop, state=state), name="gas"),
        asyncio.create_task(_flusher(buffer, stop), name="flusher"),
        asyncio.create_task(detector_loop(state, stop), name="detector"),
    ]

    try:
        if duration_s is not None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=duration_s)
            except asyncio.TimeoutError:
                stop.set()
        else:
            await stop.wait()
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        buffer.flush_all()
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        log.info("ingestion stopped. obi_rows=%d dex_rows=%d gas_rows=%d opp_rows=%d",
                 arb_store.row_count("obi_snapshots"),
                 arb_store.row_count("dex_quotes"),
                 arb_store.row_count("gas_history"),
                 arb_store.row_count("opportunities"))


def cli_main() -> int:
    parser = argparse.ArgumentParser(description="arbitrage_strategy ingestion process")
    parser.add_argument("--duration", type=float, default=None,
                        help="run for N seconds then exit (default: run forever)")
    parser.add_argument("--dex-api-key", default=os.environ.get("ONEINCH_API_KEY"),
                        help="1inch API key (else uses public rate-limited endpoint)")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(duration_s=args.duration, dex_api_key=args.dex_api_key))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
