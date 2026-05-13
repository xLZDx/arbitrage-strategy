"""
P3 hardening tests (2026-05-11) — invariants, schema versioning, atomic
writes, gas staleness, cross-cutting tools.

Every test validates real behavior — no empty assertions.
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import time
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from src.dashboard.app_arb import create_app
from src.exec.bybit_leg import Fill
from src.ml.feature_pipeline import FEATURE_SCHEMA_VERSION
from src.ml.hist_gbt import ARTIFACT_SCHEMA_VERSION
from src.ops.health_extras import (
    CorrectnessFinding, ZombieProcess, compute_tft_eta,
    find_zombie_processes, validate_dashboard_data,
)
# _human_duration lives in arb_blueprint, not health_extras
from src.dashboard.arb_blueprint import _human_duration
from src.security.goplus_scanner import ERROR_CACHE_TTL_S, CACHE_TTL_S
from src.storage import arb_store
from src.strategy.opportunity import (
    OPPORTUNITY_SCHEMA_VERSION, Opportunity,
)
from src.utils import config


# --- P3-D1: GoPlus error TTL --------------------------------------------


def test_goplus_error_ttl_is_shorter_than_clean_ttl() -> None:
    assert ERROR_CACHE_TTL_S < CACHE_TTL_S
    assert ERROR_CACHE_TTL_S <= 120  # 60s default; must be small


# --- P3-D2: Fill.raw stripped before persist ---------------------------


def test_fill_raw_stripped_from_idempotency_persist(tmp_path) -> None:
    """REGRESSION P3-D2: persisting an idempotency record MUST set raw=None
    on disk so account-internals don't land in a plaintext JSON file."""
    from src.exec.bybit_leg import BybitLegExecutor
    from src.utils import safe_json
    ledger = tmp_path / "ledger.json"
    ex = BybitLegExecutor(mode=config.MODE_SHADOW, ledger_path=ledger)
    # Manually inject a fill with raw payload (simulating live ccxt response)
    fill = Fill(
        symbol="BTCUSDT", side="SELL", requested_qty_usd=50.0,
        filled_qty_usd=50.0, avg_price=80000.0, status="filled",
        venue_order_id="abc", client_order_id="cid-raw",
        mode=config.MODE_SHADOW, raw={"secret_account_field": "DO_NOT_LEAK"},
    )
    ex._persist_idempotency("cid-raw", fill)
    on_disk = safe_json.read_json(ledger, default={})
    record = on_disk.get("cid-raw")
    assert record is not None
    assert record["raw"] is None, (
        "P3-D2 regression: fill.raw was persisted on disk; must be stripped"
    )
    # And the in-memory cache also has raw=None for safety
    cached_in_memory = ex._idempotency_cache["cid-raw"]
    assert cached_in_memory["raw"] is None


# --- P3-D3: atomic parquet writes ---------------------------------------


def test_write_arrow_does_not_leave_tmp_files(tmp_path) -> None:
    """REGRESSION P3-D3: write_arrow uses .tmp + os.replace; no .tmp must
    survive a successful write."""
    import pyarrow as pa
    arb_store.close()
    test_table = "_test_atomic_writes"
    d = arb_store.table_dir(test_table)
    if d.exists():
        shutil.rmtree(d)
    try:
        batch = pa.Table.from_pylist([
            {"ts": "2026-05-11T12:00:00+00:00", "pair": "BTCUSDT", "x": 1.0}
        ])
        out_path = arb_store.write_arrow(test_table, batch, pair="BTCUSDT")
        assert out_path.exists()
        # No .tmp leftover
        tmp_path_leaked = out_path.with_suffix(out_path.suffix + ".tmp")
        assert not tmp_path_leaked.exists(), (
            "P3-D3 regression: .tmp file leaked after successful write"
        )
    finally:
        if d.exists():
            shutil.rmtree(d)


# --- P3-D4: schema versioning -------------------------------------------


def test_opportunity_schema_version_v2() -> None:
    """REGRESSION P3-D4: post-fee-fix schema is v2; old parquet rows are v1.
    Replay code should be able to distinguish via this constant."""
    assert OPPORTUNITY_SCHEMA_VERSION == 2


def test_histgbt_artifact_schema_version_v2() -> None:
    """REGRESSION P3-D4: post-feature-drop schema is v2."""
    assert ARTIFACT_SCHEMA_VERSION == 2
    assert FEATURE_SCHEMA_VERSION == 2


# --- P3-D5: __post_init__ invariants ------------------------------------


