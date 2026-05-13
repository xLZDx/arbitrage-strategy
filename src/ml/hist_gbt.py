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
DEFAULT_VETO_THRESHOLD = 0.55  # below this -> REJECT

# P1-9 (2026-05-11): training data floor. AFML guidance says >=100 samples
# per feature; with 10 features that's 1000. We pick 1500 as a conservative
# floor that also ensures meaningful holdout CI on AUC.
MIN_TRAINING_SAMPLES = 1500

# P1-8 (2026-05-11): embargo size for PurgedKFold. Default = 1% of n_samples.
# For ~6 opps/sec, 1% of 1500 = 15 samples ≈ 2.5 seconds of embargo, ample
# for atomic arb whose label horizon is sub-second to seconds.
DEFAULT_EMBARGO_PCT = 0.01


ARTIFACT_SCHEMA_VERSION: int = 2  # P3-D4 (2026-05-11): bumped at feature-schema v2


@dataclass
class HistGBTArtifact:
    """Wraps the trained model + metadata for inference + auditing.

    P3-D4 (2026-05-11): `schema_version` distinguishes v1 (pre-leakage-fix,
    14 features incl. label-leakers) from v2 (10 features, clean). Loading
    a v1 artifact while running v2 code is an explicit error, not a silent
    feature-dim mismatch.
    """
    model: object  # lightgbm.LGBMClassifier
    feature_columns: tuple[str, ...]
    holdout_auc: float
    n_train: int
    n_holdout: int
    pos_rate_train: float
    trained_at: str
    veto_threshold: float = DEFAULT_VETO_THRESHOLD
    schema_version: int = ARTIFACT_SCHEMA_VERSION
    embargo_pct: float = 0.0          # P1-8 — recorded so audit can verify
    pos_rate_holdout: float = 0.0     # ml-engineer #11

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
    embargo_pct: float = DEFAULT_EMBARGO_PCT,
    require_min_samples: bool = True,
) -> HistGBTArtifact:
    """
    Walk-forward train with EMBARGO purging (AFML Ch. 7).

    Holdout is the last N% chronologically; train is the first (100-N)%
    MINUS the embargo window immediately before the holdout. This stops
    autocorrelated samples within `embargo_pct * n` of the holdout
    boundary from leaking into training.

    P1-9 (2026-05-11): refuses to train with < MIN_TRAINING_SAMPLES unless
    require_min_samples=False (override only for unit tests).
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
    n = len(X)
    if require_min_samples and n < MIN_TRAINING_SAMPLES:
        raise ValueError(
            f"need >= {MIN_TRAINING_SAMPLES} samples to train (got {n}). "
            f"AFML guidance: >=100 samples per feature. With 10 features the "
            f"floor is 1000; we use 1500 for conservative holdout CI. "
            f"Either capture more GO trades or pass require_min_samples=False "
            f"for testing (NOT for production)."
        )
    # Hard minimum even under override: >=20 for the split math.
    if n < 20:
        raise ValueError(f"need >= 20 samples to train, got {n}")

    # Walk-forward split: sort by timestamp if given, else use natural order.
    if timestamps is not None:
        order = np.argsort(timestamps)
        X = X[order]
        y = y[order]
    n_holdout = max(1, int(round(n * holdout_pct)))
    embargo_count = max(0, int(round(n * embargo_pct)))
    n_train = max(1, n - n_holdout - embargo_count)
    X_tr = X[:n_train]
    y_tr = y[:n_train]
    X_hd = X[n - n_holdout:]
    y_hd = y[n - n_holdout:]
    embargo_rows = embargo_count  # captured for the artifact metadata

    walk_forward_ok = (len(np.unique(y_tr)) >= 2 and len(np.unique(y_hd)) >= 2)
    if not walk_forward_ok:
        # P1-8 (2026-05-11): the random-shuffle fallback IS a look-ahead
        # violation. We refuse to use it for production training and instead
        # surface the imbalance to the operator. Tests can override via
        # require_min_samples=False AND will still hit this if data is bad.
        log.error(
            "PurgedKFold walk-forward produced a single-class fold "
            "(train classes=%s, holdout classes=%s). Refusing to fall back "
            "to random shuffle — that would inject look-ahead bias. Fix: "
            "capture more diverse data (mix of GO/SKIP, longer time span).",
            set(y_tr.tolist()), set(y_hd.tolist()),
        )
        raise ValueError(
            "walk-forward split produced a single-class fold; refusing "
            "random-shuffle fallback (look-ahead bias). Re-run with more data."
        )

    model = lgb.LGBMClassifier(
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        num_leaves=num_leaves,
        random_state=random_state,
        verbose=-1,
        class_weight="balanced",  # ml-engineer recommendation
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
        schema_version=ARTIFACT_SCHEMA_VERSION,
        embargo_pct=embargo_pct,
        pos_rate_holdout=float(np.mean(y_hd)),
    )


# --- Persistence ---------------------------------------------------------


def _sha256_of_file(path: Path) -> str:
    """Return hex SHA-256 digest of file contents."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_path(artifact_path: Path) -> Path:
    return artifact_path.with_suffix(artifact_path.suffix + ".sha256")


def save_artifact(artifact: HistGBTArtifact,
                  path: Path | str | None = None) -> Path:
    """Persist + write a SHA-256 manifest sibling file (P1-3 RCE prevention).

    The manifest is a single line with the hex digest. `load_artifact`
    verifies the file's current digest matches before deserializing —
    a supply-chain swap or path-traversal write of the artifact file
    raises RuntimeError rather than executing arbitrary Python via joblib."""
    import joblib  # type: ignore
    if path is None:
        path = config.MODEL_DIR / DEFAULT_MODEL_FILENAME
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, path)
    # Write integrity manifest next to the artifact
    digest = _sha256_of_file(path)
    _manifest_path(path).write_text(digest + "\n", encoding="utf-8")
    return path


def load_artifact(path: Path | str | None = None,
                  verify_integrity: bool = True) -> HistGBTArtifact | None:
    """Returns None if no model exists yet (vs raising).

    SAFETY (P1-3 2026-05-11): when verify_integrity=True (default), the
    file's current SHA-256 is compared to the manifest written by
    save_artifact. Mismatch -> RuntimeError, NO deserialization. This
    blocks a supply-chain RCE where a malicious artifact file would
    execute arbitrary Python at joblib.load time.

    verify_integrity=False is allowed for first-run loading of legacy
    artifacts that pre-date the manifest format; emit WARNING."""
    import joblib  # type: ignore
    if path is None:
        path = config.MODEL_DIR / DEFAULT_MODEL_FILENAME
    p = Path(path)
    if not p.exists():
        return None

    if verify_integrity:
        manifest = _manifest_path(p)
        if not manifest.exists():
            log.warning(
                "Model %s has no SHA-256 manifest; refusing to load. "
                "Either regenerate via save_artifact() or pass "
                "verify_integrity=False (audit risk).", p)
            raise RuntimeError(
                f"missing integrity manifest for {p}; refusing joblib.load"
            )
        expected = manifest.read_text(encoding="utf-8").strip()
        actual = _sha256_of_file(p)
        if expected != actual:
            raise RuntimeError(
                f"artifact integrity check FAILED for {p}: "
                f"expected SHA-256 {expected[:16]}..., got {actual[:16]}..."
                " — possible supply-chain attack; refusing joblib.load"
            )

    artifact = joblib.load(p)
    if not isinstance(artifact, HistGBTArtifact):
        raise RuntimeError(
            f"deserialized object is {type(artifact).__name__}, "
            f"expected HistGBTArtifact"
        )
    return artifact
