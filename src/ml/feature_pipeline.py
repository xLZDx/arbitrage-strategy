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
#
# AFML FEATURE-DESIGN NOTE (P1-7 2026-05-11):
# The original feature set included `expected_net_bps`, `gross_bps`,
# `gas_cost_bps`, and `slippage_haircut_bps`. Those are the cost-stack
# OUTPUTS — the same numbers the simulator uses to compute the label
# `realized_pnl_usd > 0`. Including them as features causes label leakage:
# the model learns the decision rule, not edge. They are DROPPED in v2.
# The dropped names live in FEATURE_COLUMNS_V1 for backward compat with
# older artifacts (see HistGBTArtifact.schema_version).
FEATURE_COLUMNS_V1: tuple[str, ...] = (
    "spread_bps", "gross_bps", "weighted_obi", "obi_delta",
    "cancellation_rate", "gas_gwei", "gas_cost_bps", "slippage_haircut_bps",
    "expected_net_bps", "notional_usd", "is_bybit_high", "hour_of_day",
    "minute_of_hour", "log_notional",
)

# v2 — label-leakage-clean. 9 features instead of 14.
FEATURE_COLUMNS: tuple[str, ...] = (
    "spread_bps",          # signed spread; market state, not a cost output
    "weighted_obi",        # microstructure signal
    "obi_delta",           # microstructure derivative
    "cancellation_rate",   # spoofing detector
    "gas_gwei",            # raw gas; NOT gas_cost_bps (which is computed from notional)
    "notional_usd",        # trade size
    "is_bybit_high",       # 0/1 encoding of direction
    "hour_sin",            # cyclical encoding (replaces hour_of_day)
    "hour_cos",
    "log_notional",        # robust against scale jumps
    # Phase 7 appends tft_60s_pred at the end (additive).
)
FEATURE_SCHEMA_VERSION: int = 2


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
    Build a single-row feature vector (v2 schema, leakage-clean).

    tft_60s_pred: optional Phase-7 TFT output. None → column omitted (matches
    Phase 6 schema). Provided → appended (matches Phase 7 schema).
    """
    spread = float(opportunity.get("spread_bps", 0.0))
    direction = opportunity.get("direction", "bybit_high")
    notional = max(0.01, float(opportunity.get("notional_usd", 0.0)))
    hh, mm = _hour_minute(opportunity.get("ts", ""))
    # Cyclical hour encoding so minute-59 → minute-0 isn't a max-distance jump.
    hour_frac = (hh + mm / 60.0) / 24.0
    hour_sin = float(np.sin(2 * np.pi * hour_frac))
    hour_cos = float(np.cos(2 * np.pi * hour_frac))

    base = [
        spread,
        float(opportunity.get("weighted_obi", 0.0)),
        float(opportunity.get("obi_delta", 0.0)),
        float(opportunity.get("cancellation_rate", 0.0)),
        float(opportunity.get("gas_gwei", 0.0)),
        notional,
        1.0 if direction == "bybit_high" else 0.0,
        hour_sin,
        hour_cos,
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
