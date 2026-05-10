"""
Multi-relay broadcast (Phase 8 upgrade to MEV-lite).

Replaces the single-relay PrivateRpcRouter with a parallel submitter that
fires the same signed tx to multiple relays simultaneously. The first
inclusion wins (others see "already known" and discard). Better inclusion
rate at the cost of a small fan-out overhead.

Activation criterion (per Plan §5 Phase 8 exit): only enabled if Phase-5
logs show single-relay inclusion < 95% on the primary. Otherwise stay
single-relay.

Default relays for Base:
  - Flashbots Protect    (rpc.flashbots.net/fast)
  - MEV Blocker         (rpc.mevblocker.io)
  - bloXroute Protect   (rpc.bloxroute.com/protect)
  - Eden Network        (api.edennetwork.io/v1/rpc)
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence

from src.exec.private_rpc_router import PrivateRpcRouter, SubmissionResult
from src.utils import config

log = logging.getLogger(__name__)


DEFAULT_MAINNET_RELAYS: tuple[str, ...] = (
    "https://rpc.flashbots.net/fast",
    "https://rpc.mevblocker.io",
    "https://rpc.bloxroute.com/protect",
    "https://api.edennetwork.io/v1/rpc",
)


@dataclass
class RelayStats:
    """Per-relay inclusion counters for the relay-selection learner.
    Phase 8.X: weighted relay choice based on these stats."""
    submissions: int = 0
    successes: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.submissions == 0:
            return 1.0
        return self.successes / self.submissions

    @property
    def avg_latency_ms(self) -> float:
        if self.submissions == 0:
            return 0.0
        return self.total_latency_ms / self.submissions


@dataclass
class MultiRelaySubmitter:
    """
    Fires the same signed-tx hex to N relays in parallel via threads.
    Returns the FIRST successful submission, plus per-relay stats.

    Phase 8 default = mainnet relays. SHADOW returns mock from each.
    TESTNET = single-relay public path (multi-relay is overkill on testnet).
    """
    mode: str | None = None
    relays: Sequence[str] = ()
    timeout_per_relay_s: float = 3.0
    stats: dict[str, RelayStats] = field(default_factory=lambda: defaultdict(RelayStats))

    def __post_init__(self) -> None:
        if self.mode is None:
            self.mode = config.EXECUTION_MODE
        if not self.relays:
            self.relays = self._default_relays_for_mode()

    def _default_relays_for_mode(self) -> tuple[str, ...]:
        if self.mode == config.MODE_MAINNET:
            return DEFAULT_MAINNET_RELAYS
        if self.mode == config.MODE_TESTNET:
            return ("https://sepolia.base.org",)
        return ("shadow://relay1", "shadow://relay2")

    def submit(self, signed_tx_hex: str) -> SubmissionResult:
        """
        Returns the first successful SubmissionResult from any relay.
        If ALL relays fail, returns the last error.
        """
        if len(self.relays) == 1:
            return self._submit_one(self.relays[0], signed_tx_hex)

        results: list[SubmissionResult] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.relays)) as ex:
            future_to_relay = {
                ex.submit(self._submit_one, relay, signed_tx_hex): relay
                for relay in self.relays
            }
            for future in concurrent.futures.as_completed(
                future_to_relay, timeout=self.timeout_per_relay_s + 1.0
            ):
                try:
                    res = future.result()
                except Exception as e:
                    relay = future_to_relay[future]
                    res = SubmissionResult(
                        tx_hash=None, relay=relay, submitted_at_ts=time.time(),
                        status="error", error=f"{type(e).__name__}: {e}",
                    )
                results.append(res)
                if res.status in ("submitted", "shadow"):
                    return res
        # All failed — return the last error.
        return results[-1] if results else SubmissionResult(
            tx_hash=None, relay="", submitted_at_ts=time.time(),
            status="error", error="no_relays",
        )

    def _submit_one(self, relay_url: str, signed_tx_hex: str) -> SubmissionResult:
        t0 = time.time()
        router = PrivateRpcRouter(mode=self.mode, relay_url=relay_url)
        res = router.submit_signed_tx(signed_tx_hex)
        latency_ms = (time.time() - t0) * 1000.0
        stats = self.stats[relay_url]
        stats.submissions += 1
        stats.total_latency_ms += latency_ms
        if res.status in ("submitted", "shadow"):
            stats.successes += 1
        else:
            stats.errors += 1
        return res

    def stats_summary(self) -> dict[str, dict]:
        return {r: {"submissions": s.submissions, "successes": s.successes,
                    "errors": s.errors,
                    "success_rate": round(s.success_rate, 4),
                    "avg_latency_ms": round(s.avg_latency_ms, 2)}
                for r, s in self.stats.items()}
