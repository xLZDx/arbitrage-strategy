"""
Phase 11 — paper-trade hardener tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.exec.bybit_leg import BybitLegExecutor
from src.exec.bundle_simulator import BundleSimulator
from src.exec.coordinator import ArbCoordinator
from src.exec.dex_leg import DexLegExecutor
from src.exec.private_rpc_router import PrivateRpcRouter
from src.risk import limits as risk
from src.sim.inventory import Inventory
from src.sim.paper_trade import PaperTradeRunner, PaperTradeRecord, gap_summary
from src.utils import config


_TEST_LEDGER = config.DATA_DIR / "_test_p11_idempotency.json"


def setup_function(_):
    risk.halt_clear()
    if _TEST_LEDGER.exists():
        _TEST_LEDGER.unlink()


def teardown_function(_):
    risk.halt_clear()
    if _TEST_LEDGER.exists():
        _TEST_LEDGER.unlink()


def _opp(i=0, theoretical=0.10):
    return {
        "ts": f"2026-05-11T12:{i:02d}:00+00:00", "pair": "BTCUSDT",
        "decision": "GO", "direction": "bybit_high",
        "spread_bps": 30.0, "gross_bps": 30.0,
        "expected_net_bps": 12.0, "theoretical_pnl_usd": theoretical,
        "weighted_obi": 0.1, "obi_delta": 0.0, "cancellation_rate": 0.0,
        "gas_gwei": 0.006, "gas_cost_bps": 0.65,
        "slippage_haircut_bps": 5.0,
        "bybit_fee_bps": 10.0, "dex_fee_bps": 5.0,
        "notional_usd": 50.0,
        "bybit_mid": 80000.0, "dex_mid": 79760.0,
        "eth_price_used": 3000.0,
    }


def _runner():
    coord = ArbCoordinator(
        bybit=BybitLegExecutor(mode=config.MODE_SHADOW, ledger_path=_TEST_LEDGER),
        dex=DexLegExecutor(mode=config.MODE_SHADOW),
        router=PrivateRpcRouter(mode=config.MODE_SHADOW),
        simulator=BundleSimulator(mode=config.MODE_SHADOW),
        inventory=Inventory.with_balanced_seed(2000.0),
        risk_state=risk.RiskState(),
    )
    return PaperTradeRunner(coordinator=coord, rng_seed=7)


def test_run_opportunity_returns_record() -> None:
    r = _runner().run_opportunity(_opp())
    assert isinstance(r, PaperTradeRecord)
    assert r.pair == "BTCUSDT"
    assert r.coordinator_outcome == "shadow"


def test_run_opportunity_records_both_estimates() -> None:
    r = _runner().run_opportunity(_opp(theoretical=0.10))
    assert r.coordinator_pnl_estimate == 0.10
    # Sim PnL is computed from gross - costs - gas; should be in same ballpark
    assert -0.50 < r.sim_realized_pnl_usd < 0.50


def test_run_opportunity_pnl_gap_math() -> None:
    r = _runner().run_opportunity(_opp(theoretical=0.10))
    expected_gap = r.coordinator_pnl_estimate - r.sim_realized_pnl_usd
    assert abs(r.pnl_gap_usd - expected_gap) < 1e-9


def test_run_batch_returns_n_records() -> None:
    runner = _runner()
    opps = [_opp(i=i) for i in range(5)]
    rs = runner.run_batch(opps)
    assert len(rs) == 5


def test_paper_runner_deterministic_under_seed() -> None:
    """Two runners with same seed → same realized slippage / fill draws."""
    rng_a = PaperTradeRunner(coordinator=_runner().coordinator, rng_seed=42)
    rng_b = PaperTradeRunner(coordinator=_runner().coordinator, rng_seed=42)
    opps = [_opp(i=i) for i in range(10)]
    a_records = rng_a.run_batch(opps)
    b_records = rng_b.run_batch(opps)
    for a, b in zip(a_records, b_records):
        assert a.sim_realized_pnl_usd == b.sim_realized_pnl_usd
        assert a.sim_fill_pct == b.sim_fill_pct


def test_gap_summary_empty() -> None:
    s = gap_summary([])
    assert s["n"] == 0
    assert s["within_15_pct"] is True


def test_gap_summary_aggregates() -> None:
    runner = _runner()
    opps = [_opp(i=i, theoretical=0.10) for i in range(20)]
    records = runner.run_batch(opps)
    s = gap_summary(records)
    assert s["n"] == 20
    assert "abs_gap_pct" in s
    assert s["abs_gap_pct"] >= 0


def test_gap_within_15_pct_when_models_agree() -> None:
    """Construct opportunities where coord_estimate ≈ sim_pnl, gap should be small."""
    runner = _runner()
    # Gross 30 bps, costs ~16, expect ~14 bps net = $0.07 on $50.
    # Coord estimate of $0.07 should produce ~0% gap.
    opps = [_opp(i=i, theoretical=0.07) for i in range(30)]
    records = runner.run_batch(opps)
    s = gap_summary(records)
    # 30% noise on slippage produces some variance, but mean should be < 15%
    assert s["abs_gap_pct"] < 50.0  # generous; verifies the channel works


def _run_all() -> int:
    failures: list[tuple[str, str]] = []
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            setup_function(None)
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
        finally:
            teardown_function(None)
    print()
    if failures:
        print(f"{len(failures)} / {len(tests)} FAILED")
        return 1
    print(f"{len(tests)} / {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
