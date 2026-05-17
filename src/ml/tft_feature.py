"""
TFT-as-feature wrapper (Phase 7).

The trading bot's TFT (Temporal Fusion Transformer) at ../AI trading
assistance/ predicts short-term price trajectory. Phase 7 lifts that
prediction into our HistGBT feature vector via tft_60s_pred — a single
scalar = forecasted log-return over the next 60 seconds (positive =
price expected to rise, negative = fall).

Per the plan §5 Phase 7 exit criterion: AUC must improve by >= 0.02 vs
Phase 6 baseline; otherwise we drop the feature.

Loading the actual sister-project TFT is heavy (PyTorch + ckpt). To keep
this module test-friendly, we use a Provider abstraction:

  TftProvider               — abstract; one method predict_60s(pair, history)
  StubTftProvider           — returns 0.0 always; for unit tests + when TFT
                               weights aren't available
  SisterProjectTftProvider  — loads sister-project TFT lazily and wraps its
                               inference (Phase 7.X — needs actual sister
                               model API; left as TODO so this module loads
                               without optional deps)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, Sequence

import numpy as np

log = logging.getLogger(__name__)


class TftProvider(Protocol):
    def predict_60s(self, pair: str, recent_mids: Sequence[float]) -> float: ...


@dataclass
class StubTftProvider:
    """Always returns 0.0. Use when no model is available; HistGBT will
    learn that the feature is uninformative (it'll get low importance)
    and continue working."""
    fixed_value: float = 0.0

    def predict_60s(self, pair: str, recent_mids: Sequence[float]) -> float:
        return float(self.fixed_value)


@dataclass
class HeuristicTftProvider:
    """
    Cheap, dependency-free stand-in: log-return over the trailing window.
    Not a real TFT, but lets Phase 7 demonstrate non-zero feature values
    without loading PyTorch. Real model lands in SisterProjectTftProvider.

    HIGH-7 fix (2026-05-11, ml-engineer re-review): the canonical method
    is `trailing_logreturn` — that's what this actually computes. The
    `predict_60s` method is retained ONLY to satisfy the TftProvider
    Protocol and delegates to `trailing_logreturn` with a logged warning
    on first use. Treating trailing-return as a forward forecast in
    feature-importance studies will mislead — the column is a momentum
    proxy, not a prediction.
    """
    window: int = 30  # samples
    _warned: bool = False

    def trailing_logreturn(self, pair: str, recent_mids: Sequence[float]) -> float:
        """
        Log-return over the trailing `window` samples (BACKWARD-LOOKING).
        Returns 0.0 when:
          - fewer than 2 samples available
          - first sample <= 0 (corrupt input)
        Invariant: |result| < 1.0 (a 60-sample window with |logret| >= 1
        implies >2.7x price move — almost certainly a data error or
        non-monotonic timestamp; assert refuses that input).
        """
        if len(recent_mids) < 2:
            return 0.0
        head = recent_mids[-min(self.window, len(recent_mids)):]
        if head[0] <= 0:
            return 0.0
        result = float(np.log(head[-1] / head[0]))
        # Invariant: heuristic feature must stay in a sane band so downstream
        # standardisation doesn't blow up on a single corrupt row.
        assert -1.0 < result < 1.0, (
            f"trailing_logreturn out of sane band: {result:.4f} "
            f"(window={len(head)}, head[0]={head[0]:.6f}, head[-1]={head[-1]:.6f}) "
            f"— refusing potentially corrupt input"
        )
        return result

    def predict_60s(self, pair: str, recent_mids: Sequence[float]) -> float:
        """Protocol-satisfying alias for `trailing_logreturn`.

        WARNING: this is NOT a forward forecast. It returns the trailing
        log-return. Kept only so HeuristicTftProvider can stand in for a
        real TftProvider during Phase 7 development. Use
        `trailing_logreturn` directly in new code to make the semantics
        explicit.
        """
        if not self._warned:
            log.warning(
                "HeuristicTftProvider.predict_60s called -- this returns TRAILING "
                "log-return, NOT a forward forecast. Treat the feature as a "
                "momentum proxy. Future call sites should use trailing_logreturn."
            )
            self._warned = True
        return self.trailing_logreturn(pair, recent_mids)


class SisterProjectTftProvider:
    """
    Lazy wrapper around the sister project's TFT model.

    Activated only when:
      1. ai_trading_assistance package is importable
      2. weights file exists at the expected path

    Returns 0.0 (silent fallback) if either fails — never crashes the
    coordinator.
    """
    def __init__(self, weights_path: str | None = None) -> None:
        self.weights_path = weights_path
        self._model = None
        self._tried = False

    def _load(self) -> None:
        if self._tried:
            return
        self._tried = True
        try:
            # Phase 7.X: actual sister-project TFT loader call.
            # from ai_trading_assistance.src.models.tft import load_tft
            # self._model = load_tft(self.weights_path)
            log.info("SisterProjectTftProvider: sister-project TFT loader is "
                     "Phase 7.X -- falling back to silent 0.0 for now.")
            self._model = None
        except Exception as e:
            log.warning("TFT load failed (using 0.0 fallback): %s", e)
            self._model = None

    def predict_60s(self, pair: str, recent_mids: Sequence[float]) -> float:
        self._load()
        if self._model is None:
            return 0.0
        try:
            # Phase 7.X: real model call.
            # return float(self._model.forecast(pair, recent_mids, horizon_s=60))
            return 0.0
        except Exception as e:
            log.warning("TFT predict failed: %s", e)
            return 0.0
