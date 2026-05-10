"""
Uniswap V3 SwapRouter02 calldata encoder.

Builds ABI-encoded calldata for exactInputSingle:

  function exactInputSingle(ExactInputSingleParams calldata params)
      external payable returns (uint256 amountOut);

  struct ExactInputSingleParams {
      address tokenIn;
      address tokenOut;
      uint24 fee;
      address recipient;
      uint256 amountIn;
      uint256 amountOutMinimum;
      uint160 sqrtPriceLimitX96;
  }

Function selector for exactInputSingle((address,address,uint24,address,uint256,uint256,uint160)):
  keccak256("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
  = 0x04e45aaf

This encoding is deterministic — given the same inputs you always get the
same calldata bytes. Tests pin known input → known output so refactors
can't silently break the on-chain ABI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from eth_abi import encode  # type: ignore
from eth_utils import to_canonical_address  # type: ignore

# Function selector — keccak256(...)[:4]
EXACT_INPUT_SINGLE_SELECTOR: Final[bytes] = bytes.fromhex("04e45aaf")


@dataclass(frozen=True)
class ExactInputSingleParams:
    token_in: str            # 0x... address
    token_out: str
    fee: int                 # uint24, in 1/10000% units (500 = 0.05%, 3000 = 0.30%)
    recipient: str
    amount_in: int           # uint256, in token's smallest unit
    amount_out_minimum: int  # uint256
    sqrt_price_limit_x96: int = 0  # uint160; 0 = no limit


def encode_exact_input_single(params: ExactInputSingleParams) -> bytes:
    """
    Returns the FULL calldata: selector + ABI-encoded tuple. Hex-encode
    via .hex() for transmission.
    """
    if params.fee < 0 or params.fee > 0xFFFFFF:
        raise ValueError(f"fee {params.fee} out of uint24 range")
    if params.amount_in < 0 or params.amount_out_minimum < 0:
        raise ValueError("amount fields must be non-negative")
    if params.sqrt_price_limit_x96 < 0 or params.sqrt_price_limit_x96 > (2 ** 160 - 1):
        raise ValueError("sqrtPriceLimitX96 out of uint160 range")

    encoded_struct = encode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"],
        [(
            to_canonical_address(params.token_in),
            to_canonical_address(params.token_out),
            params.fee,
            to_canonical_address(params.recipient),
            params.amount_in,
            params.amount_out_minimum,
            params.sqrt_price_limit_x96,
        )],
    )
    return EXACT_INPUT_SINGLE_SELECTOR + encoded_struct


def encode_exact_input_single_hex(params: ExactInputSingleParams) -> str:
    """Returns the calldata as a 0x-prefixed hex string."""
    return "0x" + encode_exact_input_single(params).hex()
