"""
Phase 6 tests — feature pipeline + HistGBT trainer + load/save +
coordinator veto integration.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from src.exec.bybit_leg import BybitLegExecutor
from src.exec.bundle_simulator import BundleSimulator
from src.exec.coordinator import ArbCoordinator
from src.exec.dex_leg import DexLegExecutor
from src.exec.private_rpc_router import PrivateRpcRouter
from src.ml.feature_pipeline import (
    FEATURE_COLUMNS, extract_features, feature_columns,
    label_from_sim_trade, stack_features,
)
from src.ml.hist_gbt import (
    DEFAULT_VETO_THRESHOLD, HistGBTArtifact, load_artifact, save_artifact,
    train_histgbt,
)
from src.risk import limits as risk
from src.sim.inventory import Inventory
from src.utils import config


_TEST_LEDGER = config.DATA_DIR / "_test_p6_idempotency.json"


def setup_function(_):
    risk.halt_clear()
    if _TEST_LEDGER.exists():
        _TEST_LEDGER.unlink()


def teardown_function(_):
    risk.halt_clear()
    if _TEST_LEDGER.exists():
        _TEST_LEDGER.unlink()


def _opp(spread=20.0, obi=0.1, gas=0.006,
         expected_net_bps=12.0, ts="2026-05-11T12:00:00+00:00",
         pair="BTCUSDT", direction="bybit_high", notional=50.0):
    return {
        "ts": ts, "pair": pair, "decision": "GO",
        "direction": direction, "spread_bps": spread,
        "gross_bps": abs(spread), "weighted_obi": obi, "obi_delta": 0.01,
        "cancellation_rate": 0.0,
        "gas_gwei": gas, "gas_cost_bps": 0.65,
        "slippage_haircut_bps": 5.0,
        "expected_net_bps": expected_net_bps, "notional_usd": notional,
        "bybit_mid": 80000.0, "dex_mid": 79900.0,
        "theoretical_pnl_usd": 0.05,
    }


# --- feature_pipeline -----------------------------------------------------


def test_feature_columns_phase6_excludes_tft() -> None:
    cols = feature_columns(include_tft=False)
    assert "tft_60s_pred" not in cols
    assert cols == FEATURE_COLUMNS


def test_feature_columns_phase7_appends_tft() -> None:
    cols = feature_columns(include_tft=True)
    assert cols[-1] == "tft_60s_pred"
    assert len(cols) == len(FEATURE_COLUMNS) + 1


def test_extract_features_returns_correct_dim() -> None:
    f = extract_features(_opp())
    assert f.shape == (len(FEATURE_COLUMNS),)


def test_extract_features_with_tft_appends() -> None:
    f = extract_features(_opp(), tft_60s_pred=0.42)
    assert f.shape == (len(FEATURE_COLUMNS) + 1,)
    assert f[-1] == 0.42


def test_extract_features_handles_missing_keys() -> None:
    """An opportunity dict with missing optional fields shouldn't crash."""
    minimal = {"ts": "2026-05-11T10:00:00+00:00", "pair": "BTCUSDT",
               "decision": "GO", "direction": "bybit_high", "notional_usd": 50.0}
    f = extract_features(minimal)
    assert f.shape == (len(FEATURE_COLUMNS),)
    assert not np.any(np.isnan(f))


def test_extract_features_direction_encoding() -> None:
    fbh = extract_features(_opp(direction="bybit_high"))
    fdh = extract_features(_opp(direction="dex_high"))
    cols = feature_columns()
    is_bybit_idx = cols.index("is_bybit_high")
    assert fbh[is_bybit_idx] == 1.0
    assert fdh[is_bybit_idx] == 0.0


def test_extract_features_hour_minute_parsing() -> None:
    f = extract_features(_opp(ts="2026-05-11T14:37:00+00:00"))
    cols = feature_columns()
    assert f[cols.index("hour_of_day")] == 14
    assert f[cols.index("minute_of_hour")] == 37


def test_extract_features_log_notional_grows() -> None:
    f1 = extract_features(_opp(notional=10.0))
    f2 = extract_features(_opp(notional=10000.0))
    cols = feature_columns()
    log_idx = cols.index("log_notional")
    assert f2[log_idx] > f1[log_idx]


