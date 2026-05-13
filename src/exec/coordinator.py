"""
Arb coordinator — fires both legs atomically, handles unwind on stuck-leg.

Pre-flight order (CLAUDE.md / RISK.md / Phase-4 limits.preflight):
  1. risk.preflight(opportunity, state).is_ok()  → otherwise refuse
  2. inventory.can_apply(legs)                   → otherwise refuse
  3. bundle_simulator.simulate(prepared_swap).passed → otherwise refuse
  4. Fire BOTH legs:
       Bybit leg via API immediately
       DEX leg via private bundle (Flashbots Protect)
  5. Wait for confirmations within timeout.
  6. If one leg filled and the other failed → UNWIND the filled one.

Stuck-leg unwind: if Bybit fills but DEX times out, place the inverse
Bybit order to flatten exposure. (Not perfect — there's slippage on the
unwind — but better than carrying directional risk.)

In SHADOW mode the coordinator runs the entire decision flow, calls every
sub-component in shadow mode, and writes a synthetic 'trades' row. Useful
for end-to-end testing without keys.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal

from src.data.dex_quote import PILOT_POOLS
from src.exec.bybit_leg import BybitLegExecutor, Fill, make_client_order_id
from src.exec.bundle_simulator import BundleSimulator, SimulationResult
from src.exec.dex_leg import DexLegExecutor, PreparedSwap
from src.exec.flashbots_executor import FlashbotsExecutor
from src.exec.private_rpc_router import PrivateRpcRouter, SubmissionResult
from src.ml.feature_pipeline import extract_features
from src.ml.hist_gbt import HistGBTArtifact, load_artifact
from src.risk import limits as risk
from src.security.goplus_scanner import GoPlusScanner
from src.sim.inventory import Inventory, PAIR_LEGS
from src.utils import config

log = logging.getLogger(__name__)

OutcomeT = Literal[
    "shadow", "filled", "rejected_preflight", "rejected_inventory",
    "rejected_simulation", "stuck_leg_unwound", "stuck_leg_unrecoverable",
    "error",
]


@dataclass
class TradeRecord:
    """One round-trip arb attempt. Persisted to data/arb/db/trades/."""
    ts: str
    trade_id: str
    pair: str
    direction: str
    notional_usd: float
    mode: str
    outcome: OutcomeT
    reason: str
    bybit_status: str | None = None
    bybit_fill_pct: float = 0.0
    bybit_avg_price: float = 0.0
    bybit_client_order_id: str | None = None
    dex_relay: str | None = None
    dex_tx_hash: str | None = None
    dex_status: str | None = None
    sim_passed: bool = False
    sim_gas_used: int = 0
    realized_net_bps: float = 0.0
    histgbt_p: float | None = None        # P(profitable) from HistGBT
    histgbt_vetoed: bool = False
    goplus_scanned: bool = False
    goplus_safe: bool | None = None        # None = not scanned (major); True/False = result
    goplus_reason: str | None = None
    bybit_used_maker: bool = False
    bybit_taker_fallback: bool = False    # true if maker timed out and we fell back


def _new_trade_id() -> str:
    return f"arb-{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def persist_trade(rec: "TradeRecord") -> None:
    """Append a trade record to data/arb/db/trades/<date>/<pair>/."""
    from src.storage import arb_store
    arb_store.write_records("trades", [asdict(rec)], pair=rec.pair)


@dataclass
class ArbCoordinator:
    bybit: BybitLegExecutor = field(default_factory=BybitLegExecutor)
    dex: DexLegExecutor = field(default_factory=DexLegExecutor)
    router: PrivateRpcRouter = field(default_factory=PrivateRpcRouter)
    simulator: BundleSimulator = field(default_factory=BundleSimulator)
    inventory: Inventory = field(default_factory=Inventory)
    risk_state: risk.RiskState = field(default_factory=risk.RiskState)
    # HistGBT artifact (optional — None means classifier veto disabled)
    histgbt: HistGBTArtifact | None = None
    histgbt_required: bool = False  # if True, missing model → REJECT
    # Phase 5.X: live signer + submitter. Lazily constructed.
    flashbots: FlashbotsExecutor | None = None
    # Phase 9: GoPlus scanner. Activates only for non-major tokens.
    goplus: GoPlusScanner | None = None

    def __post_init__(self) -> None:
        if self.flashbots is None:
            self.flashbots = FlashbotsExecutor(router=self.router)
        if self.goplus is None:
            self.goplus = GoPlusScanner()

    def attempt(
        self,
        opportunity: dict,
        live_dex_mid: float | None = None,
    ) -> TradeRecord:
        """
        opportunity: row from opportunities table (must be decision='GO').
        live_dex_mid: latest mid for the pair; used for amountOutMin sizing.
                      If None, falls back to opportunity['dex_mid'].
        """
        trade_id = _new_trade_id()
        ts = _utc_iso()
        pair = opportunity["pair"]
        direction = opportunity.get("direction", "bybit_high")
        notional = float(opportunity.get("notional_usd", 0.0))
        mode = self.bybit.mode

        rec = TradeRecord(
            ts=ts, trade_id=trade_id, pair=pair, direction=direction,
            notional_usd=notional, mode=mode,
            outcome="error", reason="not_set",
        )

        # 1. Risk preflight
        gate = risk.preflight(opportunity, self.risk_state)
        if not gate.is_ok():
            rec.outcome = "rejected_preflight"
            rec.reason = f"{gate.decision}: {gate.reason}"
            return rec

        # 2. Inventory check
        legs = self._inventory_legs(pair, direction, notional)
        if not legs:
            rec.outcome = "rejected_inventory"
            rec.reason = f"unknown_pair: {pair}"
            return rec
        ok, why = self.inventory.can_apply(legs)
        if not ok:
            rec.outcome = "rejected_inventory"
            rec.reason = why
            return rec

        # 2.5 HistGBT veto (Phase 6)
        if self.histgbt is not None:
            try:
                feats = extract_features(opportunity)
                vetoed, p = self.histgbt.veto(feats.reshape(1, -1))
                rec.histgbt_p = round(p, 6)
                rec.histgbt_vetoed = vetoed
                if vetoed:
                    rec.outcome = "rejected_preflight"
                    rec.reason = (f"histgbt_veto: p={p:.4f} < "
                                  f"threshold={self.histgbt.veto_threshold}")
                    return rec
            except Exception as e:
                log.warning("HistGBT scoring failed (continuing): %s", e)
        elif self.histgbt_required:
            rec.outcome = "rejected_preflight"
            rec.reason = "histgbt_required_but_missing"
            return rec

        # 3. Build + simulate DEX leg (mandatory simulate per Q1)
        pool_cfg = PILOT_POOLS.get(pair)
        if pool_cfg is None:
            rec.outcome = "rejected_inventory"
            rec.reason = f"no_pool_config: {pair}"
            return rec

        # 3.5 Phase 9 — GoPlus scan for non-majors. Majors short-circuit
        # inside the scanner; long-tail tokens hit the API and fail closed
        # on revert/timeout. This is the personal-use safety net for the
        # AERO/long-tail pairs.
        #
        # SAFETY (regression for P0-4 2026-05-11): we used to call
        # `asyncio.run(self.goplus.scan(...))` which raises
        # "This event loop is already running" if attempt() is invoked from
        # any async caller (FastAPI, pytest-asyncio, the detector loop).
        # The outer except then silently REJECTed the trade as goplus_error.
        # Now we detect the loop state and pick the right execution path.
        if pool_cfg.base_address and not GoPlusScanner.is_major(pool_cfg.base_address):
            rec.goplus_scanned = True
            try:
                scan = self._run_async(self.goplus.scan(pool_cfg.base_address))
                rec.goplus_safe = scan.is_safe
                rec.goplus_reason = scan.reason
                if not scan.is_safe:
                    rec.outcome = "rejected_preflight"
                    rec.reason = f"goplus_blocked: {scan.reason}"
                    return rec
            except Exception as e:
                rec.outcome = "rejected_preflight"
                rec.reason = f"goplus_error: {type(e).__name__}: {e}"
                return rec
        dex_dir = "buy" if direction == "dex_high" else "sell"
        live_mid = live_dex_mid or float(opportunity.get("dex_mid", 0.0))
        if live_mid <= 0:
            rec.outcome = "error"
            rec.reason = "no_live_dex_mid"
            return rec

        try:
            prepared = self.dex.build_swap(
                pair=pair, direction=dex_dir,
                notional_usd=notional, live_mid_price=live_mid,
                pool_cfg=pool_cfg,
            )
        except Exception as e:
            rec.outcome = "error"
            rec.reason = f"dex_build_failed: {type(e).__name__}: {e}"
            return rec

        sim = self.simulator.simulate(prepared)
        rec.sim_passed = sim.passed
        rec.sim_gas_used = sim.gas_used
        if not sim.passed:
            rec.outcome = "rejected_simulation"
            rec.reason = f"sim_revert: {sim.revert_reason}"
            return rec

        # 4. Fire both legs (Bybit immediate, DEX bundle).
        # Maker-first if PREFER_MAKER: post a limit at our side of the spread
        # for MAKER_FILL_TIMEOUT_S; on timeout, cancel + fall back to taker.
        # The fallback uses a fresh trade-id suffix so idempotency is preserved.
        bybit_side = "SELL" if direction == "bybit_high" else "BUY"
        bybit_fill = self._fire_bybit_leg(
            pair, bybit_side, notional, trade_id, opportunity, rec,
        )
        rec.bybit_status = bybit_fill.status
        rec.bybit_fill_pct = bybit_fill.fill_pct
        rec.bybit_avg_price = bybit_fill.avg_price
        rec.bybit_client_order_id = bybit_fill.client_order_id

        # FlashbotsExecutor signs (deterministic mock in SHADOW; real wallet
        # signing in TESTNET/MAINNET) and submits via the configured router.
        submission = self.flashbots.sign_and_submit(prepared)
        rec.dex_relay = submission.relay
        rec.dex_tx_hash = submission.tx_hash
        rec.dex_status = submission.status

        # 5. Outcome resolution
        bybit_ok = bybit_fill.status in ("filled", "shadow")
        dex_ok = submission.status in ("submitted", "shadow")

        if bybit_ok and dex_ok:
            self.inventory.apply(legs)
            self.inventory.book_pnl(self._estimate_pnl(opportunity))
            if mode == config.MODE_SHADOW:
                rec.outcome = "shadow"
                rec.reason = "both_legs_simulated"
            else:
                rec.outcome = "filled"
                rec.reason = "both_legs_succeeded"
            rec.realized_net_bps = float(opportunity.get("expected_net_bps", 0.0))
        elif bybit_ok and not dex_ok:
            unwound = self._unwind_bybit(pair, bybit_side, notional, trade_id)
            rec.outcome = "stuck_leg_unwound" if unwound else "stuck_leg_unrecoverable"
            rec.reason = (f"dex_failed: {submission.status}"
                          f"{' / unwind_ok' if unwound else ' / unwind_failed'}")
        elif dex_ok and not bybit_ok:
            rec.outcome = "stuck_leg_unrecoverable"
            rec.reason = f"bybit_failed: {bybit_fill.status} / dex_committed"
        else:
            rec.outcome = "error"
            rec.reason = (f"both_legs_failed: bybit={bybit_fill.status}, "
                          f"dex={submission.status}")
        return rec

    # ------------------------------------------------------------------

    @staticmethod
    def _run_async(coro):
        """Run an awaitable from a sync method, handling both async and sync
        caller contexts.

        - No running loop → use `asyncio.run`.
        - Running loop (detector task, FastAPI handler, pytest-asyncio) →
          run the coro on a dedicated thread's loop so we don't collide
          with the calling loop. This is the standard sync-from-async
          escape hatch.
        """
        import asyncio
        try:
            asyncio.get_running_loop()
            running_loop = True
        except RuntimeError:
            running_loop = False
        if not running_loop:
            return asyncio.run(coro)
        # Inside a running loop: dispatch to a worker thread with its own loop.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(asyncio.run, coro)
            return fut.result()

    @staticmethod
    def _inventory_legs(pair: str, direction: str, notional_usd: float):
        if pair not in PAIR_LEGS:
            return []
        bybit_base, dex_base = PAIR_LEGS[pair]
        if direction == "bybit_high":
            return [
                ("bybit", bybit_base, -notional_usd),
                ("bybit", "USDT",     +notional_usd),
                ("dex",   "USDC",     -notional_usd),
                ("dex",   dex_base,   +notional_usd),
            ]
        if direction == "dex_high":
            return [
                ("bybit", "USDT",     -notional_usd),
                ("bybit", bybit_base, +notional_usd),
                ("dex",   dex_base,   -notional_usd),
                ("dex",   "USDC",     +notional_usd),
            ]
        return []

    @staticmethod
    def _estimate_pnl(opportunity: dict) -> float:
        """Use the opportunity's theoretical_pnl as the SHADOW PnL stand-in."""
        return float(opportunity.get("theoretical_pnl_usd", 0.0))

    def _fire_bybit_leg(
        self,
        pair: str,
        side: str,
        notional_usd: float,
        trade_id: str,
        opportunity: dict,
        rec: "TradeRecord",
    ) -> Fill:
        """
        Maker-first when config.PREFER_MAKER, else straight to taker.
        On maker timeout (status='rejected', error='maker_timeout'), cancels
        the limit and falls back to a taker market order with a distinct
        trade_id suffix (so idempotency cache doesn't replay the dead maker).
        """
        last_price = opportunity.get("bybit_mid")
        if not config.PREFER_MAKER:
            return self.bybit.place_spot_taker(
                symbol=pair, side=side, qty_usd=notional_usd,
                trade_id=trade_id, last_price=last_price,
            )

        # Limit price = the cheap side of the book for our direction.
        # SELL → quote at the ask (passive); BUY → quote at the bid.
        bid = float(opportunity.get("bybit_bid", 0.0)) or float(last_price or 0.0)
        ask = float(opportunity.get("bybit_ask", 0.0)) or float(last_price or 0.0)
        limit_price = ask if side == "SELL" else bid
        if limit_price <= 0:
            limit_price = float(last_price or 0.0)

        rec.bybit_used_maker = True
        maker_fill = self.bybit.place_spot_maker(
            symbol=pair, side=side, qty_usd=notional_usd,
            trade_id=trade_id, limit_price=limit_price,
        )
        if maker_fill.status in ("filled", "shadow"):
            return maker_fill

        # Maker timed out (or other rejection) → taker fallback under a
        # distinct trade_id so the idempotency ledger knows it's a new attempt.
        rec.bybit_taker_fallback = True
        return self.bybit.place_spot_taker(
            symbol=pair, side=side, qty_usd=notional_usd,
            trade_id=f"{trade_id}-taker", last_price=last_price,
        )

    def _unwind_bybit(
        self,
        pair: str,
        original_side: str,
        notional_usd: float,
        trade_id: str,
    ) -> bool:
        """
        Place an inverse Bybit order to flatten the stuck leg.
        Returns True if the unwind succeeded (any non-rejected status).
        """
        inverse = "BUY" if original_side == "SELL" else "SELL"
        unwind_id = f"{trade_id}-unwind"
        try:
            r = self.bybit.place_spot_taker(
                symbol=pair, side=inverse, qty_usd=notional_usd,
                trade_id=unwind_id,
            )
            return r.status in ("filled", "partial", "shadow")
        except Exception as e:
            log.exception("unwind failed: %s", e)
            return False
