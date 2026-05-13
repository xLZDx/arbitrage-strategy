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
from src.utils import config, safe_json

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


@bp.route("/soak_summary", methods=["GET"])
def soak_summary():
    """Panel data for the soak summary card.

    Aggregates the same info that scripts/show_overnight_summary.py prints
    but as JSON for the dashboard."""
    out = {"tables": {}, "spread_distribution": [],
           "decisions": [], "go_pnl_total": 0.0,
           "drift_alerts": [], "last_trades": []}

    for table in ("obi_snapshots", "dex_quotes", "gas_history",
                   "opportunities", "sim_trades", "trades", "paper_trades"):
        if not arb_store.table_exists(table):
            out["tables"][table] = None
            continue
        glob = (arb_store.table_dir(table) / "**" / "*.parquet").as_posix()
        rows = arb_store.query(
            f"SELECT MIN(ts), MAX(ts), COUNT(*) FROM read_parquet('{glob}', hive_partitioning=1)"
        )
        first, last, n = rows[0] if rows else (None, None, 0)
        out["tables"][table] = (
            {"n": int(n), "first": first, "last": last} if n else None
        )

    if arb_store.table_exists("opportunities"):
        glob = (arb_store.table_dir("opportunities") / "**" / "*.parquet").as_posix()
        rows = arb_store.query(f"""
            SELECT pair, COUNT(*) AS n,
                   MIN(spread_bps) AS min_bps,
                   MEDIAN(spread_bps) AS med_bps,
                   MAX(spread_bps) AS max_bps,
                   AVG(ABS(spread_bps)) AS avg_abs_bps
            FROM read_parquet('{glob}', hive_partitioning=1)
            GROUP BY pair ORDER BY pair
        """)
        out["spread_distribution"] = [
            {"pair": r[0], "n": int(r[1]),
             "min": round(float(r[2]), 4), "median": round(float(r[3]), 4),
             "max": round(float(r[4]), 4), "avg_abs": round(float(r[5]), 4)}
            for r in rows
        ]
        rows = arb_store.query(f"""
            SELECT decision, reason, COUNT(*) AS n,
                   COALESCE(SUM(theoretical_pnl_usd), 0.0) AS pnl
            FROM read_parquet('{glob}', hive_partitioning=1)
            GROUP BY decision, reason ORDER BY decision, n DESC
        """)
        out["decisions"] = [
            {"decision": r[0], "reason": r[1], "n": int(r[2]),
             "pnl": round(float(r[3]), 4)}
            for r in rows
        ]
        rows = arb_store.query(f"""
            SELECT COALESCE(SUM(theoretical_pnl_usd), 0.0)
            FROM read_parquet('{glob}', hive_partitioning=1)
            WHERE decision = 'GO'
        """)
        out["go_pnl_total"] = round(float(rows[0][0]), 4) if rows else 0.0

    drift_path = config.LOG_DIR / "drift_alerts.jsonl"
    if drift_path.exists():
        lines = drift_path.read_text(encoding="utf-8").strip().splitlines()
        out["drift_alerts"] = lines[-10:]

    if arb_store.table_exists("trades"):
        glob = (arb_store.table_dir("trades") / "**" / "*.parquet").as_posix()
        rows = arb_store.query(f"""
            SELECT ts, pair, outcome, reason, realized_net_bps
            FROM read_parquet('{glob}', hive_partitioning=1)
            ORDER BY ts DESC LIMIT 5
        """)
        out["last_trades"] = [
            {"ts": r[0], "pair": r[1], "outcome": r[2],
             "reason": r[3], "realized_net_bps": float(r[4])}
            for r in rows
        ]
    return jsonify(out)