def test_stack_features_basic() -> None:
    opps = [_opp() for _ in range(5)]
    X = stack_features(opps)
    assert X.shape == (5, len(FEATURE_COLUMNS))


def test_stack_features_empty() -> None:
    X = stack_features([])
    assert X.shape[0] == 0


def test_stack_features_tft_length_mismatch_raises() -> None:
    try:
        stack_features([_opp()] * 3, tft_preds=[0.1, 0.2])
    except ValueError as e:
        assert "length" in str(e).lower()
        return
    assert False


# --- label_from_sim_trade -------------------------------------------------


def test_label_positive_when_pnl_positive_and_filled() -> None:
    trade = {"realized_pnl_usd": 0.05, "inventory_ok": True, "fill_pct": 1.0}
    assert label_from_sim_trade(trade) == 1


def test_label_zero_when_pnl_negative() -> None:
    trade = {"realized_pnl_usd": -0.05, "inventory_ok": True, "fill_pct": 1.0}
    assert label_from_sim_trade(trade) == 0


def test_label_zero_when_inventory_rejected() -> None:
    """Even with positive PnL, if inv_ok=False the trade didn't happen."""
    trade = {"realized_pnl_usd": 0.10, "inventory_ok": False, "fill_pct": 0.0}
    assert label_from_sim_trade(trade) == 0


def test_label_zero_when_unfilled() -> None:
    trade = {"realized_pnl_usd": 0.10, "inventory_ok": True, "fill_pct": 0.0}
    assert label_from_sim_trade(trade) == 0


# --- train_histgbt --------------------------------------------------------


def _toy_dataset(n: int = 60):
    """Synthetic features where a simple linear combo predicts the label.
    Makes the test deterministic and the AUC well > random."""
    rng = np.random.RandomState(0)
    n_features = len(FEATURE_COLUMNS)
    X = rng.randn(n, n_features)
    # label depends on first feature (spread) AND third (obi)
    score = 0.6 * X[:, 0] + 0.4 * X[:, 2] + rng.randn(n) * 0.3
    y = (score > 0).astype(np.int32)
    timestamps = [f"2026-05-11T{i // 60:02d}:{i % 60:02d}:00+00:00"
                   for i in range(n)]
    return X, y, timestamps


def test_train_histgbt_returns_artifact() -> None:
    X, y, ts = _toy_dataset()
    art = train_histgbt(X, y, timestamps=ts, n_estimators=50)
    assert isinstance(art, HistGBTArtifact)
    assert art.n_train + art.n_holdout == len(X)
    assert art.feature_columns == FEATURE_COLUMNS


def test_train_histgbt_holdout_auc_above_random() -> None:
    X, y, ts = _toy_dataset(n=100)
    art = train_histgbt(X, y, timestamps=ts, n_estimators=100)
    assert art.holdout_auc > 0.6, f"toy AUC too low: {art.holdout_auc}"


def test_train_histgbt_rejects_single_class() -> None:
    X, _, ts = _toy_dataset()
    y = np.zeros(len(X), dtype=np.int32)  # all zeros
    try:
        train_histgbt(X, y, timestamps=ts)
    except ValueError as e:
        assert "both classes" in str(e)
        return
    assert False


def test_train_histgbt_rejects_too_few_samples() -> None:
    X = np.random.randn(10, len(FEATURE_COLUMNS))
    y = np.array([0, 1] * 5)
    try:
        train_histgbt(X, y, n_estimators=10)
    except ValueError as e:
        assert ">= 20" in str(e)
        return
    assert False


def test_predict_proba_shape() -> None:
    X, y, ts = _toy_dataset()
    art = train_histgbt(X, y, timestamps=ts, n_estimators=50)
    p = art.predict_proba(X[:5])
    assert p.shape == (5,)
    assert ((p >= 0.0) & (p <= 1.0)).all()


def test_predict_proba_dim_mismatch_raises() -> None:
    X, y, ts = _toy_dataset()
    art = train_histgbt(X, y, timestamps=ts, n_estimators=50)
    bad = np.zeros((1, len(FEATURE_COLUMNS) - 2))
    try:
        art.predict_proba(bad)
    except ValueError as e:
        assert "feature dim mismatch" in str(e)
        return
    assert False


