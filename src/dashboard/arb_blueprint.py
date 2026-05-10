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