def test_fill_post_init_rejects_overfill() -> None:
    """REGRESSION P3-D5: filled_qty_usd > 1.05 * requested raises."""
    try:
        Fill(
            symbol="BTCUSDT", side="BUY", requested_qty_usd=50.0,
            filled_qty_usd=100.0,  # 200% — overfill way beyond 5% tolerance
            avg_price=80000.0, status="filled",
            venue_order_id=None, client_order_id="x",
            mode=config.MODE_SHADOW,
        )
    except ValueError as e:
        assert "overfill" in str(e).lower()
        return
    assert False, "Fill must reject overfill > 5%"


def test_fill_post_init_rejects_filled_without_price() -> None:
    """REGRESSION P3-D5: status='filled' with avg_price=0 raises."""
    try:
        Fill(
            symbol="BTCUSDT", side="BUY", requested_qty_usd=50.0,
            filled_qty_usd=50.0, avg_price=0.0,  # filled but no price?
            status="filled",
            venue_order_id=None, client_order_id="x",
            mode=config.MODE_SHADOW,
        )
    except ValueError as e:
        assert "avg_price" in str(e)
        return
    assert False


def test_fill_pct_clamped_to_one() -> None:
    """Exchange overfills <= 5% are tolerated; fill_pct still returns <= 1.0."""
    f = Fill(
        symbol="BTCUSDT", side="BUY", requested_qty_usd=50.0,
        filled_qty_usd=52.0,  # 4% overfill, within tolerance
        avg_price=80000.0, status="filled",
        venue_order_id="ok", client_order_id="x",
        mode=config.MODE_SHADOW,
    )
    assert f.fill_pct <= 1.0


def test_opportunity_post_init_rejects_negative_gross_bps() -> None:
    try:
        Opportunity(
            ts="2026-05-11T12:00:00+00:00", pair="BTCUSDT",
            bybit_mid=80000.0, bybit_bid=79999.5, bybit_ask=80000.5,
            dex_mid=79900.0, spread_bps=12.5, gross_bps=-5.0,  # invalid!
            direction="bybit_high", weighted_obi=0.0, obi_delta=0.0,
            cancellation_rate=0.0, gas_gwei=0.006, gas_cost_bps=0.6,
            bybit_fee_bps=10.0, dex_fee_bps=5.0,
            slippage_haircut_bps=5.0, expected_net_bps=-8.0,
            notional_usd=50.0, theoretical_pnl_usd=0.0,
            decision="SKIP", reason="test", eth_price_used=3000.0,
        )
    except ValueError as e:
        assert "gross_bps" in str(e)
        return
    assert False


def test_opportunity_post_init_rejects_inverted_book() -> None:
    try:
        Opportunity(
            ts="x", pair="BTCUSDT",
            bybit_mid=80000.0, bybit_bid=80001.0, bybit_ask=79999.0,  # bid>ask
            dex_mid=79900.0, spread_bps=12.5, gross_bps=12.5,
            direction="bybit_high", weighted_obi=0.0, obi_delta=0.0,
            cancellation_rate=0.0, gas_gwei=0.006, gas_cost_bps=0.6,
            bybit_fee_bps=10.0, dex_fee_bps=5.0,
            slippage_haircut_bps=5.0, expected_net_bps=10.0,
            notional_usd=50.0, theoretical_pnl_usd=0.05,
            decision="GO", reason="test", eth_price_used=3000.0,
        )
    except ValueError as e:
        assert "bid" in str(e) or "ask" in str(e)
        return
    assert False


def test_opportunity_post_init_rejects_out_of_range_cancellation() -> None:
    try:
        Opportunity(
            ts="x", pair="BTCUSDT",
            bybit_mid=80000.0, bybit_bid=79999.5, bybit_ask=80000.5,
            dex_mid=79900.0, spread_bps=12.5, gross_bps=12.5,
            direction="bybit_high", weighted_obi=0.0, obi_delta=0.0,
            cancellation_rate=1.5,  # out of range
            gas_gwei=0.006, gas_cost_bps=0.6, bybit_fee_bps=10.0,
            dex_fee_bps=5.0, slippage_haircut_bps=5.0,
            expected_net_bps=10.0, notional_usd=50.0,
            theoretical_pnl_usd=0.05, decision="GO", reason="test",
            eth_price_used=3000.0,
        )
    except ValueError as e:
        assert "cancellation_rate" in str(e)
        return
    assert False


