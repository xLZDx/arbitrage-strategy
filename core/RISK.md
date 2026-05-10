# Risk Controls — arbitrage_strategy

**Status:** Phase 0 specification. Implementation lands in Phase 4.

---

## 1. Hard limits (parameterized in `src/utils/config.py`)

| Constant | Default | Notes |
|---|---|---|
| `BANKROLL_PER_SIDE_USD` | 500 | Q4 stub. Revisit before Phase 12. |
| `PER_TRADE_CAP_PCT` | 10 | % of bankroll per single trade. |
| `DAILY_LOSS_CAP_PCT` | 5 | % of bankroll. Auto-HALT if breached. |
| `DRAWDOWN_TRIGGER_PCT` | 15 | Rolling 24h. Auto-HALT if breached. |
| `MIN_NET_BPS` | 8 | Minimum expected net bps to send. |
| `MAX_SLIPPAGE_BPS_DYNAMIC` | computed | OBI + realized-slippage driven; capped at 30 bps absolute. |
| `MIN_BUNDLE_INCLUSION_RATE` | 0.80 | Below this in rolling 1h → alert. |
| `BRIBE_FLOOR_GWEI` | dynamic | gas oracle p50; bid above it. |

---

## 2. Kill switch — HALT file flag

- **File:** `data/arb/HALT`
- **Behavior:** every execution path checks for existence of this file at the top of every cycle. If present, refuse to send any order or bundle.
- **Reaction time:** ≤ 2 seconds (verified by Phase 4 drill).
- **Set via:** `touch data/arb/HALT` (manual), risk module (auto on cap breach), or dashboard button (Phase 5+).
- **Cleared via:** `rm data/arb/HALT` (manual only — never automatic).

---

## 3. Auto-HALT triggers

The risk module sets HALT when ANY of:
1. Daily loss exceeds `DAILY_LOSS_CAP_PCT`.
2. Drawdown exceeds `DRAWDOWN_TRIGGER_PCT`.
3. Bundle inclusion rate falls below `MIN_BUNDLE_INCLUSION_RATE` for 30+ minutes.
4. Inventory imbalance > 25% on either side (CEX-DEX hedge broken).
5. Three consecutive bundle simulations revert.
6. Unhandled exception in any process.
7. Bybit API rate-limit error rate > 5% over 5 minutes.

Auto-HALT is **opt-out per source** via config flag, but defaults to on.

---

## 4. Withdrawal-disabled flag

- Bybit API key for this bot must have **withdrawal disabled** at exchange level.
- The bot itself also checks `WITHDRAWALS_ENABLED = False` in config before any balance-changing call beyond spot trading.
- Confirmed via Phase 5 startup check: bot refuses to start if API key has withdrawal scope.

---

## 5. Pre-flight checks (every trade)

In order, all must pass before sending:

1. HALT file absent.
2. Daily loss < cap.
3. Inventory healthy on both sides (sufficient + balanced).
4. `expected_net_profit > simulated_gas + bribe_floor + cex_fees`.
5. `expected_net_profit > MIN_NET_BPS * notional`.
6. HistGBT veto score > threshold (Phase 6+).
7. `simulate_bundle()` succeeds.
8. `amountOutMin` and `deadline` populated.
9. Risk module ACK.

Any failure → log structured rejection, no send.

---

## 6. Drills

To be executed at the end of Phase 4 and replayed before any Phase-12 ramp:

1. **HALT drill:** flip flag, verify all execution paths refuse within 2s.
2. **Loss cap drill:** simulate -5.1% daily; verify auto-HALT.
3. **Inventory drill:** drain DEX-side balance to 50% target; verify executor refuses.
4. **Withdrawal drill:** call bybit balance-change with `WITHDRAWALS_ENABLED=False`; verify exception.
5. **Stuck-leg drill:** simulate Bybit fill + DEX revert; verify unwind path executes.

Drill results logged to `logs/drills_<date>.jsonl`. Failure of any drill blocks live ramp.

---

## 7. Observability

Dashboard risk-state strip (Phase 4 card) shows live:
- HALT status (red/green)
- Today's PnL vs cap
- 24h drawdown vs trigger
- Inventory balance both sides
- Bundle inclusion rate (rolling 1h)
- Last 5 rejected trades with reason code
