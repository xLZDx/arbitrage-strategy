"""
Phase 10 — drift detector tests.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from src.ml.drift_detector import (
    DEFAULT_KL_THRESHOLD, DriftAlert, DriftDetector, FeatureDriftWatcher,
    _safe_hist, kl_divergence,
)


def test_kl_divergence_zero_for_identical() -> None:
    p = np.array([0.25, 0.25, 0.25, 0.25])
    assert kl_divergence(p, p) == 0.0


def test_kl_divergence_positive_for_different() -> None:
    p = np.array([0.9, 0.05, 0.025, 0.025])
    q = np.array([0.25, 0.25, 0.25, 0.25])
    assert kl_divergence(p, q) > 0.0


def test_safe_hist_smoothed_no_zeros() -> None:
    """Even with empty bins the result has no zeros (Laplace smoothing)."""
    values = np.array([1.0, 1.0, 1.0])  # all in same bin
    h = _safe_hist(values, bins=5, range_=(0.0, 5.0))
    assert (h > 0).all()
    assert abs(h.sum() - 1.0) < 1e-9


def test_safe_hist_empty_input_uniform() -> None:
    h = _safe_hist(np.array([]), bins=4, range_=(0.0, 1.0))
    assert h.shape == (4,)
    np.testing.assert_array_almost_equal(h, [0.25] * 4)


def test_drift_detector_fit_creates_watchers() -> None:
    rng = np.random.RandomState(0)
    X = rng.randn(100, 3)
    d = DriftDetector()
    d.fit(X, ("a", "b", "c"))
    assert set(d.watchers.keys()) == {"a", "b", "c"}


def test_drift_detector_fit_dim_mismatch_raises() -> None:
    rng = np.random.RandomState(0)
    X = rng.randn(50, 3)
    d = DriftDetector()
    try:
        d.fit(X, ("a", "b"))  # 2 names, 3 cols
    except ValueError as e:
        assert "match" in str(e)
        return
    assert False


def test_drift_detector_no_alert_when_distribution_matches() -> None:
    rng = np.random.RandomState(0)
    X = rng.randn(500, 1)
    d = DriftDetector()
    d.fit(X, ("a",))
    # Push more from same distribution
    for v in rng.randn(200):
        d.push("a", float(v))
    alerts = d.check()
    assert alerts == []


def test_drift_detector_alerts_on_distribution_shift() -> None:
    rng = np.random.RandomState(0)
    X = rng.randn(500, 1)  # standard normal
    d = DriftDetector(threshold=0.3)
    d.fit(X, ("a",))
    # Push a hugely shifted distribution
    for v in rng.randn(200) * 0.1 + 5.0:  # mean shift to 5
        d.push("a", float(v))
    alerts = d.check()
    assert len(alerts) >= 1
    assert alerts[0].feature_name == "a"
    assert alerts[0].kl_divergence > 0.3


def test_drift_detector_cooldown_blocks_repeat() -> None:
    rng = np.random.RandomState(0)
    X = rng.randn(500, 1)
    d = DriftDetector(threshold=0.1, cooldown_s=300.0)
    d.fit(X, ("a",))
    for v in rng.randn(200) + 5.0:
        d.push("a", float(v))
    a1 = d.check()
    a2 = d.check()
    assert len(a1) >= 1
    assert a2 == []  # cooldown blocks


def test_drift_detector_unknown_feature_silent() -> None:
    rng = np.random.RandomState(0)
    X = rng.randn(50, 1)
    d = DriftDetector()
    d.fit(X, ("a",))
    d.push("not_a_feature", 999.0)  # silent no-op
    # No exceptions, no leak into watchers
    assert len(d.watchers["a"].recent) == 0


def test_drift_detector_does_not_alert_below_min_samples() -> None:
    rng = np.random.RandomState(0)
    X = rng.randn(500, 1)
    d = DriftDetector(threshold=0.1)
    d.fit(X, ("a",))
    # Push only a handful of shifted samples — not enough
    for v in rng.randn(10) + 10.0:
        d.push("a", float(v))
    alerts = d.check()
    assert alerts == []


def test_push_row_distributes_to_features() -> None:
    rng = np.random.RandomState(0)
    X = rng.randn(50, 3)
    d = DriftDetector()
    d.fit(X, ("a", "b", "c"))
    d.push_row(np.array([1.0, 2.0, 3.0]), ("a", "b", "c"))
    assert d.watchers["a"].recent[-1] == 1.0
    assert d.watchers["b"].recent[-1] == 2.0
    assert d.watchers["c"].recent[-1] == 3.0


def _run_all() -> int:
    failures: list[tuple[str, str]] = []
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print()
    if failures:
        print(f"{len(failures)} / {len(tests)} FAILED")
        return 1
    print(f"{len(tests)} / {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