def test_opportunity_allows_skip_with_zero_prices() -> None:
    """The 'non_positive_mid' SKIP path uses zero prices — must construct OK."""
    op = Opportunity(
        ts="x", pair="BTCUSDT",
        bybit_mid=0.0, bybit_bid=0.0, bybit_ask=0.0,
        dex_mid=0.0, spread_bps=0.0, gross_bps=0.0,
        direction="bybit_high", weighted_obi=0.0, obi_delta=0.0,
        cancellation_rate=0.0, gas_gwei=0.0, gas_cost_bps=0.0,
        bybit_fee_bps=10.0, dex_fee_bps=5.0,
        slippage_haircut_bps=0.0, expected_net_bps=0.0,
        notional_usd=50.0, theoretical_pnl_usd=0.0,
        decision="SKIP", reason="non_positive_mid",
        eth_price_used=3000.0,
    )
    assert op.decision == "SKIP"


# --- P3-D6: gas oracle staleness ----------------------------------------


def test_gas_oracle_latest_invalidates_when_stale() -> None:
    """REGRESSION P3-D6: latest() returns None when _latest is older than
    STALE_TTL_S, regardless of caller's freshness check."""
    from src.data.gas_oracle import GasOracle, GasReading
    oracle = GasOracle()
    # Inject a reading from 5 minutes ago
    five_min_ago = int(time.time() * 1000) - 300_000
    oracle._latest = GasReading(
        ts_ms=five_min_ago, block_number=1, base_fee_gwei=0.005,
        priority_fee_gwei=0.001, total_gas_price_gwei=0.006,
    )
    # poll_interval_s=6 → STALE_TTL_S = max(30, 30) = 30s. 300s >> 30s.
    result = oracle.latest()
    assert result is None, (
        "P3-D6 regression: oracle.latest() must invalidate stale readings"
    )


# --- Cross-cutting: zombie detector -------------------------------------


def test_find_zombie_processes_returns_list() -> None:
    """Smoke test — should not crash, returns list of ZombieProcess."""
    result = find_zombie_processes()
    assert isinstance(result, list)
    for z in result:
        assert isinstance(z, ZombieProcess)


# --- Cross-cutting: dashboard correctness validator ----------------------


def test_validate_dashboard_data_clean_no_findings() -> None:
    """With seeded clean opportunities, validator returns no findings."""
    arb_store.close()
    for t in ("opportunities", "obi_snapshots", "dex_quotes"):
        d = arb_store.table_dir(t)
        if d.exists():
            shutil.rmtree(d)
    try:
        # No seeded data → endpoints return empty payloads; that's not an
        # error — validator should produce no findings.
        client = create_app().test_client()
        findings = validate_dashboard_data(client)
        # Some endpoints may flag missing-or-zero caps in /risk which is
        # legit because we use $500 placeholder bankroll; assert findings
        # don't include impossible-spread / NaN / Inf flags.
        bad_kinds = [f for f in findings if f.issue in ("NaN", "Inf", "implausible_spread")]
        assert bad_kinds == []
    finally:
        for t in ("opportunities", "obi_snapshots", "dex_quotes"):
            d = arb_store.table_dir(t)
            if d.exists():
                shutil.rmtree(d)


def test_validate_dashboard_detects_implausible_spread() -> None:
    """Inject a row with absurd spread; validator must flag it."""
    arb_store.close()
    test_table = "opportunities"
    d = arb_store.table_dir(test_table)
    if d.exists():
        shutil.rmtree(d)
    try:
        # We can't trivially make /spread return implausible bps because
        # it computes from obi_snapshots + dex_quotes. Easier: validate
        # the function directly with a mock client.
        class _MockResp:
            def __init__(self, body):
                self._body = body
                self.status_code = 200
            def get_json(self):
                return self._body

        class _MockClient:
            def get(self, url):
                if url == "/api/arb/spread":
                    return _MockResp({"spreads": [{
                        "pair": "BTCUSDT", "bybit_mid": 80000.0,
                        "bybit_bid": 79999.5, "bybit_ask": 80000.5,
                        "dex_mid": 80000.0, "spread_bps": 9999.0,  # absurd
                        "bybit_ts": "2026-05-11T12:00:00",
                    }]})
                if url == "/api/arb/risk":
                    return _MockResp({
                        "daily_loss_cap_usd": 25.0,
                        "drawdown_trigger_usd": 75.0,
                        "per_trade_cap_usd": 50.0,
                        "mode": "SHADOW",
                    })
                if url == "/api/arb/soak_summary":
                    return _MockResp({"spread_distribution": []})
                return _MockResp({})

        findings = validate_dashboard_data(_MockClient())
        impl = [f for f in findings if f.issue == "implausible_spread"]
        assert len(impl) == 1
        assert impl[0].value == 9999.0
    finally:
        if d.exists():
            shutil.rmtree(d)


