"""
Flashbots / private-bundle signer + submitter.

Phase 5 SHADOW: returns a mock signed-tx hex.
Phase 5.X (live testnet/mainnet): signs the prepared swap with the wallet
private key and submits via PrivateRpcRouter.

Why a separate module from PrivateRpcRouter:
  - router.submit_signed_tx() takes a *signed* hex; this module produces it.
  - keeps the "what we submit" / "how we submit" concerns separate.
  - allows future Phase 8 multi-relay broadcast to plug in cleanly.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass

from src.exec.multi_relay import MultiRelaySubmitter
from src.exec.private_rpc_router import PrivateRpcRouter, SubmissionResult
from src.exec.wallet_signer import WalletSigner
from src.utils import config

log = logging.getLogger(__name__)


@dataclass
class FlashbotsExecutor:
    """Signs PreparedSwap → submits via PrivateRpcRouter (single-relay) OR
    MultiRelaySubmitter (Phase 8 — when config.MULTI_RELAY=True)."""
    router: PrivateRpcRouter
    signer: WalletSigner | None = None
    multi_relay: MultiRelaySubmitter | None = None
    mode: str | None = None
    wallet_private_key_env: str = "BASE_WALLET_PRIVATE_KEY"
    use_multi_relay: bool | None = None  # None → follow config.MULTI_RELAY

    def __post_init__(self) -> None:
        if self.mode is None:
            self.mode = self.router.mode
        if self.signer is None:
            self.signer = WalletSigner(
                mode=self.mode,
                private_key_env=self.wallet_private_key_env,
            )
        if self.use_multi_relay is None:
            self.use_multi_relay = config.MULTI_RELAY
        if self.use_multi_relay and self.multi_relay is None:
            self.multi_relay = MultiRelaySubmitter(mode=self.mode)

    def sign_and_submit(self, prepared_swap, nonce: int | None = None) -> SubmissionResult:
        """
        Signs the prepared swap with the wallet key and submits via the
        configured router (single relay) or multi-relay broadcaster.

        SHADOW: signer returns deterministic mock hex; submitter returns
                "shadow" SubmissionResult with mock tx_hash.
        TESTNET/MAINNET: signer needs BASE_WALLET_PRIVATE_KEY env var.
        """
        if self.mode != config.MODE_SHADOW:
            priv = os.environ.get(self.wallet_private_key_env)
            if not priv:
                return SubmissionResult(
                    tx_hash=None, relay=self.router.relay_url,
                    submitted_at_ts=0.0, status="error",
                    error=f"{self.wallet_private_key_env} not set",
                )
        try:
            signed = self.signer.sign_swap(prepared_swap, nonce=nonce)
        except Exception as e:
            log.exception("sign failed")
            return SubmissionResult(
                tx_hash=None, relay=self.router.relay_url,
                submitted_at_ts=0.0, status="error",
                error=f"sign_error: {type(e).__name__}: {e}",
            )
        if self.use_multi_relay and self.multi_relay is not None:
            return self.multi_relay.submit(signed.raw_hex)
        return self.router.submit_signed_tx(signed.raw_hex)

    def relay_stats(self) -> dict:
        """Phase 8 inclusion-rate telemetry. Empty in single-relay mode."""
        if self.multi_relay is not None:
            return self.multi_relay.stats_summary()
        return {}