@bp.route("/run_replay", methods=["POST"])
def run_replay_endpoint():
    """Runs the replay simulator server-side and returns aggregate results."""
    import random
    from src.sim.replay import replay
    from src.utils import config as cfg

    body = request.get_json(silent=True) or {}
    seed = int(body.get("seed", 0))
    bankroll = float(body.get("bankroll", cfg.BANKROLL_PER_SIDE_USD))
    write = bool(body.get("write", False))

    if not arb_store.table_exists("opportunities"):
        return jsonify({"error": "no_opportunities_yet"}), 400

    glob = (arb_store.table_dir("opportunities") / "**" / "*.parquet").as_posix()
    keys = ("ts", "pair", "bybit_mid", "bybit_bid", "bybit_ask", "dex_mid",
            "spread_bps", "gross_bps", "direction", "weighted_obi", "obi_delta",
            "cancellation_rate", "gas_gwei", "gas_cost_bps", "bybit_fee_bps",
            "dex_fee_bps", "slippage_haircut_bps", "expected_net_bps",
            "notional_usd", "theoretical_pnl_usd", "decision", "reason",
            "eth_price_used")
    rows = arb_store.query(
        f"SELECT {','.join(keys)} FROM read_parquet('{glob}', hive_partitioning=1) "
        f"ORDER BY ts ASC"
    )
    opps = [dict(zip(keys, r)) for r in rows]
    result = replay(opps, initial_usd_per_side=bankroll, rng=random.Random(seed))

    if write and result.trades:
        from dataclasses import asdict
        rows_by_pair = {}
        for t in result.trades:
            rows_by_pair.setdefault(t.pair, []).append(asdict(t))
        for pair, rows in rows_by_pair.items():
            arb_store.write_records("sim_trades", rows, pair=pair)

    sharpe = result.sharpe()
    return jsonify({
        "n_opportunities": len(opps),
        "n_go": result.n_trades,
        "n_filled": result.n_filled,
        "n_inventory_rejected": result.n_inventory_rejected,
        "hit_rate": result.hit_rate,
        "cumulative_pnl_usd": result.cumulative_pnl_usd,
        "avg_realized_net_bps": result.avg_realized_net_bps,
        "sharpe": sharpe,
        "kill_criterion_breached": (sharpe is not None and sharpe < 1.0),
        "written": write and len(result.trades) > 0,
        "starting_equity": result.starting_equity_usd,
        "final_equity": result.equity_curve[-1][1] if result.equity_curve else result.starting_equity_usd,
    })


@bp.route("/train_histgbt", methods=["POST"])
def train_histgbt_endpoint():
    """Train HistGBT on captured opportunities + sim_trades, save artifact."""
    import numpy as np
    from src.ml.feature_pipeline import label_from_sim_trade, stack_features
    from src.ml.hist_gbt import save_artifact, train_histgbt

    if not arb_store.table_exists("opportunities") or \
       not arb_store.table_exists("sim_trades"):
        return jsonify({
            "error": "need_both_opportunities_and_sim_trades",
            "hint": "Run /api/arb/run_replay first with write=true",
        }), 400

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
    opps = []
    labels = []
    timestamps = []
    for r in rows:
        d = dict(zip(keys, r))
        opps.append(d)
        labels.append(label_from_sim_trade(d))
        timestamps.append(d["ts"])
    if len(opps) < 20:
        return jsonify({
            "error": "too_few_samples",
            "detail": f"need >= 20 GO trades with sim_trade labels, got {len(opps)}",
        }), 400
    X = stack_features(opps)
    y = np.array(labels, dtype=np.int32)
    try:
        art = train_histgbt(X, y, timestamps=timestamps)
    except ValueError as e:
        return jsonify({"error": "train_failed", "detail": str(e)}), 400
    path = save_artifact(art)
    return jsonify({
        "saved": str(path),
        "holdout_auc": art.holdout_auc,
        "n_train": art.n_train,
        "n_holdout": art.n_holdout,
        "pos_rate_train": art.pos_rate_train,
        "veto_threshold": art.veto_threshold,
    })


