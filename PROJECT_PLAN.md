---
project: arbitrage_strategy
as_of: 2026-11-01
active_sprint: S5
scope: single_sprint
approved_sprints: S5
operator_approved: true
---
# arbitrage_strategy - project plan

Source of truth: `core/PLAN.md` (phases 0-13, locked decisions Q1-Q6), `core/P2_DECISIONS_2026-05-11.md`, `core/AGENT_REVIEW_2026-05-11.md`, `core/DECISION_LOG.md`, `CLAUDE.md`, `AGENTS.md`. S1-S4 group the phases PLAN.md marks DONE (all in SHADOW mode) and the four remediation commits; S5 is derived from open work in those documents; S6 is the live ramp. No live or testnet trading has ever run; ACCEPTED here means implemented in SHADOW and recorded in PLAN.md, not profitable or live.

## Roadmap
| Sprint | Title | Status | Goal |
|---|---|---|---|
| S1 | Phases 0-2: bootstrap, market plumbing, opportunity detector | ACCEPTED | Repo skeleton, Bybit L2 plus Uniswap V3 plus gas ingestion, rule-based detector (PLAN.md marks DONE). |
| S2 | Phases 3-5: simulator, risk/ops, execution plus MEV-lite | ACCEPTED | Replay simulator, HALT and risk gate, execution coordinator in SHADOW. |
| S3 | Phases 6-13: ML, multi-relay, scanner, drift, ramp guard | ACCEPTED | Full SHADOW stack committed in 7866e22; Phase 13 is a stub by decision Q1. |
| S4 | Pre-merge review remediation | ACCEPTED | Close the AMBER verdict of the 2026-05-11 seven-agent review (P0, P1, P2/P3, re-review). |
| S5 | Pre-testnet follow-ups | ACTIVE | Goal (derived): finish the open non-trading work recorded in the project documents before any testnet key is supplied. |
| S6 | Testnet and live ramp | BLOCKED_OPERATOR | Run testnet round-trips and the capped mainnet ramp; needs operator keys, bankroll and explicit risk acknowledgement. |

## Sprint S5 - Pre-testnet follow-ups
Goal: (derived) Close the open non-trading items the documents leave behind: executor process split (Phase 5.Y), working-tree migration residue, reproducible install, and unmet SHADOW exit criteria.

| Task | Title | Acceptance | Status | Evidence | Owner | Depends | Commit |
|---|---|---|---|---|---|---|---|
| S5-T01 | Phase 5.Y: extract executor into a separate OS process | `src/exec/executor_main.py` exists with its own PID file, HALT re-check every 250 ms, and the "kill ingestion mid-trade" drill passes | NOT_STARTED | P2_DECISIONS Decision 2 approved it; `src/exec` has no executor_main.py | assistant | | |
| S5-T02 | Commit or discard the D:\test 2 to D:\Repo path-fix residue | The five modified files are reviewed and committed on their own; the eight `*.unicode_fix.bak` files are an operator decision (deletion) | NOT_STARTED | git status shows modified .claude/settings.json, core/PLAN.md, app_arb.py, stop_all.ps1, one test, plus untracked .bak files; CLAUDE.md says they are migration residue | assistant | | |
| S5-T03 | Make the install path consume uv.lock | Bootstrap docs and pyproject use `uv sync --frozen` or a locked export instead of plain pip | NOT_STARTED | DECISION_LOG 2026-08-30 records the gap as known and not fixed | assistant | | |
| S5-T04 | Phase 1 24h continuous-capture exit check | 24h capture with no gaps and under 1 percent packet loss | NOT_STARTED | PLAN.md notes "24h soak pending observation" | assistant | | |
| S5-T05 | Phase 11 paper-trade soak | 7 days in SHADOW, simulated vs paper PnL within 15 percent, zero unhandled exceptions | NOT_STARTED | PLAN.md marks the phase DONE as code; no soak result in any document | assistant | S5-T01 | |
| S5-T06 | Phase 14 namespace refactor src to arb | Done only if a revisit trigger in Decision 1 fires | PARKED | P2_DECISIONS Decision 1 deferred it | assistant | | |
| S5-T07 | Phase 9 scanner activation for long-tail tokens | Activates only if tokens outside the majors allowlist are added | PARKED | PLAN.md: dormant, pilot pairs are all majors | assistant | | |
| S5-T08 | Keep suite green after S5 changes | 0 failures on `venv\Scripts\python.exe -m pytest tests\ -q`; last recorded count is 322 assertions in 24 files | PARTIAL | PLAN.md and AGENTS.md record 322 assertions; no run after 2026-05-13 is evidenced | assistant | | |

## Sprint S1 - Phases 0-2: bootstrap, market plumbing, opportunity detector
Goal: Bootstrap the repo and build read-only plumbing and a rule-based detector.

