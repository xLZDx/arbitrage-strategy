"""
SHADOW-mode end-to-end demo for the Phase 5 coordinator.

Reads recent GO opportunities from data/arb/db/opportunities/ (capture
some via `restart_all.ps1` first, or use --synthetic to inject fake ones).
Runs each through the coordinator and persists trade records.

Run:
  ./venv/Scripts/python.exe scripts/run_shadow_executor.py
  ./venv/Scripts/python.exe scripts/run_shadow_executor.py --synthetic --n 10
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.exec.coordinator import ArbCoordinator, persist_trade
from src.sim.inventory import Inventory
from src.storage import arb_store
from src.utils import config


def _load_recent_go(limit: int):
    if not arb_store.table_exists("opportunities"):
        return []
    glob = (arb_store.table_dir("opportunities") / "**" / "*.parquet").as_posix()
    sql = f"""
        SELECT ts, pair, decision, direction, notional_usd,
               expected_net_bps, theoretical_pnl_usd,
               bybit_mid, dex_mid, weighted_obi
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE decision = 'GO'
        ORDER BY ts DESC
        LIMIT {int(limit)}
    """
    rows = arb_store.query(sql)
    keys = ("ts", "pair", "decision", "direction", "notional_usd",
            "expected_net_bps", "theoretical_pnl_usd",
            "bybit_mid", "dex_mid", "weighted_obi")
    return [dict(zip(keys, r)) for r in rows]


def _synthetic(n: int):
    out = []
    for i in range(n):
        out.append({
            "ts": f"2026-05-10T13:00:{i:02d}+00:00",
            "pair": ["BTCUSDT", "ETHUSDT"][i % 2],
            "decision": "GO",
            "direction": "bybit_high" if i % 2 == 0 else "dex_high",
            "notional_usd": 50.0,
            "expected_net_bps": 18.0,
            "theoretical_pnl_usd": 0.09,
            "bybit_mid": 80000.0 if i % 2 == 0 else 3000.0,
            "dex_mid": 79850.0 if i % 2 == 0 else 2998.0,
            "weighted_obi": 0.1,
        })
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="SHADOW coordinator demo")
    p.add_argument("--synthetic", action="store_true",
                   help="Use synthetic GO opps instead of reading from storage")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--bankroll", type=float, default=2000.0)
    args = p.parse_args()

    if config.EXECUTION_MODE != config.MODE_SHADOW:
        print(f"WARN: EXECUTION_MODE={config.EXECUTION_MODE}; this script is "
              "designed for SHADOW. Set ARB_MODE=SHADOW to be safe.")

    opps = _synthetic(args.n) if args.synthetic else _load_recent_go(args.n)
    if not opps:
        print("No GO opportunities. Run with --synthetic to generate them.")
        return 1

    coord = ArbCoordinator(inventory=Inventory.with_balanced_seed(args.bankroll))
    counts: dict[str, int] = {}
    for op in opps:
        rec = coord.attempt(op)
        counts[rec.outcome] = counts.get(rec.outcome, 0) + 1
        persist_trade(rec)

    print(f"=== SHADOW EXECUTOR ({len(opps)} opportunities, ${args.bankroll}/side) ===")
    for k, v in sorted(counts.items()):
        print(f"  {k:30s}  {v}")
    print(f"  inventory realized PnL:  ${coord.inventory.realized_pnl_usd:+.4f}")
    print(f"  trades written to data/arb/db/trades/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
