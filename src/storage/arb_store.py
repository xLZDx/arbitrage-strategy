"""
Storage layer for arbitrage_strategy.

Wraps DuckDB + partitioned Parquet under data/arb/db/. Thread-safe via a
module-level RLock around every con.execute() (per the 2026-05-08 thread-safety
incident in sister project — concurrent ParquetClient.query() crashed dashboard).

Tables (created idempotently on first init):
- obi_snapshots(ts, pair, side, level, price, size, weighted_obi)
- dex_quotes(ts, pair, side, in_amount, out_amount, gas_estimate, source)
- gas_history(ts, base_fee_gwei, priority_fee_gwei, block_number)
- opportunities(ts, pair, spread_bps, expected_net_bps, theoretical_pnl,
                obi_bybit, obi_dex, gas_gwei, decision, reason)
- trades(ts, pair, side, venue, notional_usd, fill_price, slippage_bps,
         gas_paid_gwei, status, reason)
- inventory(ts, venue, asset, balance, usd_value)

All timestamps are UTC ISO8601 strings stored as VARCHAR (DuckDB handles
sorting/comparison). Pair = Bybit symbol (BTCUSDT, ETHUSDT, SOLUSDT).

Partitioning is by (date, pair) inside a Hive-style directory tree:
data/arb/db/<table>/date=YYYY-MM-DD/pair=BTCUSDT/part-*.parquet

Reads use DuckDB's native Parquet scanning (read_parquet with hive_partitioning=1).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pyarrow as pa

from src.utils import config

_LOCK = threading.RLock()
_CON: duckdb.DuckDBPyConnection | None = None


# --- Connection management -------------------------------------------------


def _ensure_connection() -> duckdb.DuckDBPyConnection:
    global _CON
    if _CON is None:
        config.DUCKDB_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        _CON = duckdb.connect(database=":memory:")
        _CON.execute(f"SET temp_directory='{config.DUCKDB_TEMP_DIR.as_posix()}'")
        _CON.execute("SET memory_limit='2GB'")
    return _CON


@contextmanager
def locked_con():
    """All callers MUST use this context manager. Holds _LOCK during execute."""
    with _LOCK:
        yield _ensure_connection()


def close() -> None:
    global _CON
    with _LOCK:
        if _CON is not None:
            _CON.close()
            _CON = None


# --- Path helpers ----------------------------------------------------------


def table_dir(table: str) -> Path:
    return config.DB_DIR / table


def partition_path(table: str, ts_iso: str, pair: str) -> Path:
    date = ts_iso[:10]  # YYYY-MM-DD
    p = table_dir(table) / f"date={date}" / f"pair={pair}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Writes ----------------------------------------------------------------


def write_arrow(table: str, batch: pa.Table, pair: str, ts_iso: str | None = None) -> Path:
    """
    Write an Arrow table to a partitioned Parquet file under data/arb/db/<table>/.
    Returns the file path. Caller is responsible for batching (see config.ARROW_BATCH_*).
    """
    if ts_iso is None:
        ts_iso = utc_now_iso()
    pdir = partition_path(table, ts_iso, pair)
    fname = f"part-{ts_iso.replace(':', '').replace('-', '')[:15]}.parquet"
    fpath = pdir / fname
    import pyarrow.parquet as pq
    pq.write_table(batch, fpath, compression="zstd")
    return fpath


def write_records(table: str, records: Iterable[dict[str, Any]], pair: str) -> Path:
    """Convenience: take dict records, build Arrow table, write."""
    rows = list(records)
    if not rows:
        return Path()
    batch = pa.Table.from_pylist(rows)
    ts = rows[0].get("ts") or utc_now_iso()
    return write_arrow(table, batch, pair, ts_iso=ts)


# --- Reads -----------------------------------------------------------------


def query(sql: str, params: list[Any] | None = None) -> list[tuple]:
    """Execute arbitrary DuckDB SQL. Use scan_table() for Parquet reads."""
    with locked_con() as con:
        if params:
            return con.execute(sql, params).fetchall()
        return con.execute(sql).fetchall()


def scan_table(table: str, where: str | None = None, limit: int | None = None) -> list[tuple]:
    """
    Scan a partitioned Parquet table via DuckDB.
    Returns list of tuples (use scan_table_arrow for columnar).
    """
    glob = (table_dir(table) / "**" / "*.parquet").as_posix()
    sql = f"SELECT * FROM read_parquet('{glob}', hive_partitioning=1)"
    if where:
        sql += f" WHERE {where}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return query(sql)


def scan_table_arrow(table: str, where: str | None = None, limit: int | None = None) -> pa.Table:
    glob = (table_dir(table) / "**" / "*.parquet").as_posix()
    sql = f"SELECT * FROM read_parquet('{glob}', hive_partitioning=1)"
    if where:
        sql += f" WHERE {where}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with locked_con() as con:
        result = con.execute(sql).arrow()
        # DuckDB returns either a Table or a RecordBatchReader depending on version.
        # Normalize to Table.
        if isinstance(result, pa.Table):
            return result
        return result.read_all()


def table_exists(table: str) -> bool:
    """True iff at least one Parquet file exists for the table."""
    d = table_dir(table)
    if not d.exists():
        return False
    return any(d.rglob("*.parquet"))


def row_count(table: str) -> int:
    if not table_exists(table):
        return 0
    glob = (table_dir(table) / "**" / "*.parquet").as_posix()
    sql = f"SELECT COUNT(*) FROM read_parquet('{glob}', hive_partitioning=1)"
    return query(sql)[0][0]
