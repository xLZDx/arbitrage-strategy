"""
P0-8 (2026-05-11) — functional tests for 7 new dashboard endpoints.

Per CLAUDE.md "Functional Tests Prove Behavior": every test below seeds
real data, hits the endpoint via Flask test_client, and asserts on the
observable behavior — never empty assertions, never just "didn't crash".

Endpoints covered:
  POST /api/arb/halt              {action: set|clear}
  GET/POST /api/arb/maker_mode    {enabled: bool}
  POST /api/arb/counterfactual    {bybit_fee_bps, notional_usd}
  POST /api/arb/run_replay        {seed, bankroll, write}
  POST /api/arb/train_histgbt
  POST /api/arb/run_drill
  GET  /api/arb/soak_summary
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.dashboard.app_arb import create_app
from src.risk import limits as rl
from src.storage import arb_store
from src.utils import config


_TABLES_TOUCHED = ("opportunities", "sim_trades", "trades", "paper_trades",
                    "obi_snapshots", "dex_quotes", "gas_history")


def _cleanup():
    arb_store.close()
    for t in _TABLES_TOUCHED:
        d = arb_store.table_dir(t)
        if d.exists():
            shutil.rmtree(d)


def setup_function(_):
    rl.halt_clear()
    _cleanup()


def teardown_function(_):
    rl.halt_clear()
    _cleanup()
    os.environ.pop("ARB_PREFER_MAKER", None)


def _client():
    return create_app().test_client()


def _seed_opportunity(ts="2026-05-11T12:00:00+00:00", pair="BTCUSDT",
                       spread_bps=100.0, dex_fee_bps=5.0,
                       slippage_haircut_bps=5.0, gas_cost_bps=0.65,
                       decision="GO", notional_usd=50.0):
    bybit_mid = 80000.0
    dex_mid = bybit_mid * (1.0 - spread_bps / 10_000.0)
    expected_net_bps = spread_bps - 10.0 - dex_fee_bps - gas_cost_bps - slippage_haircut_bps
    pnl = notional_usd * expected_net_bps / 10_000.0
    arb_store.write_records("opportunities", [{
        "ts": ts, "pair": pair, "bybit_mid": bybit_mid,
        "bybit_bid": bybit_mid - 0.5, "bybit_ask": bybit_mid + 0.5,
        "dex_mid": dex_mid, "spread_bps": spread_bps, "gross_bps": abs(spread_bps),
        "direction": "bybit_high", "weighted_obi": 0.0, "obi_delta": 0.0,
        "cancellation_rate": 0.0, "gas_gwei": 0.006, "gas_cost_bps": gas_cost_bps,
        "bybit_fee_bps": 10.0, "dex_fee_bps": dex_fee_bps,
        "slippage_haircut_bps": slippage_haircut_bps,
        "expected_net_bps": expected_net_bps, "notional_usd": notional_usd,
        "theoretical_pnl_usd": pnl, "decision": decision,
        "reason": "passes_threshold" if decision == "GO" else "negative_after_costs",
        "eth_price_used": 3000.0,
    }], pair=pair)


# --- /api/arb/halt -------------------------------------------------------


def test_halt_set_creates_flag() -> None:
    r = _client().post("/api/arb/halt",
                        json={"action": "set", "reason": "from test"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["halt_active"] is True
    assert "from test" in (body["reason"] or "")
    assert rl.halt_active() is True


def test_halt_clear_removes_flag() -> None:
    rl.halt_set("preexisting")
    assert rl.halt_active()
    r = _client().post("/api/arb/halt", json={"action": "clear"})
    body = r.get_json()
    assert body["halt_active"] is False
    assert body["cleared"] is True
    assert rl.halt_active() is False


def test_halt_invalid_action_returns_400() -> None:
    r = _client().post("/api/arb/halt", json={"action": "bogus"})
    assert r.status_code == 400
    assert "action" in r.get_json()["error"]


# --- /api/arb/maker_mode -------------------------------------------------


def test_maker_mode_get_default_false() -> None:
    flag = config.DATA_DIR / "MAKER_PREFERRED"
    if flag.exists():
        flag.unlink()
    os.environ.pop("ARB_PREFER_MAKER", None)
    r = _client().get("/api/arb/maker_mode")
    body = r.get_json()
    assert body["enabled"] is False


def test_maker_mode_post_enable_creates_flag() -> None:
    flag = config.DATA_DIR / "MAKER_PREFERRED"
    if flag.exists():
        flag.unlink()
    r = _client().post("/api/arb/maker_mode", json={"enabled": True})
    body = r.get_json()
    assert body["enabled"] is True
    assert body["needs_restart"] is True
    assert flag.exists()
    # cleanup
    flag.unlink()


def test_maker_mode_post_disable_removes_flag() -> None:
    flag = config.DATA_DIR / "MAKER_PREFERRED"
    flag.touch()
    r = _client().post("/api/arb/maker_mode", json={"enabled": False})
    assert r.get_json()["enabled"] is False
    assert not flag.exists()


# --- /api/arb/counterfactual ---------------------------------------------


def test_counterfactual_empty_returns_400() -> None:
    r = _client().post("/api/arb/counterfactual", json={"bybit_fee_bps": 1.0})
    assert r.status_code == 400
    assert "no_opportunities_yet" in r.get_json()["error"]


def test_counterfactual_known_good_math() -> None:
    """Seed one opportunity. With maker fee (1bps), known-good math predicts GO.
    100 bps gross - 1 maker - 5 dex - 0.65 gas - 5 slippage = 88.35 bps net.
    PnL on $50 notional = 50 * 88.35 / 10000 = $0.44175."""
    _seed_opportunity(spread_bps=100.0, notional_usd=50.0)
    r = _client().post("/api/arb/counterfactual",
                        json={"bybit_fee_bps": 1.0, "notional_usd": 50.0})
    assert r.status_code == 200
    body = r.get_json()
    assert body["n_total"] == 1
    assert body["n_go"] == 1, f"Expected 1 GO under maker fees, got body={body}"
    assert body["n_skip"] == 0
    assert abs(body["go_pnl_total"] - 0.44175) < 0.01, (
        f"PnL math regression: got {body['go_pnl_total']}, expected ~0.4418"
    )
    assert body["bybit_fee_bps_used"] == 1.0
    by_pair = {row["pair"]: row for row in body["by_pair"]}
    assert "BTCUSDT" in by_pair
    assert by_pair["BTCUSDT"]["go"] == 1


def test_counterfactual_taker_fees_eliminate_borderline_go() -> None:
    """20 bps gross spread — clears maker cost stack (~12 bps) but
    NOT taker cost stack (~21 bps). Counterfactual must reflect that."""
    _seed_opportunity(spread_bps=20.0, notional_usd=50.0)
    r_maker = _client().post("/api/arb/counterfactual",
                              json={"bybit_fee_bps": 1.0}).get_json()
    r_taker = _client().post("/api/arb/counterfactual",
                              json={"bybit_fee_bps": 10.0}).get_json()
    assert r_maker["n_go"] >= 1
    assert r_taker["n_go"] == 0


# --- /api/arb/run_replay -------------------------------------------------


def test_run_replay_empty_returns_400() -> None:
    r = _client().post("/api/arb/run_replay", json={"write": False})
    assert r.status_code == 400


def test_run_replay_with_go_opportunities() -> None:
    for i in range(3):
        _seed_opportunity(ts=f"2026-05-11T12:00:{i:02d}+00:00",
                           spread_bps=100.0, decision="GO")
    r = _client().post("/api/arb/run_replay",
                        json={"seed": 0, "bankroll": 2000.0, "write": False})
    assert r.status_code == 200
    body = r.get_json()
    assert body["n_opportunities"] == 3
    assert body["n_go"] == 3
    assert body["n_filled"] >= 1
    assert "cumulative_pnl_usd" in body
    assert "sharpe" in body
    assert body["written"] is False


def test_run_replay_write_persists_sim_trades() -> None:
    for i in range(3):
        _seed_opportunity(ts=f"2026-05-11T12:00:{i:02d}+00:00",
                           spread_bps=100.0, decision="GO")
    body = _client().post("/api/arb/run_replay",
                           json={"write": True, "bankroll": 2000.0}).get_json()
    assert body["written"] is True
    # Verify sim_trades got written
    assert arb_store.row_count("sim_trades") >= 1


# --- /api/arb/train_histgbt ----------------------------------------------


def test_train_histgbt_too_few_samples_returns_400() -> None:
    _seed_opportunity()
    # No sim_trades either
    r = _client().post("/api/arb/train_histgbt", json={})
    assert r.status_code == 400
    body = r.get_json()
    assert "need_both" in body["error"] or "too_few_samples" in body["error"]


# --- /api/arb/run_drill --------------------------------------------------


def test_run_drill_returns_eight_checks() -> None:
    r = _client().post("/api/arb/run_drill", json={})
    assert r.status_code == 200
    body = r.get_json()
    assert body["n_total"] == 8
    assert "checks" in body
    assert len(body["checks"]) == 8
    for c in body["checks"]:
        assert "name" in c
        assert "ok" in c
        assert isinstance(c["ok"], bool)


def test_run_drill_preserves_pre_existing_halt() -> None:
    """REGRESSION P0-2: drill must restore live HALT it cleared."""
    rl.halt_set("live operator halt")
    assert rl.halt_active()
    r = _client().post("/api/arb/run_drill", json={})
    assert r.status_code == 200
    # After drill: HALT must STILL be active
    assert rl.halt_active() is True, (
        "REGRESSION: drill cleared an operator-set HALT without restoring it. "
        "This is the kill-switch-bypass-via-UI bug."
    )
    reason = rl.halt_reason()
    assert reason is not None and "live operator halt" in reason


def test_run_drill_no_halt_leaves_no_halt() -> None:
    assert not rl.halt_active()
    _client().post("/api/arb/run_drill", json={})
    assert not rl.halt_active()


def test_run_drill_writes_drill_marker() -> None:
    """REGRESSION P0-1: drill_runs.jsonl must be written so the live-ramp
    guard's freshness check can pass."""
    drill_log = config.LOG_DIR / "drill_runs.jsonl"
    sz_before = drill_log.stat().st_size if drill_log.exists() else 0
    _client().post("/api/arb/run_drill", json={})
    assert drill_log.exists()
    assert drill_log.stat().st_size > sz_before, (
        "Drill must append to drill_runs.jsonl. The endpoint regressed if "
        "this fails (P0-1 NameError on safe_json)."
    )


