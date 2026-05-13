"""
Bybit spot leg executor.

Modes (config.EXECUTION_MODE):
  SHADOW   — log decision, return synthetic Fill, no API call.
  TESTNET  — call Bybit testnet REST. Requires BYBIT_TESTNET_API_KEY/SECRET.
  MAINNET  — call Bybit mainnet REST. Requires BYBIT_MAINNET_API_KEY/SECRET
             AND ARB_MAINNET_GATE=1 (extra defense).

Idempotency: every order carries a deterministic clientOrderId derived from
the trade_id. Replays of the same trade_id return the original fill instead
of placing a duplicate.

Withdrawal safety: at construction time, a startup probe verifies the API
key cannot withdraw funds (calls Bybit 'GET /v5/user/query-api' and asserts
'Withdraw' is not in the permission set). Mainnet refuses to start otherwise.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Literal

from src.utils import config, safe_json

log = logging.getLogger(__name__)

SideT = Literal["BUY", "SELL"]
StatusT = Literal["filled", "partial", "rejected", "shadow", "error"]


@dataclass(frozen=True)
class Fill:
    """Result of a leg attempt. Always returned (never None) so the
    coordinator can log every decision."""
    symbol: str
    side: SideT
    requested_qty_usd: float
    filled_qty_usd: float
    avg_price: float
    status: StatusT
    venue_order_id: str | None
    client_order_id: str
    mode: str
    error: str | None = None
    raw: dict | None = None

    def __post_init__(self) -> None:
        """P3-D5 (2026-05-11): enforce invariants the type system can't.
        Post-fix re-review (2026-05-11): SHADOW mode constructs synthetic
        fills using last_price which CAN be None/0 in early-startup
        scenarios; we treat status='shadow' as the canonical SHADOW marker
        and only enforce avg_price>0 on LIVE 'filled' status."""
        if self.requested_qty_usd < 0:
            raise ValueError(f"requested_qty_usd must be >= 0, got {self.requested_qty_usd}")
        if self.filled_qty_usd < 0:
            raise ValueError(f"filled_qty_usd must be >= 0, got {self.filled_qty_usd}")
        # 5% overfill tolerance for exchange rounding quirks
        if self.requested_qty_usd > 0 and self.filled_qty_usd > self.requested_qty_usd * 1.05:
            raise ValueError(
                f"overfill: filled_qty_usd={self.filled_qty_usd} > 105% of "
                f"requested {self.requested_qty_usd}"
            )
        # LIVE 'filled' status REQUIRES a positive price (downstream PnL math
        # would otherwise divide-or-multiply against 0). SHADOW uses status
        # 'shadow' for synthetic fills and can have avg_price=0 legitimately.
        if self.status == "filled" and self.avg_price <= 0:
            raise ValueError(
                f"LIVE status='filled' requires avg_price > 0, got {self.avg_price}. "
                f"For SHADOW synthetic fills use status='shadow' instead."
            )

    @property
    def fill_pct(self) -> float:
        if self.requested_qty_usd <= 0:
            return 0.0
        # Clamp to [0, 1] — exchange overfills land in __post_init__ check
        return min(1.0, self.filled_qty_usd / self.requested_qty_usd)


def make_client_order_id(trade_id: str, leg: str = "bybit") -> str:
    """Deterministic short ID for idempotency. Bybit limit: 36 chars."""
    h = hashlib.sha1(f"{trade_id}|{leg}".encode()).hexdigest()[:24]
    return f"arb-{leg}-{h}"


class BybitLegExecutor:
    def __init__(
        self,
        mode: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        ledger_path=None,
    ) -> None:
        self.mode = mode or config.EXECUTION_MODE
        self.api_key = api_key or self._key_for_mode("API_KEY")
        self.api_secret = api_secret or self._key_for_mode("API_SECRET")
        self._ledger_path = ledger_path or (config.DATA_DIR / "bybit_idempotency.json")
        self._idempotency_cache: dict[str, dict] = (
            safe_json.read_json(self._ledger_path, default={}) or {}
        )
        self._client = None

        if self.mode == config.MODE_MAINNET:
            self._assert_mainnet_gate_open()
        if self.mode in (config.MODE_TESTNET, config.MODE_MAINNET):
            self._init_client()

    def _key_for_mode(self, kind: str) -> str | None:
        env_var = f"BYBIT_{self.mode}_{kind}" if self.mode != config.MODE_SHADOW else None
        return os.environ.get(env_var) if env_var else None

    def _assert_mainnet_gate_open(self) -> None:
        if os.environ.get("ARB_MAINNET_GATE") != "1":
            raise RuntimeError(
                "Mainnet execution refused: ARB_MAINNET_GATE=1 not set. "
                "This is a deliberate second-defense flag — set it explicitly."
            )

    def _init_client(self) -> None:
        if not self.api_key or not self.api_secret:
            raise RuntimeError(
                f"Bybit {self.mode} credentials missing. "
                f"Set BYBIT_{self.mode}_API_KEY and BYBIT_{self.mode}_API_SECRET."
            )
        try:
            import ccxt  # type: ignore
        except ImportError as e:
            raise RuntimeError("ccxt not installed; pip install ccxt") from e
        params = {"apiKey": self.api_key, "secret": self.api_secret,
                  "enableRateLimit": True}
        if self.mode == config.MODE_TESTNET:
            params["options"] = {"recvWindow": 5000, "defaultType": "spot",
                                  "testnet": True}
        self._client = ccxt.bybit(params)
        if self.mode == config.MODE_TESTNET:
            self._client.set_sandbox_mode(True)
        # Withdrawal-permission check
        self._assert_withdrawal_disabled()

    def _assert_withdrawal_disabled(self) -> None:
        """Probe the API key's permissions; refuse to start if Withdraw is set.

        SAFETY (regression for P0-3 2026-05-11): on MAINNET, ANY exception
        from the probe (network timeout, ccxt parse error, auth error) must
        raise — NOT log-and-continue. A transient blip pre-fix would let a
        withdrawal-capable key proceed silently. TESTNET keeps the
        warn-and-continue policy because the consequence is bounded.
        """
        try:
            info = self._client.privateGetV5UserQueryApi()  # type: ignore
            perms = info.get("result", {}).get("permissions", {})
            wallet = perms.get("Wallet", []) or []
            if "Withdraw" in wallet:
                raise RuntimeError(
                    f"Bybit {self.mode} API key has Withdraw permission — REFUSED. "
                    "Disable withdrawals on this key at exchange level."
                )
        except RuntimeError:
            raise
        except Exception as e:
            if self.mode == config.MODE_MAINNET:
                raise RuntimeError(
                    f"Bybit MAINNET withdrawal-perm probe threw unexpectedly — "
                    f"REFUSED to start without proof key cannot withdraw: "
                    f"{type(e).__name__}: {e}"
                ) from e
            log.warning("withdrawal-perm probe failed (continuing on %s): %s",
                        self.mode, e)

    # ------------------------------------------------------------------

    def place_spot_maker(
        self,
        symbol: str,
        side: SideT,
        qty_usd: float,
        trade_id: str,
        limit_price: float,
        timeout_s: float | None = None,
    ) -> Fill:
        """
        Post-only limit order. Saves ~9 bps vs taker. If it doesn't fill
        within timeout_s, returns a "rejected" Fill with reason
        "maker_timeout" so the caller can fall back to a taker.

        SHADOW: returns a synthetic shadow fill at limit_price (no API call).
        """
        from src.utils import config
        client_order_id = make_client_order_id(trade_id, "bybit-maker")
        cached = self._idempotency_cache.get(client_order_id)
        if cached:
            log.info("idempotent maker replay: %s", client_order_id)
            return Fill(**cached)
        if self.mode == config.MODE_SHADOW:
            fill = Fill(
                symbol=symbol, side=side,
                requested_qty_usd=qty_usd, filled_qty_usd=qty_usd,
                avg_price=limit_price, status="shadow",
                venue_order_id=None, client_order_id=client_order_id,
                mode=self.mode,
            )
        else:
            fill = self._live_maker_fill(symbol, side, qty_usd, client_order_id,
                                          limit_price, timeout_s)
        self._persist_idempotency(client_order_id, fill)
        return fill

    def _live_maker_fill(self, symbol, side, qty_usd, coid, limit_price,
                         timeout_s) -> Fill:
        from src.utils import config
        if self._client is None:
            return Fill(symbol, side, qty_usd, 0.0, 0.0, "error", None, coid,
                        self.mode, error="client_not_initialized")
        timeout_s = timeout_s or config.MAKER_FILL_TIMEOUT_S
        try:
            qty_base = qty_usd / float(limit_price)
            order = self._client.create_limit_order(  # type: ignore
                symbol=symbol, side=side.lower(), amount=qty_base,
                price=float(limit_price),
                params={"clientOrderId": coid, "timeInForce": "PostOnly"},
            )
            order_id = str(order.get("id") or "")
            import time as _t
            deadline = _t.time() + timeout_s
            while _t.time() < deadline:
                try:
                    refreshed = self._client.fetch_order(order_id, symbol)  # type: ignore
                except Exception:
                    refreshed = order
                filled = float(refreshed.get("filled") or 0.0)
                avg_price = float(refreshed.get("average") or limit_price)
                if filled > 0 and (filled * avg_price) >= qty_usd * 0.95:
                    return Fill(
                        symbol=symbol, side=side,
                        requested_qty_usd=qty_usd,
                        filled_qty_usd=filled * avg_price,
                        avg_price=avg_price, status="filled",
                        venue_order_id=order_id, client_order_id=coid,
                        mode=self.mode, raw=refreshed,
                    )
                _t.sleep(0.1)
            try:
                self._client.cancel_order(order_id, symbol)  # type: ignore
            except Exception as ce:
                log.debug("cancel after maker timeout failed: %s", ce)
            return Fill(
                symbol=symbol, side=side, requested_qty_usd=qty_usd,
                filled_qty_usd=0.0, avg_price=0.0, status="rejected",
                venue_order_id=order_id, client_order_id=coid,
                mode=self.mode, error="maker_timeout",
            )
        except Exception as e:
            log.exception("bybit maker order failed")
            return Fill(symbol, side, qty_usd, 0.0, 0.0, "rejected", None, coid,
                         self.mode, error=f"{type(e).__name__}: {e}")

    def _persist_idempotency(self, coid: str, fill: Fill) -> None:
        """Persist replay record.

        P3-D2 (2026-05-11): the raw ccxt response (fill.raw) is intentionally
        NOT persisted. The raw dict can include account-level fields (fee
        breakdowns, linked wallet identifiers, IP-attributed timestamps).
        Keep audit data in a separate JSONL log; the idempotency ledger is
        replay-only and gets read back into memory unverified at
        construction time — so it must not contain anything sensitive that
        an attacker with disk access could harvest, and must not be a vector
        for poisoning Fill(**cached) replays.
        """
        try:
            self._idempotency_cache[coid] = {
                "symbol": fill.symbol, "side": fill.side,
                "requested_qty_usd": fill.requested_qty_usd,
                "filled_qty_usd": fill.filled_qty_usd,
                "avg_price": fill.avg_price, "status": fill.status,
                "venue_order_id": fill.venue_order_id,
                "client_order_id": fill.client_order_id,
                "mode": fill.mode, "error": fill.error,
                # raw=None on disk; audit trail goes elsewhere if needed
                "raw": None,
            }
            safe_json.write_json(self._ledger_path, self._idempotency_cache)
        except Exception as e:
            log.warning("failed to persist idempotency record: %s", e)

    def place_spot_taker(
        self,
        symbol: str,
        side: SideT,
        qty_usd: float,
        trade_id: str,
        last_price: float | None = None,
    ) -> Fill:
        """
        Idempotent spot taker order.

        symbol:   Bybit symbol, e.g. "BTCUSDT"
        side:     BUY or SELL
        qty_usd:  USD-equivalent notional
        trade_id: caller-supplied ID (used for clientOrderId)
        last_price: latest mid-price; used for SHADOW synthetic fill +
                    Bybit market-order quote-quantity sizing in TESTNET/MAINNET
        """
        client_order_id = make_client_order_id(trade_id, "bybit")

        cached = self._idempotency_cache.get(client_order_id)
        if cached:
            log.info("idempotent replay: %s already attempted", client_order_id)
            return Fill(**cached)

        if self.mode == config.MODE_SHADOW:
            fill = self._shadow_fill(symbol, side, qty_usd, client_order_id, last_price)
        else:
            fill = self._live_fill(symbol, side, qty_usd, client_order_id, last_price)

        # P3-D2 (2026-05-11): route through the shared helper that strips
        # fill.raw before persist. Pre-fix this inline copy kept the raw
        # ccxt response on disk (sensitive account data) and was a
        # maintenance hazard (two diverging copies of persistence logic).
        self._persist_idempotency(client_order_id, fill)
        return fill

    def _shadow_fill(self, symbol, side, qty_usd, coid, last_price) -> Fill:
        price = float(last_price or 0.0)
        return Fill(
            symbol=symbol, side=side,
            requested_qty_usd=qty_usd, filled_qty_usd=qty_usd,
            avg_price=price, status="shadow",
            venue_order_id=None, client_order_id=coid,
            mode=self.mode,
        )

    def _live_fill(self, symbol, side, qty_usd, coid, last_price) -> Fill:
        if self._client is None:
            return Fill(symbol, side, qty_usd, 0.0, 0.0, "error", None, coid,
                        self.mode, error="client_not_initialized")
        try:
            params = {"clientOrderId": coid}
            order = self._client.create_market_order(  # type: ignore
                symbol=symbol, side=side.lower(),
                amount=None, params={**params, "quoteOrderQty": qty_usd},
            )
            filled = float(order.get("filled") or 0.0)
            avg_price = float(order.get("average") or last_price or 0.0)
            filled_usd = filled * avg_price if avg_price > 0 else 0.0
            status: StatusT = "filled" if filled_usd >= qty_usd * 0.95 else "partial"
            return Fill(
                symbol=symbol, side=side,
                requested_qty_usd=qty_usd, filled_qty_usd=filled_usd,
                avg_price=avg_price, status=status,
                venue_order_id=str(order.get("id") or ""),
                client_order_id=coid, mode=self.mode, raw=order,
            )
        except Exception as e:
            log.exception("bybit %s order failed", side)
            return Fill(
                symbol=symbol, side=side,
                requested_qty_usd=qty_usd, filled_qty_usd=0.0,
                avg_price=0.0, status="rejected",
                venue_order_id=None, client_order_id=coid,
                mode=self.mode, error=f"{type(e).__name__}: {e}",
            )
