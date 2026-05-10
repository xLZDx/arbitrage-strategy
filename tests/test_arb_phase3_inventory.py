"""
Phase 3 — Inventory tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.sim.inventory import (
    ASSETS_BY_VENUE, Inventory, PAIR_LEGS,
)


def test_inventory_with_initial_usd_symmetric() -> None:
    inv = Inventory.with_initial_usd(500.0)
    assert inv.get("bybit", "USDT") == 500.0
    assert inv.get("dex", "USDC") == 500.0
    for asset in ASSETS_BY_VENUE["bybit"]:
        if asset != "USDT":
            assert inv.get("bybit", asset) == 0.0
    for asset in ASSETS_BY_VENUE["dex"]:
        if asset != "USDC":
            assert inv.get("dex", asset) == 0.0


def test_pair_legs_cover_all_pilot_pairs() -> None:
    """Every pilot pair must have an inventory mapping."""
    for pair in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        assert pair in PAIR_LEGS
        bybit_base, dex_base = PAIR_LEGS[pair]
        assert bybit_base in ASSETS_BY_VENUE["bybit"]
        assert dex_base in ASSETS_BY_VENUE["dex"]


def test_can_apply_rejects_below_zero() -> None:
    inv = Inventory.with_initial_usd(100.0)
    legs = [("bybit", "USDT", -150.0)]   # over-spend
    ok, reason = inv.can_apply(legs)
    assert not ok
    assert "insufficient_bybit_USDT" in reason
    # state untouched
    assert inv.get("bybit", "USDT") == 100.0


def test_apply_atomic_all_or_nothing() -> None:
    inv = Inventory.with_initial_usd(100.0)
    # one leg ok (-50 USDC) but second would push BTC < 0
    legs = [
        ("dex", "USDC", -50.0),
        ("bybit", "BTC", -1.0),  # we have 0 BTC
    ]
    ok, reason = inv.apply(legs)
    assert not ok
    # No partial application
    assert inv.get("dex", "USDC") == 100.0
    assert inv.get("bybit", "BTC") == 0.0


def test_apply_succeeds_and_persists() -> None:
    """Real CEX-DEX arb requires pre-positioned BOTH base asset AND USD on each side.
    Without pre-positioning, the first 'sell BTC' leg has no BTC to sell."""
    inv = Inventory.with_initial_usd(100.0)
    # Pre-position 0.001 BTC on Bybit and 0.001 cbBTC on DEX
    inv.adjust("bybit", "BTC", 0.001)
    inv.adjust("dex", "cbBTC", 0.001)
    # dex_high: buy BTC on Bybit (debit USDT, credit BTC),
    # sell cbBTC on DEX (debit cbBTC, credit USDC)
    legs = [
        ("bybit", "USDT", -50.0),
        ("bybit", "BTC",  +0.0006),
        ("dex",   "cbBTC", -0.0006),
        ("dex",   "USDC",  +50.0),
    ]
    ok, _ = inv.apply(legs)
    assert ok
    assert inv.get("bybit", "USDT") == 50.0
    assert abs(inv.get("bybit", "BTC") - 0.0016) < 1e-12  # 0.001 + 0.0006
    assert abs(inv.get("dex", "cbBTC") - 0.0004) < 1e-12  # 0.001 - 0.0006
    assert inv.get("dex", "USDC") == 150.0


def test_total_usd_sums_balances() -> None:
    """Phase 3 stores balances as USD-equivalent → total = sum.
    Regression: earlier version multiplied by price → produced $13M on
    a $1k portfolio."""
    inv = Inventory.with_initial_usd(500.0)
    inv.adjust("bybit", "BTC", 400.0)    # +$400 USD-equivalent
    inv.adjust("bybit", "USDT", -400.0)  # -$400 USDT
    # bybit: 100 USDT + 400 BTC = 500
    # dex:   500 USDC = 500
    assert inv.total_usd() == 1000.0
    # Price args accepted but ignored (forward-compat with Phase 5)
    assert inv.total_usd({"BTC": 99999.0}, {"cbBTC": 99999.0}) == 1000.0


def test_imbalance_ratio_zero_when_balanced() -> None:
    inv = Inventory.with_initial_usd(500.0)
    assert inv.imbalance_ratio() == 0.0


def test_imbalance_ratio_high_when_one_side_drained() -> None:
    inv = Inventory.with_initial_usd(500.0)
    inv.adjust("bybit", "USDT", -300.0)  # bybit now $200, dex $500
    r = inv.imbalance_ratio()
    # |200 - 500| / 700 = 300/700 ≈ 0.429
    assert 0.4 < r < 0.5


def test_imbalance_ratio_safe_when_total_zero() -> None:
    inv = Inventory()  # empty
    assert inv.imbalance_ratio() == 0.0


# --- with_balanced_seed ---------------------------------------------------


def test_balanced_seed_splits_50_50_by_default() -> None:
    inv = Inventory.with_balanced_seed(usd_per_side=600.0)
    # 50% stable: $300 USDT + $300 USDC
    assert inv.get("bybit", "USDT") == 300.0
    assert inv.get("dex", "USDC") == 300.0
    # 50% non-stable split across 3 pairs = $100/asset
    assert inv.get("bybit", "BTC") == 100.0
    assert inv.get("bybit", "ETH") == 100.0
    assert inv.get("bybit", "SOL") == 100.0
    assert inv.get("dex", "cbBTC") == 100.0
    assert inv.get("dex", "WETH") == 100.0
    assert inv.get("dex", "wSOL") == 100.0


def test_balanced_seed_custom_split() -> None:
    inv = Inventory.with_balanced_seed(usd_per_side=300.0, usd_split_pct=70.0)
    # 70% stable: $210 each
    assert inv.get("bybit", "USDT") == 210.0
    assert inv.get("dex", "USDC") == 210.0
    # 30% non-stable / 3 pairs = $30 each
    assert inv.get("bybit", "BTC") == 30.0


def test_balanced_seed_supports_bybit_high_first_trade() -> None:
    """The whole point: a balanced seed must let SELL-BTC-on-Bybit succeed
    on the first call (regression for the unbalanced-seed bug)."""
    inv = Inventory.with_balanced_seed(usd_per_side=300.0)
    legs = [
        ("bybit", "BTC",   -50.0),  # sell $50 of BTC on Bybit
        ("bybit", "USDT",  +50.0),
        ("dex",   "USDC",  -50.0),
        ("dex",   "cbBTC", +50.0),
    ]
    ok, _ = inv.apply(legs)
    assert ok


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