@bp.route("/run_drill", methods=["POST"])
def run_drill_endpoint():
    """Risk drill — equivalent to scripts/risk_drill.py, returns 8 check results.

    SAFETY (regression for P0-2 2026-05-11): snapshot any live HALT BEFORE the
    drill clears it, and restore it AFTER. Previously the drill unconditionally
    cleared the HALT flag, which let an operator silently bypass the kill
    switch by clicking 'Run drill' in the dashboard.
    """
    from src.risk import limits as rl
    checks = []

    def _record(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": ok, "detail": detail})

    # Snapshot any pre-existing live HALT so we can restore it.
    prior_halt_active = rl.halt_active()
    prior_halt_reason = rl.halt_reason() if prior_halt_active else None

    rl.halt_clear()
    state = rl.RiskState()
    gate = rl.preflight(opportunity=None, state=state)
    _record("clean_state_ok", gate.decision == "OK", gate.reason)

    rl.halt_set("drill: manual halt")
    import time as _t
    t0 = _t.time()
    gate = rl.preflight(opportunity=None, state=state)
    elapsed = _t.time() - t0
    _record("manual_halt_within_2s",
             gate.decision == "HALT" and elapsed < 2.0,
             f"{elapsed*1000:.1f}ms")
    rl.halt_clear()

    state = rl.RiskState()
    state.today_realized_pnl_usd = -state.daily_loss_cap_usd - 1.0
    triggered = rl.maybe_auto_halt(state)
    _record("daily_loss_auto_halt", triggered and rl.halt_active(),
             f"cap=${state.daily_loss_cap_usd:.2f}")
    rl.halt_clear()

    state = rl.RiskState()
    state.rolling_24h_drawdown_usd = state.drawdown_trigger_usd + 0.01
    triggered = rl.maybe_auto_halt(state)
    _record("drawdown_auto_halt", triggered and rl.halt_active(),
             f"trigger=${state.drawdown_trigger_usd:.2f}")
    rl.halt_clear()

    state = rl.RiskState()
    state.inventory_imbalance = 0.30
    triggered = rl.maybe_auto_halt(state)
    _record("imbalance_auto_halt", triggered and rl.halt_active(),
             "0.30 > 0.25")
    rl.halt_clear()

    state = rl.RiskState()
    bad_opp = {"decision": "GO", "notional_usd": state.per_trade_cap_usd * 5,
               "expected_net_bps": 50.0}
    gate = rl.preflight(opportunity=bad_opp, state=state)
    _record("oversized_notional_reject",
             gate.decision == "REJECT" and "notional_exceeds_cap" in gate.reason,
             gate.reason)

    state = rl.RiskState()
    weak = {"decision": "GO", "notional_usd": 50.0, "expected_net_bps": 1.0}
    gate = rl.preflight(opportunity=weak, state=state)
    _record("below_min_bps_reject",
             gate.decision == "REJECT" and "below_min_net_bps" in gate.reason,
             gate.reason)

    rl.halt_clear()
    gate = rl.preflight(opportunity=None, state=rl.RiskState())
    _record("clear_restores_ok", gate.decision == "OK", gate.reason)

    # Restore any pre-existing live HALT that the drill cleared (P0-2 safety).
    if prior_halt_active:
        rl.halt_set(prior_halt_reason or "restored after drill")

    # Write drill marker for live-ramp guard freshness check.
    drill_log = config.LOG_DIR / "drill_runs.jsonl"
    drill_log.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    safe_json.append_jsonl(drill_log, {
        "ts": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "passed": all(c["ok"] for c in checks),
        "prior_halt_restored": prior_halt_active,
    })

    return jsonify({
        "passed": all(c["ok"] for c in checks),
        "n_total": len(checks),
        "n_passed": sum(1 for c in checks if c["ok"]),
        "checks": checks,
    })


@bp.route("/halt", methods=["POST"])
def halt_endpoint():
    """Set or clear the HALT flag from the dashboard."""
    from src.risk import limits as rl
    body = request.get_json(silent=True) or {}
    action = body.get("action", "")
    if action == "set":
        reason = body.get("reason", "dashboard manual halt")
        rl.halt_set(f"dashboard: {reason}")
        return jsonify({"halt_active": True, "reason": rl.halt_reason()})
    if action == "clear":
        cleared = rl.halt_clear()
        return jsonify({"halt_active": False, "cleared": cleared})
    return jsonify({"error": "action must be 'set' or 'clear'"}), 400


@bp.route("/maker_mode", methods=["GET", "POST"])
def maker_mode_endpoint():
    """Get or toggle ARB_PREFER_MAKER for the current process.

    Note: this affects only the running dashboard process. The ingestion
    process picks up the new value at its next restart_all.ps1.
    A persistent toggle goes through a file flag at data/arb/MAKER_PREFERRED.
    """
    flag_path = config.DATA_DIR / "MAKER_PREFERRED"
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled", False))
        if enabled:
            flag_path.touch()
        else:
            flag_path.unlink(missing_ok=True)
        os.environ["ARB_PREFER_MAKER"] = "1" if enabled else "0"
        # Reload config so any in-process reads see the new value
        import importlib
        importlib.reload(config)
        return jsonify({"enabled": enabled, "needs_restart": True,
                        "flag_path": str(flag_path)})
    return jsonify({
        "enabled": flag_path.exists() or os.environ.get("ARB_PREFER_MAKER") == "1",
        "flag_path": str(flag_path),
    })


@bp.route("/counterfactual", methods=["POST"])
def counterfactual_endpoint():
    """Re-evaluate captured opportunities with different fee/notional assumptions.

    Body: {bybit_fee_bps: float, notional_usd: float (optional)}
    Returns: {n_go, n_skip, go_pnl_total, by_pair: [...]}
    """
    from src.strategy.opportunity import detect_opportunity

    if not arb_store.table_exists("opportunities"):
        return jsonify({"error": "no_opportunities_yet"}), 400

    body = request.get_json(silent=True) or {}
    bybit_fee_bps = float(body.get("bybit_fee_bps", 1.0))  # default maker
    notional_override = body.get("notional_usd")

    glob = (arb_store.table_dir("opportunities") / "**" / "*.parquet").as_posix()
    keys = ("ts", "pair", "bybit_bid", "bybit_ask", "dex_mid", "weighted_obi",
            "obi_delta", "cancellation_rate", "gas_gwei", "dex_fee_bps",
            "notional_usd", "eth_price_used")
    rows = arb_store.query(
        f"SELECT {','.join(keys)} FROM read_parquet('{glob}', hive_partitioning=1)"
    )

    by_pair = {}
    total_go = 0
    total_skip = 0
    total_pnl = 0.0
    for r in rows:
        d = dict(zip(keys, r))
        notional = float(notional_override) if notional_override else float(d["notional_usd"])
        op = detect_opportunity(
            ts=d["ts"], pair=d["pair"],
            bybit_bid=d["bybit_bid"], bybit_ask=d["bybit_ask"],
            dex_mid=d["dex_mid"], weighted_obi=d["weighted_obi"],
            obi_delta=d["obi_delta"], cancellation_rate=d["cancellation_rate"],
            gas_total_gwei=d["gas_gwei"], pool_fee_bps=d["dex_fee_bps"],
            notional_usd=notional, eth_price_usd=d["eth_price_used"],
            bybit_fee_bps=bybit_fee_bps,
        )
        slot = by_pair.setdefault(op.pair, {"go": 0, "skip": 0, "pnl": 0.0,
                                              "max_net_bps": -1e9})
        if op.decision == "GO":
            slot["go"] += 1
            slot["pnl"] += op.theoretical_pnl_usd
            total_go += 1
            total_pnl += op.theoretical_pnl_usd
        else:
            slot["skip"] += 1
            total_skip += 1
        if op.expected_net_bps > slot["max_net_bps"]:
            slot["max_net_bps"] = op.expected_net_bps

    return jsonify({
        "n_total": total_go + total_skip,
        "n_go": total_go,
        "n_skip": total_skip,
        "go_pnl_total": round(total_pnl, 4),
        "bybit_fee_bps_used": bybit_fee_bps,
        "notional_usd_used": notional_override,
        "by_pair": [
            {"pair": pair,
             "go": s["go"], "skip": s["skip"],
             "pnl": round(s["pnl"], 4),
             "max_net_bps": round(s["max_net_bps"], 4)}
            for pair, s in sorted(by_pair.items())
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


@bp.route("/zombies", methods=["GET"])
def zombies():
    """P3 cross-cutting: list orphan python.exe processes referencing the
    arb project but not registered in any PID file."""
    from src.ops.health_extras import find_zombie_processes
    z = find_zombie_processes()
    return jsonify({
        "n_zombies": len(z),
        "zombies": [
            {"pid": p.pid, "command_line": p.command_line, "reason": p.reason}
            for p in z
        ],
    })


@bp.route("/correctness", methods=["GET"])
def correctness():
    """P3 cross-cutting: dashboard correctness probe — NaN/Inf/impossible
    spread / bid>ask / missing risk caps / unknown mode. Returns findings."""
    from src.ops.health_extras import validate_dashboard_data
    from flask import current_app
    findings = validate_dashboard_data(current_app.test_client())
    return jsonify({
        "n_findings": len(findings),
        "findings": [
            {"endpoint": f.endpoint, "field": f.field,
             "issue": f.issue, "value": _safe_value(f.value)}
            for f in findings
        ],
    })


def _safe_value(v):
    """JSON-safe coercion of CorrectnessFinding.value."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        import math
        if math.isnan(v) or math.isinf(v):
            return str(v)
        return v
    return str(v)


@bp.route("/tft_eta", methods=["GET"])
def tft_eta():
    """P3 cross-cutting: TFT training ETA from REAL sister-project training
    logs. Returns null eta_seconds if not enough data to project — explicitly
    refuses to guess (per operator directive 2026-05-11)."""
    from src.ops.health_extras import compute_tft_eta
    est = compute_tft_eta()
    return jsonify({
        "measured_steps": est.measured_steps,
        "total_steps": est.total_steps,
        "mean_elapsed_per_step_s": est.mean_elapsed_per_step_s,
        "eta_seconds": est.eta_seconds,
        "eta_human": _human_duration(est.eta_seconds) if est.eta_seconds else None,
        "confidence": est.confidence,
        "source": est.source,
        "reason": est.reason,
    })


def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


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
