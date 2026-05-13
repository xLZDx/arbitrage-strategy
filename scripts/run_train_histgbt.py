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


def _load_training_pairs(
    drop_inv_rejected: bool = True,
) -> tuple[list[dict], list[int], list[str], dict]:
    """
    Join opportunities with sim_trades by (ts, pair) — labels come from
    realized PnL of replayed trades.

    HIGH-4 fix (2026-05-11, ml-engineer re-review): inventory-rejected or
    unfilled rows are DROPPED from the training set when drop_inv_rejected=True
    (default). Including them as label=0 confounds the model: the trade never
    executed, so its outcome is undefined. Treating "didn't fire" as "would have
    lost" teaches a spurious decision rule (e.g. "predict 0 when inventory is
    low"). Drop them; the model should only learn from trades that actually
    ran. Set drop_inv_rejected=False to reproduce pre-fix behavior for audit.

    Returns: (opps, labels, timestamps, stats) where stats reports dropped row
    counts for observability.
    """
    if not arb_store.table_exists("opportunities") or \
       not arb_store.table_exists("sim_trades"):
        return [], [], [], {"total": 0, "dropped_inv_rejected": 0, "dropped_unfilled": 0, "kept": 0}

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
    dropped_inv = 0
    dropped_unfilled = 0
    for r in rows:
        d = dict(zip(keys, r))
        inv_ok = bool(d.get("inventory_ok", False))
        fill_pct = float(d.get("fill_pct", 0.0))
        if drop_inv_rejected:
            if not inv_ok:
                dropped_inv += 1
                continue
            if fill_pct <= 0:
                dropped_unfilled += 1
                continue
        opps.append(d)
        labels.append(label_from_sim_trade(d))
        timestamps.append(d["ts"])
    stats = {
        "total": len(rows),
        "dropped_inv_rejected": dropped_inv,
        "dropped_unfilled": dropped_unfilled,
        "kept": len(opps),
    }
    return opps, labels, timestamps, stats


def main() -> int:
    p = argparse.ArgumentParser(description="Train HistGBT")
    p.add_argument("--threshold", type=float, default=0.55,
                   help="Veto threshold (model.predict_proba < this → REJECT)")
    p.add_argument("--n-estimators", type=int, default=200)
    p.add_argument("--learning-rate", type=float, default=0.05)
    args = p.parse_args()

    opps, labels, ts, stats = _load_training_pairs()
    if stats["total"] > 0:
        print(f"Filter stats: total={stats['total']}, "
              f"dropped_inv_rejected={stats['dropped_inv_rejected']}, "
              f"dropped_unfilled={stats['dropped_unfilled']}, "
              f"kept={stats['kept']}")
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
