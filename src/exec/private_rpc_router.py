"""
Private-RPC router (MEV-lite per Q1).

Routes DEX transactions to a private RPC instead of the public mempool to
defeat sandwich attacks. Phase-5 default for Base is Flashbots Protect
(works on EVM L2s) — single-relay submission. Phase 8 will add multi-relay
broadcast (Flashbots + MEV Blocker + bloXroute + Eden).

Modes:
  SHADOW   — return mock tx hash; no submission.
  TESTNET  — submit via testnet RPC (public is fine on testnet).
  MAINNET  — submit via Flashbots Protect endpoint.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass

from src.utils import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmissionResult:
    tx_hash: str | None
    relay: str
    submitted_at_ts: float
    status: str         # "submitted", "shadow", "error"
    error: str | None = None


class PrivateRpcRouter:
    """
    Single-relay submitter. Phase 8 upgrade adds parallel multi-relay.
    """

    DEFAULT_MAINNET_RELAY = "https://rpc.flashbots.net/fast"  # Flashbots Protect
    DEFAULT_TESTNET_RELAY = "https://sepolia.base.org"        # public, fine for testnet

    def __init__(
        self,
        mode: str | None = None,
        relay_url: str | None = None,
        rpc_url: str | None = None,
    ) -> None:
        self.mode = mode or config.EXECUTION_MODE
        self.relay_url = relay_url or self._default_relay()
        self.rpc_url = rpc_url or config.BASE_RPC_URL
        self._w3 = None

    def _default_relay(self) -> str:
        if self.mode == config.MODE_MAINNET:
            return self.DEFAULT_MAINNET_RELAY
        if self.mode == config.MODE_TESTNET:
            return self.DEFAULT_TESTNET_RELAY
        return "shadow://no-submission"

    def _ensure_w3(self):
        if self._w3 is None and self.mode in (config.MODE_TESTNET, config.MODE_MAINNET):
            from web3 import Web3, HTTPProvider  # type: ignore
            self._w3 = Web3(HTTPProvider(self.relay_url))
        return self._w3

    def submit_signed_tx(self, signed_tx_hex: str) -> SubmissionResult:
        """
        Submit a pre-signed raw tx hex to the configured relay.

        signed_tx_hex: 0x-prefixed RLP-encoded signed transaction.
        """
        if self.mode == config.MODE_SHADOW:
            mock_hash = "0x" + secrets.token_hex(32)
            log.info("[SHADOW] would submit tx via %s -> %s", self.relay_url, mock_hash)
            return SubmissionResult(
                tx_hash=mock_hash, relay=self.relay_url,
                submitted_at_ts=time.time(), status="shadow",
            )

        try:
            w3 = self._ensure_w3()
            if w3 is None:
                raise RuntimeError("web3 client init failed")
            tx_hash = w3.eth.send_raw_transaction(  # type: ignore
                bytes.fromhex(signed_tx_hex.replace("0x", ""))
            )
            return SubmissionResult(
                tx_hash=tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash),
                relay=self.relay_url,
                submitted_at_ts=time.time(),
                status="submitted",
            )
        except Exception as e:
            log.exception("submit failed via %s", self.relay_url)
            return SubmissionResult(
                tx_hash=None, relay=self.relay_url,
                submitted_at_ts=time.time(), status="error",
                error=f"{type(e).__name__}: {e}",
            )
