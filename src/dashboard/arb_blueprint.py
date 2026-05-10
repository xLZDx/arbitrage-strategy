"""
Flask blueprint for /api/arb/* endpoints.

Designed to be mounted into either:
  (a) the sister trading-bot Flask app at port 5000 (Q5 primary), or
  (b) the standalone app_arb.py at port 5001 (Q5 fallback / Phase 1 default).

The sister-project mount is deferred until after Phase 1 ships. For now
the standalone runner exercises the blueprint independently so the live bot
isn't disturbed.

Endpoints (Phase 1):
  GET /api/arb/health             — liveness + ingestion process state
  GET /api/arb/pairs              — list of pilot pairs
  GET /api/arb/spread             — latest spread vs Bybit per pair
  GET /api/arb/obi/<pair>?n=N     — last N OBI snapshots for a pair (default 60)
  GET /api/arb/gas                — latest gas reading on Base

Reads are read-only against data/arb/db/*.parquet via arb_store with
_LOCK held — does not interfere with the ingestion writer.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from src.storage import arb_store
from src.utils import config

log = logging.getLogger(__name__)

bp = Blueprint("arb", __name__, url_prefix=config.DASHBOARD_API_PREFIX)


def _is_ingestion_running() -> tuple[bool, int | None]:
    """Real process-exists check (Windows + POSIX). Returns (alive, pid_or_None)."""
    pid_file = config.PIDS_DIR / "ingestion.pid"
    if not pid_file.exists():
        return False, None
    try:
        pid = int(pid_file.read_text().strip())
    except Exception:
        return False, None
    if os.name == "nt":
        # Windows: OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION (0x1000).
        # Fall back to tasklist if ctypes unavailable.
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True, pid
            return False, pid
        except Exception:
            return False, pid
    try:
        os.kill(pid, 0)
        return True, pid
    except (OSError, ProcessLookupError):
        return False, pid


@bp.route("/health", methods=["GET"])
def health():
    running, pid = _is_ingestion_running()
    return jsonify({
        "status": "ok",
        "mode": config.EXECUTION_MODE,
        "ingestion_running": running,
        "ingestion_pid": pid,
        "halt_active": config.halt_active(),
        "pilot_pairs": list(config.PILOT_PAIRS),
    })


@bp.route("/pairs", methods=["GET"])
def pairs():
    return jsonify({"pairs": list(config.PILOT_PAIRS)})


@bp.route("/spread", methods=["GET"])
def spread():
    """
    Latest spread per pair = Bybit_mid vs DEX_mid in basis points.
    Phase 1 read-only view; Phase 2 opportunity detector adds richer signals.
    """
    out = []
    if not arb_store.table_exists("obi_snapshots"):
        return jsonify({"spreads": [], "note": "no data yet"})
    obi_glob = (arb_store.table_dir("obi_snapshots") / "**" / "*.parquet").as_posix()
    sql = f"""
        WITH latest_obi AS (
            SELECT pair, MAX(ts) AS ts
            FROM read_parquet('{obi_glob}', hive_partitioning=1)
            GROUP BY pair
        ),
        bybit AS (
            SELECT o.pair, o.best_bid, o.best_ask, (o.best_bid + o.best_ask) / 2.0 AS mid, o.ts
            FROM read_parquet('{obi_glob}', hive_partitioning=1) o
            JOIN latest_obi l ON o.pair = l.pair AND o.ts = l.ts
        )
        SELECT pair, best_bid, best_ask, mid, ts FROM bybit ORDER BY pair
    """
    bybit_rows = arb_store.query(sql)

    dex_rows = []
    if arb_store.table_exists("dex_quotes"):
        dex_glob = (arb_store.table_dir("dex_quotes") / "**" / "*.parquet").as_posix()
        dex_sql = f"""
            WITH latest AS (
                SELECT pair, MAX(ts) AS ts
                FROM read_parquet('{dex_glob}', hive_partitioning=1)
                GROUP BY pair
            )
            SELECT d.pair, d.mid_price, d.ts
            FROM read_parquet('{dex_glob}', hive_partitioning=1) d
            JOIN latest l ON d.pair = l.pair AND d.ts = l.ts
        """
        dex_rows = arb_store.query(dex_sql)
    dex_by_pair = {r[0]: {"mid": r[1], "ts": r[2]} for r in dex_rows}

    for pair, bb, ba, bmid, bts in bybit_rows:
        d = dex_by_pair.get(pair)
        spread_bps = None
        dex_mid = None
        if d and bmid > 0:
            dex_mid = d["mid"]
            spread_bps = round((bmid - dex_mid) / bmid * 10_000, 2)
        out.append({
            "pair": pair,
            "bybit_mid": round(bmid, 6),
            "bybit_bid": bb,
            "bybit_ask": ba,
            "dex_mid": round(dex_mid, 6) if dex_mid is not None else None,
            "spread_bps": spread_bps,  # positive: Bybit above DEX
            "bybit_ts": bts,
            "dex_ts": d["ts"] if d else None,
        })
    return jsonify({"spreads": out})


@bp.route("/obi/<pair>", methods=["GET"])
def obi_history(pair: str):
    pair = pair.upper()
    if pair not in config.PILOT_PAIRS:
        return jsonify({"error": f"unknown pair: {pair}"}), 404
    if not arb_store.table_exists("obi_snapshots"):
        return jsonify({"pair": pair, "snapshots": []})
    n = max(1, min(int(request.args.get("n", 60)), 1000))
    glob = (arb_store.table_dir("obi_snapshots") / "**" / "*.parquet").as_posix()
    sql = f"""
        SELECT ts, weighted_obi, obi_delta, cancellation_rate,
               best_bid, best_ask
        FROM read_parquet('{glob}', hive_partitioning=1)
        WHERE pair = '{pair}'
        ORDER BY ts DESC
        LIMIT {n}
    """
    rows = arb_store.query(sql)
    snapshots = [
        {"ts": r[0], "weighted_obi": r[1], "obi_delta": r[2],
         "cancellation_rate": r[3], "best_bid": r[4], "best_ask": r[5]}
        for r in reversed(rows)  # chronological for sparkline
    ]
    return jsonify({"pair": pair, "snapshots": snapshots})


@bp.route("/opportunities", methods=["GET"])
def opportunities():
    """
    Phase 2 opportunity feed.

    Query params:
      n        — max rows (default 50, max 500)
      decision — optional "GO" or "SKIP" filter
      pair     — optional pair filter
    """
    if not arb_store.table_exists("opportunities"):
        return jsonify({"opportunities": [], "note": "no data yet"})
    n = max(1, min(int(request.args.get("n", 50)), 500))
    decision = request.args.get("decision")
    pair = request.args.get("pair")
    where = []
    if decision in ("GO", "SKIP"):
        where.append(f"decision = '{decision}'")
    if pair and pair.upper() in config.PILOT_PAIRS:
        where.append(f"pair = '{pair.upper()}'")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    glob = (arb_store.table_dir("opportunities") / "**" / "*.parquet").as_posix()
    sql = f"""
        SELECT ts, pair, decision, reason, direction,
               spread_bps, gross_bps, expected_net_bps,
               theoretical_pnl_usd, gas_cost_bps, slippage_haircut_bps,
               weighted_obi, cancellation_rate
        FROM read_parquet('{glob}', hive_partitioning=1)
        {where_sql}
        ORDER BY ts DESC
        LIMIT {n}
    """
    rows = arb_store.query(sql)
    keys = ("ts", "pair", "decision", "reason", "direction",
            "spread_bps", "gross_bps", "expected_net_bps",
            "theoretical_pnl_usd", "gas_cost_bps", "slippage_haircut_bps",
            "weighted_obi", "cancellation_rate")
    return jsonify({"opportunities": [dict(zip(keys, r)) for r in rows]})


@bp.route("/pnl_simulated", methods=["GET"])
def pnl_simulated():
    """
    Cumulative would-have-been PnL across all GO opportunities.

    Phase 2 = sum(theoretical_pnl_usd) where decision='GO'.
    Phase 3 simulator will replace this with realistic fills + slippage.
    """
    if not arb_store.table_exists("opportunities"):
        return jsonify({"cumulative": 0.0, "go_count": 0, "skip_count": 0,
                        "by_pair": []})
    glob = (arb_store.table_dir("opportunities") / "**" / "*.parquet").as_posix()
    summary_sql = f"""
        SELECT
            COUNT(*) FILTER (WHERE decision = 'GO') as go_count,
            COUNT(*) FILTER (WHERE decision = 'SKIP') as skip_count,
            COALESCE(SUM(theoretical_pnl_usd) FILTER (WHERE decision = 'GO'), 0.0)
              as cumulative
        FROM read_parquet('{glob}', hive_partitioning=1)
    """
    pair_sql = f"""
        SELECT pair,
               COUNT(*) FILTER (WHERE decision = 'GO') as go_count,
               COUNT(*) FILTER (WHERE decision = 'SKIP') as skip_count,
               COALESCE(SUM(theoretical_pnl_usd) FILTER (WHERE decision = 'GO'), 0.0)
                 as cumulative,
               COALESCE(AVG(expected_net_bps) FILTER (WHERE decision = 'GO'), 0.0)
                 as avg_go_net_bps
        FROM read_parquet('{glob}', hive_partitioning=1)
        GROUP BY pair
        ORDER BY pair
    """
    summary = arb_store.query(summary_sql)[0]
    pair_rows = arb_store.query(pair_sql)
    return jsonify({
        "cumulative": round(float(summary[2]), 4),
        "go_count": int(summary[0]),
        "skip_count": int(summary[1]),
        "by_pair": [
            {"pair": r[0], "go_count": int(r[1]), "skip_count": int(r[2]),
             "cumulative_usd": round(float(r[3]), 4),
             "avg_go_net_bps": round(float(r[4]), 4)}
            for r in pair_rows
        ],
    })


@bp.route("/model_status", methods=["GET"])
def model_status():
    """Phase 6 — HistGBT artifact metadata."""
    from src.ml.hist_gbt import load_artifact
    art = load_artifact()
    if art is None:
        return jsonify({"loaded": False,
                        "note": "no model found; train via scripts/run_train_histgbt.py"})
    return jsonify({
        "loaded": True,
        "trained_at": art.trained_at,
        "holdout_auc": art.holdout_auc,
        "n_train": art.n_train,
        "n_holdout": art.n_holdout,
        "pos_rate_train": art.pos_rate_train,
        "veto_threshold": art.veto_threshold,
        "feature_columns": list(art.feature_columns),
    })


@bp.route("/risk", methods=["GET"])
def risk_state():
    """Phase 4 — current risk state + pre-flight gate result."""
    from src.ops import health as ops_health
    from src.risk import limits as risk_limits

    # Build a lightweight RiskState from sim_trades (today's PnL).
    state = risk_limits.RiskState()
    if arb_store.table_exists("sim_trades"):
        glob = (arb_store.table_dir("sim_trades") / "**" / "*.parquet").as_posix()
        sql = f"""
            SELECT COALESCE(SUM(realized_pnl_usd), 0.0) AS pnl
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE ts >= strftime('%Y-%m-%d', current_date) || 'T00:00:00'
        """
        rows = arb_store.query(sql)
        if rows:
            state.today_realized_pnl_usd = float(rows[0][0])

    gate = risk_limits.preflight(opportunity=None, state=state)
    snap = ops_health.services_snapshot()
    return jsonify({
        "halt_active": risk_limits.halt_active(),
        "halt_reason": risk_limits.halt_reason(),
        "mode": config.EXECUTION_MODE,
        "today_realized_pnl_usd": round(state.today_realized_pnl_usd, 4),
        "daily_loss_cap_usd": state.daily_loss_cap_usd,
        "drawdown_trigger_usd": state.drawdown_trigger_usd,
        "per_trade_cap_usd": state.per_trade_cap_usd,
        "preflight_ok": gate.is_ok(),
        "preflight_decision": gate.decision,
        "preflight_reason": gate.reason,
        "services": snap["services"],
        "table_freshness_s": snap["table_freshness_s"],
    })


@bp.route("/sim_trades", methods=["GET"])
def sim_trades():
    """Phase 3 simulator output. Latest N replayed trades."""
    if not arb_store.table_exists("sim_trades"):
        return jsonify({"trades": [], "note": "no replay output yet"})
    n = max(1, min(int(request.args.get("n", 50)), 500))
    pair = request.args.get("pair")
    where = []
    if pair and pair.upper() in config.PILOT_PAIRS:
        where.append(f"pair = '{pair.upper()}'")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    glob = (arb_store.table_dir("sim_trades") / "**" / "*.parquet").as_posix()
    sql = f"""
        SELECT ts, pair, direction, notional_usd, spread_bps,
               expected_net_bps, realized_slippage_bps, realized_pnl_usd,
               realized_net_bps, fill_pct, inventory_ok, inventory_reason,
               portfolio_usd_after
        FROM read_parquet('{glob}', hive_partitioning=1)
        {where_sql}
        ORDER BY ts DESC
        LIMIT {n}
    """
    rows = arb_store.query(sql)
    keys = ("ts", "pair", "direction", "notional_usd", "spread_bps",
            "expected_net_bps", "realized_slippage_bps", "realized_pnl_usd",
            "realized_net_bps", "fill_pct", "inventory_ok", "inventory_reason",
            "portfolio_usd_after")
    return jsonify({"trades": [dict(zip(keys, r)) for r in rows]})


@bp.route("/sim_summary", methods=["GET"])
def sim_summary():
    """
    Phase 3 backtest summary: cumulative PnL, hit rate, sim-vs-theoretical
    gap. The Sharpe number gates the project per CLAUDE.md kill criterion.
    """
    if not arb_store.table_exists("sim_trades"):
        return jsonify({
            "n_trades": 0, "n_filled": 0, "n_inventory_rejected": 0,
            "cumulative_pnl_usd": 0.0, "hit_rate": 0.0,
            "avg_realized_net_bps": 0.0,
            "avg_theoretical_net_bps": 0.0,
            "realized_vs_theoretical_gap_bps": 0.0,
            "by_pair": [],
        })
    glob = (arb_store.table_dir("sim_trades") / "**" / "*.parquet").as_posix()
    summary_sql = f"""
        SELECT
            COUNT(*) AS n,
            COUNT(*) FILTER (WHERE inventory_ok AND fill_pct > 0) AS n_filled,
            COUNT(*) FILTER (WHERE NOT inventory_ok) AS n_rejected,
            COALESCE(SUM(realized_pnl_usd), 0.0) AS cum_pnl,
            COALESCE(AVG(realized_net_bps) FILTER (WHERE inventory_ok AND fill_pct > 0), 0.0) AS avg_real,
            COALESCE(AVG(expected_net_bps) FILTER (WHERE inventory_ok AND fill_pct > 0), 0.0) AS avg_exp,
            COALESCE(COUNT(*) FILTER (WHERE inventory_ok AND fill_pct > 0 AND realized_pnl_usd > 0)
                * 1.0 / NULLIF(COUNT(*) FILTER (WHERE inventory_ok AND fill_pct > 0), 0), 0.0)
                AS hit_rate
        FROM read_parquet('{glob}', hive_partitioning=1)
    """
    by_pair_sql = f"""
        SELECT pair,
               COUNT(*) FILTER (WHERE inventory_ok AND fill_pct > 0) AS n_filled,
               COALESCE(SUM(realized_pnl_usd), 0.0) AS cum_pnl,
               COALESCE(AVG(realized_net_bps) FILTER (WHERE inventory_ok AND fill_pct > 0), 0.0) AS avg_real
        FROM read_parquet('{glob}', hive_partitioning=1)
        GROUP BY pair ORDER BY pair
    """
    s = arb_store.query(summary_sql)[0]
    pair_rows = arb_store.query(by_pair_sql)
    return jsonify({
        "n_trades": int(s[0]),
        "n_filled": int(s[1]),
        "n_inventory_rejected": int(s[2]),
        "cumulative_pnl_usd": round(float(s[3]), 4),
        "hit_rate": round(float(s[6]), 4),
        "avg_realized_net_bps": round(float(s[4]), 4),
        "avg_theoretical_net_bps": round(float(s[5]), 4),
        "realized_vs_theoretical_gap_bps": round(float(s[4]) - float(s[5]), 4),
        "by_pair": [
            {"pair": r[0], "n_filled": int(r[1]),
             "cumulative_usd": round(float(r[2]), 4),
             "avg_realized_net_bps": round(float(r[3]), 4)}
            for r in pair_rows
        ],
    })


@bp.route("/gas", methods=["GET"])
def gas_latest():
    if not arb_store.table_exists("gas_history"):
        return jsonify({"gas": None, "note": "no data yet"})
    glob = (arb_store.table_dir("gas_history") / "**" / "*.parquet").as_posix()
    sql = f"""
        SELECT ts, block_number, base_fee_gwei, priority_fee_gwei, total_gas_price_gwei
        FROM read_parquet('{glob}', hive_partitioning=1)
        ORDER BY ts DESC LIMIT 1
    """
    rows = arb_store.query(sql)
    if not rows:
        return jsonify({"gas": None})
    r = rows[0]
    return jsonify({
        "gas": {
            "ts": r[0], "block_number": r[1],
            "base_fee_gwei": r[2], "priority_fee_gwei": r[3],
            "total_gas_price_gwei": r[4],
        }
    })
