"""
Phase 13 — DRL navigator stub tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.ml.drl_navigator import (
    DummyDrlNavigator, HoldOnHighGasNavigator, make_observation,
)


def test_dummy_always_picks_direct() -> None:
    nav = DummyDrlNavigator()
    for obs in ([0.0]*4, [0.5, 30.0, 100.0, 1e6], [-1.0, -50.0, 0.0, 0.0]):
        assert nav.choose_action(obs) == "direct"


def test_hold_on_high_gas_holds_above_threshold() -> None:
    nav = HoldOnHighGasNavigator(gas_threshold_gwei=5.0)
    obs = (0.1, 20.0, 10.0, 1_000_000.0)  # gas=10 > 5
    assert nav.choose_action(obs) == "hold"


def test_hold_on_high_gas_swaps_below_threshold() -> None:
    nav = HoldOnHighGasNavigator(gas_threshold_gwei=5.0)
    obs = (0.1, 20.0, 0.006, 1_000_000.0)
    assert nav.choose_action(obs) == "direct"


def test_hold_on_high_gas_safe_with_short_obs() -> None:
    nav = HoldOnHighGasNavigator()
    assert nav.choose_action([1.0]) == "direct"


def test_make_observation_extracts_canonical_features() -> None:
    opp = {"weighted_obi": 0.4, "spread_bps": 25.0,
           "gas_gwei": 0.006, "dex_liquidity_usd": 5_000_000.0}
    obs = make_observation(opp)
    assert obs == (0.4, 25.0, 0.006, 5_000_000.0)


def test_make_observation_defaults_zero_for_missing() -> None:
    obs = make_observation({})
    assert obs == (0.0, 0.0, 0.0, 0.0)


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
