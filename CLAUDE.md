> **Inherits global rules from `D:\test 2\CLAUDE.md`** — approval gate, no-guessing, regression tests, git lifecycle (including todo-in-commits), shell pre-approval, D:-drive-only disk policy. Read that file too.

# arbitrage_strategy — Project Context

## Layout
- Working directory: `D:\test 2\arbitrage_strategy`
- Sister project (path-editable dep): `D:\test 2\AI trading assistance`
- Python venv: `venv\` (separate from sister project)
- Dashboard: shared with trading bot at port 5000, "Arbitrage" tab + `/api/arb/*` namespace

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