def test_veto_returns_decision_and_p() -> None:
    X, y, ts = _toy_dataset()
    art = train_histgbt(X, y, timestamps=ts, n_estimators=50,
                         veto_threshold=0.50)
    vetoed, p = art.veto(X[0])
    assert isinstance(vetoed, bool)
    assert 0.0 <= p <= 1.0
    assert vetoed == (p < 0.50)


# --- save / load round-trip ----------------------------------------------


def test_save_load_roundtrip() -> None:
    X, y, ts = _toy_dataset()
    art = train_histgbt(X, y, timestamps=ts, n_estimators=50)
    with tempfile.NamedTemporaryFile(suffix=".joblib", delete=False) as f:
        tmp_path = Path(f.name)
    try:
        save_artifact(art, tmp_path)
        loaded = load_artifact(tmp_path)
        assert loaded is not None
        assert loaded.holdout_auc == art.holdout_auc
        # Inference matches
        p_orig = art.predict_proba(X[:3])
        p_load = loaded.predict_proba(X[:3])
        np.testing.assert_array_almost_equal(p_orig, p_load)
    finally:
        tmp_path.unlink(missing_ok=True)


def test_load_artifact_returns_none_when_missing() -> None:
    assert load_artifact(Path("/tmp/definitely-does-not-exist.joblib")) is None


# --- coordinator veto integration ----------------------------------------


def _coord(histgbt=None, required=False, inventory=None):
    return ArbCoordinator(
        bybit=BybitLegExecutor(mode=config.MODE_SHADOW, ledger_path=_TEST_LEDGER),
        dex=DexLegExecutor(mode=config.MODE_SHADOW),
        router=PrivateRpcRouter(mode=config.MODE_SHADOW),
        simulator=BundleSimulator(mode=config.MODE_SHADOW),
        inventory=inventory or Inventory.with_balanced_seed(500.0),
        risk_state=risk.RiskState(),
        histgbt=histgbt,
        histgbt_required=required,
    )


def _good_opp():
    return {
        "ts": "2026-05-11T12:00:00+00:00", "pair": "BTCUSDT",
        "decision": "GO", "direction": "bybit_high", "notional_usd": 50.0,
        "expected_net_bps": 20.0, "theoretical_pnl_usd": 0.10,
        "bybit_mid": 80000.0, "dex_mid": 79900.0,
        "spread_bps": 12.5, "gross_bps": 12.5,
        "weighted_obi": 0.1, "obi_delta": 0.01, "cancellation_rate": 0.0,
        "gas_gwei": 0.006, "gas_cost_bps": 0.65,
        "slippage_haircut_bps": 5.0,
    }


def test_coordinator_no_model_means_no_veto_field() -> None:
    coord = _coord(histgbt=None)
    rec = coord.attempt(_good_opp())
    assert rec.outcome == "shadow"
    assert rec.histgbt_vetoed is False
    assert rec.histgbt_p is None


def test_coordinator_required_but_missing_rejects() -> None:
    coord = _coord(histgbt=None, required=True)
    rec = coord.attempt(_good_opp())
    assert rec.outcome == "rejected_preflight"
    assert "histgbt_required_but_missing" in rec.reason


def test_coordinator_model_high_proba_passes() -> None:
    """A trained model that returns high P should NOT veto."""
    X, y, ts = _toy_dataset()
    art = train_histgbt(X, y, timestamps=ts, n_estimators=50,
                         veto_threshold=0.0)  # never vetoes
    coord = _coord(histgbt=art)
    rec = coord.attempt(_good_opp())
    assert rec.outcome == "shadow"
    assert rec.histgbt_p is not None
    assert rec.histgbt_vetoed is False


def test_coordinator_model_low_proba_vetoes() -> None:
    X, y, ts = _toy_dataset()
    art = train_histgbt(X, y, timestamps=ts, n_estimators=50,
                         veto_threshold=1.01)  # always vetoes (P never > 1)
    coord = _coord(histgbt=art)
    rec = coord.attempt(_good_opp())
    assert rec.outcome == "rejected_preflight"
    assert "histgbt_veto" in rec.reason
    assert rec.histgbt_vetoed is True


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
