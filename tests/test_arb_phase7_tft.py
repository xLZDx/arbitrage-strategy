"""
Phase 7 — TFT-as-feature wrapper tests.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.ml.tft_feature import (
    HeuristicTftProvider, SisterProjectTftProvider, StubTftProvider,
)
from src.ml.feature_pipeline import extract_features, feature_columns


def test_stub_provider_returns_fixed() -> None:
    p = StubTftProvider(fixed_value=0.42)
    assert p.predict_60s("BTCUSDT", [1, 2, 3]) == 0.42
    assert p.predict_60s("ETHUSDT", []) == 0.42


def test_stub_provider_default_zero() -> None:
    p = StubTftProvider()
    assert p.predict_60s("BTCUSDT", [100, 101]) == 0.0


def test_heuristic_provider_zero_when_too_few_samples() -> None:
    p = HeuristicTftProvider()
    assert p.predict_60s("BTCUSDT", []) == 0.0
    assert p.predict_60s("BTCUSDT", [100]) == 0.0


def test_heuristic_provider_positive_for_uptrend() -> None:
    p = HeuristicTftProvider(window=10)
    mids = [100.0 + i for i in range(20)]   # rising
    out = p.predict_60s("BTCUSDT", mids)
    assert out > 0.0


def test_heuristic_provider_negative_for_downtrend() -> None:
    p = HeuristicTftProvider(window=10)
    mids = [120.0 - i for i in range(20)]   # falling
    out = p.predict_60s("BTCUSDT", mids)
    assert out < 0.0


def test_heuristic_provider_safe_on_zero_first() -> None:
    p = HeuristicTftProvider(window=10)
    assert p.predict_60s("BTCUSDT", [0.0, 1.0, 2.0]) == 0.0


def test_heuristic_provider_log_return_magnitude() -> None:
    """Mid doubles → log return ≈ 0.693."""
    p = HeuristicTftProvider(window=10)
    out = p.predict_60s("BTCUSDT", [100.0, 200.0])
    assert abs(out - math.log(2.0)) < 1e-9


def test_sister_project_provider_silent_fallback() -> None:
    """Without weights, returns 0.0 — never raises."""
    p = SisterProjectTftProvider(weights_path=None)
    assert p.predict_60s("BTCUSDT", [100, 101, 102]) == 0.0


def test_sister_project_provider_only_loads_once() -> None:
    p = SisterProjectTftProvider()
    p.predict_60s("X", [1])
    p.predict_60s("X", [1])
    assert p._tried is True


# --- integration: TFT feature flows into feature_pipeline ----------------


def test_extract_features_with_tft_appends_value() -> None:
    p = StubTftProvider(fixed_value=0.123)
    opp = {"ts": "2026-05-11T10:00:00+00:00", "pair": "BTCUSDT",
           "decision": "GO", "direction": "bybit_high",
           "spread_bps": 10.0, "weighted_obi": 0.1, "notional_usd": 50.0}
    pred = p.predict_60s("BTCUSDT", [80000.0, 80100.0])
    f = extract_features(opp, tft_60s_pred=pred)
    cols = feature_columns(include_tft=True)
    assert f[cols.index("tft_60s_pred")] == 0.123


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
