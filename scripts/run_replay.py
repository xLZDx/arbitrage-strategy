"""
Replay simulator CLI.

Reads opportunities from data/arb/db/opportunities/, runs the simulator,
and writes sim_trades to data/arb/db/sim_trades/. Prints summary metrics.

Run:
  ./venv/Scripts/python.exe scripts/run_replay.py [--seed N] [--bankroll N]
                                                  [--from YYYY-MM-DD] [--to YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.sim.replay import replay
from src.storage import arb_store
from src.utils import config


OPPORTUNITY_KEYS = (
    "ts", "pair", "bybit_mid", "bybit_bid", "bybit_ask", "dex_mid",
    "spread_bps", "gross_bps", "direction", "weighted_obi", "obi_delta",
    "cancellation_rate", "gas_gwei", "gas_cost_bps", "bybit_fee_bps",
    "dex_fee_bps", "slippage_haircut_bps", "expected_net_bps",
    "notional_usd", "theoretical_pnl_usd", "decision", "reason",
    "eth_price_used",
)


def _load_opportunities(date_from: str | None, date_to: str | None):
    if not arb_store.table_exists("opportunities"):
        return []
    where = []
    if date_from:
        where.append(f"ts >= '{date_from}'")
    if date_to:
        where.append(f"ts <= '{date_to}T23:59:59'")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    glob = (arb_store.table_dir("opportunities") / "**" / "*.parquet").as_posix()
    sql = f"""
        SELECT {", ".join(OPPORTUNITY_KEYS)}
        FROM read_parquet('{glob}', hive_partitioning=1)
        {where_sql}
        ORDER BY ts ASC
    """
    rows = arb_store.query(sql)
    return [dict(zip(OPPORTUNITY_KEYS, r)) for r in rows]


def main() -> int:
    p = argparse.ArgumentParser(description="arbitrage_strategy replay simulator")
    p.add_argument("--seed", type=int, default=0,
                   help="RNG seed for slippage/partial-fill draws (default 0)")
    p.add_argument("--bankroll", type=float, default=config.BANKROLL_PER_SIDE_USD,
                   help=f"USD per side, default ${config.BANKROLL_PER_SIDE_USD}")
    p.add_argument("--from", dest="date_from", default=None,
                   help="ISO date YYYY-MM-DD (inclusive)")
    p.add_argument("--to", dest="date_to", default=None,
                   help="ISO date YYYY-MM-DD (inclusive)")
    p.add_argument("--write", action="store_true",
                   help="Write sim_trades to storage (default: dry-run, summary only)")
    args = p.parse_args()

    opps = _load_opportunities(args.date_from, args.date_to)
    if not opps:
        print("No opportunities found. Run ingestion first to capture data.")
        return 1
    print(f"Loaded {len(opps)} opportunities (GO + SKIP). "
          f"Replaying with bankroll=${args.bankroll}/side, seed={args.seed}.")

    rng = random.Random(args.seed)
    result = replay(opps, initial_usd_per_side=args.bankroll, rng=rng)

    sharpe = result.sharpe()
    print()
    print("=== REPLAY SUMMARY ===")
    print(f"  GO opportunities replayed: {result.n_trades}")
    print(f"  Filled (inventory ok):     {result.n_filled}")
    print(f"  Inventory-rejected:        {result.n_inventory_rejected}")
    print(f"  Hit rate:                  {result.hit_rate * 100:.1f}%")
    print(f"  Cumulative PnL USD:        {result.cumulative_pnl_usd:+.4f}")
    print(f"  Avg realized net bps:      {result.avg_realized_net_bps:+.3f}")
    if sharpe is not None:
        print(f"  Annualized Sharpe (proxy): {sharpe:+.3f}")
        if sharpe < 1.0:
            print(f"  KILL CRITERION: Sharpe < 1.0 over this slice. "
                  f"Per CLAUDE.md, project pauses for venue/pair-set rethink.")
    else:
        print(f"  Sharpe: insufficient trades")
    print(f"  Starting equity: ${result.starting_equity_usd:.2f}")
    if result.equity_curve:
        print(f"  Final equity:    ${result.equity_curve[-1][1]:.4f}")

    if args.write and result.trades:
        rows_by_pair: dict[str, list[dict]] = {}
        for t in result.trades:
            rows_by_pair.setdefault(t.pair, []).append(asdict(t))
        for pair, rows in rows_by_pair.items():
            arb_store.write_records("sim_trades", rows, pair=pair)
        print(f"  Wrote {len(result.trades)} sim_trades rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
