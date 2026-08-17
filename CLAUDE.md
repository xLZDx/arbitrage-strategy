> **Inherits global rules from `D:\Repo\CLAUDE.md`** — approval gate, no-guessing, regression tests, git lifecycle (including todo-in-commits), shell pre-approval, D:-drive-only disk policy. Read that file too.

# arbitrage_strategy — Project Context

## Layout
- Working directory: `D:\Repo\arbitrage_strategy`
- Sister project (path-editable dep): `D:\Repo\AI_trading_assistance`
- Python venv: `venv\` (separate from sister project)
- Dashboard: **two processes, one URL.** The standalone arb dashboard runs on **:5002**; the
  trading bot's dashboard on :5000 reverse-proxies `/arb/*` and `/api/arb/*` to it and presents it
  as an "Arbitrage" tab. Port 5001 is *not* available — the sister project's monitor owns it, which
  is why `72e96d4` moved this one to 5002. Do not "consolidate" the two processes: separate
  processes are what prevents `src/*` namespace shadowing between the two projects, which the
  architecture review named as its top risk.

## Scope
- CEX-DEX statistical arbitrage: Bybit Spot ↔ Base L2 DEX
- Pilot pairs: BTC/USDT, ETH/USDT, SOL/USDT
- Bankroll stub: $500/side until Phase 12 ramp
- Default mode: **SHADOW**. TESTNET via env var. Mainnet flag separate and gated.

## Storage / Paths
- DuckDB temp dir: `data/arb/cache/duckdb_temp/`
- Logs: `logs/arb_<service>_<date>.jsonl`, daily rotation
- Secrets in `.env` (never committed). Templates in `.env.example`.

## Risk Defaults (MANDATORY)
- HALT file flag: `data/arb/HALT` — every execution path checks within ≤2s.
- Daily loss cap: 5% of `BANKROLL_PER_SIDE_USD`.
- Per-trade cap: 10% of `BANKROLL_PER_SIDE_USD`.
- Withdrawal-disabled flag is checked before any Bybit balance change.
- All DEX swaps require `amountOutMin` + `deadline`.
- All on-chain sends go through bundle simulation first.

## Test paths
- `tests/test_arb_<phase>.py` mirroring trading-bot pattern.
- 0 failures gate every push.

## Per-task workflow
- After every code change: run `restart_all.ps1` so the live system reflects latest code.

## Plan Source of Truth
- `core/PLAN.md` is the canonical roadmap. Update it when scope changes.
- Per-phase deliverables, exit criteria, and test gates are listed there.

## History — built in four days, stopped by a review, dormant since

21 commits. The distribution matters more than the count: **17 of them land between 2026-05-10 and
2026-05-13**, one is a rename backup, and the last (`3b18b6a`, 2026-07-28) is tooling enablement.
Nothing has changed in the product since May.

**2026-05-10 — phases 0 through 5, in one day.** Bootstrap, config/storage/OBI features, Bybit L2
websocket + Uniswap V3 mid + Base gas oracle, dashboard, rule-based opportunity detector with
would-have-been PnL, replay simulator with cross-venue inventory, risk gate with ops drill, and
execution + MEV-lite end to end in SHADOW.

**2026-05-11 — phases 6 through 13, in one commit** (`7866e22`, "full ML/MEV/security/risk stack —
SHADOW complete"): HistGBT classifier, TFT-as-feature, multi-relay broadcast, GoPlus scanner, drift
detector, paper-trade hardener, live-ramp guard, DRL navigator stub. Then live DEX calldata and an
EIP-1559 wallet signer, a maker-fee path, an auto-rebalancer, and a full GUI control panel — the
last of which shipped alongside a **"CRITICAL dex-fee bug fix"** (`206b516`). That bug is the
origin story for a rule below: the fee tier was off by 100×.

**2026-05-13 — seven specialist agents said no.** `core/AGENT_REVIEW_2026-05-11.md` is the document
that stopped this project, and it is the most important file in the repository after the plan.
Verdict: **AMBER — NOT MERGE-READY**, six fixes required before any testnet key touches `.env`.
Findings flagged independently by two or more agents:

- `_assert_withdrawal_disabled` **fail-opens on a network error** — a withdrawal-capable key would
  have proceeded if the probe ever threw.
- `PoolConfig.fee_bps` was misnamed: it held the raw Uniswap tier, not basis points.
- The walk-forward validator fell back to a **random shuffle**, violating time order.
- `/api/arb/run_drill` called `rl.halt_clear()` unconditionally — **clicking "Run drill" in the UI
  cleared a live HALT**, bypassing the kill switch.
- `TESTNET` mode defaulted `BASE_RPC_URL` to `https://mainnet.base.org`, so a `.env` that set
  TESTNET but omitted the RPC would have signed and broadcast to **mainnet**.
- `src/*` namespace shadowing between this project and the trading bot — the architect's top risk.
- The 100× dex-fee bug had **no regression test**.

Four commits closed them: `c8f4aa8` (9 P0 blockers), `1aae378` (10 pre-testnet items), `1b34cbe`
(P2/P3 hardening plus four cross-cutting tools) and `c0e5c6f` (6 follow-ups from a post-fix
re-review). Read the review before touching execution, config defaults or the risk gate —
every one of those findings is a class of bug this codebase has already produced once.

**Since 2026-05-13 — nothing.** `core/PLAN.md` still reads "ALL PHASES IMPLEMENTED IN SHADOW",
322 regression assertions across 24 test files. The project never advanced past SHADOW: live
execution has always been blocked on operator-supplied `BYBIT_TESTNET_API_KEY` / `_SECRET` and
`BASE_WALLET_PRIVATE_KEY`, which were never provided. Treat any statement about this project being
"live", "trading" or "ramping" as false unless git says otherwise.

**Uncommitted migration residue.** The working tree carries `D:\test 2` → `D:\Repo` path fixes in
`CLAUDE.md`, `core/PLAN.md`, `.claude/settings.json`, `stop_all.ps1`, `src/dashboard/app_arb.py`
and one test, plus eight untracked `*.unicode_fix.bak` files. They are leftovers from the
2026-08-14 workspace migration, not in-flight feature work.
