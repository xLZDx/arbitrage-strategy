"""
Phase 4 — risk module tests (HALT ops, RiskState, preflight, auto-HALT).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.risk import limits as risk
from src.utils import config


def setup_function(_):
    risk.halt_clear()


def teardown_function(_):
    risk.halt_clear()


# --- HALT file ops --------------------------------------------------------


def test_halt_clear_initial_state() -> None:
    assert not risk.halt_active()
    assert risk.halt_reason() is None


def test_halt_set_creates_file_with_reason() -> None:
    p = risk.halt_set("test reason 123")
    assert p.exists()
    assert risk.halt_active()
    reason = risk.halt_reason()
    assert reason is not None
    assert "test reason 123" in reason


def test_halt_clear_removes_file() -> None:
    risk.halt_set("temp")
    assert risk.halt_active()
    cleared = risk.halt_clear()
    assert cleared is True
    assert not risk.halt_active()


def test_halt_clear_returns_false_when_already_clear() -> None:
    cleared = risk.halt_clear()
    assert cleared is False


def test_halt_set_atomic_overwrites() -> None:
    risk.halt_set("first")
    risk.halt_set("second")
    assert "second" in risk.halt_reason()


# --- RiskState properties --------------------------------------------------


def test_riskstate_caps_match_config() -> None:
    state = risk.RiskState(bankroll_per_side_usd=1000.0)
    # 5% of 1000 = 50
    assert state.daily_loss_cap_usd == 50.0
    # 15% of 1000 = 150
    assert state.drawdown_trigger_usd == 150.0
    # 10% of 1000 = 100
    assert state.per_trade_cap_usd == 100.0


def test_riskstate_default_uses_config() -> None:
    state = risk.RiskState()
    assert state.bankroll_per_side_usd == config.BANKROLL_PER_SIDE_USD


# --- preflight ------------------------------------------------------------


def test_preflight_ok_when_clean_no_opp() -> None:
    gate = risk.preflight(None, risk.RiskState())
    assert gate.decision == "OK"
    assert gate.is_ok()


def test_preflight_halt_when_halt_flag_set() -> None:
    risk.halt_set("manual")
    gate = risk.preflight(None, risk.RiskState())
    assert gate.decision == "HALT"
    assert "halt_flag" in gate.reason


def test_preflight_halt_on_daily_loss_breach() -> None:
    state = risk.RiskState()
    state.today_realized_pnl_usd = -state.daily_loss_cap_usd - 0.01
    gate = risk.preflight(None, state)
    assert gate.decision == "HALT"
    assert "daily_loss_cap" in gate.reason


def test_preflight_halt_on_drawdown_trigger() -> None:
    state = risk.RiskState()
    state.rolling_24h_drawdown_usd = state.drawdown_trigger_usd + 1.0
    gate = risk.preflight(None, state)
    assert gate.decision == "HALT"
    assert "drawdown" in gate.reason


def test_preflight_halt_on_inventory_imbalance() -> None:
    state = risk.RiskState()
    state.inventory_imbalance = 0.30
    gate = risk.preflight(None, state)
    assert gate.decision == "HALT"
    assert "inventory_imbalance" in gate.reason


def test_preflight_halt_on_consecutive_bundle_reverts() -> None:
    state = risk.RiskState()
    state.consecutive_bundle_reverts = 3
    gate = risk.preflight(None, state)
    assert gate.decision == "HALT"
    assert "bundle_reverts" in gate.reason


def test_preflight_reject_on_oversized_notional() -> None:
    state = risk.RiskState()
    opp = {"decision": "GO", "notional_usd": state.per_trade_cap_usd * 2,
           "expected_net_bps": 20.0}
    gate = risk.preflight(opp, state)
    assert gate.decision == "REJECT"
    assert "notional_exceeds_cap" in gate.reason


def test_preflight_reject_on_zero_notional() -> None:
    state = risk.RiskState()
    opp = {"decision": "GO", "notional_usd": 0.0, "expected_net_bps": 20.0}
    gate = risk.preflight(opp, state)
    assert gate.decision == "REJECT"
    assert "non_positive" in gate.reason


def test_preflight_reject_when_decision_not_go() -> None:
    state = risk.RiskState()
    opp = {"decision": "SKIP", "notional_usd": 50.0, "expected_net_bps": 20.0}
    gate = risk.preflight(opp, state)
    assert gate.decision == "REJECT"
    assert "not_go" in gate.reason


def test_preflight_reject_below_min_net_bps() -> None:
    state = risk.RiskState()
    opp = {"decision": "GO", "notional_usd": 50.0,
           "expected_net_bps": config.MIN_NET_BPS - 1.0}
    gate = risk.preflight(opp, state)
    assert gate.decision == "REJECT"
    assert "below_min_net_bps" in gate.reason


def test_preflight_ok_for_valid_opp() -> None:
    state = risk.RiskState()
    opp = {"decision": "GO", "notional_usd": state.per_trade_cap_usd * 0.5,
           "expected_net_bps": config.MIN_NET_BPS + 5.0}
    gate = risk.preflight(opp, state)
    assert gate.decision == "OK"


def test_preflight_responds_within_2s() -> None:
    """Per RISK.md drill: HALT must propagate within 2 seconds."""
    risk.halt_set("speed test")
    t0 = time.time()
    gate = risk.preflight(None, risk.RiskState())
    elapsed = time.time() - t0
    assert gate.decision == "HALT"
    assert elapsed < 2.0, f"preflight took {elapsed:.3f}s, exceeds 2s budget"


# --- maybe_auto_halt -------------------------------------------------------


def test_auto_halt_no_op_when_clean() -> None:
    triggered = risk.maybe_auto_halt(risk.RiskState())
    assert not triggered
    assert not risk.halt_active()


def test_auto_halt_no_op_when_already_halted() -> None:
    risk.halt_set("pre-existing")
    state = risk.RiskState()
    state.today_realized_pnl_usd = -1_000_000.0
    triggered = risk.maybe_auto_halt(state)
    assert not triggered  # already halted, returns False
    assert risk.halt_active()


def test_auto_halt_fires_on_daily_loss() -> None:
    state = risk.RiskState()
    state.today_realized_pnl_usd = -state.daily_loss_cap_usd - 0.01
    assert risk.maybe_auto_halt(state)
    assert risk.halt_active()
    assert "daily_loss_cap" in risk.halt_reason()


def test_auto_halt_fires_on_drawdown() -> None:
    state = risk.RiskState()
    state.rolling_24h_drawdown_usd = state.drawdown_trigger_usd + 1.0
    assert risk.maybe_auto_halt(state)
    assert risk.halt_active()
    assert "drawdown" in risk.halt_reason()


def test_auto_halt_fires_on_imbalance() -> None:
    state = risk.RiskState()
    state.inventory_imbalance = 0.40
    assert risk.maybe_auto_halt(state)
    assert "imbalance" in risk.halt_reason()


def test_auto_halt_fires_on_bundle_reverts() -> None:
    state = risk.RiskState()
    state.consecutive_bundle_reverts = 5
    assert risk.maybe_auto_halt(state)
    assert "bundle reverts" in risk.halt_reason()


def test_auto_halt_fires_on_bybit_rate_limit() -> None:
    state = risk.RiskState()
    state.bybit_rate_limit_err_rate_5min = 0.10
    assert risk.maybe_auto_halt(state)
    assert "bybit rate-limit" in risk.halt_reason()


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
