"""
Order Book Imbalance (OBI) features.

Three signals computed from L2 depth:
1. weighted_obi:  Weighted multi-level OBI in [-1, 1].
                  Positive = buy pressure, negative = sell pressure.
                  Weights decay geometrically with level distance to defeat
                  spoofing at the top of book.
2. obi_delta:     Change in weighted OBI vs the previous snapshot.
                  Large positive flip often precedes upward breakout.
3. cancellation_rate:
                  Estimated rate of order cancellations (0..1) using a
                  rolling buffer of (OBI, traded volume) pairs. High OBI
                  swings with near-zero traded volume = artificial liquidity
                  (algorithmic spoofing) — confidence in OBI signal drops.

This file is pure Python: no I/O, no Bybit deps. Easy to unit test.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from src.utils import config


OrderBook = dict[str, list[list[float]]]  # {"bids": [[px, sz], ...], "asks": [...]}


@dataclass(frozen=True)
class ObiSnapshot:
    weighted_obi: float
    obi_delta: float
    cancellation_rate: float
    bid_volume: float
    ask_volume: float
    levels_used: int


def calculate_weighted_obi(
    order_book: OrderBook,
    levels: int = config.OBI_LEVELS,
    decay_factor: float = config.OBI_DECAY,
) -> float:
    """
    Weighted multi-level OBI. Returns value in [-1, 1].

    OBI = (sum(bid_vol * weight) - sum(ask_vol * weight)) /
          (sum(bid_vol * weight) + sum(ask_vol * weight))

    where weight[i] = decay_factor ** i  (i = 0..levels-1)

    Edge cases:
      - empty book → 0.0
      - all-zero volumes → 0.0
      - fewer than `levels` levels available → uses what's there
    """
    bids_list = order_book.get("bids") or []
    asks_list = order_book.get("asks") or []
    if not bids_list and not asks_list:
        return 0.0

    n = min(levels, len(bids_list), len(asks_list))
    if n == 0:
        return 0.0

    bids = np.asarray(bids_list[:n], dtype=np.float64)
    asks = np.asarray(asks_list[:n], dtype=np.float64)
    bid_vols = bids[:, 1]
    ask_vols = asks[:, 1]

    weights = np.power(decay_factor, np.arange(n, dtype=np.float64))
    wbv = float(np.sum(bid_vols * weights))
    wav = float(np.sum(ask_vols * weights))
    denom = wbv + wav
    if denom <= 0.0:
        return 0.0
    return float(round((wbv - wav) / denom, 6))


def compute_volumes(
    order_book: OrderBook,
    levels: int = config.OBI_LEVELS,
) -> tuple[float, float, int]:
    """Return (bid_volume_sum, ask_volume_sum, levels_used) over top-N levels."""
    bids_list = order_book.get("bids") or []
    asks_list = order_book.get("asks") or []
    n = min(levels, len(bids_list), len(asks_list))
    if n == 0:
        return 0.0, 0.0, 0
    bids = np.asarray(bids_list[:n], dtype=np.float64)
    asks = np.asarray(asks_list[:n], dtype=np.float64)
    return float(bids[:, 1].sum()), float(asks[:, 1].sum()), n


class ObiTracker:
    """
    Stateful OBI tracker per pair. Holds rolling history of OBI values and
    traded volume to compute delta + cancellation rate.

    Push one snapshot per WebSocket book update via push_book().
    Push trade volume between snapshots via add_trade_volume().
    """

    def __init__(
        self,
        levels: int = config.OBI_LEVELS,
        decay_factor: float = config.OBI_DECAY,
        history_size: int = config.OBI_HISTORY_BUFFER,
    ) -> None:
        self.levels = levels
        self.decay_factor = decay_factor
        self._history: deque[float] = deque(maxlen=history_size)
        self._volume_between_snapshots: float = 0.0
        self._cancellation_window: deque[tuple[float, float]] = deque(maxlen=history_size)

    def add_trade_volume(self, volume: float) -> None:
        """Accumulate executed-trade volume between book snapshots."""
        if volume > 0:
            self._volume_between_snapshots += float(volume)

    def push_book(self, order_book: OrderBook) -> ObiSnapshot:
        obi = calculate_weighted_obi(order_book, self.levels, self.decay_factor)
        bid_vol, ask_vol, used = compute_volumes(order_book, self.levels)

        if self._history:
            obi_delta = round(obi - self._history[-1], 6)
            self._cancellation_window.append((abs(obi - self._history[-1]),
                                              self._volume_between_snapshots))
        else:
            obi_delta = 0.0
            self._cancellation_window.append((0.0, self._volume_between_snapshots))

        cancel_rate = self._compute_cancellation_rate()

        self._history.append(obi)
        self._volume_between_snapshots = 0.0

        return ObiSnapshot(
            weighted_obi=obi,
            obi_delta=obi_delta,
            cancellation_rate=cancel_rate,
            bid_volume=bid_vol,
            ask_volume=ask_vol,
            levels_used=used,
        )

    def _compute_cancellation_rate(self) -> float:
        """
        Heuristic: fraction of recent snapshots where |delta_obi| > 0.1
        but traded_volume == 0. Range [0, 1]. High value = likely spoofing.
        """
        if not self._cancellation_window:
            return 0.0
        suspicious = sum(
            1 for (d, v) in self._cancellation_window
            if d > 0.1 and v == 0.0
        )
        return round(suspicious / len(self._cancellation_window), 4)

    @property
    def history(self) -> Sequence[float]:
        return tuple(self._history)