# --- /api/arb/soak_summary -----------------------------------------------


def test_soak_summary_returns_structured_payload() -> None:
    r = _client().get("/api/arb/soak_summary")
    assert r.status_code == 200
    body = r.get_json()
    assert "tables" in body
    assert "spread_distribution" in body
    assert "decisions" in body
    assert "go_pnl_total" in body
    # Tables key should have entries for each known table (some may be None)
    expected_tables = {"obi_snapshots", "dex_quotes", "gas_history",
                        "opportunities", "sim_trades", "trades", "paper_trades"}
    assert expected_tables.issubset(set(body["tables"].keys()))


def test_soak_summary_aggregates_seeded_opportunities() -> None:
    _seed_opportunity(ts="2026-05-11T10:00:00+00:00", pair="BTCUSDT",
                       spread_bps=100.0, decision="GO")
    _seed_opportunity(ts="2026-05-11T10:00:01+00:00", pair="BTCUSDT",
                       spread_bps=-50.0, decision="SKIP")
    _seed_opportunity(ts="2026-05-11T10:00:00+00:00", pair="ETHUSDT",
                       spread_bps=20.0, decision="GO")
    body = _client().get("/api/arb/soak_summary").get_json()
    assert body["tables"]["opportunities"]["n"] == 3
    decisions = {d["decision"]: d for d in body["decisions"]}
    assert "GO" in decisions
    assert "SKIP" in decisions
    assert decisions["GO"]["n"] == 2
    # Spread distribution should split per pair
    pairs = {row["pair"] for row in body["spread_distribution"]}
    assert {"BTCUSDT", "ETHUSDT"}.issubset(pairs)


def _run_all() -> int:
    failures: list[tuple[str, str]] = []
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            setup_function(None)
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
        finally:
            teardown_function(None)
    print()
    if failures:
        print(f"{len(failures)} / {len(tests)} FAILED")
        return 1
    print(f"{len(tests)} / {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
