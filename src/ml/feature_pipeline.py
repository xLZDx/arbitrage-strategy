"""
Feature pipeline for HistGBT (Phase 6) and TFT-as-feature (Phase 7).

Single source of truth for what gets fed to the model: an ordered tuple
of feature names + a row-extractor that takes an opportunity dict and
returns a feature vector. Sister-project parity is critical — Phase 7
adds tft_60s_pred as a column without breaking Phase 6 inference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

# Order matters: model is trained on this order, must predict in same order.
FEATURE_COLUMNS: tuple[str, ...] = (
    "spread_bps",
    "gross_bps",
    "weighted_obi",
    "obi_delta",
    "cancellation_rate",
    "gas_gwei",
    "gas_cost_bps",
    "slippage_haircut_bps",
    "expected_net_bps",
    "notional_usd",
    "is_bybit_high",        # 0/1 encoding of direction
    "hour_of_day",          # parsed from ts
    "minute_of_hour",
    "log_notional",         # robust against scale jumps
    # Phase 7 will append tft_60s_pred at the end (additive — Phase 6 models
    # without TFT continue to work; Phase 7 models extend the schema).
)


def _hour_minute(ts: str) -> tuple[int, int]:
    """Parse 'YYYY-MM-DDTHH:MM:SS+00:00' or similar → (hour, minute).
    Returns (0, 0) on parse failure (no exception)."""
    try:
        time_part = ts.split("T", 1)[1]
        hh, mm, *_ = time_part.split(":")
        return int(hh), int(mm)
    except Exception:
        return 0, 0


def extract_features(opportunity: dict, tft_60s_pred: float | None = None) -> np.ndarray:
    """
    Build a single-row feature vector.

    tft_60s_pred: optional Phase-7 TFT output. None → column omitted (matches
    Phase 6 schema). Provided → appended (matches Phase 7 schema).
    """
    spread = float(opportunity.get("spread_bps", 0.0))
    direction = opportunity.get("direction", "bybit_high")
    notional = max(0.01, float(opportunity.get("notional_usd", 0.0)))
    hh, mm = _hour_minute(opportunity.get("ts", ""))

    base = [
        spread,
        float(opportunity.get("gross_bps", abs(spread))),
        float(opportunity.get("weighted_obi", 0.0)),
        float(opportunity.get("obi_delta", 0.0)),
        float(opportunity.get("cancellation_rate", 0.0)),
        float(opportunity.get("gas_gwei", 0.0)),
        float(opportunity.get("gas_cost_bps", 0.0)),
        float(opportunity.get("slippage_haircut_bps", 0.0)),
        float(opportunity.get("expected_net_bps", 0.0)),
        notional,
        1.0 if direction == "bybit_high" else 0.0,
        float(hh),
        float(mm),
        float(np.log1p(notional)),
    ]
    if tft_60s_pred is not None:
        base.append(float(tft_60s_pred))
    return np.asarray(base, dtype=np.float64)


def feature_columns(include_tft: bool = False) -> tuple[str, ...]:
    if include_tft:
        return FEATURE_COLUMNS + ("tft_60s_pred",)
    return FEATURE_COLUMNS


def stack_features(opportunities: Sequence[dict],
                   tft_preds: Sequence[float] | None = None) -> np.ndarray:
    if tft_preds is not None and len(tft_preds) != len(opportunities):
        raise ValueError(
            f"tft_preds length {len(tft_preds)} != opps length {len(opportunities)}"
        )
    rows = []
    for i, op in enumerate(opportunities):
        tft = tft_preds[i] if tft_preds is not None else None
        rows.append(extract_features(op, tft_60s_pred=tft))
    return np.vstack(rows) if rows else np.zeros((0, len(feature_columns(tft_preds is not None))))


# --- labelling ------------------------------------------------------------


def label_from_sim_trade(trade: dict, min_pnl_threshold_usd: float = 0.0) -> int:
    """
    Binary label for HistGBT training: 1 = profitable trade, 0 = losing/skipped.

    Uses sim_trades.realized_pnl_usd > threshold AS the truth. Phase 11 will
    replace this with realized PnL from live execution once available.
    """
    pnl = float(trade.get("realized_pnl_usd", 0.0))
    inv_ok = bool(trade.get("inventory_ok", False))
    fill_pct = float(trade.get("fill_pct", 0.0))
    # Inventory-rejected or unfilled trades are negative samples (don't count
    # as wins even if the spread looked great in retrospect).
    if not inv_ok or fill_pct <= 0:
        return 0
    return 1 if pnl > min_pnl_threshold_usd else 0
