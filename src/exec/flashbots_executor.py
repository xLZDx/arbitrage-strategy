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

from src.exec.private_rpc_router import PrivateRpcRouter, SubmissionResult
from src.utils import config

log = logging.getLogger(__name__)


@dataclass
class FlashbotsExecutor:
    router: PrivateRpcRouter
    mode: str | None = None
    wallet_private_key_env: str = "BASE_WALLET_PRIVATE_KEY"

    def __post_init__(self) -> None:
        if self.mode is None:
            self.mode = self.router.mode

    def sign_and_submit(self, prepared_swap, nonce: int | None = None) -> SubmissionResult:
        """
        Signs the prepared swap with the wallet key and submits via the
        configured router.

        SHADOW: returns mock submission, no signing.
        TESTNET/MAINNET: requires BASE_WALLET_PRIVATE_KEY env var. Lifted
        per-call (NOT cached) so a key rotation takes effect immediately.
        """
        if self.mode == config.MODE_SHADOW:
            return self.router.submit_signed_tx("0x" + secrets.token_hex(80))

        priv = os.environ.get(self.wallet_private_key_env)
        if not priv:
            return SubmissionResult(
                tx_hash=None, relay=self.router.relay_url,
                submitted_at_ts=0.0, status="error",
                error=f"{self.wallet_private_key_env} not set",
            )

        # Deferred to Phase 5.X (needs PoolConfig token-address fields,
        # gas estimation, EIP-1559 fee setup, nonce management).
        return SubmissionResult(
            tx_hash=None, relay=self.router.relay_url,
            submitted_at_ts=0.0, status="error",
            error="live_signing_not_implemented_phase_5x",
        )
