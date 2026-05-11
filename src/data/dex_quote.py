"""
DEX price reference via Uniswap V3 pool slot0().

Phase 1 = read-only price reference for spread detection. We read sqrtPriceX96
directly from the canonical Uniswap V3 pool on Base for each pilot pair. This
gives mid-price without size impact, which is exactly what we want for
"is there a spread vs Bybit?" detection.

Phase 5 will add a separate executor-side quote (1inch / 0x router) that
includes size impact for the actual swap. Mid-price (this module) and
execution price (Phase 5) are correctly different things.

Why slot0() and not 1inch:
- No API key needed
- On-chain ground truth (the actual pool the DEX swap will hit)
- Single eth_call, fast
- 1inch public v5.2 was deprecated 2024; v6 requires a dev-portal key

Pool addresses (Uniswap V3 on Base):
  WETH/USDC 0.05%  0xd0b53D9277642d899DF5C87A3966A349A798F224
  cbBTC/USDC 0.05% 0x4e962BB3889Bf030368F56810A9c96B83CB3E778  (cbBTC = Coinbase wrapped BTC, deepest BTC pool on Base)
  wSOL/USDC 0.3%   resolved at startup via the factory if address unknown

USDC on Base: 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 (decimals=6)
WETH on Base: 0x4200000000000000000000000000000000000006 (decimals=18)
cbBTC on Base: 0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf (decimals=8)

For sqrtPriceX96 → price conversion:
  price_token1_per_token0 = (sqrtPriceX96 / 2^96)^2 * 10^(d0 - d1)
where d0, d1 are token decimals and tokens are sorted by address (token0 < token1).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from web3 import AsyncWeb3, AsyncHTTPProvider

from src.utils import config

log = logging.getLogger(__name__)

# slot0() function selector — returns (uint160 sqrtPriceX96, int24 tick, ...)
SLOT0_SELECTOR = bytes.fromhex("3850c7bd")

TWO_POW_96 = 2 ** 96


@dataclass(frozen=True)
class PoolConfig:
    """
    Pool config in semantic (base, quote) terms. Inversion vs token0/token1
    is handled by DexPriceReader so the rest of the system always sees
    mid_price = "quote per base" matching the Bybit pair convention
    (BTCUSDT means USD per BTC).
    """
    pool_address: str
    base_symbol: str       # what we're pricing, e.g. "WETH", "cbBTC"
    quote_symbol: str      # what we're pricing in, e.g. "USDC"
    base_decimals: int
    quote_decimals: int
    base_is_token0: bool   # True if base-token address < quote-token address
    fee_bps: int           # pool fee in bps (500 = 0.05%, 3000 = 0.30%)
    bybit_pair: str
    base_address: str = ""    # Phase 5.X — token contract address on Base
    quote_address: str = ""   # Phase 5.X — token contract address on Base


# Canonical token addresses on Base mainnet (lowercase per checksum-insensitive ABI use)
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH_BASE = "0x4200000000000000000000000000000000000006"
CBBTC_BASE = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
AERO_BASE = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"  # Aerodrome native

# Canonical Uniswap V3 pools on Base. Address ordering verified 2026-05-10.
# WETH (0x42..) < USDC (0x83..)  → WETH is token0
# USDC (0x83..) < cbBTC (0xcb..) → USDC is token0
PILOT_POOLS: dict[str, PoolConfig] = {
    "ETHUSDT": PoolConfig(
        pool_address="0xd0b53D9277642d899DF5C87A3966A349A798F224",
        base_symbol="WETH",
        quote_symbol="USDC",
        base_decimals=18,
        quote_decimals=6,
        base_is_token0=True,    # WETH < USDC
        fee_bps=500,
        bybit_pair="ETHUSDT",
        base_address=WETH_BASE,
        quote_address=USDC_BASE,
    ),
    "BTCUSDT": PoolConfig(
        # cbBTC on Base — Coinbase Wrapped BTC, deeper than WBTC on Base.
        pool_address="0x4e962BB3889Bf030368F56810A9c96B83CB3E778",
        base_symbol="cbBTC",
        quote_symbol="USDC",
        base_decimals=8,
        quote_decimals=6,
        base_is_token0=False,   # USDC < cbBTC, so we invert
        fee_bps=500,
        bybit_pair="BTCUSDT",
        base_address=CBBTC_BASE,
        quote_address=USDC_BASE,
    ),
    # SOLUSDT pool address is volatile (multiple bridged-Solana issuers).
    # Phase 1.X: resolve via factory at startup.
    # AEROUSDT: pool address 0x82321f3BEB69f503380D6B233857d5C43562e2D0
    # was returning garbage on first live run (-4 trillion bps spread). Either
    # wrong address or decimal-orientation bug. Disabled until verified via
    # Uniswap V3 factory.getPool(AERO, USDC, 10000) lookup. The opportunity
    # detector now has IMPLAUSIBLE_SPREAD_BPS guard so even if reactivated
    # with a wrong address, it can't produce fake GO signals.
    # "AEROUSDT": PoolConfig(...),
}


@dataclass(frozen=True)
class DexQuote:
    """DEX mid-price reference per pool."""
    ts_ms: int
    pair: str
    pool_address: str
    sqrt_price_x96: int
    mid_price: float          # quote-token per base-token (e.g. USDC per WETH)
    fee_bps: int
    source: str = "uniswap_v3_slot0"


def sqrt_price_x96_to_mid(
    sqrt_price_x96: int,
    token0_decimals: int,
    token1_decimals: int,
) -> float:
    """
    Convert Uniswap V3 sqrtPriceX96 to a human-readable price (token1 per token0).

    Formula:  price = (sqrtPriceX96 / 2^96)^2 * 10^(d0 - d1)
    """
    if sqrt_price_x96 == 0:
        return 0.0
    ratio = sqrt_price_x96 / TWO_POW_96
    raw = ratio * ratio
    return raw * (10 ** (token0_decimals - token1_decimals))


class DexPriceReader:
    """
    Reads mid-price for each configured pool every poll_interval_s seconds.

    Usage:
        reader = DexPriceReader()
        await reader.start()
        async for quote in reader.stream():
            handle(quote)
        await reader.stop()
    """

    def __init__(
        self,
        rpc_url: str = config.BASE_RPC_URL,
        poll_interval_s: float = config.DEX_QUOTE_POLL_INTERVAL_S,
        pools: dict[str, PoolConfig] | None = None,
    ) -> None:
        self.rpc_url = rpc_url
        self.poll_interval_s = poll_interval_s
        self.pools = pools if pools is not None else PILOT_POOLS
        self._w3: AsyncWeb3 | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._w3 = AsyncWeb3(AsyncHTTPProvider(self.rpc_url))
        self._stop.clear()

    async def stop(self) -> None:
        self._stop.set()
        if self._w3 is not None:
            try:
                provider = self._w3.provider
                if hasattr(provider, "disconnect"):
                    await provider.disconnect()
            except Exception:
                pass

    async def read_pool(self, cfg: PoolConfig) -> DexQuote | None:
        if self._w3 is None:
            return None
        try:
            raw = await self._w3.eth.call({
                "to": self._w3.to_checksum_address(cfg.pool_address),
                "data": "0x" + SLOT0_SELECTOR.hex(),
            })
            # slot0() returns (uint160, int24, uint16, uint16, uint16, uint8, bool)
            # uint160 sqrtPriceX96 is in the first 32-byte slot, right-aligned.
            if len(raw) < 32:
                return None
            sqrt_price_x96 = int.from_bytes(raw[:32], byteorder="big", signed=False)
            # Compute raw "token1 per token0" in human units.
            if cfg.base_is_token0:
                d0, d1 = cfg.base_decimals, cfg.quote_decimals
            else:
                d0, d1 = cfg.quote_decimals, cfg.base_decimals
            raw_mid = sqrt_price_x96_to_mid(sqrt_price_x96, d0, d1)
            # Normalize to "quote per base" regardless of token0/token1 order.
            if cfg.base_is_token0:
                mid = raw_mid
            else:
                mid = (1.0 / raw_mid) if raw_mid > 0 else 0.0
            return DexQuote(
                ts_ms=int(time.time() * 1000),
                pair=cfg.bybit_pair,
                pool_address=cfg.pool_address,
                sqrt_price_x96=sqrt_price_x96,
                mid_price=mid,
                fee_bps=cfg.fee_bps,
            )
        except Exception as e:
            log.debug("slot0 read failed for %s (%s): %s", cfg.bybit_pair, cfg.pool_address, e)
            return None

    async def stream(self):
        if self._w3 is None:
            await self.start()
        while not self._stop.is_set():
            tasks = [self.read_pool(cfg) for cfg in self.pools.values()]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, DexQuote):
                    yield r
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_s)
            except asyncio.TimeoutError:
                pass


# Backwards-compatible alias (older code may import DexQuotePoller)
DexQuotePoller = DexPriceReader
