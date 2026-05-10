"""
Bundle simulator — MANDATORY pre-send check (per Q1 MEV-lite spec).

Calls eth_call against Base RPC to dry-run the prepared swap. If it
reverts, the coordinator MUST abort. This is the single most important
safety check between Phase 5 and Phase 8.

Per the configuration:
  SHADOW   — return success with mock gas estimate (no RPC call).
  TESTNET  — call testnet RPC.
  MAINNET  — call mainnet RPC (read-only; no signing).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.utils import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SimulationResult:
    success: bool
    gas_used: int
    revert_reason: str | None
    mode: str

    @property
    def passed(self) -> bool:
        return self.success and self.revert_reason is None


class BundleSimulator:
    DEFAULT_GAS_LIMIT = 350_000  # generous for V3 single-pool swap

    def __init__(
        self,
        mode: str | None = None,
        rpc_url: str | None = None,
    ) -> None:
        self.mode = mode or config.EXECUTION_MODE
        self.rpc_url = rpc_url or config.BASE_RPC_URL
        self._w3 = None

    def _ensure_w3(self):
        if self._w3 is None and self.mode != config.MODE_SHADOW:
            from web3 import Web3, HTTPProvider  # type: ignore
            self._w3 = Web3(HTTPProvider(self.rpc_url))
        return self._w3

    def simulate(self, prepared_swap) -> SimulationResult:
        """
        prepared_swap: PreparedSwap from src.exec.dex_leg.

        SHADOW: returns success with the prepared_swap's expected gas, no
        RPC call (the underlying calldata is mock anyway).

        Otherwise: eth_call against Base RPC; on revert returns success=False
        with the revert reason in revert_reason.
        """
        if self.mode == config.MODE_SHADOW or getattr(prepared_swap, "is_shadow", False):
            return SimulationResult(
                success=True, gas_used=self.DEFAULT_GAS_LIMIT,
                revert_reason=None, mode=self.mode,
            )

        try:
            w3 = self._ensure_w3()
            if w3 is None:
                return SimulationResult(False, 0, "web3_client_unavailable", self.mode)
            tx = {
                "to": prepared_swap.router_address,
                "from": prepared_swap.to_address,
                "data": "0x" + prepared_swap.data_hex,
                "value": prepared_swap.value_wei,
                "gas": self.DEFAULT_GAS_LIMIT,
            }
            # eth_call returns the would-be return data; raises on revert.
            result = w3.eth.call(tx)  # type: ignore
            # Estimate gas separately — eth_call doesn't always return it.
            try:
                gas = int(w3.eth.estimate_gas(tx))  # type: ignore
            except Exception:
                gas = self.DEFAULT_GAS_LIMIT
            return SimulationResult(
                success=True, gas_used=gas,
                revert_reason=None, mode=self.mode,
            )
        except Exception as e:
            reason = self._extract_revert_reason(e)
            log.warning("bundle simulation reverted: %s", reason)
            return SimulationResult(
                success=False, gas_used=0,
                revert_reason=reason, mode=self.mode,
            )

    def _extract_revert_reason(self, e: Exception) -> str:
        msg = str(e)
        # web3.py raises ContractLogicError with "execution reverted: <reason>"
        if "execution reverted" in msg.lower():
            parts = msg.split("execution reverted")
            if len(parts) > 1:
                tail = parts[1].lstrip(" :;,")
                return tail[:200] if tail else "execution_reverted_no_reason"
            return "execution_reverted"
        return f"{type(e).__name__}: {msg[:200]}"
