"""
Bybit L2 order-book WebSocket client.

Subscribes to orderbook.<depth>.<symbol> on the public spot stream for the
3 pilot pairs. Maintains a local snapshot per symbol, applies incremental
deltas, and yields normalized snapshots downstream via an asyncio.Queue.

Endpoint: wss://stream.bybit.com/v5/public/spot

Message format (Bybit v5):
  {"topic": "orderbook.50.BTCUSDT", "type": "snapshot" | "delta",
   "ts": 1234567890123,
   "data": {"s": "BTCUSDT",
            "b": [[price_str, size_str], ...],
            "a": [[price_str, size_str], ...],
            "u": update_id, "seq": cross_seq}}

Reconnect strategy:
  - Exponential backoff capped at config.BYBIT_WS_MAX_BACKOFF_S
  - On reconnect, re-subscribes and discards local snapshot until a fresh
    full snapshot ('type': 'snapshot') arrives.

This module is pure I/O. OBI computation is downstream in features/obi.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

import websockets
from websockets.exceptions import ConnectionClosed

from src.utils import config

log = logging.getLogger(__name__)


@dataclass
class BookSnapshot:
    """Normalized L2 snapshot ready for OBI computation."""
    symbol: str
    ts_ms: int
    bids: list[list[float]]  # [[price, size], ...] sorted desc
    asks: list[list[float]]  # [[price, size], ...] sorted asc
    update_id: int
    is_full_snapshot: bool

    def as_dict(self) -> dict:
        return {"bids": self.bids, "asks": self.asks}


@dataclass
class _LocalBook:
    """Internal: per-symbol mutable book state for delta application."""
    bids: dict[float, float] = field(default_factory=dict)  # price -> size
    asks: dict[float, float] = field(default_factory=dict)
    last_update_id: int = -1
    has_snapshot: bool = False

    def apply(self, side: str, levels: list[list[str]]) -> None:
        target = self.bids if side == "b" else self.asks
        for px_str, sz_str in levels:
            px = float(px_str)
            sz = float(sz_str)
            if sz == 0.0:
                target.pop(px, None)
            else:
                target[px] = sz

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.has_snapshot = False
        self.last_update_id = -1

    def to_snapshot(self, symbol: str, ts_ms: int, full: bool, update_id: int) -> BookSnapshot:
        bids = sorted(self.bids.items(), key=lambda x: -x[0])[: config.BYBIT_L2_DEPTH]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[: config.BYBIT_L2_DEPTH]
        return BookSnapshot(
            symbol=symbol,
            ts_ms=ts_ms,
            bids=[[p, s] for p, s in bids],
            asks=[[p, s] for p, s in asks],
            update_id=update_id,
            is_full_snapshot=full,
        )


class BybitL2Stream:
    """
    Async generator yielding BookSnapshot per book update.

    Usage:
        async for snap in BybitL2Stream(["BTCUSDT", "ETHUSDT"]).run():
            handle(snap)
    """

    def __init__(
        self,
        symbols: list[str],
        depth: int = config.BYBIT_L2_DEPTH,
        url: str = config.BYBIT_WS_PUBLIC_URL,
    ) -> None:
        self.symbols = list(symbols)
        self.depth = depth
        self.url = url
        self._books: dict[str, _LocalBook] = {s: _LocalBook() for s in symbols}
        self._stop = asyncio.Event()
        self._connected = False

    def stop(self) -> None:
        self._stop.set()

    @property
    def connected(self) -> bool:
        return self._connected

    async def run(self) -> AsyncIterator[BookSnapshot]:
        backoff = config.BYBIT_WS_RECONNECT_S
        while not self._stop.is_set():
            try:
                async for snap in self._connect_and_stream():
                    backoff = config.BYBIT_WS_RECONNECT_S  # reset on successful message
                    yield snap
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                log.warning("WS connection error: %s -- reconnecting in %.1fs", e, backoff)
            except Exception as e:
                log.exception("WS unexpected error: %s -- reconnecting in %.1fs", e, backoff)
            finally:
                self._connected = False
                for book in self._books.values():
                    book.reset()
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, config.BYBIT_WS_MAX_BACKOFF_S)

    async def _connect_and_stream(self) -> AsyncIterator[BookSnapshot]:
        async with websockets.connect(
            self.url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._connected = True
            sub = {
                "op": "subscribe",
                "args": [f"orderbook.{self.depth}.{s}" for s in self.symbols],
            }
            await ws.send(json.dumps(sub))
            log.info("subscribed to %s", sub["args"])

            while not self._stop.is_set():
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                msg = json.loads(raw)
                snap = self._handle_message(msg)
                if snap is not None:
                    yield snap

    def _handle_message(self, msg: dict) -> BookSnapshot | None:
        # Subscription ack / pong / op response — ignore
        if "topic" not in msg or "data" not in msg:
            return None

        topic = msg["topic"]  # e.g. "orderbook.50.BTCUSDT"
        if not topic.startswith("orderbook."):
            return None

        symbol = topic.split(".")[-1]
        if symbol not in self._books:
            return None

        msg_type = msg.get("type", "delta")
        ts_ms = int(msg.get("ts", time.time() * 1000))
        data = msg["data"]

        book = self._books[symbol]

        if msg_type == "snapshot":
            book.reset()
            book.apply("b", data.get("b", []))
            book.apply("a", data.get("a", []))
            book.last_update_id = int(data.get("u", 0))
            book.has_snapshot = True
            return book.to_snapshot(symbol, ts_ms, full=True, update_id=book.last_update_id)

        # delta — only apply if we have a snapshot baseline
        if not book.has_snapshot:
            return None
        book.apply("b", data.get("b", []))
        book.apply("a", data.get("a", []))
        book.last_update_id = int(data.get("u", book.last_update_id))
        return book.to_snapshot(symbol, ts_ms, full=False, update_id=book.last_update_id)
