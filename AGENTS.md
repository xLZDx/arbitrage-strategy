# AGENTS.md — arbitrage_strategy

Tool-agnostic conventions for any coding agent operating in this checkout — Claude Code, OpenAI
Codex/ChatGPT, Aider, or anything else. `CLAUDE.md` is the Claude Code entry point and carries
scope, layout, risk defaults and the project history; this file is for everyone else, and for the
parts that apply regardless of which agent is reading them.

The operator's `~/.claude/CLAUDE.md` is the sole authority and is never outranked by anything here.

## Approval

- Approval is **only** the literal `GO` / `ГО`. "yes", "approved", "looks good", "мой выбор",
  "я бы дал GO" do not release a gate.
- Push is a **separate** `push` authorization naming the specific commits. A local commit never
  implies permission to push.
- Anything that can move real value — flipping execution mode, touching a wallet signer, adding a
  key to `.env`, broadcasting a transaction — needs its own explicit confirmation even under a
  broader GO.

## Build / test / run

```powershell
cd "D:\Repo\arbitrage_strategy"
venv\Scripts\python.exe -m pytest tests\ -q      # 322 assertions across 24 files; 0 failures gates every push
.\restart_all.ps1                                # bring the live system up to the latest code
.\stop_all.ps1
```

Python ≥3.11, own `venv\` (deliberately separate from the sister project's). The trading bot is a
path-editable dependency; ML extras (`lightgbm`, `torch`, `darts`, `river`) are an optional group.

Tests are named `tests/test_arb_<phase>.py`, mirroring the trading-bot convention. A new phase gets
its own file.

**Two dashboard processes, one URL.** The standalone arb dashboard runs on **:5002**; the trading
bot's dashboard on :5000 reverse-proxies `/arb/*` and `/api/arb/*` into an "Arbitrage" tab. Port
5001 belongs to the sister project's monitor — `72e96d4` exists because of that collision. Do not
merge the two processes: separate processes are what keeps `src/*` from shadowing between the two
projects, which the architecture review named as its top risk.

## Safety rules that are not negotiable

**SHADOW is the default and stays the default.** `ARB_MODE` moves to TESTNET only via env var, and
mainnet is a separate gate on top of that. Never flip either without an explicit, risk-acknowledged
GO. As of the last commit this project has never run outside SHADOW, and live execution has always
been blocked on operator-supplied keys that were never provided.

**The HALT file is the kill switch.** `data/arb/HALT` — every execution path checks it within ≤2s.
Do not add a code path that clears it as a side effect. This has happened once: `/api/arb/run_drill`
called `halt_clear()` unconditionally, so clicking "Run drill" in the UI restored trading after an
auto-HALT had fired. The fix snapshots HALT state before the drill and restores it after.

**Fail closed on every safety probe.** `_assert_withdrawal_disabled` once caught a broad
`Exception` around a network call, logged a warning and continued — meaning a withdrawal-capable
key would have proceeded whenever the probe timed out. In mainnet mode a probe that cannot complete
must raise, not warn.

**Config defaults must never be more dangerous than the mode.** `BASE_RPC_URL` once defaulted to
`https://mainnet.base.org` even under `ARB_MODE=TESTNET`, so a `.env` that set TESTNET and omitted
the RPC would have signed and broadcast to mainnet. Any default that points at production must be
gated on the mode, and startup must refuse a TESTNET run whose resolved URL is a mainnet endpoint.

**Both execution legs carry the mainnet gate.** `BybitLegExecutor` enforces `ARB_MAINNET_GATE`; so
must `WalletSigner._ensure_account()`. One-sided enforcement means direct construction bypasses the
defense.

**Every DEX swap carries `amountOutMin` and `deadline`, and every on-chain send is bundle-simulated
first.** No exceptions for "just a test".

**Risk caps.** Daily loss cap 5% of `BANKROLL_PER_SIDE_USD`; per-trade cap 10%. The withdrawal-
disabled flag is checked before any Bybit balance change. These are parameterised, not hard-coded —
change the config, never the check.

## Correctness traps this codebase has already fallen into

- **Fee units.** `PoolConfig.fee_bps` once held the raw Uniswap tier rather than basis points — a
  100× error that reached the dashboard. Any field naming a unit must actually be in that unit, and
  a fee change ships with a regression test. The original bug shipped without one.
- **Time order in validation.** The walk-forward validator once fell back to a random shuffle when
  it could not split cleanly. A shuffle fallback in a time-series validator is a silent leakage
  bug; make it raise instead.
- **Silent rejection through a broad `except`.** `asyncio.run()` inside the sync coordinator raised
  `RuntimeError: This event loop is already running` when called from an async caller, and the
  enclosing `except Exception` turned that into a rejected trade tagged `goplus_error`. A control-
  flow error must never be laundered into a domain verdict.
- **Missing imports in rarely-exercised endpoints.** `/api/arb/run_drill` called a module that was
  never imported, so every POST 500'd — and because the live-ramp guard checks drill freshness, the
  Phase 12 gate could never have opened. Endpoints that only a guard reads still need a test.

All seven of these come from `core/AGENT_REVIEW_2026-05-11.md`. Read it before touching execution,
config defaults, the risk gate or the ML validation path.

## Windows scripting

`.ps1` / `.bat` / `.cmd` are UTF-8 **with BOM** with ASCII-only bodies. Temporary files go on `D:`,
never `C:`.

## Git safety

1. Never push without explicit authorization for that specific commit list.
2. Read `git log @{u}..HEAD --oneline` before every push attempt and compare against what was
   authorized.
3. Never `--no-verify`, never `--force`, never amend a pushed commit.
4. Remote `origin` is `github.com/xLZDx/arbitrage-strategy`, branch `main`.
5. The working tree currently carries uncommitted `D:\test 2` → `D:\Repo` path fixes and eight
   untracked `*.unicode_fix.bak` files, left over from the 2026-08-14 workspace migration. They are
   not in-flight feature work — do not treat them as someone else's active change, and do not sweep
   them into an unrelated commit either.

## Agent cadence

Opt-in, not automatic. Do the work, self-review it, and say what you think is most likely wrong.
Escalate to a specialist when the change touches execution, wallet signing, the risk gate, mode
selection or ML validation — the surfaces where this project's seven-agent review actually found
things. Do not spawn a panel on a plan, a doc, or a small change.

`/rosetta` (Prepare → Research → Plan → Act → Validate) is available **on demand**, not always-on:
`.github/prompts/rosetta.prompt.md`. It composes with this cadence rather than replacing it, and it
changes nothing about approval: `GO` / `ГО` remains the only authorization, and push remains
separate.

## Where everything else lives

| Need | Go to |
|---|---|
| Scope, layout, storage paths, risk defaults, history | `CLAUDE.md` |
| Canonical roadmap, per-phase exit criteria, locked Q1–Q6 decisions | `core/PLAN.md` |
| The seven-agent pre-merge audit that gated this project | `core/AGENT_REVIEW_2026-05-11.md` |
| P2 decision record | `core/P2_DECISIONS_2026-05-11.md` |
| Architecture | `core/ARCHITECTURE.md` |
| Risk model | `core/RISK.md` |
| Original product spec | `arbitrage.txt` (repo root) |
