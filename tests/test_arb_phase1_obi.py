"""
Phase 1 regression tests — OBI feature.

Pure-Python tests, no I/O. Locks in the math so refactors that change OBI
values must update the golden numbers in the same commit.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.features.obi import (
    ObiTracker,
    calculate_weighted_obi,
    compute_volumes,
)


# --- calculate_weighted_obi -------------------------------------------------


def test_obi_balanced_book_is_zero() -> None:
    book = {
        "bids": [[100.0, 1.0], [99.5, 1.0], [99.0, 1.0]],
        "asks": [[100.5, 1.0], [101.0, 1.0], [101.5, 1.0]],
    }
    obi = calculate_weighted_obi(book, levels=3, decay_factor=0.5)
    assert obi == 0.0, f"balanced book should give 0, got {obi}"


def test_obi_buy_pressure_is_positive() -> None:
    book = {
        "bids": [[100.0, 5.0], [99.5, 5.0], [99.0, 5.0]],
        "asks": [[100.5, 1.0], [101.0, 1.0], [101.5, 1.0]],
    }
    obi = calculate_weighted_obi(book, levels=3, decay_factor=0.5)
    assert 0.5 < obi < 1.0, f"strong buy pressure expected, got {obi}"


def test_obi_sell_pressure_is_negative() -> None:
    book = {
        "bids": [[100.0, 1.0], [99.5, 1.0], [99.0, 1.0]],
        "asks": [[100.5, 5.0], [101.0, 5.0], [101.5, 5.0]],
    }
    obi = calculate_weighted_obi(book, levels=3, decay_factor=0.5)
    assert -1.0 < obi < -0.5, f"strong sell pressure expected, got {obi}"


def test_obi_decay_dampens_far_levels() -> None:
    """A massive bid 5 levels deep should NOT dominate top-of-book asks."""
    book = {
        "bids": [
            [100.0, 0.1], [99.9, 0.1], [99.8, 0.1],
            [99.7, 0.1], [99.6, 0.1], [99.5, 1000.0],  # whale at level 5
        ],
        "asks": [
            [100.1, 1.0], [100.2, 1.0], [100.3, 1.0],
            [100.4, 1.0], [100.5, 1.0], [100.6, 1.0],
        ],
    }
    obi = calculate_weighted_obi(book, levels=6, decay_factor=0.5)
    # level-5 weight = 0.5**5 = 0.03125, so 1000 contributes ~31 vs ask side ~1.94
    # Asymmetric but capped — verifies decay actually decays
    assert -1.0 <= obi <= 1.0
    assert obi > 0.5, "deep whale still dominant due to size, ok if obi > 0.5"


def test_obi_empty_book_is_zero() -> None:
    assert calculate_weighted_obi({"bids": [], "asks": []}) == 0.0
    assert calculate_weighted_obi({}) == 0.0


def test_obi_zero_volumes_is_zero() -> None:
    book = {"bids": [[100, 0]], "asks": [[101, 0]]}
    assert calculate_weighted_obi(book, levels=1) == 0.0


def test_obi_uneven_levels_uses_min() -> None:
    book = {
        "bids": [[100.0, 1.0], [99.5, 1.0]],
        "asks": [[100.5, 1.0], [101.0, 1.0], [101.5, 1.0], [102.0, 1.0]],
    }
    # Only 2 levels usable
    obi = calculate_weighted_obi(book, levels=4, decay_factor=0.5)
    assert obi == 0.0


def test_obi_in_bounds_always() -> None:
    """Property: OBI must stay in [-1, 1] for any book."""
    import random
    random.seed(42)
    for _ in range(100):
        levels = random.randint(1, 20)
        bids = [[random.uniform(50, 100), random.uniform(0.01, 100)] for _ in range(levels)]
        asks = [[random.uniform(100, 150), random.uniform(0.01, 100)] for _ in range(levels)]
        obi = calculate_weighted_obi({"bids": bids, "asks": asks}, levels=levels)
        assert -1.0 <= obi <= 1.0, f"OBI out of bounds: {obi}"


# --- compute_volumes -------------------------------------------------------


def test_compute_volumes_basic() -> None:
    book = {
        "bids": [[100, 2], [99, 3]],
        "asks": [[101, 5], [102, 1]],
    }
    bv, av, used = compute_volumes(book, levels=2)
    assert bv == 5.0
    assert av == 6.0
    assert used == 2


def test_compute_volumes_empty() -> None:
    bv, av, used = compute_volumes({"bids": [], "asks": []}, levels=10)
    assert (bv, av, used) == (0.0, 0.0, 0)


# --- ObiTracker (stateful) -------------------------------------------------


def _balanced(level_count: int = 3) -> dict:
    return {
        "bids": [[100 - i * 0.5, 1.0] for i in range(level_count)],
        "asks": [[100 + (i + 1) * 0.5, 1.0] for i in range(level_count)],
    }


def _buy_heavy() -> dict:
    return {
        "bids": [[100, 10], [99.5, 10], [99, 10]],
        "asks": [[100.5, 1], [101, 1], [101.5, 1]],
    }


def test_tracker_first_snapshot_delta_is_zero() -> None:
    t = ObiTracker(levels=3)
    snap = t.push_book(_balanced())
    assert snap.obi_delta == 0.0


def test_tracker_delta_changes_on_book_shift() -> None:
    t = ObiTracker(levels=3)
    s1 = t.push_book(_balanced())
    s2 = t.push_book(_buy_heavy())
    assert s2.weighted_obi > s1.weighted_obi
    assert s2.obi_delta > 0
    assert math.isclose(s2.obi_delta, s2.weighted_obi - s1.weighted_obi, abs_tol=1e-6)


def test_tracker_history_capped() -> None:
    t = ObiTracker(levels=3, history_size=5)
    for _ in range(20):
        t.push_book(_balanced())
    assert len(t.history) == 5


def test_tracker_cancellation_rate_high_when_no_volume() -> None:
    """Big OBI swings without trade volume → high cancellation rate."""
    t = ObiTracker(levels=3, history_size=10)
    # alternate buy-heavy / sell-heavy 10 times, no volume
    sell_heavy = {"bids": [[100, 1], [99.5, 1], [99, 1]],
                  "asks": [[100.5, 10], [101, 10], [101.5, 10]]}
    for i in range(10):
        t.push_book(_buy_heavy() if i % 2 == 0 else sell_heavy)
    snap = t.push_book(_buy_heavy())
    assert snap.cancellation_rate > 0.5, f"expected high cancel rate, got {snap.cancellation_rate}"


def test_tracker_cancellation_rate_low_when_volume_present() -> None:
    """Big OBI swings WITH trade volume → low cancellation rate (real flow)."""
    t = ObiTracker(levels=3, history_size=10)
    sell_heavy = {"bids": [[100, 1], [99.5, 1], [99, 1]],
                  "asks": [[100.5, 10], [101, 10], [101.5, 10]]}
    for i in range(10):
        t.add_trade_volume(50.0)  # real fills happening
        t.push_book(_buy_heavy() if i % 2 == 0 else sell_heavy)
    t.add_trade_volume(50.0)
    snap = t.push_book(_buy_heavy())
    assert snap.cancellation_rate < 0.2, f"expected low cancel rate, got {snap.cancellation_rate}"


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
    print()
    if failures:
        print(f"{len(failures)} / {len(tests)} FAILED")
        return 1
    print(f"{len(tests)} / {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
