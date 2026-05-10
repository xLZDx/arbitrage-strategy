"""
Phase 9 — GoPlus security scanner tests.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx

from src.security.goplus_scanner import (
    GoPlusScanner, MAJORS_ALLOWLIST, ScanResult, TRUST_SCORE_MIN,
)


def test_majors_allowlist_short_circuits_no_network() -> None:
    """Major tokens skip the API entirely."""
    s = GoPlusScanner()
    res = asyncio.run(s.scan("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"))  # USDC
    assert res.is_safe
    assert res.reason == "in_majors_allowlist"


def test_is_major_case_insensitive() -> None:
    assert GoPlusScanner.is_major("0x833589FCD6EDB6E08F4C7C32D4F71B54BDA02913")
    assert not GoPlusScanner.is_major("0x000000000000000000000000000000000000dead")


def _mock_resp(status: int, body: dict) -> object:
    class _R:
        status_code = status
        def json(self):
            return body
    return _R()


def _addr() -> str:
    return "0x000000000000000000000000000000000000beef"


def test_clean_token_is_safe() -> None:
    body = {"result": {_addr(): {
        "is_honeypot": "0", "is_blacklisted": "0",
        "cannot_buy": "0", "cannot_sell_all": "0",
        "trust_score": "95",
    }}}
    s = GoPlusScanner()
    with patch("httpx.AsyncClient.get",
                AsyncMock(return_value=_mock_resp(200, body))):
        res = asyncio.run(s.scan(_addr()))
    assert res.is_safe
    assert res.reason == "clean"
    assert res.trust_score == 95


def test_honeypot_blocks() -> None:
    body = {"result": {_addr(): {"is_honeypot": "1", "trust_score": "10"}}}
    s = GoPlusScanner()
    with patch("httpx.AsyncClient.get",
                AsyncMock(return_value=_mock_resp(200, body))):
        res = asyncio.run(s.scan(_addr()))
    assert not res.is_safe
    assert res.reason == "honeypot"
    assert res.is_honeypot is True


def test_blacklist_blocks() -> None:
    body = {"result": {_addr(): {"is_honeypot": "0", "is_blacklisted": "1",
                                    "trust_score": "60"}}}
    s = GoPlusScanner()
    with patch("httpx.AsyncClient.get",
                AsyncMock(return_value=_mock_resp(200, body))):
        res = asyncio.run(s.scan(_addr()))
    assert not res.is_safe
    assert res.reason == "blacklisted"


def test_low_trust_score_blocks() -> None:
    body = {"result": {_addr(): {"is_honeypot": "0", "trust_score": str(TRUST_SCORE_MIN - 5)}}}
    s = GoPlusScanner()
    with patch("httpx.AsyncClient.get",
                AsyncMock(return_value=_mock_resp(200, body))):
        res = asyncio.run(s.scan(_addr()))
    assert not res.is_safe
    assert "trust_score_low" in res.reason


def test_cannot_sell_blocks() -> None:
    body = {"result": {_addr(): {"cannot_sell_all": "1", "trust_score": "90"}}}
    s = GoPlusScanner()
    with patch("httpx.AsyncClient.get",
                AsyncMock(return_value=_mock_resp(200, body))):
        res = asyncio.run(s.scan(_addr()))
    assert not res.is_safe
    assert "cannot_buy_or_sell" in res.reason


def test_api_error_fails_closed() -> None:
    """Non-200 → refuse the trade. Conservative default."""
    s = GoPlusScanner()
    with patch("httpx.AsyncClient.get",
                AsyncMock(return_value=_mock_resp(500, {}))):
        res = asyncio.run(s.scan(_addr()))
    assert not res.is_safe
    assert "goplus_http_500" in res.reason


def test_network_error_fails_closed() -> None:
    s = GoPlusScanner()
    with patch("httpx.AsyncClient.get",
                AsyncMock(side_effect=httpx.ConnectError("nope"))):
        res = asyncio.run(s.scan(_addr()))
    assert not res.is_safe
    assert "network_error" in res.reason


def test_cache_hit_skips_second_call() -> None:
    body = {"result": {_addr(): {"is_honeypot": "0", "trust_score": "95"}}}
    s = GoPlusScanner()
    mock_get = AsyncMock(return_value=_mock_resp(200, body))
    with patch("httpx.AsyncClient.get", mock_get):
        asyncio.run(s.scan(_addr()))
        asyncio.run(s.scan(_addr()))
    # Only one HTTP call thanks to cache
    assert mock_get.call_count == 1


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
