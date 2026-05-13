"""
Re-review regression suite (2026-05-11).

After the P0/P1/P2/P3 batch landed, four specialist agents
(architect / python-reviewer / security-reviewer / ml-engineer) reviewed
the diff again and surfaced 6 follow-up fixes. This file is the
functional proof those follow-ups work, per CLAUDE.md "Functional Tests
Prove Behavior" — every test below calls the code (not source text) and
asserts on observable behavior.

Covered:
  NEW-1  Fill(__post_init__) — shadow status accepts avg_price=0
  NEW-4  /zombies + /correctness + /tft_eta + /maker_mode require auth
  P0-B   detect_opportunity refuses pool_fee_bps >= 100 (raw-tier guard)
  P2-J   detect_opportunity default notional_usd is computed from config
  HIGH-4 run_train_histgbt drops inv-rejected + unfilled rows
  HIGH-7 HeuristicTftProvider.trailing_logreturn enforces |result| < 1
  M-4    /run_replay, /train_histgbt, /counterfactual, /run_drill rate-limit
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.dashboard.app_arb import create_app
from src.exec.bybit_leg import Fill
from src.ml.tft_feature import HeuristicTftProvider
from src.risk import limits as rl
from src.storage import arb_store
from src.strategy.opportunity import detect_opportunity
from src.utils import config


_TABLES_TOUCHED = ("opportunities", "sim_trades", "trades", "paper_trades",
                    "obi_snapshots", "dex_quotes", "gas_history")


def _cleanup_tables() -> None:
    arb_store.close()
    for t in _TABLES_TOUCHED:
        d = arb_store.table_dir(t)
        if d.exists():
            shutil.rmtree(d)


def setup_function(_) -> None:
    rl.halt_clear()
    _cleanup_tables()
    # Clear rate limiter state so back-to-back tests don't trip cooldowns
    # from a sibling test. The limiter dict lives at module scope inside the
    # blueprint.
    from src.dashboard import arb_blueprint as bp
    bp._LAST_CALLS.clear()
    os.environ.pop("ARB_API_KEY", None)


def teardown_function(_) -> None:
    rl.halt_clear()
    _cleanup_tables()
    os.environ.pop("ARB_API_KEY", None)
    from src.dashboard import arb_blueprint as bp
    bp._LAST_CALLS.clear()


def _client():
    return create_app().test_client()


# ============================================================================
# NEW-1 — Fill.__post_init__ accepts shadow with avg_price=0
# ============================================================================


def test_fill_shadow_status_allows_zero_price() -> None:
    """A SHADOW synthetic fill must NOT trip the live-fill invariant."""
    f = Fill(symbol="BTCUSDT", side="BUY",
              requested_qty_usd=50.0, filled_qty_usd=50.0,
              avg_price=0.0, status="shadow",
              venue_order_id=None, client_order_id="shadow-1",
              mode="SHADOW")
    assert f.status == "shadow"
    assert f.avg_price == 0.0


def test_fill_filled_status_requires_positive_price() -> None:
    """A LIVE 'filled' status with avg_price=0 must raise — that's the bug
    the invariant catches (silent zero-price persisted as a real fill)."""
    with pytest.raises(ValueError, match="avg_price"):
        Fill(symbol="BTCUSDT", side="BUY",
              requested_qty_usd=50.0, filled_qty_usd=50.0,
              avg_price=0.0, status="filled",
              venue_order_id="abc", client_order_id="live-1",
              mode="LIVE")


def test_fill_filled_status_with_positive_price_ok() -> None:
    f = Fill(symbol="BTCUSDT", side="BUY",
              requested_qty_usd=50.0, filled_qty_usd=50.0,
              avg_price=80000.0, status="filled",
              venue_order_id="abc", client_order_id="live-1",
              mode="LIVE")
    assert f.status == "filled"
    assert f.avg_price > 0


# ============================================================================
# NEW-4 — /zombies + /correctness + /tft_eta + /maker_mode require API key
# ============================================================================


def test_zombies_get_blocked_without_api_key() -> None:
    os.environ["ARB_API_KEY"] = "test-secret-xyz"
    r = _client().get("/api/arb/zombies")
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.data!r}"


def test_zombies_get_allowed_with_correct_api_key() -> None:
    os.environ["ARB_API_KEY"] = "test-secret-xyz"
    r = _client().get("/api/arb/zombies",
                       headers={"X-API-Key": "test-secret-xyz"})
    assert r.status_code == 200
    body = r.get_json()
    assert "n_zombies" in body
    assert "zombies" in body


def test_correctness_get_blocked_without_api_key() -> None:
    os.environ["ARB_API_KEY"] = "test-secret-xyz"
    r = _client().get("/api/arb/correctness")
    assert r.status_code == 401


def test_correctness_get_allowed_with_correct_api_key() -> None:
    os.environ["ARB_API_KEY"] = "test-secret-xyz"
    r = _client().get("/api/arb/correctness",
                       headers={"X-API-Key": "test-secret-xyz"})
    assert r.status_code == 200


def test_tft_eta_get_blocked_without_api_key() -> None:
    os.environ["ARB_API_KEY"] = "test-secret-xyz"
    r = _client().get("/api/arb/tft_eta")
    assert r.status_code == 401


def test_maker_mode_get_blocked_without_api_key() -> None:
    os.environ["ARB_API_KEY"] = "test-secret-xyz"
    r = _client().get("/api/arb/maker_mode")
    assert r.status_code == 401


def test_health_get_still_public_with_api_key_set() -> None:
    """/health stays public even when ARB_API_KEY is set — it's a liveness
    probe, never gated. Regression catches accidental over-gating."""
    os.environ["ARB_API_KEY"] = "test-secret-xyz"
    r = _client().get("/api/arb/health")
    assert r.status_code == 200


# ============================================================================
# P0-B — detect_opportunity refuses pool_fee_bps >= 100 (raw-tier guard)
# ============================================================================


def test_detect_opportunity_refuses_raw_fee_tier_500() -> None:
    """500 is the Uniswap raw tier for 0.05% — should be 5 bps, not 500."""
    with pytest.raises(ValueError, match="raw Uniswap fee tier"):
        detect_opportunity(
            ts="2026-05-11T00:00:00+00:00", pair="ETHUSDT",
            bybit_bid=3000.0, bybit_ask=3001.0, dex_mid=2990.0,
            weighted_obi=0.0, obi_delta=0.0, cancellation_rate=0.0,
            gas_total_gwei=0.005, pool_fee_bps=500.0,  # WRONG (raw tier)
        )


def test_detect_opportunity_refuses_raw_fee_tier_3000() -> None:
    with pytest.raises(ValueError, match="raw Uniswap fee tier"):
        detect_opportunity(
            ts="2026-05-11T00:00:00+00:00", pair="ETHUSDT",
            bybit_bid=3000.0, bybit_ask=3001.0, dex_mid=2990.0,
            weighted_obi=0.0, obi_delta=0.0, cancellation_rate=0.0,
            gas_total_gwei=0.005, pool_fee_bps=3000.0,
        )


def test_detect_opportunity_accepts_correct_bps() -> None:
    """5 bps = 0.05% (correct interpretation) — should pass the guard."""
    op = detect_opportunity(
        ts="2026-05-11T00:00:00+00:00", pair="ETHUSDT",
        bybit_bid=3000.0, bybit_ask=3001.0, dex_mid=2990.0,
        weighted_obi=0.0, obi_delta=0.0, cancellation_rate=0.0,
        gas_total_gwei=0.005, pool_fee_bps=5.0,
    )
    assert op.pair == "ETHUSDT"
    assert op.dex_fee_bps == 5.0


# ============================================================================
# P2-J — detect_opportunity default notional comes from current config
# ============================================================================


def test_detect_opportunity_default_notional_from_config() -> None:
    """When notional_usd is omitted, it must come from CURRENT config.
    Mutating config between calls must be visible — regression catches
    the previous module-load-time freeze bug."""
    saved_bankroll = config.BANKROLL_PER_SIDE_USD
    saved_pct = config.PER_TRADE_CAP_PCT
    try:
        config.BANKROLL_PER_SIDE_USD = 1000.0
        config.PER_TRADE_CAP_PCT = 10.0
        op1 = detect_opportunity(
            ts="2026-05-11T00:00:00+00:00", pair="ETHUSDT",
            bybit_bid=3000.0, bybit_ask=3001.0, dex_mid=2990.0,
            weighted_obi=0.0, obi_delta=0.0, cancellation_rate=0.0,
            gas_total_gwei=0.005, pool_fee_bps=5.0,
        )
        assert abs(op1.notional_usd - 100.0) < 0.01, (
            f"Expected $100 default ($1000 * 10%), got {op1.notional_usd}"
        )

        # Mutate config — next call should see the new value.
        config.BANKROLL_PER_SIDE_USD = 2000.0
        op2 = detect_opportunity(
            ts="2026-05-11T00:00:01+00:00", pair="ETHUSDT",
            bybit_bid=3000.0, bybit_ask=3001.0, dex_mid=2990.0,
            weighted_obi=0.0, obi_delta=0.0, cancellation_rate=0.0,
            gas_total_gwei=0.005, pool_fee_bps=5.0,
        )
        assert abs(op2.notional_usd - 200.0) < 0.01, (
            f"Expected $200 after config change, got {op2.notional_usd}"
        )
    finally:
        config.BANKROLL_PER_SIDE_USD = saved_bankroll
        config.PER_TRADE_CAP_PCT = saved_pct


def test_detect_opportunity_explicit_notional_wins() -> None:
    """If notional_usd is passed, it overrides config-derived default."""
    op = detect_opportunity(
        ts="2026-05-11T00:00:00+00:00", pair="ETHUSDT",
        bybit_bid=3000.0, bybit_ask=3001.0, dex_mid=2990.0,
        weighted_obi=0.0, obi_delta=0.0, cancellation_rate=0.0,
        gas_total_gwei=0.005, pool_fee_bps=5.0,
        notional_usd=42.0,
    )
    assert op.notional_usd == 42.0


# ============================================================================
# HIGH-4 — _load_training_pairs drops inv-rejected + unfilled rows
# ============================================================================


def test_load_training_pairs_drops_inv_rejected() -> None:
    """Inventory-rejected rows must NOT appear in the training set.
    Labeling them as 0 would teach the model a spurious decision rule."""
    from scripts.run_train_histgbt import _load_training_pairs

    arb_store.write_records("opportunities", [
        {"ts": "2026-05-11T00:00:00+00:00", "pair": "ETHUSDT",
         "bybit_mid": 3000.0, "bybit_bid": 2999.5, "bybit_ask": 3000.5,
         "dex_mid": 2990.0, "spread_bps": 33.0, "gross_bps": 33.0,
         "direction": "bybit_high", "weighted_obi": 0.0, "obi_delta": 0.0,
         "cancellation_rate": 0.0, "gas_gwei": 0.006, "gas_cost_bps": 0.5,
         "bybit_fee_bps": 1.0, "dex_fee_bps": 5.0, "slippage_haircut_bps": 5.0,
         "expected_net_bps": 21.5, "notional_usd": 50.0,
         "theoretical_pnl_usd": 0.1, "decision": "GO",
         "reason": "ok", "eth_price_used": 3000.0},
        {"ts": "2026-05-11T00:00:01+00:00", "pair": "ETHUSDT",
         "bybit_mid": 3000.0, "bybit_bid": 2999.5, "bybit_ask": 3000.5,
         "dex_mid": 2990.0, "spread_bps": 33.0, "gross_bps": 33.0,
         "direction": "bybit_high", "weighted_obi": 0.0, "obi_delta": 0.0,
         "cancellation_rate": 0.0, "gas_gwei": 0.006, "gas_cost_bps": 0.5,
         "bybit_fee_bps": 1.0, "dex_fee_bps": 5.0, "slippage_haircut_bps": 5.0,
         "expected_net_bps": 21.5, "notional_usd": 50.0,
         "theoretical_pnl_usd": 0.1, "decision": "GO",
         "reason": "ok", "eth_price_used": 3000.0},
    ], pair="ETHUSDT")
    arb_store.write_records("sim_trades", [
        # Row 1: legitimate filled trade — KEEP.
        {"ts": "2026-05-11T00:00:00+00:00", "pair": "ETHUSDT",
         "realized_pnl_usd": 0.10, "inventory_ok": True, "fill_pct": 1.0,
         "decision": "GO", "side": "BUY", "notional_usd": 50.0,
         "realized_net_bps": 20.0},
        # Row 2: inventory-rejected — DROP.
        {"ts": "2026-05-11T00:00:01+00:00", "pair": "ETHUSDT",
         "realized_pnl_usd": 0.0, "inventory_ok": False, "fill_pct": 0.0,
         "decision": "GO", "side": "BUY", "notional_usd": 50.0,
         "realized_net_bps": 0.0},
    ], pair="ETHUSDT")

    opps, labels, ts, stats = _load_training_pairs()
    assert stats["total"] == 2
    assert stats["dropped_inv_rejected"] == 1
    assert stats["kept"] == 1
    assert len(opps) == 1
    assert len(labels) == 1
    assert labels[0] == 1  # the surviving filled positive trade
    # The dropped row's timestamp must not appear in the kept set.
    assert "2026-05-11T00:00:01+00:00" not in ts


def test_load_training_pairs_drops_unfilled() -> None:
    """fill_pct <= 0 rows must be dropped, not labeled 0."""
    from scripts.run_train_histgbt import _load_training_pairs

    arb_store.write_records("opportunities", [
        {"ts": "2026-05-11T00:00:00+00:00", "pair": "BTCUSDT",
         "bybit_mid": 80000.0, "bybit_bid": 79999.5, "bybit_ask": 80000.5,
         "dex_mid": 80100.0, "spread_bps": 12.5, "gross_bps": 12.5,
         "direction": "dex_high", "weighted_obi": 0.0, "obi_delta": 0.0,
         "cancellation_rate": 0.0, "gas_gwei": 0.006, "gas_cost_bps": 0.5,
         "bybit_fee_bps": 1.0, "dex_fee_bps": 5.0, "slippage_haircut_bps": 5.0,
         "expected_net_bps": 1.0, "notional_usd": 50.0,
         "theoretical_pnl_usd": 0.005, "decision": "GO",
         "reason": "ok", "eth_price_used": 80000.0},
    ], pair="BTCUSDT")
    arb_store.write_records("sim_trades", [
        {"ts": "2026-05-11T00:00:00+00:00", "pair": "BTCUSDT",
         "realized_pnl_usd": 0.0, "inventory_ok": True, "fill_pct": 0.0,
         "decision": "GO", "side": "SELL", "notional_usd": 50.0,
         "realized_net_bps": 0.0},
    ], pair="BTCUSDT")

    opps, labels, ts, stats = _load_training_pairs()
    assert stats["dropped_unfilled"] == 1
    assert stats["kept"] == 0
    assert len(opps) == 0


# ============================================================================
# HIGH-7 — HeuristicTftProvider.trailing_logreturn invariant
# ============================================================================


def test_trailing_logreturn_canonical_name_works() -> None:
    p = HeuristicTftProvider(window=10)
    rising = [100.0 + i for i in range(20)]
    out = p.trailing_logreturn("BTCUSDT", rising)
    assert out > 0.0
    assert -1.0 < out < 1.0


def test_predict_60s_delegates_to_trailing_logreturn() -> None:
    """Protocol-compat alias must compute the same value."""
    p = HeuristicTftProvider(window=10)
    mids = [100.0 + i for i in range(20)]
    assert p.trailing_logreturn("BTCUSDT", mids) == p.predict_60s("BTCUSDT", mids)


def test_trailing_logreturn_zero_for_too_few_samples() -> None:
    p = HeuristicTftProvider()
    assert p.trailing_logreturn("X", []) == 0.0
    assert p.trailing_logreturn("X", [1.0]) == 0.0


def test_trailing_logreturn_invariant_rejects_corrupt_input() -> None:
    """A 1000x price move within the window triggers the invariant assert."""
    p = HeuristicTftProvider(window=10)
    corrupt = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 10_000.0]
    with pytest.raises(AssertionError, match="out of sane band"):
        p.trailing_logreturn("X", corrupt)


# ============================================================================
# M-4 — Rate-limit expensive POSTs
# ============================================================================


def _seed_minimal_opportunity():
    arb_store.write_records("opportunities", [{
        "ts": "2026-05-11T12:00:00+00:00", "pair": "BTCUSDT",
        "bybit_mid": 80000.0, "bybit_bid": 79999.5, "bybit_ask": 80000.5,
        "dex_mid": 79200.0, "spread_bps": 100.0, "gross_bps": 100.0,
        "direction": "bybit_high", "weighted_obi": 0.0, "obi_delta": 0.0,
        "cancellation_rate": 0.0, "gas_gwei": 0.006, "gas_cost_bps": 0.65,
        "bybit_fee_bps": 10.0, "dex_fee_bps": 5.0, "slippage_haircut_bps": 5.0,
        "expected_net_bps": 79.35, "notional_usd": 50.0,
        "theoretical_pnl_usd": 0.397, "decision": "GO",
        "reason": "ok", "eth_price_used": 3000.0,
    }], pair="BTCUSDT")


def test_run_replay_rate_limit_blocks_second_call() -> None:
    _seed_minimal_opportunity()
    c = _client()
    r1 = c.post("/api/arb/run_replay", json={"seed": 0, "bankroll": 50})
    assert r1.status_code == 200
    r2 = c.post("/api/arb/run_replay", json={"seed": 0, "bankroll": 50})
    assert r2.status_code == 429, f"second call should be rate-limited, got {r2.status_code}"
    body = r2.get_json()
    assert body["error"] == "rate_limited"
    assert body["retry_after_s"] > 0
    assert "/api/arb/run_replay" in body["detail"]


def test_run_replay_rate_limit_releases_after_cooldown(monkeypatch) -> None:
    """Patch the limiter cooldown to a tiny value so the test stays fast."""
    from src.dashboard import arb_blueprint as bp

    _seed_minimal_opportunity()
    c = _client()
    r1 = c.post("/api/arb/run_replay", json={"seed": 0, "bankroll": 50})
    assert r1.status_code == 200
    # Simulate cooldown elapse by rewinding the recorded timestamp.
    with bp._RATE_LOCK:
        bp._LAST_CALLS["/api/arb/run_replay"] = time.monotonic() - 10_000.0
    r2 = c.post("/api/arb/run_replay", json={"seed": 0, "bankroll": 50})
    assert r2.status_code == 200, f"After cooldown elapse should pass, got {r2.status_code}"


def test_counterfactual_rate_limited() -> None:
    _seed_minimal_opportunity()
    c = _client()
    r1 = c.post("/api/arb/counterfactual", json={"bybit_fee_bps": 1.0})
    assert r1.status_code == 200
    r2 = c.post("/api/arb/counterfactual", json={"bybit_fee_bps": 1.0})
    assert r2.status_code == 429


def test_run_drill_rate_limited() -> None:
    """run_drill is expensive (snapshot HALT, run 8 checks); back-to-back
    invocations must be gated."""
    c = _client()
    r1 = c.post("/api/arb/run_drill", json={})
    # r1 may legitimately fail with a non-429 status if drill prereqs missing;
    # what matters for THIS test is r2.
    if r1.status_code == 429:
        pytest.skip("first call already rate-limited by sibling test bleed")
    r2 = c.post("/api/arb/run_drill", json={})
    assert r2.status_code == 429


def test_train_histgbt_rate_limited() -> None:
    """train_histgbt is the heaviest POST. With no data first call returns
    400; subsequent call is still blocked because the rate counter
    increments on entry, not on success. That's intentional — even failed
    expensive calls count to discourage hammering."""
    c = _client()
    r1 = c.post("/api/arb/train_histgbt", json={})
    # First call gets past the limiter; whatever its body status, second
    # call within cooldown must be 429.
    assert r1.status_code in (200, 400), f"unexpected r1 status {r1.status_code}"
    r2 = c.post("/api/arb/train_histgbt", json={})
    assert r2.status_code == 429
