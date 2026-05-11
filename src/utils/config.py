"""
Centralized constants for arbitrage_strategy.

All tunables live here. Risk-critical limits land in src/risk/limits.py
(Phase 4) and import from this file. Per CLAUDE.md: never C: paths.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Paths -----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data" / "arb"
DB_DIR = DATA_DIR / "db"
CACHE_DIR = DATA_DIR / "cache"
DUCKDB_TEMP_DIR = CACHE_DIR / "duckdb_temp"
LOG_DIR = REPO_ROOT / "logs"
MODEL_DIR = REPO_ROOT / "models"
PIDS_DIR = DATA_DIR / "pids"
HALT_FILE = DATA_DIR / "HALT"

for _d in (DATA_DIR, DB_DIR, CACHE_DIR, DUCKDB_TEMP_DIR, LOG_DIR, MODEL_DIR, PIDS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Pilot trading pairs (Q3 lock-in) --------------------------------------

PILOT_PAIRS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "AEROUSDT")

# Bybit fee tiers (bps). Maker = post-only limit; taker = market.
# Using maker drops fees ~10x, but adds latency risk + miss probability.
BYBIT_MAKER_FEE_BPS: float = 1.0   # ~1 bp at low VIP tier
BYBIT_TAKER_FEE_BPS: float = 10.0
# Coordinator picks maker when configured AND opportunity allows (some
# strategies need immediate fill — taker — but most CEX-DEX arb tolerates
# the few-second wait for a maker fill).
PREFER_MAKER: bool = os.environ.get("ARB_PREFER_MAKER", "0") == "1"
MAKER_FILL_TIMEOUT_S: float = 3.0  # if maker doesn't fill in this window → cancel + fall back to taker

# Bybit symbol -> on-chain wrapped token pair on Base.
# DEX pool addresses are filled in src/data/dex_quote.py at startup
# (resolved via 1inch/0x token list lookup, not hardcoded here to avoid drift).
BYBIT_TO_BASE_TOKEN: dict[str, tuple[str, str]] = {
    "BTCUSDT": ("WBTC", "USDC"),   # Base WBTC paired with USDC (no native USDT pool depth)
    "ETHUSDT": ("WETH", "USDC"),
    "SOLUSDT": ("wSOL", "USDC"),   # bridged Solana on Base
}

# --- Bankroll & risk (Q4 stub) ---------------------------------------------

BANKROLL_PER_SIDE_USD: float = float(os.environ.get("ARB_BANKROLL_USD", "500"))
PER_TRADE_CAP_PCT: float = 10.0           # of bankroll
DAILY_LOSS_CAP_PCT: float = 5.0           # of bankroll
DRAWDOWN_TRIGGER_PCT: float = 15.0        # rolling 24h
MIN_NET_BPS: float = 8.0                  # minimum net bps to send
MAX_SLIPPAGE_BPS_ABSOLUTE: float = 30.0   # hard ceiling on dynamic tolerance
MIN_BUNDLE_INCLUSION_RATE: float = 0.80   # rolling 1h, alert below

# --- Mode flags ------------------------------------------------------------

MODE_SHADOW = "SHADOW"
MODE_TESTNET = "TESTNET"
MODE_MAINNET = "MAINNET"
EXECUTION_MODE: str = os.environ.get("ARB_MODE", MODE_SHADOW)
WITHDRAWALS_ENABLED: bool = os.environ.get("ARB_WITHDRAWALS_ENABLED", "0") == "1"

# --- Venue -----------------------------------------------------------------

VENUE_CHAIN = "base"
BASE_RPC_URL: str = os.environ.get(
    "BASE_RPC_URL", "https://mainnet.base.org"  # public default; override in .env
)
BASE_CHAIN_ID = 8453

# --- WebSocket / ingestion -------------------------------------------------

BYBIT_WS_PUBLIC_URL = "wss://stream.bybit.com/v5/public/spot"
BYBIT_L2_DEPTH = 50           # 1, 50, 200, 500 are valid Bybit depths
BYBIT_WS_RECONNECT_S = 2.0
BYBIT_WS_MAX_BACKOFF_S = 60.0

DEX_QUOTE_POLL_INTERVAL_S = 1.0  # 1Hz quote refresh
GAS_POLL_INTERVAL_S = 6.0        # ~one Base block

# --- OBI feature (Phase 1) -------------------------------------------------

OBI_LEVELS = 10
OBI_DECAY = 0.5
OBI_HISTORY_BUFFER = 100  # circular buffer for OBI delta + spoofing detection

# --- Storage ---------------------------------------------------------------

PARQUET_PARTITION_BY = ("date", "pair")
ARROW_BATCH_FLUSH_ROWS = 500
ARROW_BATCH_FLUSH_S = 5.0

# --- Dashboard -------------------------------------------------------------

DASHBOARD_API_PREFIX = "/api/arb"
DASHBOARD_PORT_PRIMARY = 5000
DASHBOARD_PORT_FALLBACK = 5002  # 5001 is sister-project monitor; arb uses 5002


def is_shadow_mode() -> bool:
    return EXECUTION_MODE == MODE_SHADOW


def is_mainnet() -> bool:
    return EXECUTION_MODE == MODE_MAINNET


def halt_active() -> bool:
    """Cheap file-existence check. Called at top of every executor cycle."""
    return HALT_FILE.exists()
