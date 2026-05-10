"""
Online drift detector (Phase 10).

Watches the distribution of model features in live opportunities vs the
distribution at training time. When a feature's KL divergence exceeds the
configured threshold, raises an alert and (in Phase 10.X) triggers a
nightly retrain.

Implementation: per-feature histograms with exponential decay so the
"current" window is implicitly recent. KL is computed batch-vs-batch,
not online-vs-batch (simpler, equivalent in steady state).

Alert log: logs/drift_alerts.jsonl. Dashboard reads from there.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from src.utils import config, safe_json

log = logging.getLogger(__name__)


DEFAULT_KL_THRESHOLD = 0.5         # ~moderate drift
DEFAULT_HISTOGRAM_BINS = 20
DEFAULT_MIN_SAMPLES = 50           # don't fire before this many obs
DEFAULT_ALERT_COOLDOWN_S = 300     # 5 min between alerts per feature


@dataclass
class DriftAlert:
    ts: str
    feature_name: str
    kl_divergence: float
    n_recent: int


def _safe_hist(values: np.ndarray, bins: int, range_: tuple[float, float]
               ) -> np.ndarray:
    """Histogram normalized to a probability mass with epsilon smoothing."""
    if len(values) == 0:
        return np.full(bins, 1.0 / bins)
    counts, _ = np.histogram(values, bins=bins, range=range_)
    # Laplace smoothing (avoid zeros for KL)
    p = (counts + 1) / (counts.sum() + bins)
    return p


def kl_divergence(p: np.ndarray, q: np.ndarray) -> float:
    """KL(p || q). Both should be normalized; non-zero (smoothed)."""
    return float(np.sum(p * np.log(p / q)))


@dataclass
class FeatureDriftWatcher:
    """Per-feature watcher: holds the training-distribution histogram and
    a rolling buffer of recent observations."""
    feature_name: str
    train_min: float
    train_max: float
    train_hist: np.ndarray
    bins: int = DEFAULT_HISTOGRAM_BINS
    window: int = 500
    recent: deque = field(default_factory=lambda: deque(maxlen=500))

    def push(self, value: float) -> None:
        self.recent.append(float(value))

    def kl(self) -> float | None:
        if len(self.recent) < DEFAULT_MIN_SAMPLES:
            return None
        recent = np.array(self.recent, dtype=np.float64)
        recent_hist = _safe_hist(recent, self.bins,
                                  (self.train_min, self.train_max))
        return kl_divergence(recent_hist, self.train_hist)


@dataclass
class DriftDetector:
    """
    Multi-feature drift detector.

    fit(training_X, feature_names) — call once with the training matrix.
    push(name, value)              — call per live observation.
    check() -> list[DriftAlert]    — returns one alert per feature exceeding
                                     the threshold (cooldown enforced).
    """
    threshold: float = DEFAULT_KL_THRESHOLD
    bins: int = DEFAULT_HISTOGRAM_BINS
    window: int = 500
    cooldown_s: float = DEFAULT_ALERT_COOLDOWN_S
    watchers: dict[str, FeatureDriftWatcher] = field(default_factory=dict)
    _last_alert_at: dict[str, float] = field(default_factory=dict)
    _alert_log: str = field(default="")

    def __post_init__(self) -> None:
        if not self._alert_log:
            self._alert_log = str(config.LOG_DIR / "drift_alerts.jsonl")

    def fit(self, training_X: np.ndarray, feature_names: Sequence[str]) -> None:
        if training_X.ndim != 2 or training_X.shape[1] != len(feature_names):
            raise ValueError(
                f"training_X shape {training_X.shape} doesn't match "
                f"{len(feature_names)} feature names"
            )
        self.watchers = {}
        for i, name in enumerate(feature_names):
            col = training_X[:, i]
            cmin, cmax = float(col.min()), float(col.max())
            if cmin == cmax:
                cmax = cmin + 1.0  # avoid zero-range
            train_hist = _safe_hist(col, self.bins, (cmin, cmax))
            self.watchers[name] = FeatureDriftWatcher(
                feature_name=name, train_min=cmin, train_max=cmax,
                train_hist=train_hist, bins=self.bins, window=self.window,
                recent=deque(maxlen=self.window),
            )

    def push(self, name: str, value: float) -> None:
        w = self.watchers.get(name)
        if w is None:
            return
        w.push(value)

    def push_row(self, X_row: np.ndarray, feature_names: Sequence[str]) -> None:
        for v, name in zip(X_row.tolist(), feature_names):
            self.push(name, v)

    def check(self) -> list[DriftAlert]:
        from datetime import datetime, timezone
        alerts: list[DriftAlert] = []
        now = time.time()
        for name, w in self.watchers.items():
            kl = w.kl()
            if kl is None or kl < self.threshold:
                continue
            last = self._last_alert_at.get(name, 0.0)
            if (now - last) < self.cooldown_s:
                continue
            alert = DriftAlert(
                ts=datetime.now(timezone.utc).isoformat(),
                feature_name=name,
                kl_divergence=round(kl, 4),
                n_recent=len(w.recent),
            )
            alerts.append(alert)
            self._last_alert_at[name] = now
            try:
                safe_json.append_jsonl(self._alert_log, {
                    "ts": alert.ts, "feature": alert.feature_name,
                    "kl": alert.kl_divergence, "n_recent": alert.n_recent,
                })
            except Exception as e:
                log.warning("drift alert log write failed: %s", e)
            log.warning("[DRIFT] %s KL=%.3f over %d obs",
                        alert.feature_name, alert.kl_divergence, alert.n_recent)
        return alerts
