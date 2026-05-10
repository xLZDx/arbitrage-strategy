"""
Phase 5.X — calldata encoder + wallet signer + live-mode dex_leg tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.data.dex_quote import (
    CBBTC_BASE, PILOT_POOLS, USDC_BASE, WETH_BASE,
)
from src.exec.calldata import (
    EXACT_INPUT_SINGLE_SELECTOR, ExactInputSingleParams,
    encode_exact_input_single, encode_exact_input_single_hex,
)
from src.exec.dex_leg import DexLegExecutor, SWAP_ROUTER_BASE
from src.exec.wallet_signer import WalletSigner
from src.utils import config


# A throwaway test private key — DO NOT use for anything real.
# Address: 0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf
TEST_PRIVATE_KEY = (
    "0x0000000000000000000000000000000000000000000000000000000000000001"
)
TEST_ADDRESS = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"


def setup_function(_):
    os.environ.pop("BASE_WALLET_PRIVATE_KEY", None)
    os.environ.pop("BASE_WALLET_ADDRESS", None)
    os.environ.pop("ARB_MAINNET_GATE", None)


def teardown_function(_):
    os.environ.pop("BASE_WALLET_PRIVATE_KEY", None)
    os.environ.pop("BASE_WALLET_ADDRESS", None)
    os.environ.pop("ARB_MAINNET_GATE", None)


# --- token addresses on PoolConfig ---------------------------------------


def test_pool_config_has_token_addresses() -> None:
    eth = PILOT_POOLS["ETHUSDT"]
    assert eth.base_address.lower() == WETH_BASE
    assert eth.quote_address.lower() == USDC_BASE
    btc = PILOT_POOLS["BTCUSDT"]
    assert btc.base_address.lower() == CBBTC_BASE
    assert btc.quote_address.lower() == USDC_BASE


# --- calldata encoder ----------------------------------------------------


def test_selector_is_exact_input_single() -> None:
    """Function selector matches Uniswap V3 SwapRouter02.exactInputSingle.
    keccak256("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
    """
    assert EXACT_INPUT_SINGLE_SELECTOR == bytes.fromhex("04e45aaf")


def test_encode_basic_swap_returns_bytes() -> None:
    p = ExactInputSingleParams(
        token_in=USDC_BASE, token_out=WETH_BASE, fee=500,
        recipient=TEST_ADDRESS,
        amount_in=50_000_000,                        # 50 USDC
        amount_out_minimum=int(0.0166 * 1e18),       # ~0.0166 WETH
        sqrt_price_limit_x96=0,
    )
    data = encode_exact_input_single(p)
    # selector (4 bytes) + 7 abi-encoded fields, each 32 bytes = 4 + 224 = 228
    assert len(data) == 228
    assert data[:4] == EXACT_INPUT_SINGLE_SELECTOR


def test_encode_hex_round_trip() -> None:
    p = ExactInputSingleParams(
        token_in=USDC_BASE, token_out=WETH_BASE, fee=500,
        recipient=TEST_ADDRESS,
        amount_in=1_000_000, amount_out_minimum=1, sqrt_price_limit_x96=0,
    )
    h = encode_exact_input_single_hex(p)
    assert h.startswith("0x")
    assert len(h) == 2 + 228 * 2


def test_encode_deterministic() -> None:
    """Same inputs → identical bytes (regression-pin against ABI drift)."""
    kwargs = dict(
        token_in=USDC_BASE, token_out=WETH_BASE, fee=500,
        recipient=TEST_ADDRESS,
        amount_in=1_000_000, amount_out_minimum=1, sqrt_price_limit_x96=0,
    )
    a = encode_exact_input_single(ExactInputSingleParams(**kwargs))
    b = encode_exact_input_single(ExactInputSingleParams(**kwargs))
    assert a == b


def test_encode_rejects_oversized_fee() -> None:
    p = ExactInputSingleParams(
        token_in=USDC_BASE, token_out=WETH_BASE, fee=2 ** 25,  # > uint24
        recipient=TEST_ADDRESS,
        amount_in=1, amount_out_minimum=0,
    )
    try:
        encode_exact_input_single(p)
    except ValueError as e:
        assert "uint24" in str(e)
        return
    assert False


def test_encode_rejects_negative_amount() -> None:
    p = ExactInputSingleParams(
        token_in=USDC_BASE, token_out=WETH_BASE, fee=500,
        recipient=TEST_ADDRESS, amount_in=-1, amount_out_minimum=0,
    )
    try:
        encode_exact_input_single(p)
    except ValueError as e:
        assert "non-negative" in str(e)
        return
    assert False


def test_encode_rejects_oversized_sqrt_price_limit() -> None:
    p = ExactInputSingleParams(
        token_in=USDC_BASE, token_out=WETH_BASE, fee=500,
        recipient=TEST_ADDRESS, amount_in=1, amount_out_minimum=0,
        sqrt_price_limit_x96=2 ** 161,  # > uint160
    )
    try:
        encode_exact_input_single(p)
    except ValueError as e:
        assert "uint160" in str(e)
        return
    assert False


# --- dex_leg live-mode build_swap ---------------------------------------


def test_dex_live_build_swap_produces_real_calldata() -> None:
    os.environ["BASE_WALLET_ADDRESS"] = TEST_ADDRESS
    ex = DexLegExecutor(mode=config.MODE_TESTNET)
    cfg = PILOT_POOLS["ETHUSDT"]
    swap = ex.build_swap(
        pair="ETHUSDT", direction="buy",
        notional_usd=50.0, live_mid_price=3000.0, pool_cfg=cfg,
    )
    assert not swap.is_shadow
    assert swap.router_address == SWAP_ROUTER_BASE
    assert swap.chain_id == config.BASE_CHAIN_ID
    # data_hex starts with the function selector (no 0x prefix)
    assert swap.data_hex.startswith(EXACT_INPUT_SINGLE_SELECTOR.hex())


def test_dex_live_buy_direction_uses_correct_tokens() -> None:
    """buy = USDC → WETH; tokenIn is USDC, tokenOut is WETH."""
    os.environ["BASE_WALLET_ADDRESS"] = TEST_ADDRESS
    ex = DexLegExecutor(mode=config.MODE_TESTNET)
    cfg = PILOT_POOLS["ETHUSDT"]
    swap = ex.build_swap("ETHUSDT", "buy", 50.0, 3000.0, cfg)
    # Verify USDC address is in the encoded calldata as tokenIn
    # First arg position after selector: 4 + 12 (left-pad) = 16, then 20 bytes.
    encoded = bytes.fromhex(swap.data_hex)
    token_in_bytes = encoded[16:36]   # bytes 16..36 are the 20-byte address
    assert token_in_bytes.hex() == USDC_BASE.lower().replace("0x", "")


def test_dex_live_sell_direction_uses_correct_tokens() -> None:
    os.environ["BASE_WALLET_ADDRESS"] = TEST_ADDRESS
    ex = DexLegExecutor(mode=config.MODE_TESTNET)
    cfg = PILOT_POOLS["BTCUSDT"]
    swap = ex.build_swap("BTCUSDT", "sell", 50.0, 80000.0, cfg)
    encoded = bytes.fromhex(swap.data_hex)
    token_in_bytes = encoded[16:36]
    assert token_in_bytes.hex() == CBBTC_BASE.lower().replace("0x", "")


def test_dex_live_without_wallet_address_raises() -> None:
    os.environ.pop("BASE_WALLET_ADDRESS", None)
    ex = DexLegExecutor(mode=config.MODE_TESTNET)
    cfg = PILOT_POOLS["ETHUSDT"]
    try:
        ex.build_swap("ETHUSDT", "buy", 50.0, 3000.0, cfg)
    except RuntimeError as e:
        assert "WALLET_ADDRESS" in str(e)
        return
    assert False


# --- wallet_signer SHADOW ------------------------------------------------


def test_signer_shadow_returns_deterministic_hex() -> None:
    s = WalletSigner(mode=config.MODE_SHADOW)
    cfg = PILOT_POOLS["ETHUSDT"]
    swap = DexLegExecutor(mode=config.MODE_SHADOW).build_swap(
        "ETHUSDT", "buy", 50.0, 3000.0, cfg)
    a = s.sign_swap(swap)
    b = s.sign_swap(swap)
    assert a.is_shadow
    assert a.raw_hex == b.raw_hex
    assert a.raw_hex.startswith("0x02")  # mock prefix mimics EIP-1559 type byte


def test_signer_address_none_in_shadow() -> None:
    s = WalletSigner(mode=config.MODE_SHADOW)
    assert s.address is None


def test_signer_live_without_key_raises_on_use() -> None:
    s = WalletSigner(mode=config.MODE_TESTNET)
    cfg = PILOT_POOLS["ETHUSDT"]
    swap = DexLegExecutor(mode=config.MODE_SHADOW).build_swap(
        "ETHUSDT", "buy", 50.0, 3000.0, cfg)
    try:
        s.sign_swap(swap)
    except RuntimeError as e:
        assert "BASE_WALLET_PRIVATE_KEY" in str(e)
        return
    assert False


def test_signer_address_with_test_key() -> None:
    """eth_account.Account.from_key derives the canonical address from priv."""
    os.environ["BASE_WALLET_PRIVATE_KEY"] = TEST_PRIVATE_KEY
    s = WalletSigner(mode=config.MODE_TESTNET)
    assert s.address == TEST_ADDRESS


def _run_all() -> int:
    failures: list[tuple[str, str]] = []
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            setup_function(None)
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
        finally:
            teardown_function(None)
    print()
    if failures:
        print(f"{len(failures)} / {len(tests)} FAILED")
        return 1
    print(f"{len(tests)} / {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
