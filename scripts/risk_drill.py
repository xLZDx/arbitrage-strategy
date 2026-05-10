"""
Risk drill — verify HALT flag halts execution paths within 2s.

Per CLAUDE.md / RISK.md drill spec. Run before any Phase-12 ramp.

What it checks:
  1. HALT clear -> preflight returns OK
  2. Set HALT manually -> preflight returns HALT within next call
  3. Auto-HALT on simulated daily-loss-cap breach
  4. Auto-HALT on inventory imbalance > 25%
  5. Clear HALT, verify OK again

Run:
  ./venv/Scripts/python.exe scripts/risk_drill.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.risk import limits as risk_limits
from src.utils import config


def _log(msg: str, ok: bool = True) -> None:
    tag = "  PASS" if ok else "  FAIL"
    print(f"{tag}  {msg}")


def main() -> int:
    print("=== RISK DRILL ===")
    failures = 0

    # Start clean
    risk_limits.halt_clear()
    if risk_limits.halt_active():
        _log("could not clear HALT before drill", ok=False)
        return 2

    # 1. Clean state -> OK
    state = risk_limits.RiskState()
    gate = risk_limits.preflight(opportunity=None, state=state)
    if gate.decision == "OK":
        _log("clean state -> preflight OK")
    else:
        _log(f"clean state expected OK, got {gate.decision}: {gate.reason}", ok=False)
        failures += 1

    # 2. Manual HALT -> preflight returns HALT
    risk_limits.halt_set("drill: manual halt")
    t0 = time.time()
    gate = risk_limits.preflight(opportunity=None, state=state)
    elapsed = time.time() - t0
    if gate.decision == "HALT" and elapsed < 2.0:
        _log(f"manual HALT detected in {elapsed*1000:.1f}ms")
    else:
        _log(f"expected HALT within 2s, got {gate.decision} after {elapsed:.3f}s", ok=False)
        failures += 1
    risk_limits.halt_clear()

    # 3. Auto-HALT on daily-loss-cap breach
    state = risk_limits.RiskState()
    state.today_realized_pnl_usd = -state.daily_loss_cap_usd - 1.0  # over cap
    triggered = risk_limits.maybe_auto_halt(state)
    after = risk_limits.halt_active()
    if triggered and after:
        _log(f"daily-loss breach auto-HALT fired (cap=${state.daily_loss_cap_usd:.2f})")
    else:
        _log(f"expected auto-HALT on daily-loss breach, triggered={triggered} active={after}", ok=False)
        failures += 1
    risk_limits.halt_clear()

    # 4. Auto-HALT on drawdown trigger
    state = risk_limits.RiskState()
    state.rolling_24h_drawdown_usd = state.drawdown_trigger_usd + 0.01
    triggered = risk_limits.maybe_auto_halt(state)
    after = risk_limits.halt_active()
    if triggered and after:
        _log(f"drawdown auto-HALT fired (trigger=${state.drawdown_trigger_usd:.2f})")
    else:
        _log(f"expected auto-HALT on drawdown, triggered={triggered}", ok=False)
        failures += 1
    risk_limits.halt_clear()

    # 5. Auto-HALT on inventory imbalance
    state = risk_limits.RiskState()
    state.inventory_imbalance = 0.30
    triggered = risk_limits.maybe_auto_halt(state)
    after = risk_limits.halt_active()
    if triggered and after:
        _log(f"inventory-imbalance auto-HALT fired (0.30 > 0.25)")
    else:
        _log(f"expected auto-HALT on imbalance, triggered={triggered}", ok=False)
        failures += 1
    risk_limits.halt_clear()

    # 6. Per-trade cap rejection
    state = risk_limits.RiskState()
    bad_opp = {"decision": "GO", "notional_usd": state.per_trade_cap_usd * 5,
               "expected_net_bps": 50.0}
    gate = risk_limits.preflight(opportunity=bad_opp, state=state)
    if gate.decision == "REJECT" and "notional_exceeds_cap" in gate.reason:
        _log(f"per-trade cap rejection: {gate.reason}")
    else:
        _log(f"expected REJECT on oversized notional, got {gate.decision}: {gate.reason}", ok=False)
        failures += 1

    # 7. Below MIN_NET_BPS rejection
    state = risk_limits.RiskState()
    weak_opp = {"decision": "GO", "notional_usd": 50.0, "expected_net_bps": 1.0}
    gate = risk_limits.preflight(opportunity=weak_opp, state=state)
    if gate.decision == "REJECT" and "below_min_net_bps" in gate.reason:
        _log(f"weak-opp rejection: {gate.reason}")
    else:
        _log(f"expected REJECT on weak opp, got {gate.decision}: {gate.reason}", ok=False)
        failures += 1

    # 8. Clear HALT verifies cleanly
    risk_limits.halt_clear()
    state = risk_limits.RiskState()
    gate = risk_limits.preflight(opportunity=None, state=state)
    if gate.decision == "OK":
        _log("HALT cleared -> preflight OK again")
    else:
        _log(f"after clear, expected OK got {gate.decision}: {gate.reason}", ok=False)
        failures += 1

    print()
    if failures:
        print(f"DRILL FAILED: {failures} check(s) did not pass.")
        return 1
    print("DRILL PASSED: all 8 checks ok.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