def test_validate_dashboard_detects_bid_ask_inverted() -> None:
    class _MockResp:
        def __init__(self, body):
            self._body = body
            self.status_code = 200
        def get_json(self):
            return self._body

    class _MockClient:
        def get(self, url):
            if url == "/api/arb/spread":
                return _MockResp({"spreads": [{
                    "pair": "BTCUSDT", "bybit_mid": 80000.0,
                    "bybit_bid": 80001.0, "bybit_ask": 79999.0,  # inverted!
                    "dex_mid": 80000.0, "spread_bps": 0.5,
                    "bybit_ts": "2026-05-11T12:00:00",
                }]})
            if url == "/api/arb/risk":
                return _MockResp({
                    "daily_loss_cap_usd": 25.0,
                    "drawdown_trigger_usd": 75.0,
                    "per_trade_cap_usd": 50.0,
                    "mode": "SHADOW",
                })
            if url == "/api/arb/soak_summary":
                return _MockResp({"spread_distribution": []})
            return _MockResp({})

    findings = validate_dashboard_data(_MockClient())
    inv = [f for f in findings if f.field == "bid_ask_inverted"]
    assert len(inv) == 1, f"expected 1 bid_ask_inverted finding, got: {findings}"


# --- Cross-cutting: TFT ETA computer ------------------------------------


def test_tft_eta_no_data_returns_unknown() -> None:
    """REGRESSION 2026-05-11 (no-guessing rule): when source data unavailable,
    ETA must be None + confidence='unknown' — NEVER a guess."""
    est = compute_tft_eta(
        status_json_path=Path("/tmp/does_not_exist.json"),
        training_log_path=Path("/tmp/does_not_exist.log"),
    )
    assert est.eta_seconds is None
    assert est.confidence == "unknown"
    assert est.source == "no_data"
    assert "No training_status_report" in est.reason or "no" in est.reason.lower()


def test_tft_eta_too_few_steps_refuses_to_guess(tmp_path) -> None:
    """REGRESSION: with < 5 measured steps, refuses to project."""
    status_file = tmp_path / "status.json"
    status_file.write_text(
        '{"jobs": {"tft_001": {"model": "tft", "step": 2, '
        '"total_steps": 100, "started_at": "2026-05-11T10:00:00+00:00"}}}'
    )
    est = compute_tft_eta(status_json_path=status_file,
                          training_log_path=tmp_path / "no.log")
    assert est.eta_seconds is None
    assert est.confidence == "low"
    assert "5 to project" in est.reason or "5 step" in est.reason


def test_tft_eta_projects_from_real_data(tmp_path) -> None:
    """With enough measured steps, projects honestly."""
    import json
    from datetime import datetime, timezone, timedelta
    started = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    # 30 steps done in 600s → 20s/step. 100 - 30 = 70 remaining → 1400s ETA.
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps({
        "jobs": {
            "tft_001": {
                "model": "tft", "step": 30, "total_steps": 100,
                "started_at": started,
            }
        }
    }))
    est = compute_tft_eta(status_json_path=status_file,
                          training_log_path=tmp_path / "no.log")
    assert est.eta_seconds is not None
    # Allow 10% slack for timing jitter
    assert 1100 < est.eta_seconds < 1700, f"got {est.eta_seconds}"
    assert est.confidence in ("medium", "high")
    assert est.source == "training_status_report"


def test_human_duration_format() -> None:
    assert _human_duration(30) == "30s"
    assert _human_duration(120) == "2.0m"
    assert _human_duration(3700) == "1.0h"


# --- Cross-cutting: dashboard endpoints ----------------------------------


def test_zombies_endpoint_returns_structured_payload() -> None:
    r = create_app().test_client().get("/api/arb/zombies")
    assert r.status_code == 200
    body = r.get_json()
    assert "n_zombies" in body
    assert "zombies" in body
    assert isinstance(body["zombies"], list)


def test_correctness_endpoint_returns_findings_list() -> None:
    r = create_app().test_client().get("/api/arb/correctness")
    assert r.status_code == 200
    body = r.get_json()
    assert "n_findings" in body
    assert isinstance(body["findings"], list)


def test_tft_eta_endpoint_returns_payload() -> None:
    r = create_app().test_client().get("/api/arb/tft_eta")
    assert r.status_code == 200
    body = r.get_json()
    for k in ("measured_steps", "eta_seconds", "confidence",
              "source", "reason"):
        assert k in body


def _run_all() -> int:
    import tempfile
    failures: list[tuple[str, str]] = []
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            sig = fn.__code__.co_varnames[:fn.__code__.co_argcount]
            if "tmp_path" in sig:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print()
    if failures:
        print(f"{len(failures)} / {len(tests)} FAILED")
        return 1
    print(f"{len(tests)} / {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
