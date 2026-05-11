"""
Inventory auto-rebalancer (Phase 4.X).

Phase 4 originally only ALERTED on inventory imbalance. This module adds
the actual rebalance pathway:

  1. Watch the live inventory.
  2. When imbalance > REBALANCE_TRIGGER_PCT, plan a rebalancing transfer.
  3. In SHADOW: log the plan only.
  4. In TESTNET/MAINNET: execute the transfer (Phase 4.X.X — needs venue
     withdraw/deposit APIs; for now SHADOW + planning is the deliverable).

The "plan" is a list of transfer legs that would restore balance:
  - debit the heavy side's stable, credit the light side's stable
  - the actual transfer is bridge or bot-operator manual move

Personal-use focus: this is what keeps a CEX-DEX arb sustainable. Without
rebalancing, the inventory drifts after each trade and eventually one side
runs out, halting all activity. Phase 4 alert-only meant the operator had
to manually rebalance every few hours; this automates the planning step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from src.sim.inventory import Inventory
from src.utils import config, safe_json

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransferLeg:
    """One leg of a rebalancing transfer."""
    venue_from: Literal["bybit", "dex"]
    venue_to: Literal["bybit", "dex"]
    asset: str
    amount_usd: float


@dataclass(frozen=True)
class RebalancePlan:
    ts: str
    imbalance_before: float
    imbalance_after_estimated: float
    legs: tuple[TransferLeg, ...]
    severity: Literal["WARN", "ACT", "NO-OP"]
    reason: str


def plan_rebalance(
    inv: Inventory,
    trigger_pct: float = config.REBALANCE_TRIGGER_PCT,
) -> RebalancePlan:
    """
    Computes the smallest transfer that brings imbalance below trigger_pct.

    Strategy: move stable from the heavy side to the light side. Stables
    are the cheapest asset to transfer (no DEX swap needed; bridge fee only).
    """
    ts = datetime.now(timezone.utc).isoformat()
    bybit_total = sum(inv.bybit.values())
    dex_total = sum(inv.dex.values())
    total = bybit_total + dex_total

    if total <= 0:
        return RebalancePlan(ts, 0.0, 0.0, (), "NO-OP", "empty_inventory")

    imbalance = abs(bybit_total - dex_total) / total
    if imbalance < trigger_pct:
        return RebalancePlan(ts, round(imbalance, 4), round(imbalance, 4), (),
                              "NO-OP", f"below_trigger_{trigger_pct:.2f}")

    # Heavy side gives up half the gap; light side receives it. After the
    # transfer, both sides are within stable rounding of equal.
    gap = abs(bybit_total - dex_total)
    transfer_usd = gap / 2.0

    if bybit_total > dex_total:
        from_venue, to_venue = "bybit", "dex"
        from_stable, to_stable = "USDT", "USDC"
    else:
        from_venue, to_venue = "dex", "bybit"
        from_stable, to_stable = "USDC", "USDT"

    available = inv.get(from_venue, from_stable)
    transfer_usd = min(transfer_usd, available)
    if transfer_usd <= 0:
        return RebalancePlan(ts, round(imbalance, 4), round(imbalance, 4), (),
                              "NO-OP", f"no_stable_on_{from_venue}")

    legs = (
        TransferLeg(venue_from=from_venue, venue_to=to_venue,
                    asset=from_stable, amount_usd=transfer_usd),
    )
    # Estimated post-transfer imbalance (assumes 1:1 stable peg)
    new_bybit = bybit_total + (transfer_usd if from_venue == "dex" else -transfer_usd)
    new_dex = dex_total + (transfer_usd if from_venue == "bybit" else -transfer_usd)
    new_total = new_bybit + new_dex
    new_imbalance = (abs(new_bybit - new_dex) / new_total) if new_total > 0 else 0.0
    severity: Literal["WARN", "ACT", "NO-OP"] = (
        "ACT" if config.AUTO_REBALANCE else "WARN"
    )
    return RebalancePlan(
        ts=ts,
        imbalance_before=round(imbalance, 4),
        imbalance_after_estimated=round(new_imbalance, 4),
        legs=legs,
        severity=severity,
        reason=f"transfer_{transfer_usd:.2f}_{from_stable}_{from_venue}_to_{to_venue}",
    )


def apply_rebalance(inv: Inventory, plan: RebalancePlan) -> bool:
    """
    Applies the plan to the inventory ledger (SHADOW-equivalent).

    In TESTNET/MAINNET this would:
      - Withdraw stable from the heavy venue (Bybit API or DEX wallet send)
      - Bridge to the light venue (cross-chain or CEX deposit)
      - Wait for confirmations
      - Update inventory ledger after confirmation
    For Phase 4.X SHADOW we just adjust the in-memory ledger and log.
    """
    if not plan.legs:
        return False
    for leg in plan.legs:
        inv.adjust(leg.venue_from, leg.asset, -leg.amount_usd)
        # Translate stable across venues (USDT <-> USDC at peg for SHADOW)
        to_asset = "USDC" if leg.venue_to == "dex" else "USDT"
        inv.adjust(leg.venue_to, to_asset, +leg.amount_usd)
    log.info("[REBALANCE %s] applied: %s", plan.severity, plan.reason)
    return True


def log_rebalance_plan(plan: RebalancePlan) -> None:
    """Append plan to data/arb/rebalance_plans.jsonl."""
    if not plan.legs and plan.severity == "NO-OP":
        return
    path = config.LOG_DIR / "rebalance_plans.jsonl"
    record = {
        "ts": plan.ts,
        "imbalance_before": plan.imbalance_before,
        "imbalance_after_estimated": plan.imbalance_after_estimated,
        "severity": plan.severity,
        "reason": plan.reason,
        "legs": [
            {"from": l.venue_from, "to": l.venue_to,
             "asset": l.asset, "amount_usd": l.amount_usd}
            for l in plan.legs
        ],
    }
    try:
        safe_json.append_jsonl(path, record)
    except Exception as e:
        log.warning("failed to log rebalance plan: %s", e)


def watch_and_rebalance(inv: Inventory) -> RebalancePlan:
    """
    One-shot check: build plan, log it, optionally apply it.
    Returns the plan so callers can inspect.
    """
    plan = plan_rebalance(inv)
    log_rebalance_plan(plan)
    if plan.severity == "ACT" and config.AUTO_REBALANCE:
        apply_rebalance(inv, plan)
    return plan
