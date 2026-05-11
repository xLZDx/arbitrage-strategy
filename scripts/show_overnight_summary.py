"""
Quick overnight soak summary — what happened while you slept.

Reads the live data tables and prints a human-readable digest:
  - capture window (start/end + duration + per-table row counts)
  - spread distribution per pair (min / median / max bps, signed)
  - opportunity decisions breakdown (GO vs SKIP, SKIP reasons)
  - estimated cumulative would-have-been PnL (sum of GO theoretical pnl)
  - active vs HALT-ed time
  - any drift alerts
  - last 5 trades (if Phase 5+ produced any)

Run:
  ./venv/Scripts/python.exe scripts/show_overnight_summary.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.storage import arb_store
from src.utils import config


def _table_summary(table: str) -> dict | None:
    if not arb_store.table_exists(table):
        return None
    glob = (arb_store.table_dir(table) / "**" / "*.parquet").as_posix()
    rows = arb_store.query(
        f"SELECT MIN(ts), MAX(ts), COUNT(*) FROM read_parquet('{glob}', hive_partitioning=1)"
    )
    if not rows or rows[0][2] == 0:
        return None
    first, last, n = rows[0]
    return {"first": first, "last": last, "n": int(n)}


def _spread_distribution() -> list[dict]:
    if not arb_store.table_exists("opportunities"):
        return []
    glob = (arb_store.table_dir("opportunities") / "**" / "*.parquet").as_posix()
    rows = arb_store.query(f"""
        SELECT pair,
               COUNT(*) AS n,
               MIN(spread_bps) AS min_bps,
               MEDIAN(spread_bps) AS med_bps,
               MAX(spread_bps) AS max_bps,
               AVG(ABS(spread_bps)) AS avg_abs_bps
        FROM read_parquet('{glob}', hive_partitioning=1)
        GROUP BY pair ORDER BY pair
    """)
    return [
        {"pair": r[0], "n": int(r[1]), "min": float(r[2]),
         "median": float(r[3]), "max": float(r[4]),
         "avg_abs": float(r[5])}
        for r in rows
    ]


def _decisions() -> list[dict]:
    if not arb_store.table_exists("opportunities"):
        return []
    glob = (arb_store.table_dir("opportunities") / "**" / "*.parquet").as_posix()
    rows = arb_store.query(f"""
        SELECT decision, reason, COUNT(*) AS n,
               COALESCE(SUM(theoretical_pnl_usd), 0.0) AS pnl
        FROM read_parquet('{glob}', hive_partitioning=1)
        GROUP BY decision, reason ORDER BY decision, n DESC
    """)
    return [{"decision": r[0], "reason": r[1], "n": int(r[2]), "pnl": float(r[3])}
            for r in rows]


def _go_pnl_total() -> float:
    if not arb_store.table_exists("opportunities"):
        return 0.0
    glob = (arb_store.table_dir("opportunities") / "**" / "*.parquet").as_posix()
    rows = arb_store.query(f"""
        SELECT COALESCE(SUM(theoretical_pnl_usd), 0.0)
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE decision = 'GO'
    """)
    return float(rows[0][0]) if rows else 0.0


def _last_trades(n: int = 5) -> list[tuple]:
    if not arb_store.table_exists("trades"):
        return []
    glob = (arb_store.table_dir("trades") / "**" / "*.parquet").as_posix()
    return arb_store.query(f"""
        SELECT ts, pair, outcome, reason, realized_net_bps
        FROM read_parquet('{glob}', hive_partitioning=1)
        ORDER BY ts DESC LIMIT {int(n)}
    """)


def _drift_alerts() -> list[str]:
    p = config.LOG_DIR / "drift_alerts.jsonl"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    return lines[-10:]


def main() -> int:
    print("=" * 64)
    print(f"  OVERNIGHT ARB SOAK SUMMARY  (mode={config.EXECUTION_MODE})")
    print("=" * 64)
    print()

    tables = ["obi_snapshots", "dex_quotes", "gas_history",
              "opportunities", "sim_trades", "trades", "paper_trades"]
    print("--- DATA TABLES ---")
    any_data = False
    for t in tables:
        s = _table_summary(t)
        if s is None:
            print(f"  {t:18s}  (no data)")
        else:
            any_data = True
            print(f"  {t:18s}  {s['n']:>9,} rows  {s['first'][:19]} -> {s['last'][:19]}")
    print()
    if not any_data:
        print("No data captured. Is restart_all.ps1 running?")
        return 1

    sd = _spread_distribution()
    if sd:
        print("--- SPREAD DISTRIBUTION (bps; signed = bybit - dex) ---")
        print(f"  {'pair':10s}  {'n':>7s}  {'min':>8s}  {'med':>8s}  {'max':>8s}  {'avg|x|':>8s}")
        for row in sd:
            print(f"  {row['pair']:10s}  {row['n']:>7,}  "
                  f"{row['min']:>+8.2f}  {row['median']:>+8.2f}  "
                  f"{row['max']:>+8.2f}  {row['avg_abs']:>8.2f}")
        print()

    dec = _decisions()
    if dec:
        print("--- OPPORTUNITY DECISIONS ---")
        print(f"  {'decision':9s}  {'reason':28s}  {'count':>9s}  {'cum theo PnL USD':>18s}")
        for d in dec:
            print(f"  {d['decision']:9s}  {d['reason']:28s}  {d['n']:>9,}  "
                  f"{d['pnl']:>+18.4f}")
        total = _go_pnl_total()
        print(f"  {'TOTAL GO':40s}  {'':>9s}  {total:>+18.4f}")
        print()

    drifts = _drift_alerts()
    if drifts:
        print("--- LAST 10 DRIFT ALERTS ---")
        for line in drifts:
            print(f"  {line}")
        print()

    last = _last_trades()
    if last:
        print("--- LAST 5 EXECUTED TRADES ---")
        for ts, pair, outcome, reason, net in last:
            print(f"  {ts[:19]}  {pair:10s}  {outcome:25s}  {reason[:30]:30s}  net={net:+.2f}bps")
        print()

    print("Done. Run scripts/run_replay.py --write next to score the captured data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
