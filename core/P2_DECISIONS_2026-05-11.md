# P2 — Architectural Decisions

**Date:** 2026-05-11
**Authority:** operator gave standing "bypass permissions" before sleeping;
defaulting to the architect agent's recommendation and proceeding.

---

## Decision 1: Namespace refactor `src/` → `arb/` — DEFERRED to Phase 14

**Architect recommendation:** rename now while the surface is ~45 files.

**Decision:** **DEFER**. Reasons:
1. Reverse-proxy is working in production today (the trading-bot tab shows
   the arb dashboard via `/arb/*` and `/api/arb/*` on port 5000).
2. The reverse-proxy boundary is clean (audit confirmed: hop-by-hop headers
   stripped, no auth leaks, no namespace shadowing risk because the two
   projects run in separate Python processes).
3. The advertised editable-dep `ai_trading_assistance` in `pyproject.toml:54`
   is commented out and unused — no actual import depends on the merge.
4. The 45-file rename is mechanical but introduces a large diff that masks
   substantive review feedback for ~1 week of subsequent commits.

**When to revisit:** if either of these triggers fires, take the refactor:
- Sister project starts genuinely depending on `arbitrage_strategy` imports
  (TFT loader, shared utilities).
- A second arb-flavored project appears under `D:\test 2\` and BOTH need to
  import from this one.

**Action item:** mark `pyproject.toml:54` with a `# TODO Phase-14` comment so
the deferral is visible in source.

---

## Decision 2: Executor in separate OS process — APPROVED for Phase 5.Y

**Architect recommendation:** split before any TESTNET round-trip.

**Decision:** **APPROVED**. Action plan:

1. **Phase 5.Y** (before any testnet key drops in `.env`):
   - Extract `src/exec/executor_main.py` as a new entry-point process.
   - It reads opportunities from the existing `data/arb/db/opportunities/`
     (already written by ingestion).
   - It owns its own PID file: `data/arb/pids/executor.pid`.
   - It re-checks HALT every 250ms (current coordinator runs in-line).
   - Crash isolation: if ingestion dies, executor closes any open Bybit
     leg via the idempotency ledger (already persistent across restarts)
     and exits cleanly.
   - `restart_all.ps1` adds it after the dashboard launch line.

2. **Phase 5.Y exit criterion:** drill the "kill ingestion mid-trade" scenario:
   - Trade in flight (SHADOW: bybit leg fills, before dex submit).
   - `Stop-Process -Id <ingestion>` kills the ingestion process.
   - Verify: executor sees the in-flight trade through to completion
     (or unwind) regardless of ingestion state.

**Why approved:** the coordinator currently runs INSIDE the ingestion
process (verified at `restart_all.ps1:46`). An ingestion crash mid-trade
leaves a stuck Bybit position with no automated unwind. Cost to fix: ~3h
of work; cost of NOT fixing: one stuck position is one operator-pager.

---

## Decision 3: Maker mode default OFF until paper-soak passes — APPROVED

**Decision:** keep `ARB_PREFER_MAKER=0` default. Activate via env var when:
- Phase 11 paper-trade soak shows |sim-vs-realized PnL gap| ≤ 15% over
  ≥ 7 days, AND
- ≥ 1,500 GO opportunities have triggered under maker-mode simulation
  (Phase 6 training data prereq).

**Why:** maker mode is the cost-floor unlock (10 bps → 1 bps) but adds
latency-dependent miss rate. The paper soak validates the partial-fill
model before we trust it with real fills.

---

## Decision 4: Bankroll for Phase 12 ramp — DEFERRED to operator

**Decision:** **OPERATOR ACTION REQUIRED.** Cannot ramp without explicit
$BANKROLL_PER_SIDE_USD set in `.env`. The current $500 stub raises in
`live_ramp.py:90` per Phase 12 design.

Counter-factual analysis (from `core/AGENT_REVIEW_2026-05-11.md`):
- $500/side: ~$8/day theoretical PnL projection
- $2k/side: ~$894/day theoretical PnL projection
- Caps scale linearly: `PER_TRADE_CAP_PCT=10`, `DAILY_LOSS_CAP_PCT=5`

Operator to set when ready.

---

## What's now done

| Decision | Action | When |
|---|---|---|
| Namespace refactor | Deferred to Phase 14 | post-1500-GO |
| Executor process split | Approved, schedule Phase 5.Y | before TESTNET |
| Maker mode default | OFF | until paper-soak passes |
| Bankroll | Operator | before Phase 12 |
