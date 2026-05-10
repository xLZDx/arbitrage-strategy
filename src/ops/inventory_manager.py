"""
Inventory monitor — alert (no auto-rebalance in Phase 4).

Periodically reads sim_trades to compute current Inventory snapshot, then
raises a warning when imbalance exceeds the configured threshold. Phase 5+
will hook this into the live executor and add an actual rebalance pathway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from src.sim.inventory import Inventory
from src.utils import config, safe_json

log = logging.getLogger(__name__)

ALERT_THRESHOLD = 0.20         # 20% imbalance fires a warning
HALT_THRESHOLD = 0.25          # 25% triggers HALT (per RISK.md / risk.limits)
ALERT_COOLDOWN_S = 60.0        # don't spam alerts more than once a minute


@dataclass
class InventoryAlert:
    ts: str
    imbalance: float
    bybit_total_usd: float
    dex_total_usd: float
    severity: str   # "WARN" or "HALT"


class InventoryManager:
    """
    Stateful watcher. Phase 4: alert log only. Phase 5: trigger rebalance ops.
    """
    def __init__(self) -> None:
        self._last_alert_ts: float = 0.0
        self._alert_log_path = config.LOG_DIR / "inventory_alerts.jsonl"

    def check(self, inv: Inventory) -> InventoryAlert | None:
        """
        Returns an InventoryAlert if imbalance exceeds threshold AND cooldown
        has elapsed since the last alert. None otherwise.
        """
        bybit_total = sum(inv.bybit.values())
        dex_total = sum(inv.dex.values())
        imbalance = inv.imbalance_ratio()
        now = datetime.now(timezone.utc).timestamp()
        if imbalance < ALERT_THRESHOLD:
            return None
        if (now - self._last_alert_ts) < ALERT_COOLDOWN_S:
            return None

        severity = "HALT" if imbalance > HALT_THRESHOLD else "WARN"
        alert = InventoryAlert(
            ts=datetime.now(timezone.utc).isoformat(),
            imbalance=round(imbalance, 4),
            bybit_total_usd=round(bybit_total, 4),
            dex_total_usd=round(dex_total, 4),
            severity=severity,
        )
        self._last_alert_ts = now
        try:
            safe_json.append_jsonl(self._alert_log_path, {
                "ts": alert.ts, "imbalance": alert.imbalance,
                "bybit_total_usd": alert.bybit_total_usd,
                "dex_total_usd": alert.dex_total_usd,
                "severity": alert.severity,
            })
        except Exception as e:
            log.warning("failed to write inventory alert log: %s", e)
        log.warning("[%s] inventory imbalance %.3f: bybit=$%.2f vs dex=$%.2f",
                    alert.severity, alert.imbalance,
                    alert.bybit_total_usd, alert.dex_total_usd)
        return alert
