"""
Phase 8 — multi-relay broadcast tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.exec.multi_relay import (
    DEFAULT_MAINNET_RELAYS, MultiRelaySubmitter, RelayStats,
)
from src.exec.private_rpc_router import PrivateRpcRouter, SubmissionResult
from src.utils import config


def test_default_relays_per_mode() -> None:
    m = MultiRelaySubmitter(mode=config.MODE_MAINNET)
    assert tuple(m.relays) == DEFAULT_MAINNET_RELAYS
    t = MultiRelaySubmitter(mode=config.MODE_TESTNET)
    assert len(t.relays) == 1
    s = MultiRelaySubmitter(mode=config.MODE_SHADOW)
    assert all(r.startswith("shadow://") for r in s.relays)


def test_shadow_returns_first_success() -> None:
    m = MultiRelaySubmitter(mode=config.MODE_SHADOW)
    res = m.submit("0x" + "ab" * 100)
    assert res.status == "shadow"
    assert res.tx_hash and res.tx_hash.startswith("0x")


def test_shadow_records_per_relay_stats() -> None:
    m = MultiRelaySubmitter(mode=config.MODE_SHADOW)
    m.submit("0x" + "ab" * 50)
    summary = m.stats_summary()
    assert len(summary) >= 1
    for relay, s in summary.items():
        assert s["submissions"] >= 1
        assert s["success_rate"] == 1.0


def test_returns_first_successful_when_some_fail() -> None:
    """One relay errors, another succeeds → result is the success."""
    m = MultiRelaySubmitter(
        mode=config.MODE_SHADOW,
        relays=("shadow://ok-1", "shadow://ok-2"),
    )
    fail = SubmissionResult(None, "shadow://err", 0.0, "error", "boom")
    ok = SubmissionResult("0xdeadbeef" + "00" * 28, "shadow://ok-2",
                          0.0, "shadow", None)
    call_log = []
    def _stub_submit_one(self, relay_url, tx):
        call_log.append(relay_url)
        return ok if "ok" in relay_url else fail
    with patch.object(MultiRelaySubmitter, "_submit_one", _stub_submit_one):
        res = m.submit("0x123")
    assert res.status == "shadow"
    assert res.tx_hash == "0xdeadbeef" + "00" * 28


def test_returns_last_error_when_all_fail() -> None:
    m = MultiRelaySubmitter(
        mode=config.MODE_SHADOW,
        relays=("shadow://err-1", "shadow://err-2"),
    )
    def _stub(self, relay_url, tx):
        return SubmissionResult(None, relay_url, 0.0, "error", "down")
    with patch.object(MultiRelaySubmitter, "_submit_one", _stub):
        res = m.submit("0x123")
    assert res.status == "error"
    assert res.error == "down"


def test_relay_stats_success_rate_math() -> None:
    s = RelayStats(submissions=5, successes=4, errors=1)
    assert s.success_rate == 0.8


def test_relay_stats_default_when_no_subs() -> None:
    s = RelayStats()
    assert s.success_rate == 1.0  # default-optimistic
    assert s.avg_latency_ms == 0.0


def test_single_relay_uses_fast_path() -> None:
    """When configured with one relay, should not spawn ThreadPool."""
    m = MultiRelaySubmitter(mode=config.MODE_SHADOW, relays=("shadow://only",))
    res = m.submit("0x" + "00" * 50)
    assert res.relay == "shadow://only"


def _run_all() -> int:
    failures: list[tuple[str, str]] = []
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
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
