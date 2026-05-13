"""
GoPlus token-security scanner (Phase 9, conditional).

ONLY activates for tokens outside the MAJORS_ALLOWLIST. Pilot pairs
(BTC/ETH/SOL with USDC quote) are all majors — Phase 9 is dormant for
the default config but ready to wire in once we add long-tail tokens.

API: https://gopluslabs.io/v1/token_security/<chain_id>?contract_addresses=...
Free tier: ~30 req/min; we cache aggressively.

Reads:
  - is_honeypot           (1 = honeypot, refuse)
  - is_blacklisted        (caller blacklisted, refuse)
  - cannot_buy / cannot_sell (refuse)
  - trust_score           (block if < TRUST_SCORE_MIN)
  - owner permissions     (mintable, pausable, blacklist_owner — flags)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from src.utils import config

log = logging.getLogger(__name__)

GOPLUS_BASE = "https://api.gopluslabs.io/api/v1/token_security"

# Tokens that skip the scanner entirely — known-safe majors. Add/remove as
# the pair set evolves. Lower-case for case-insensitive comparison.
MAJORS_ALLOWLIST: frozenset[str] = frozenset({
    # Base mainnet token addresses
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913".lower(),  # USDC
    "0x4200000000000000000000000000000000000006".lower(),  # WETH
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf".lower(),  # cbBTC
})

TRUST_SCORE_MIN = 80
CACHE_TTL_S = 3600        # 1 hour for clean / safe results
ERROR_CACHE_TTL_S = 60    # 1 minute for fail-closed errors
# P3-D1 (2026-05-11): error results MUST have a short TTL. Pre-fix, a 1h API
# blip blocked all non-major tokens for a full hour because the fail-closed
# is_safe=False got cached at the same TTL as legit clean results.


@dataclass(frozen=True)
class ScanResult:
    address: str
    is_safe: bool
    reason: str
    trust_score: int | None
    is_honeypot: bool | None
    raw: dict | None


class GoPlusScanner:
    """
    Async-friendly scanner with in-memory TTL cache.

    Use:
        scanner = GoPlusScanner()
        result = await scanner.scan(token_addr, chain_id=8453)
        if not result.is_safe: refuse
    """

    def __init__(
        self,
        chain_id: int = config.BASE_CHAIN_ID,
        api_key: str | None = None,
        cache_ttl_s: float = CACHE_TTL_S,
    ) -> None:
        self.chain_id = chain_id
        self.api_key = api_key
        self.cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, ScanResult]] = {}

    @staticmethod
    def is_major(address: str) -> bool:
        return address.lower() in MAJORS_ALLOWLIST

    def _cache_get(self, key: str) -> ScanResult | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, res, ttl = entry
        if time.time() - ts > ttl:
            del self._cache[key]
            return None
        return res

    def _cache_put(self, key: str, res: ScanResult) -> None:
        # P3-D1: clean results get the full TTL (1h); error / unsafe results
        # get a short TTL (60s) so a transient API outage doesn't block a
        # legit token for a full hour.
        ttl = self.cache_ttl_s if res.is_safe else ERROR_CACHE_TTL_S
        self._cache[key] = (time.time(), res, ttl)

    async def scan(self, token_address: str) -> ScanResult:
        addr = token_address.lower()
        if self.is_major(addr):
            return ScanResult(addr, True, "in_majors_allowlist", None, None, None)

        cached = self._cache_get(addr)
        if cached is not None:
            return cached

        url = f"{GOPLUS_BASE}/{self.chain_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, params={"contract_addresses": addr},
                                          headers=headers)
            if resp.status_code != 200:
                # API failure → fail-CLOSED (refuse to trade unknown tokens)
                res = ScanResult(addr, False, f"goplus_http_{resp.status_code}",
                                  None, None, None)
                self._cache_put(addr, res)
                return res
            body = resp.json()
            data = (body.get("result") or {}).get(addr) or {}
            res = self._parse(addr, data)
            self._cache_put(addr, res)
            return res
        except Exception as e:
            log.warning("GoPlus scan failed for %s: %s", addr, e)
            # Fail-closed on network errors too.
            return ScanResult(addr, False, f"network_error: {type(e).__name__}",
                               None, None, None)

    def _parse(self, addr: str, data: dict) -> ScanResult:
        is_honeypot = bool(int(data.get("is_honeypot", "0") or 0))
        is_blacklisted = bool(int(data.get("is_blacklisted", "0") or 0))
        cannot_buy = bool(int(data.get("cannot_buy", "0") or 0))
        cannot_sell_all = bool(int(data.get("cannot_sell_all", "0") or 0))
        trust_score = data.get("trust_score")
        try:
            trust_score = int(trust_score) if trust_score is not None else None
        except Exception:
            trust_score = None

        if is_honeypot:
            return ScanResult(addr, False, "honeypot", trust_score, True, data)
        if is_blacklisted:
            return ScanResult(addr, False, "blacklisted", trust_score, False, data)
        if cannot_buy or cannot_sell_all:
            return ScanResult(addr, False, "cannot_buy_or_sell", trust_score, False, data)
        if trust_score is not None and trust_score < TRUST_SCORE_MIN:
            return ScanResult(addr, False, f"trust_score_low_{trust_score}",
                               trust_score, False, data)
        return ScanResult(addr, True, "clean", trust_score, False, data)
