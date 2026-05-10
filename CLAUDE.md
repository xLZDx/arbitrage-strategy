# Claude Code Instructions — arbitrage_strategy

This file is loaded automatically by Claude Code in every session for this project.

## Project context
- Working directory: `D:\test 2\arbitrage_strategy`
- Sister project (path-editable dep): `D:\test 2\AI trading assistance`
- Python venv: `venv\` (separate from sister project)
- Scope: CEX-DEX statistical arbitrage, Bybit Spot ↔ Base L2 DEX
- Pilot pairs: BTC/USDT, ETH/USDT, SOL/USDT
- Bankroll stub: $500/side until Phase 12 ramp
- Dashboard: shared with trading bot at port 5000, "Arbitrage" tab + `/api/arb/*` namespace
- Default mode: SHADOW. TESTNET via env var. Mainnet flag separate and gated.

## Approval Gate (MANDATORY)
- Before writing ANY code, present a written implementation plan and wait for explicit approval.
- Answering clarifying questions ≠ approval. Only "approved" / "go ahead" / "yes" / "implement" counts.
- Sub-steps inside an approved plan are auto-approved. New scope = un-approved, re-confirm.
- Double-confirm before first Write/Edit: restate plan in one paragraph and ask "Confirm to proceed?". Wait for second affirmative.

## No Guessing (MANDATORY)
- For factual state questions, TEST first (logs, processes, HTTP, files). If you cannot test, ASK.
- Banned without a test backing them: "probably", "likely", "should be", "most likely cause".
- Lead with test results, not conclusions.

## Regression Test Maintenance (MANDATORY)
Every code change ships with a test. Tests are kept and actively maintained as a regression suite.
1. **New features:** add test (happy path + ≥1 failure mode) before reporting done.
2. **Bug fixes:** add a regression test that fails on old code, passes on the fix.
3. **Refactors:** existing tests must still pass. If a refactor needs a test rewrite, do it in the SAME commit.
4. **Removals:** audit + delete or re-target affected tests; explain in commit message.
5. **Test paths:** `tests/test_arb_<phase>.py` mirroring trading-bot pattern (`tests/test_dashboard.py`).
6. **0 failures gate every push.**
7. **Don't game it.** Permanently-skipped tests = no test.

## Workflow Rules
- **Git lifecycle every phase:**
  1. Commit current state BEFORE starting any new phase (clean rollback point).
  2. Implement the phase.
  3. Verify: full test suite green, `restart_all.ps1` clean, dashboard reflects changes.
  4. Commit + **push to remote** (without asking — standing authorization once a phase is fully done).
- **Never force-push, never `--no-verify`, never amend a pushed commit.**
- After every code change: run `restart_all.ps1` so the live system reflects latest code.
- **Bash / PowerShell commands are pre-approved** for read-only probes, log tails, training triggers, restarts, file inspection, port scans. Only ask first for: destructive ops (rm -rf, force-push, drop table), things that touch shared state outside this repo (publishing to remotes, sending external messages).

## Storage / Paths
- **All paths on D:\.** Never C:.
- DuckDB temp dir: `data/arb/cache/duckdb_temp/`.
- Pip installs always with `--no-cache-dir`.
- Logs: `logs/arb_<service>_<date>.jsonl`, daily rotation.
- Secrets in `.env` — never committed. Templates go in `.env.example`.

## Risk Defaults
- HALT file flag: `data/arb/HALT` — every execution path checks within ≤2s.
- Daily loss cap: 5% of `BANKROLL_PER_SIDE_USD`.
- Per-trade cap: 10% of `BANKROLL_PER_SIDE_USD`.
- Withdrawal-disabled flag is checked before any Bybit balance change.
- All DEX swaps require `amountOutMin` + `deadline`.
- All on-chain sends go through bundle simulation first.

## Plan Source of Truth
- `core/PLAN.md` is the canonical roadmap. Update it when scope changes.
- Per-phase deliverables, exit criteria, and test gates are listed there.
