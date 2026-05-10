"""
Base L2 gas oracle.

Polls Base RPC every config.GAS_POLL_INTERVAL_S (~one block) for:
- baseFeePerGas (latest block, EIP-1559)
- estimated priorityFeePerGas (eth_maxPriorityFeePerGas)
- block number

Used by:
- Phase 2 opportunity detector: gas as a feature
- Phase 5 executor: minimum bribe floor
- Phase 6 HistGBT: gas as a feature

The Base public RPC has rate limits. Production should use Alchemy / Infura /
QuickNode with an API key in BASE_RPC_URL.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from web3 import AsyncWeb3, AsyncHTTPProvider

from src.utils import config

log = logging.getLogger(__name__)

WEI_PER_GWEI = 10 ** 9


@dataclass(frozen=True)
class GasReading:
    ts_ms: int
    block_number: int
    base_fee_gwei: float
    priority_fee_gwei: float
    total_gas_price_gwei: float

    def estimate_swap_cost_usd(
        self,
        gas_units: int,
        eth_price_usd: float = 3000.0,  # rough; replaced with live in Phase 2
    ) -> float:
        wei = (self.base_fee_gwei + self.priority_fee_gwei) * WEI_PER_GWEI
        eth = (wei * gas_units) / 10 ** 18
        return eth * eth_price_usd


class GasOracle:
    """
    Async gas oracle. Holds last reading; consumers read via .latest().

    Usage:
        oracle = GasOracle()
        await oracle.start()
        ...
        reading = oracle.latest()
        ...
        await oracle.stop()
    """

    def __init__(
        self,
        rpc_url: str = config.BASE_RPC_URL,
        poll_interval_s: float = config.GAS_POLL_INTERVAL_S,
    ) -> None:
        self.rpc_url = rpc_url
        self.poll_interval_s = poll_interval_s
        self._w3: AsyncWeb3 | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._latest: GasReading | None = None

    async def start(self) -> None:
        self._w3 = AsyncWeb3(AsyncHTTPProvider(self.rpc_url))
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="gas_oracle")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        if self._w3 is not None:
            try:
                provider = self._w3.provider
                if hasattr(provider, "disconnect"):
                    await provider.disconnect()
            except Exception:
                pass

    def latest(self) -> GasReading | None:
        return self._latest

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                reading = await self._poll_once()
                if reading is not None:
                    self._latest = reading
            except Exception as e:
                log.warning("gas poll failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_s)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self) -> GasReading | None:
        if self._w3 is None:
            return None
        block = await self._w3.eth.get_block("latest")
        base_fee_wei = int(block.get("baseFeePerGas", 0))
        try:
            priority_wei = int(await self._w3.eth.max_priority_fee)
        except Exception:
            priority_wei = 0
        return GasReading(
            ts_ms=int(time.time() * 1000),
            block_number=int(block["number"]),
            base_fee_gwei=base_fee_wei / WEI_PER_GWEI,
            priority_fee_gwei=priority_wei / WEI_PER_GWEI,
            total_gas_price_gwei=(base_fee_wei + priority_wei) / WEI_PER_GWEI,
        )
