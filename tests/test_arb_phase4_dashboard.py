"""
Phase 4 — /api/arb/risk dashboard endpoint test.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.dashboard.app_arb import create_app
from src.risk import limits as risk


def setup_function(_):
    risk.halt_clear()


def teardown_function(_):
    risk.halt_clear()


def test_risk_endpoint_returns_200_when_clean() -> None:
    client = create_app().test_client()
    r = client.get("/api/arb/risk")
    assert r.status_code == 200
    body = r.get_json()
    assert body["halt_active"] is False
    assert body["preflight_ok"] is True
    assert body["preflight_decision"] == "OK"


def test_risk_endpoint_reports_halt_when_set() -> None:
    risk.halt_set("dashboard test")
    client = create_app().test_client()
    r = client.get("/api/arb/risk")
    body = r.get_json()
    assert body["halt_active"] is True
    assert body["preflight_ok"] is False
    assert body["preflight_decision"] == "HALT"
    assert "dashboard test" in (body["halt_reason"] or "")


def test_risk_endpoint_includes_caps() -> None:
    client = create_app().test_client()
    body = client.get("/api/arb/risk").get_json()
    assert "daily_loss_cap_usd" in body
    assert "drawdown_trigger_usd" in body
    assert "per_trade_cap_usd" in body
    assert body["daily_loss_cap_usd"] > 0
    assert body["drawdown_trigger_usd"] > body["daily_loss_cap_usd"]


def test_risk_endpoint_includes_services() -> None:
    client = create_app().test_client()
    body = client.get("/api/arb/risk").get_json()
    names = {s["name"] for s in body["services"]}
    assert names == {"ingestion", "dashboard"}


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
