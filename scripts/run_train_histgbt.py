"""
Train HistGBT spread-survival classifier on captured sim_trades.

Reads:
  data/arb/db/opportunities/  (features)
  data/arb/db/sim_trades/     (labels via realized_pnl_usd > threshold)

Run:
  ./venv/Scripts/python.exe scripts/run_train_histgbt.py
  ./venv/Scripts/python.exe scripts/run_train_histgbt.py --threshold 0.55
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from src.ml.feature_pipeline import label_from_sim_trade, stack_features
from src.ml.hist_gbt import save_artifact, train_histgbt
from src.storage import arb_store


def _load_training_pairs() -> tuple[list[dict], list[int], list[str]]:
    """
    Join opportunities with sim_trades by (ts, pair) — labels come from
    realized PnL of replayed trades.
    """
    if not arb_store.table_exists("opportunities") or \
       not arb_store.table_exists("sim_trades"):
        return [], [], []

    opp_glob = (arb_store.table_dir("opportunities") / "**" / "*.parquet").as_posix()
    sim_glob = (arb_store.table_dir("sim_trades") / "**" / "*.parquet").as_posix()
    sql = f"""
        SELECT o.ts, o.pair, o.spread_bps, o.gross_bps, o.direction,
               o.weighted_obi, o.obi_delta, o.cancellation_rate,
               o.gas_gwei, o.gas_cost_bps, o.slippage_haircut_bps,
               o.expected_net_bps, o.notional_usd,
               s.realized_pnl_usd, s.inventory_ok, s.fill_pct
        FROM read_parquet('{opp_glob}', hive_partitioning=1) o
        JOIN read_parquet('{sim_glob}', hive_partitioning=1) s
          ON o.ts = s.ts AND o.pair = s.pair
        WHERE o.decision = 'GO'
        ORDER BY o.ts ASC
    """
    rows = arb_store.query(sql)
    keys = ("ts", "pair", "spread_bps", "gross_bps", "direction",
            "weighted_obi", "obi_delta", "cancellation_rate",
            "gas_gwei", "gas_cost_bps", "slippage_haircut_bps",
            "expected_net_bps", "notional_usd",
            "realized_pnl_usd", "inventory_ok", "fill_pct")
    opps, labels, timestamps = [], [], []
    for r in rows:
        d = dict(zip(keys, r))
        opps.append(d)
        labels.append(label_from_sim_trade(d))
        timestamps.append(d["ts"])
    return opps, labels, timestamps


def main() -> int:
    p = argparse.ArgumentParser(description="Train HistGBT")
    p.add_argument("--threshold", type=float, default=0.55,
                   help="Veto threshold (model.predict_proba < this → REJECT)")
    p.add_argument("--n-estimators", type=int, default=200)
    p.add_argument("--learning-rate", type=float, default=0.05)
    args = p.parse_args()

    opps, labels, ts = _load_training_pairs()
    if not opps:
        print("No training pairs found. Run ingestion + replay first.")
        print("  1. ./restart_all.ps1                    # capture opportunities")
        print("  2. python scripts/run_replay.py --write # produce sim_trades")
        return 1
    if len(opps) < 20:
        print(f"Only {len(opps)} training samples — need >= 20.")
        print("Soak longer (more opportunities) or generate synthetic data.")
        return 1

    X = stack_features(opps)
    y = np.array(labels, dtype=np.int32)
    pos = int(y.sum())
    neg = len(y) - pos
    print(f"Training on {len(opps)} samples ({pos} positive, {neg} negative).")

    try:
        artifact = train_histgbt(X, y, timestamps=ts,
                                  veto_threshold=args.threshold,
                                  n_estimators=args.n_estimators,
                                  learning_rate=args.learning_rate)
    except ValueError as e:
        print(f"Training failed: {e}")
        return 1

    path = save_artifact(artifact)
    print(f"Saved {path}")
    print(f"  holdout AUC:   {artifact.holdout_auc:.4f}")
    print(f"  n_train:       {artifact.n_train}")
    print(f"  n_holdout:     {artifact.n_holdout}")
    print(f"  pos rate:      {artifact.pos_rate_train:.3f}")
    print(f"  veto threshold: {artifact.veto_threshold}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
