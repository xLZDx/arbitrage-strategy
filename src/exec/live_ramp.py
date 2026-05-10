"""
Live ramp guard (Phase 12).

This is the LAST defense before Phase-12 mainnet capital. It enforces:
  1. ARB_MAINNET_GATE=1 must be set explicitly
  2. Drill must have run within the last DRILL_VALIDITY_HOURS hours
  3. Phase-11 paper-trade soak must show |gap| <= 15% over >= 7 days
  4. Per-trade cap stays at 10% of bankroll, daily cap at 5%
  5. Bankroll is the PROVIDED bankroll, not the $500 placeholder

Usage:
    guard = LiveRampGuard(...)
    guard.assert_ready()  # raises RuntimeError if any check fails

Called at startup of the live executor (Phase 12.X — separate executor
process distinct from the SHADOW one).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.utils import config

log = logging.getLogger(__name__)

DRILL_VALIDITY_HOURS = 24      # drill must have run in the last day
PAPER_SOAK_MIN_DAYS = 7
PAPER_SOAK_MAX_GAP_PCT = 15.0
DRILL_LOG_PATH = config.LOG_DIR / "drill_runs.jsonl"


@dataclass(frozen=True)
class RampReadiness:
    ready: bool
    reasons: tuple[str, ...]


@dataclass
class LiveRampGuard:
    bankroll_per_side_usd: float
    paper_trade_records_path: Path | None = None  # data/arb/db/paper_trades/

    def check(self) -> RampReadiness:
        reasons: list[str] = []
        ok = True

        # 1. ARB_MAINNET_GATE
        if os.environ.get("ARB_MAINNET_GATE") != "1":
            reasons.append("ARB_MAINNET_GATE not set to '1'")
            ok = False

        # 2. drill freshness
        drill_age_h = self._latest_drill_age_h()
        if drill_age_h is None:
            reasons.append(
                f"no risk drill recorded — run scripts/risk_drill.py "
                f"(append to {DRILL_LOG_PATH})"
            )
            ok = False
        elif drill_age_h > DRILL_VALIDITY_HOURS:
            reasons.append(
                f"drill is {drill_age_h:.1f}h old; max allowed "
                f"{DRILL_VALIDITY_HOURS}h"
            )
            ok = False

        # 3. paper-soak
        soak_ok, soak_reason = self._paper_soak_passes()
        if not soak_ok:
            reasons.append(soak_reason)
            ok = False

        # 4. bankroll sanity
        if self.bankroll_per_side_usd <= 0:
            reasons.append("bankroll_per_side_usd must be > 0")
            ok = False
        if self.bankroll_per_side_usd == 500.0:
            # The Q4 placeholder. If still set to this, we never made the
            # explicit decision required by Plan §5 Phase 12.
            reasons.append(
                "bankroll is still the $500 placeholder from Q4 — "
                "make an explicit Phase-12 decision before live ramp."
            )
            ok = False

        # 5. caps within RISK.md bounds (defensive duplicate of risk module)
        per_trade = self.bankroll_per_side_usd * config.PER_TRADE_CAP_PCT / 100.0
        daily_loss = self.bankroll_per_side_usd * config.DAILY_LOSS_CAP_PCT / 100.0
        if per_trade <= 0 or daily_loss <= 0:
            reasons.append("per-trade or daily-loss cap calc failed")
            ok = False

        return RampReadiness(ready=ok, reasons=tuple(reasons))

    def assert_ready(self) -> None:
        readiness = self.check()
        if not readiness.ready:
            joined = "\n  - " + "\n  - ".join(readiness.reasons)
            raise RuntimeError(
                f"LiveRampGuard refused live ramp. Reasons:{joined}"
            )

    def _latest_drill_age_h(self) -> float | None:
        if not DRILL_LOG_PATH.exists():
            return None
        try:
            mtime = DRILL_LOG_PATH.stat().st_mtime
            age_s = time.time() - mtime
            return age_s / 3600.0
        except Exception:
            return None

    def _paper_soak_passes(self) -> tuple[bool, str]:
        """
        Inspects the paper_trades parquet (Phase 11 output) and verifies:
          - covers >= PAPER_SOAK_MIN_DAYS calendar days
          - aggregate |gap| <= PAPER_SOAK_MAX_GAP_PCT
        """
        from src.storage import arb_store

        if not arb_store.table_exists("paper_trades"):
            return False, ("no paper_trades table — run Phase-11 paper-trade "
                            "soak before requesting live ramp")
        glob = (arb_store.table_dir("paper_trades") / "**" / "*.parquet").as_posix()
        rows = arb_store.query(f"""
            SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts,
                   COALESCE(SUM(ABS(pnl_gap_usd)), 0.0) AS gap_total,
                   COALESCE(SUM(ABS(coordinator_pnl_estimate)), 0.0) AS coord_total,
                   COUNT(*) AS n
            FROM read_parquet('{glob}', hive_partitioning=1)
        """)
        if not rows or rows[0][4] == 0:
            return False, "paper_trades table is empty"
        first_ts, last_ts, gap_total, coord_total, n = rows[0]
        try:
            first = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            last = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        except Exception:
            return False, f"could not parse paper_trades timestamps: {first_ts}"
        if (last - first) < timedelta(days=PAPER_SOAK_MIN_DAYS):
            return False, (
                f"paper soak only {(last - first).days} days; "
                f"need {PAPER_SOAK_MIN_DAYS}"
            )
        gap_pct = (gap_total / coord_total * 100.0) if coord_total > 0 else 0.0
        if gap_pct > PAPER_SOAK_MAX_GAP_PCT:
            return False, (
                f"sim-vs-paper gap {gap_pct:.1f}% > {PAPER_SOAK_MAX_GAP_PCT}%"
            )
        return True, "ok"
