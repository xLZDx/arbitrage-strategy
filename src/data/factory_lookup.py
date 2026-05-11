"""
Uniswap V3 factory.getPool(tokenA, tokenB, fee) lookup.

Used at startup to resolve pools dynamically instead of hardcoding pool
addresses (which drift when issuers migrate). Resolves for any (tokenA,
tokenB, fee_tier) tuple — caller picks the fee tier with deepest liquidity.

Function selector for getPool(address,address,uint24):
  keccak256("getPool(address,address,uint24)")[:4] = 0x1698ee82
"""

from __future__ import annotations

import logging
from typing import Final

from web3 import Web3, HTTPProvider  # type: ignore

from src.utils import config

log = logging.getLogger(__name__)

# Uniswap V3 factory address — same on Base, Arbitrum, Optimism, mainnet.
UNISWAP_V3_FACTORY: Final[str] = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"

GET_POOL_SELECTOR: Final[bytes] = bytes.fromhex("1698ee82")
ZERO_ADDRESS: Final[str] = "0x0000000000000000000000000000000000000000"


def _pad_address(addr: str) -> bytes:
    """Address → 32-byte left-padded for ABI encoding."""
    raw = bytes.fromhex(addr.lower().replace("0x", ""))
    return raw.rjust(32, b"\x00")


def _pad_uint24(value: int) -> bytes:
    """uint24 → 32-byte big-endian."""
    if value < 0 or value > 0xFFFFFF:
        raise ValueError(f"value {value} out of uint24 range")
    return value.to_bytes(32, byteorder="big")


def get_pool_address(
    token_a: str,
    token_b: str,
    fee_bps: int,
    rpc_url: str | None = None,
) -> str | None:
    """
    Returns the deployed pool address (lowercase 0x-prefixed) for
    (token_a, token_b, fee_bps), or None if no pool exists.

    fee_bps values: 100 (0.01%), 500 (0.05%), 3000 (0.30%), 10000 (1.00%)
    """
    rpc = rpc_url or config.BASE_RPC_URL
    try:
        w3 = Web3(HTTPProvider(rpc))
        data = (GET_POOL_SELECTOR
                + _pad_address(token_a)
                + _pad_address(token_b)
                + _pad_uint24(fee_bps))
        result = w3.eth.call({
            "to": w3.to_checksum_address(UNISWAP_V3_FACTORY),
            "data": "0x" + data.hex(),
        })
        if len(result) < 32:
            return None
        # Last 20 bytes of the 32-byte response are the address
        addr_bytes = result[-20:]
        addr = "0x" + addr_bytes.hex()
        if addr.lower() == ZERO_ADDRESS:
            return None
        return addr.lower()
    except Exception as e:
        log.warning("factory.getPool(%s, %s, %d) failed: %s",
                    token_a, token_b, fee_bps, e)
        return None


def find_deepest_pool(
    token_a: str,
    token_b: str,
    rpc_url: str | None = None,
    fee_tiers: tuple[int, ...] = (500, 3000, 10000, 100),
) -> tuple[str, int] | None:
    """
    Tries each fee tier in order; returns the first pool found as
    (pool_address, fee_bps). For "deepest" we'd actually need to query
    pool.liquidity() — Phase X.X. For now first-found is good enough
    since tiers are tried in roughly liquidity-likelihood order.
    """
    for fee in fee_tiers:
        addr = get_pool_address(token_a, token_b, fee, rpc_url)
        if addr is not None:
            return addr, fee
    return None
