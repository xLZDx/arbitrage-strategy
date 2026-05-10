"""
EIP-1559 transaction signer.

Takes a PreparedSwap, fetches the signer address's nonce + current Base
gas, builds an EIP-1559 transaction, signs it with the wallet private key,
and returns the signed raw-tx hex ready for PrivateRpcRouter.

SHADOW: returns a deterministic mock hex so coordinator tests can run
without any private key.

Live: requires BASE_WALLET_PRIVATE_KEY env var; never logs the key.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass

from src.utils import config

log = logging.getLogger(__name__)

# EIP-1559 fee defaults — tunable per environment.
PRIORITY_FEE_GWEI_DEFAULT = 0.001  # very small on Base
FEE_BUMP_MULTIPLIER = 2.0          # max_fee = 2x current base + priority


@dataclass(frozen=True)
class SignedTx:
    raw_hex: str
    chain_id: int
    nonce: int | None
    gas_limit: int
    max_fee_per_gas_wei: int
    max_priority_fee_per_gas_wei: int
    from_address: str | None
    to_address: str
    is_shadow: bool


class WalletSigner:
    """
    EIP-1559 signer for Uniswap V3 swap calls.

    Constructor doesn't touch the network or read private keys — both happen
    lazily on first sign() call. This keeps SHADOW mode + tests fast.
    """
    def __init__(
        self,
        mode: str | None = None,
        private_key_env: str = "BASE_WALLET_PRIVATE_KEY",
        rpc_url: str | None = None,
        chain_id: int = config.BASE_CHAIN_ID,
    ) -> None:
        self.mode = mode or config.EXECUTION_MODE
        self.private_key_env = private_key_env
        self.rpc_url = rpc_url or config.BASE_RPC_URL
        self.chain_id = chain_id
        self._account = None
        self._w3 = None

    # ------------------------------------------------------------------

    def _ensure_account(self):
        if self._account is None:
            from eth_account import Account  # type: ignore
            priv = os.environ.get(self.private_key_env)
            if not priv:
                raise RuntimeError(
                    f"{self.private_key_env} not set; cannot sign in {self.mode}"
                )
            self._account = Account.from_key(priv)
        return self._account

    def _ensure_w3(self):
        if self._w3 is None:
            from web3 import Web3, HTTPProvider  # type: ignore
            self._w3 = Web3(HTTPProvider(self.rpc_url))
        return self._w3

    @property
    def address(self) -> str | None:
        """Returns wallet address. None in SHADOW (or before key loaded)."""
        if self.mode == config.MODE_SHADOW:
            return None
        try:
            return self._ensure_account().address
        except Exception:
            return None

    # ------------------------------------------------------------------

    def sign_swap(
        self,
        prepared_swap,
        nonce: int | None = None,
        gas_limit: int | None = None,
    ) -> SignedTx:
        """
        prepared_swap: PreparedSwap from src.exec.dex_leg with .data_hex,
                       .router_address, .chain_id populated.
        nonce / gas_limit: caller may supply; otherwise fetched live.
        """
        if self.mode == config.MODE_SHADOW:
            # Deterministic mock hex (160 hex chars = 80 bytes — looks like
            # a small EIP-1559 raw tx). Stable so tests can pin it.
            seed = (prepared_swap.pair + prepared_swap.direction
                    + str(prepared_swap.amount_in_wei)).encode()
            digest = hashlib.sha256(seed).hexdigest()
            return SignedTx(
                raw_hex="0x02" + digest + digest[:14],  # 160 hex chars
                chain_id=prepared_swap.chain_id,
                nonce=nonce, gas_limit=gas_limit or 250_000,
                max_fee_per_gas_wei=0,
                max_priority_fee_per_gas_wei=0,
                from_address=prepared_swap.to_address,
                to_address=prepared_swap.router_address,
                is_shadow=True,
            )

        account = self._ensure_account()
        w3 = self._ensure_w3()
        from_addr = account.address
        if nonce is None:
            nonce = w3.eth.get_transaction_count(from_addr)  # type: ignore

        # EIP-1559 fee derivation: maxFeePerGas = base * 2 + priority
        latest = w3.eth.get_block("latest")  # type: ignore
        base_fee = int(latest.get("baseFeePerGas", 0))
        priority_wei = int(PRIORITY_FEE_GWEI_DEFAULT * 1e9)
        max_fee_wei = int(base_fee * FEE_BUMP_MULTIPLIER) + priority_wei

        tx = {
            "chainId": prepared_swap.chain_id,
            "type": 2,                        # EIP-1559
            "nonce": nonce,
            "to": prepared_swap.router_address,
            "value": prepared_swap.value_wei,
            "data": ("0x" + prepared_swap.data_hex
                     if not prepared_swap.data_hex.startswith("0x")
                     else prepared_swap.data_hex),
            "gas": gas_limit or 250_000,
            "maxFeePerGas": max_fee_wei,
            "maxPriorityFeePerGas": priority_wei,
        }
        signed = account.sign_transaction(tx)
        # web3 6.x and 7.x both expose .raw_transaction (or .rawTransaction)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
        raw_hex = raw.hex() if isinstance(raw, (bytes, bytearray)) else str(raw)
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        return SignedTx(
            raw_hex=raw_hex,
            chain_id=prepared_swap.chain_id,
            nonce=nonce, gas_limit=tx["gas"],
            max_fee_per_gas_wei=max_fee_wei,
            max_priority_fee_per_gas_wei=priority_wei,
            from_address=from_addr,
            to_address=prepared_swap.router_address,
            is_shadow=False,
        )