| Task | Title | Acceptance | Status | Evidence | Owner | Depends | Commit |
|---|---|---|---|---|---|---|---|
| S1-T01 | Phase 0 bootstrap | Skeleton, plan, settings, regression scaffold | ACCEPTED | PLAN.md: DONE | assistant | | ecd197a |
| S1-T02 | Phase 1 market plumbing | Bybit L2 WS, Uniswap V3 mid, Base gas, store, OBI, dashboard | ACCEPTED | PLAN.md: DONE; 24h soak tracked in S5-T04 | assistant | S1-T01 | 5e9e460 |
| S1-T03 | Phase 2 opportunity detector | Rule-based detector with would-have-been PnL | ACCEPTED | PLAN.md: DONE | assistant | S1-T02 | 2f10cf1 |

## Sprint S2 - Phases 3-5: simulator, risk/ops, execution plus MEV-lite
Goal: Add replay simulation, risk controls and SHADOW execution.

| Task | Title | Acceptance | Status | Evidence | Owner | Depends | Commit |
|---|---|---|---|---|---|---|---|
| S2-T01 | Phase 3 replay simulator | Replay, inventory, cost stack | ACCEPTED | PLAN.md: DONE | assistant | | c46316c |
| S2-T02 | Phase 4 risk and ops | HALT, drill 8/8 pass | ACCEPTED | PLAN.md: DONE | assistant | | 5ed0018 |
| S2-T03 | Phase 5 execution and MEV-lite | Coordinator in SHADOW; live calldata and signing | ACCEPTED | PLAN.md: DONE (SHADOW); live path pending keys, see S6 | assistant | | 02fea17 |

## Sprint S3 - Phases 6-13: ML, multi-relay, scanner, drift, ramp guard
Goal: Complete the SHADOW feature set.

| Task | Title | Acceptance | Status | Evidence | Owner | Depends | Commit |
|---|---|---|---|---|---|---|---|
| S3-T01 | Phases 6-8 HistGBT, TFT feature, multi-relay | Each marked DONE in PLAN.md | ACCEPTED | PLAN.md table | assistant | | 7866e22 |
| S3-T02 | Phases 9-12 scanner, drift, paper hardener, live-ramp guard | Each marked DONE in PLAN.md | ACCEPTED | PLAN.md table; Phase 9 dormant by design | assistant | | 7866e22 |
| S3-T03 | Phase 13 DRL navigator | Stub only, PPO deferred by Q1 | PARKED | PLAN.md: STUB (deferred per Q1) | assistant | | |
| S3-T04 | Personal-use extensions and GUI control panel | Maker path, auto-rebalancer, dashboard | ACCEPTED | Commits b7a3a53, 37f7e61, 206b516 | assistant | | 206b516 |

## Sprint S4 - Pre-merge review remediation
Goal: Close the AMBER verdict of core/AGENT_REVIEW_2026-05-11.md.

| Task | Title | Acceptance | Status | Evidence | Owner | Depends | Commit |
|---|---|---|---|---|---|---|---|
| S4-T01 | P0 blockers (9) | Fixed with regression tests | ACCEPTED | CLAUDE.md history and commit c8f4aa8 | assistant | | c8f4aa8 |
| S4-T02 | P1 pre-testnet items (10) | Fixed | ACCEPTED | commit 1aae378 | assistant | S4-T01 | 1aae378 |
| S4-T03 | P2/P3 hardening and decisions | Fixed; decisions recorded in P2_DECISIONS | ACCEPTED | commit 1b34cbe; executor split and namespace items carried to S5 | assistant | S4-T02 | 1b34cbe |
| S4-T04 | Post-fix re-review follow-ups (6) | Fixed | ACCEPTED | commit c0e5c6f | assistant | S4-T03 | c0e5c6f |

## Sprint S6 - Testnet and live ramp
Goal: Move beyond SHADOW only with operator-supplied credentials and explicit risk acknowledgement.

| Task | Title | Acceptance | Status | Evidence | Owner | Depends | Commit |
|---|---|---|---|---|---|---|---|
| S6-T01 | Provide testnet keys | BYBIT_TESTNET_API_KEY, _SECRET and BASE_WALLET_PRIVATE_KEY set by the operator | BLOCKED_OPERATOR | PLAN.md and CLAUDE.md: never provided | operator | | |
| S6-T02 | Phase 5 testnet exit | 100 testnet round-trips, zero stuck legs, inclusion above 80 percent | BLOCKED_OPERATOR | Needs real keys and on-chain sends | operator | S6-T01, S5-T01 | |
| S6-T03 | Set the real BANKROLL_PER_SIDE_USD | Value set in .env; the $500 stub raises in live_ramp.py | BLOCKED_OPERATOR | P2_DECISIONS Decision 4 | operator | | |
| S6-T04 | Phase 12 capped mainnet ramp | Per-trade cap 10 percent and daily loss cap 5 percent, ramp 2x per profitable week | BLOCKED_OPERATOR | Real-money action; mainnet gate stays off | operator | S6-T02, S6-T03, S5-T05 | |
