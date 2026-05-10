"""
Risk limits + pre-flight gate.

Phase 4 = the gate that Phase 5 execution code MUST call before sending any
trade. Everything here is pure decision logic + file-flag state; no I/O
beyond reading the HALT file and reading sim_trades for daily-loss / drawdown
calculation.

Pre-flight order (per RISK.md §5):
  1. HALT file absent
  2. Daily loss < DAILY_LOSS_CAP_PCT * BANKROLL_PER_SIDE_USD
  3. Drawdown < DRAWDOWN_TRIGGER_PCT * BANKROLL_PER_SIDE_USD
  4. Inventory imbalance < 25%
  5. expected_net_profit > simulated_gas + bribe_floor + cex_fees   (Phase 2)
  6. expected_net_bps >= MIN_NET_BPS                                  (Phase 2)
  7. HistGBT veto threshold                                           (Phase 6)
  8. simulate_bundle() succeeds                                       (Phase 5)
  9. amountOutMin and deadline populated                              (Phase 5)

Steps 1-4 land here in Phase 4. Steps 5-6 already enforced in
src/strategy/opportunity.py. Steps 7-9 land in their respective phases.

Auto-HALT triggers (per RISK.md §3): set HALT flag when ANY of:
  - daily loss > cap
  - drawdown > trigger
  - inventory imbalance > 25%
  - bundle inclusion rate < threshold for 30 min          (Phase 8 — TBD)
  - 3 consecutive bundle simulation reverts                (Phase 5 — TBD)
  - unhandled exception in any process                     (Phase 5 — TBD)
  - Bybit API rate-limit error rate > 5% / 5min            (Phase 5 — TBD)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from src.utils import config

log = logging.getLogger(__name__)

GateResultT = Literal["OK", "HALT", "REJECT"]


# --- HALT file ops ---------------------------------------------------------


def halt_set(reason: str) -> Path:
    """Atomically create the HALT flag file with a reason. Returns path."""
    config.HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    text = f"{datetime.now(timezone.utc).isoformat()} {reason}\n"
    tmp = config.HALT_FILE.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(config.HALT_FILE)
    log.warning("HALT set: %s", reason)
    return config.HALT_FILE


def halt_clear() -> bool:
    """Manually remove the HALT flag. Returns True if it was present."""
    if config.HALT_FILE.exists():
        config.HALT_FILE.unlink()
        log.info("HALT cleared")
        return True
    return False


def halt_active() -> bool:
    return config.HALT_FILE.exists()


def halt_reason() -> str | None:
    if not config.HALT_FILE.exists():
        return None
    try:
        return config.HALT_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown (file unreadable)"


# --- RiskState -------------------------------------------------------------


@dataclass
class RiskState:
    """
    Snapshot of the moving risk metrics the gate cares about.
    Built fresh each cycle (not held for long); cheap to construct.
    """
    bankroll_per_side_usd: float = config.BANKROLL_PER_SIDE_USD
    today_realized_pnl_usd: float = 0.0
    today_unrealized_pnl_usd: float = 0.0   # Phase 5+ when we have open legs
    rolling_24h_drawdown_usd: float = 0.0
    inventory_imbalance: float = 0.0          # 0..1 from Inventory.imbalance_ratio()
    consecutive_bundle_reverts: int = 0       # Phase 5+
    rolling_1h_bundle_inclusion_rate: float = 1.0  # Phase 5+
    bybit_rate_limit_err_rate_5min: float = 0.0    # Phase 5+

    @property
    def daily_loss_cap_usd(self) -> float:
        return self.bankroll_per_side_usd * config.DAILY_LOSS_CAP_PCT / 100.0

    @property
    def drawdown_trigger_usd(self) -> float:
        return self.bankroll_per_side_usd * config.DRAWDOWN_TRIGGER_PCT / 100.0

    @property
    def per_trade_cap_usd(self) -> float:
        return self.bankroll_per_side_usd * config.PER_TRADE_CAP_PCT / 100.0


# --- Pre-flight gate -------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    decision: GateResultT
    reason: str

    def is_ok(self) -> bool:
        return self.decision == "OK"


def preflight(opportunity: dict | None, state: RiskState) -> GateResult:
    """
    Returns OK only if every Phase-4 check passes.

    HALT vs REJECT distinction:
      - HALT means a global condition is bad — no trades on any pair.
      - REJECT means this specific opportunity is unviable — try the next one.

    The caller must NOT execute on a non-OK result. opportunity may be None
    when the caller just wants to ask "are we paused?" globally.
    """
    if halt_active():
        return GateResult("HALT", f"halt_flag: {halt_reason()}")

    if state.today_realized_pnl_usd <= -state.daily_loss_cap_usd:
        return GateResult("HALT", f"daily_loss_cap: realized={state.today_realized_pnl_usd:.4f} cap={state.daily_loss_cap_usd:.4f}")

    if state.rolling_24h_drawdown_usd >= state.drawdown_trigger_usd:
        return GateResult("HALT", f"drawdown: {state.rolling_24h_drawdown_usd:.4f} >= {state.drawdown_trigger_usd:.4f}")

    if state.inventory_imbalance > 0.25:
        return GateResult("HALT", f"inventory_imbalance: {state.inventory_imbalance:.3f} > 0.25")

    if state.consecutive_bundle_reverts >= 3:
        return GateResult("HALT", f"bundle_reverts: {state.consecutive_bundle_reverts} >= 3")

    if state.bybit_rate_limit_err_rate_5min > 0.05:
        return GateResult("HALT", f"bybit_rate_limit_errors: {state.bybit_rate_limit_err_rate_5min:.3f} > 0.05")

    if opportunity is None:
        return GateResult("OK", "global_state_clean")

    notional = float(opportunity.get("notional_usd", 0.0))
    if notional <= 0:
        return GateResult("REJECT", "non_positive_notional")
    if notional > state.per_trade_cap_usd:
        return GateResult("REJECT", f"notional_exceeds_cap: {notional} > {state.per_trade_cap_usd}")

    if opportunity.get("decision") != "GO":
        return GateResult("REJECT", "opportunity_not_go")

    expected_net_bps = float(opportunity.get("expected_net_bps", 0.0))
    if expected_net_bps < config.MIN_NET_BPS:
        return GateResult("REJECT", f"below_min_net_bps: {expected_net_bps} < {config.MIN_NET_BPS}")

    return GateResult("OK", "pass")


# --- Auto-HALT scanner ----------------------------------------------------


def maybe_auto_halt(state: RiskState) -> bool:
    """
    Inspects RiskState and sets HALT if any auto-trigger condition is true.
    Returns True if HALT was set on this call (False if no-op or already set).
    """
    if halt_active():
        return False  # already HALT-ed

    if state.today_realized_pnl_usd <= -state.daily_loss_cap_usd:
        halt_set(f"auto: daily_loss_cap exceeded "
                 f"({state.today_realized_pnl_usd:.4f} <= -{state.daily_loss_cap_usd:.4f})")
        return True
    if state.rolling_24h_drawdown_usd >= state.drawdown_trigger_usd:
        halt_set(f"auto: drawdown trigger ({state.rolling_24h_drawdown_usd:.4f} "
                 f">= {state.drawdown_trigger_usd:.4f})")
        return True
    if state.inventory_imbalance > 0.25:
        halt_set(f"auto: inventory imbalance ({state.inventory_imbalance:.3f} > 0.25)")
        return True
    if state.consecutive_bundle_reverts >= 3:
        halt_set(f"auto: {state.consecutive_bundle_reverts} consecutive bundle reverts")
        return True
    if state.bybit_rate_limit_err_rate_5min > 0.05:
        halt_set(f"auto: bybit rate-limit error rate "
                 f"{state.bybit_rate_limit_err_rate_5min:.3f} > 0.05")
        return True
    return False
