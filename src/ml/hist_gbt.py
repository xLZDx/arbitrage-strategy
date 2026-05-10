"""
HistGBT spread-survival classifier (Phase 6).

Predicts P(trade is profitable) given the opportunity features. Acts as
a VETO in the coordinator: if model.predict_proba(features) <
config.HISTGBT_VETO_THRESHOLD, the trade is rejected before sending.

Training:
- Walk-forward CV across the timestamped sim_trades dataset.
- Training itself happens in scripts/run_train_histgbt.py to keep this
  module test-friendly (no I/O, no logging side-effects in the trainer).

Persistence: joblib (.joblib files under models/).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from src.ml.feature_pipeline import FEATURE_COLUMNS, feature_columns
from src.utils import config

log = logging.getLogger(__name__)

DEFAULT_MODEL_FILENAME = "hist_gbt_v1.joblib"
DEFAULT_VETO_THRESHOLD = 0.55  # below this → REJECT


@dataclass
class HistGBTArtifact:
    """Wraps the trained model + metadata for inference + auditing."""
    model: object  # lightgbm.LGBMClassifier
    feature_columns: tuple[str, ...]
    holdout_auc: float
    n_train: int
    n_holdout: int
    pos_rate_train: float
    trained_at: str
    veto_threshold: float = DEFAULT_VETO_THRESHOLD

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Returns array of P(label=1) per row."""
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if X.shape[1] != len(self.feature_columns):
            raise ValueError(
                f"feature dim mismatch: expected {len(self.feature_columns)}, "
                f"got {X.shape[1]}"
            )
        return self.model.predict_proba(X)[:, 1]  # type: ignore[union-attr]

    def veto(self, X: np.ndarray) -> tuple[bool, float]:
        """Returns (should_reject, p)."""
        p = float(self.predict_proba(X)[0])
        return p < self.veto_threshold, p


def train_histgbt(
    X: np.ndarray,
    y: np.ndarray,
    timestamps: Sequence[str] | None = None,
    veto_threshold: float = DEFAULT_VETO_THRESHOLD,
    include_tft: bool = False,
    n_estimators: int = 200,
    learning_rate: float = 0.05,
    num_leaves: int = 31,
    holdout_pct: float = 0.20,
    random_state: int = 42,
) -> HistGBTArtifact:
    """
    Walk-forward train: holdout is the last N% chronologically.
    """
    from datetime import datetime, timezone
    import lightgbm as lgb  # type: ignore

    if X.ndim != 2 or y.ndim != 1 or len(X) != len(y):
        raise ValueError(f"shape mismatch: X={X.shape}, y={y.shape}")
    if len(np.unique(y)) < 2:
        raise ValueError(
            f"need both classes in training data; got only {set(y.tolist())}. "
            "Capture more trades (mix of GO and SKIP) before training."
        )
    if len(X) < 20:
        raise ValueError(f"need >= 20 samples to train, got {len(X)}")

    # Walk-forward split: sort by timestamp if given, else use natural order.
    if timestamps is not None:
        order = np.argsort(timestamps)
        X = X[order]
        y = y[order]
    n_holdout = max(1, int(round(len(X) * holdout_pct)))
    n_train = len(X) - n_holdout
    X_tr, X_hd = X[:n_train], X[n_train:]
    y_tr, y_hd = y[:n_train], y[n_train:]
    if len(np.unique(y_tr)) < 2 or len(np.unique(y_hd)) < 2:
        # Fall back to random shuffle if walk-forward leaves single-class splits
        rng = np.random.RandomState(random_state)
        idx = rng.permutation(len(X))
        X = X[idx]
        y = y[idx]
        X_tr, X_hd = X[:n_train], X[n_train:]
        y_tr, y_hd = y[:n_train], y[n_train:]

    model = lgb.LGBMClassifier(
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        num_leaves=num_leaves,
        random_state=random_state,
        verbose=-1,
    )
    model.fit(X_tr, y_tr)

    from sklearn.metrics import roc_auc_score  # type: ignore
    if len(np.unique(y_hd)) >= 2:
        proba = model.predict_proba(X_hd)[:, 1]
        auc = float(roc_auc_score(y_hd, proba))
    else:
        auc = float("nan")

    return HistGBTArtifact(
        model=model,
        feature_columns=feature_columns(include_tft=include_tft),
        holdout_auc=auc,
        n_train=n_train,
        n_holdout=n_holdout,
        pos_rate_train=float(np.mean(y_tr)),
        trained_at=datetime.now(timezone.utc).isoformat(),
        veto_threshold=veto_threshold,
    )


# --- Persistence ---------------------------------------------------------


def save_artifact(artifact: HistGBTArtifact,
                  path: Path | str | None = None) -> Path:
    import joblib  # type: ignore
    if path is None:
        path = config.MODEL_DIR / DEFAULT_MODEL_FILENAME
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, path)
    return path


def load_artifact(path: Path | str | None = None) -> HistGBTArtifact | None:
    """Returns None if no model exists yet (vs raising)."""
    import joblib  # type: ignore
    if path is None:
        path = config.MODEL_DIR / DEFAULT_MODEL_FILENAME
    p = Path(path)
    if not p.exists():
        return None
    return joblib.load(p)
