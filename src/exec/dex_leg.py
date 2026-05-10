"""
DEX swap leg builder.

Builds a Uniswap V3 ExactInputSingle swap on Base with mandatory
amountOutMin and deadline. Does NOT submit the tx itself — that goes
through private_rpc_router + flashbots_executor.

Modes:
  SHADOW  — return mock prepared transaction; no real building.
  TESTNET — build a real tx targeting Base Sepolia (or whatever testnet
            we configure). Requires BASE_TESTNET_RPC_URL + wallet key.
  MAINNET — build a real tx targeting Base mainnet. Requires
            BASE_RPC_URL + wallet key + ARB_MAINNET_GATE=1.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Literal

from src.utils import config

log = logging.getLogger(__name__)

DirectionT = Literal["buy", "sell"]   # buy = USDC -> base; sell = base -> USDC

# Uniswap V3 SwapRouter02 on Base mainnet
SWAP_ROUTER_BASE = "0x2626664c2603336E57B271c5C0b26F421741e481"
DEFAULT_DEADLINE_S = 60   # tx must mine within this many seconds


@dataclass(frozen=True)
class PreparedSwap:
    """Container for a built (but not yet signed/sent) Uniswap V3 swap."""
    pair: str                  # Bybit symbol e.g. "BTCUSDT"
    direction: DirectionT
    src_token: str
    dst_token: str
    src_amount_human: float
    amount_in_wei: int
    amount_out_min_wei: int
    deadline_unix: int
    fee_tier_bps: int
    pool_address: str
    router_address: str
    chain_id: int
    to_address: str
    data_hex: str              # ABI-encoded calldata (hex without 0x)
    value_wei: int
    mode: str
    is_shadow: bool


class DexLegExecutor:
    def __init__(
        self,
        mode: str | None = None,
        slippage_tolerance_bps: float = 30.0,
        wallet_address: str | None = None,
    ) -> None:
        self.mode = mode or config.EXECUTION_MODE
        self.slippage_tolerance_bps = float(slippage_tolerance_bps)
        if self.mode == config.MODE_MAINNET:
            self._assert_mainnet_gate_open()
        self.wallet_address = wallet_address or os.environ.get(
            f"BASE_{self.mode}_WALLET_ADDRESS",
            os.environ.get("BASE_WALLET_ADDRESS"),
        )

    def _assert_mainnet_gate_open(self) -> None:
        if os.environ.get("ARB_MAINNET_GATE") != "1":
            raise RuntimeError(
                "Mainnet DEX execution refused: ARB_MAINNET_GATE=1 not set."
            )

    def build_swap(
        self,
        pair: str,
        direction: DirectionT,
        notional_usd: float,
        live_mid_price: float,
        pool_cfg,
    ) -> PreparedSwap:
        """
        Builds (does not submit) a swap.

        notional_usd: USD-equivalent value to swap (e.g. 50.0).
        live_mid_price: current pool mid (USD per base, e.g. $80,000 per BTC).
        pool_cfg: PoolConfig from src.data.dex_quote.PILOT_POOLS[pair]
        """
        if direction == "buy":
            src_token, dst_token = pool_cfg.quote_symbol, pool_cfg.base_symbol
            src_decimals = pool_cfg.quote_decimals
            dst_decimals = pool_cfg.base_decimals
            src_amount_human = notional_usd
            expected_out_human = notional_usd / live_mid_price
        elif direction == "sell":
            src_token, dst_token = pool_cfg.base_symbol, pool_cfg.quote_symbol
            src_decimals = pool_cfg.base_decimals
            dst_decimals = pool_cfg.quote_decimals
            src_amount_human = notional_usd / live_mid_price
            expected_out_human = notional_usd
        else:
            raise ValueError(f"direction must be buy/sell, got {direction}")

        amount_in_wei = int(round(src_amount_human * (10 ** src_decimals)))
        # amountOutMin = expected * (1 - tolerance). Mandatory revert protection.
        tolerance_mult = 1.0 - (self.slippage_tolerance_bps / 10_000.0)
        amount_out_min_wei = int(round(expected_out_human * tolerance_mult
                                        * (10 ** dst_decimals)))

        deadline_unix = int(time.time()) + DEFAULT_DEADLINE_S

        if self.mode == config.MODE_SHADOW:
            return PreparedSwap(
                pair=pair, direction=direction,
                src_token=src_token, dst_token=dst_token,
                src_amount_human=src_amount_human,
                amount_in_wei=amount_in_wei,
                amount_out_min_wei=amount_out_min_wei,
                deadline_unix=deadline_unix,
                fee_tier_bps=pool_cfg.fee_bps,
                pool_address=pool_cfg.pool_address,
                router_address=SWAP_ROUTER_BASE,
                chain_id=config.BASE_CHAIN_ID,
                to_address=self.wallet_address or "0x0000000000000000000000000000000000000000",
                data_hex="00" * 32,  # mock calldata
                value_wei=0,
                mode=self.mode,
                is_shadow=True,
            )

        # TESTNET / MAINNET: build real ABI-encoded calldata for
        # SwapRouter02.exactInputSingle((tokenIn, tokenOut, fee, recipient,
        # amountIn, amountOutMinimum, sqrtPriceLimitX96)).
        from eth_abi import encode  # type: ignore

        if not self.wallet_address:
            raise RuntimeError(
                f"BASE_{self.mode}_WALLET_ADDRESS not set; cannot build swap"
            )

        # Token address resolution would normally happen via PILOT_POOLS,
        # but we don't have the token addresses on PoolConfig in Phase 1.
        # Phase 5.X: extend PoolConfig with token addresses, or look up
        # from a constants table here. For now, fail loudly if invoked.
        raise NotImplementedError(
            "live (testnet/mainnet) calldata building requires Phase-5.X "
            "PoolConfig token-address fields. SHADOW mode is fully working."
        )
