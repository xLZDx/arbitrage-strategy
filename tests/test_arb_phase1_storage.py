"""
Phase 1 regression tests — arb_store (DuckDB + Parquet) + safe_json.

These tests touch disk under data/arb/ with a unique test prefix and
clean up after themselves so they're idempotent.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.storage import arb_store
from src.utils import config, safe_json

# Use a dedicated test table so we don't pollute real partitions.
TEST_TABLE = "_test_obi_snapshots"


def _cleanup() -> None:
    arb_store.close()
    p = arb_store.table_dir(TEST_TABLE)
    if p.exists():
        shutil.rmtree(p)


def setup_function(_) -> None:
    _cleanup()


def teardown_function(_) -> None:
    _cleanup()


# --- arb_store -------------------------------------------------------------


def test_partition_path_creates_hive_layout() -> None:
    p = arb_store.partition_path(TEST_TABLE, "2026-05-10T12:00:00+00:00", "BTCUSDT")
    assert p.exists()
    assert "date=2026-05-10" in str(p)
    assert "pair=BTCUSDT" in str(p)


def test_write_and_scan_roundtrip() -> None:
    rows = [
        {"ts": "2026-05-10T12:00:00+00:00", "pair": "BTCUSDT",
         "weighted_obi": 0.42, "bid_volume": 100.0, "ask_volume": 50.0},
        {"ts": "2026-05-10T12:00:01+00:00", "pair": "BTCUSDT",
         "weighted_obi": -0.13, "bid_volume": 30.0, "ask_volume": 60.0},
    ]
    fpath = arb_store.write_records(TEST_TABLE, rows, pair="BTCUSDT")
    assert fpath.exists()
    assert fpath.suffix == ".parquet"
    assert arb_store.row_count(TEST_TABLE) == 2

    out = arb_store.scan_table(TEST_TABLE, where="weighted_obi > 0")
    assert len(out) == 1
    # row layout depends on column order in the parquet file; just check value
    assert any(0.42 in r for r in out), f"expected 0.42 in result, got {out}"


def test_scan_arrow_returns_arrow_table() -> None:
    rows = [{"ts": "2026-05-10T12:00:00+00:00", "pair": "ETHUSDT",
             "weighted_obi": 0.1, "bid_volume": 10.0, "ask_volume": 9.0}]
    arb_store.write_records(TEST_TABLE, rows, pair="ETHUSDT")
    arrow = arb_store.scan_table_arrow(TEST_TABLE)
    assert arrow.num_rows == 1
    assert "weighted_obi" in arrow.column_names


def test_table_exists_false_when_empty() -> None:
    assert not arb_store.table_exists(TEST_TABLE)


def test_table_exists_true_after_write() -> None:
    arb_store.write_records(
        TEST_TABLE,
        [{"ts": "2026-05-10T12:00:00+00:00", "pair": "BTCUSDT", "weighted_obi": 0.0}],
        pair="BTCUSDT",
    )
    assert arb_store.table_exists(TEST_TABLE)


def test_locked_con_is_reentrant() -> None:
    """RLock allows nested locked_con() calls in same thread."""
    with arb_store.locked_con() as c1:
        with arb_store.locked_con() as c2:
            assert c1 is c2
            result = c2.execute("SELECT 1").fetchone()
            assert result == (1,)


def test_partitioning_separates_pairs() -> None:
    arb_store.write_records(
        TEST_TABLE,
        [{"ts": "2026-05-10T12:00:00+00:00", "pair": "BTCUSDT", "weighted_obi": 0.1}],
        pair="BTCUSDT",
    )
    arb_store.write_records(
        TEST_TABLE,
        [{"ts": "2026-05-10T12:00:00+00:00", "pair": "ETHUSDT", "weighted_obi": 0.2}],
        pair="ETHUSDT",
    )
    btc = arb_store.scan_table(TEST_TABLE, where="pair = 'BTCUSDT'")
    eth = arb_store.scan_table(TEST_TABLE, where="pair = 'ETHUSDT'")
    assert len(btc) == 1
    assert len(eth) == 1


# --- safe_json -------------------------------------------------------------


def test_safe_json_atomic_write_and_read(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    safe_json.write_json(p, {"halt": False, "drawdown": 0.0})
    assert safe_json.read_json(p) == {"halt": False, "drawdown": 0.0}


def test_safe_json_default_when_missing(tmp_path: Path) -> None:
    assert safe_json.read_json(tmp_path / "nope.json", default={"x": 1}) == {"x": 1}


def test_append_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "log.jsonl"
    safe_json.append_jsonl(p, {"ts": "2026-05-10T12:00:00", "event": "halt_set"})
    safe_json.append_jsonl(p, {"ts": "2026-05-10T12:00:01", "event": "halt_clear"})
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "halt_set"
    assert json.loads(lines[1])["event"] == "halt_clear"


def test_safe_json_overwrite_does_not_leak_temp(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    safe_json.write_json(p, {"v": 1})
    safe_json.write_json(p, {"v": 2})
    leftover = list(tmp_path.glob("state.json.*.tmp"))
    assert leftover == []
    assert safe_json.read_json(p) == {"v": 2}


# --- config ----------------------------------------------------------------


def test_config_paths_are_on_d_drive() -> None:
    """Per CLAUDE.md: never C:."""
    for p in (config.DATA_DIR, config.DB_DIR, config.CACHE_DIR,
              config.DUCKDB_TEMP_DIR, config.LOG_DIR):
        as_str = str(p).lower().replace("\\", "/")
        assert not as_str.startswith("c:/"), f"{p} is on C: drive"


def test_config_pilot_pairs_match_q3() -> None:
    assert config.PILOT_PAIRS == ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def test_config_default_mode_is_shadow() -> None:
    """Q4 + risk: default mode must be SHADOW, not TESTNET or MAINNET."""
    # Note: this test only valid when ARB_MODE env var is unset (the normal case)
    import os
    if "ARB_MODE" not in os.environ:
        assert config.is_shadow_mode()
        assert not config.is_mainnet()


def test_halt_active_false_when_flag_absent() -> None:
    if config.HALT_FILE.exists():
        config.HALT_FILE.unlink()
    assert not config.halt_active()


def test_halt_active_true_when_flag_present() -> None:
    config.HALT_FILE.touch()
    try:
        assert config.halt_active()
    finally:
        config.HALT_FILE.unlink(missing_ok=True)


def _run_all() -> int:
    failures: list[tuple[str, str]] = []
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    # crude pytest tmp_path stub for standalone runner
    class _TmpPath:
        def __init__(self, base: Path) -> None: self._base = base
        def __truediv__(self, other: str) -> Path: return self._base / other
        def glob(self, pattern: str): return self._base.glob(pattern)

    import tempfile
    for name, fn in tests:
        td = tempfile.mkdtemp(prefix="arbtest_", dir=config.DATA_DIR)
        try:
            setup_function(None)
            sig = fn.__code__.co_varnames[: fn.__code__.co_argcount]
            if "tmp_path" in sig:
                fn(_TmpPath(Path(td)))
            else:
                fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
        finally:
            shutil.rmtree(td, ignore_errors=True)
            teardown_function(None)
    print()
    if failures:
        print(f"{len(failures)} / {len(tests)} FAILED")
        return 1
    print(f"{len(tests)} / {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
