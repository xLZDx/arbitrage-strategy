# Agent Review — Pre-Merge Audit

**Date:** 2026-05-11
**Trigger:** User directive — "review the codebase, plan, and architecture before merging with trading bot"
**Method:** 7 specialist agents spawned in parallel via the project's MANDATORY review pattern (architect / python-reviewer / security-reviewer / ml-engineer / silent-failure-hunter / type-design-analyzer / pr-test-analyzer)
**Verdict:** **🟠 AMBER — NOT MERGE-READY**. Six concrete fixes required before any TESTNET key lands in `.env`.

---

## Cross-agent confluence (high-confidence findings)

These were flagged independently by 2+ agents — highest priority signal:

| Finding | Agents |
|---|---|
| `_assert_withdrawal_disabled` fail-opens on network error | security, silent-failure-hunter |
| `PoolConfig.fee_bps` misnamed (raw Uniswap tier, not bps) | type-design, python, ml |
| Walk-forward random-shuffle fallback violates time order | python (P1-F), ml (CRITICAL 2) |
| Stuck-leg paths under-tested + no auto-HALT | silent-failure-hunter, pr-test-analyzer |
| Namespace shadowing (`src/*` in both projects) | architect (top risk) |
| 100x dex_fee bug has no regression test | pr-test-analyzer (P0-1), python-reviewer |

---

## Phase A — IMMEDIATE FIXES (~1–2 hrs, must complete before next merge)

These exist in code TODAY and produce wrong/dangerous behavior:

### A.1 — `/api/arb/run_drill` is broken
- **File:** `src/dashboard/arb_blueprint.py:529`
- **Bug:** `safe_json.append_jsonl(...)` called but `safe_json` is never imported in this module. Every POST raises `NameError`, returns 500.
- **Side effect:** the `live_ramp.py` drill-freshness check (`drill_runs.jsonl` mtime within 24h) can NEVER pass, so the Phase 12 live-ramp guard will refuse forever.
- **Fix:** add `from src.utils import safe_json` at module top.

### A.2 — `/api/arb/run_drill` clears live HALT (kill-switch bypass)
- **File:** `src/dashboard/arb_blueprint.py:470`
- **Bug:** drill setup calls `rl.halt_clear()` without checking whether a real (non-drill) HALT is in effect. Clicking "Run drill" in the UI restores trading after an auto-HALT fired.
- **Fix:** snapshot `halt_was_active + reason` before clear; restore after drill if previously active.

### A.3 — Bybit withdrawal probe fail-opens on non-RuntimeError
- **File:** `src/exec/bybit_leg.py:130-132`
- **Bug:** outer `except Exception` catches network timeouts/ccxt errors and logs WARNING then continues. A withdrawal-capable key proceeds if the probe ever throws.
- **Fix:** in `MAINNET` mode, any non-RuntimeError must `raise RuntimeError(...) from e`.

### A.4 — `asyncio.run()` inside the sync coordinator
- **File:** `src/exec/coordinator.py:196`
- **Bug:** `asyncio.run()` raises `RuntimeError: This event loop is already running` if `attempt()` is called from any async caller (FastAPI/pytest-asyncio/detector loop). The outer `except Exception` at line 203 then silently REJECTS the trade as `goplus_error`.
- **Fix:** accept an event loop arg or expose `async def attempt_async()`. Use `get_event_loop().run_until_complete(...)` with explicit detection.

### A.5 — `WalletSigner` missing `ARB_MAINNET_GATE` check
- **File:** `src/exec/wallet_signer.py` (entire file)
- **Bug:** `BybitLegExecutor._assert_mainnet_gate_open()` enforces the env-var gate; `WalletSigner._ensure_account()` has no equivalent. Direct construction bypasses double-defense.
- **Fix:** add the same gate check in `WalletSigner._ensure_account()` when `self.mode == MODE_MAINNET`.

### A.6 — TESTNET defaults to mainnet RPC
- **File:** `src/utils/config.py:82-85`
- **Bug:** `BASE_RPC_URL` defaults to `https://mainnet.base.org` even when `ARB_MODE=TESTNET`. A `.env` that sets TESTNET but omits BASE_RPC_URL signs+broadcasts to mainnet.
- **Fix:** when `EXECUTION_MODE == MODE_TESTNET` and `BASE_RPC_URL` unset, default to `https://sepolia.base.org`. Raise at startup if mode is TESTNET and resolved URL points at mainnet.

### A.7 — Add tests proving the fixes
- **Files to create/extend:**
  - `tests/test_arb_phase2_opportunity.py` — add `test_pool_fee_raw_tier_500_is_not_passed_as_bps` (regression for the dex_fee 100x bug).
  - `tests/test_arb_phase4_dashboard.py` — add tests hitting `/counterfactual`, `/halt`, `/run_drill`, `/run_replay`, `/train_histgbt`, `/maker_mode`, `/soak_summary` via Flask test_client.
  - `tests/test_arb_phase5_coordinator.py` — add `test_stuck_leg_dex_failed_after_maker_fill_triggers_unwind` (covers the maker-fallback gap).

### A.8 — Restore live dashboard data flow
- **Symptom:** user screenshot showed empty cards in production despite ingestion running.
- **Likely cause:** stop_all reaped processes between commits; AERO-pool reactivation may have triggered restart timing issues; or the dashboard process started before any opportunities accumulated.
- **Action:** restart fresh, verify all endpoints serve sane data via curl before declaring fixed.

---

## Phase B — BEFORE TESTNET (~3–4 hrs)

### B.1 — Authenticate state-mutating dashboard POSTs
- **File:** `src/dashboard/app_arb.py` `create_app()`
- **Bug:** `/halt`, `/maker_mode`, `/run_replay`, `/train_histgbt`, `/run_drill`, `/counterfactual` are unauthenticated. The reverse-proxy on the trading-bot dashboard inherits the `X-API-Key` check ONLY because the bot's `before_request` covers it — if the arb dashboard is hit directly on `:5002`, no auth.
- **Fix:** add a `before_request` guard checking `X-API-Key: $ARB_API_KEY` on all non-GET routes. Mirror the trading-bot pattern.

### B.2 — Rename `PoolConfig.fee_bps` → `uniswap_fee_tier`
- **File:** `src/data/dex_quote.py:66+` (cascades to detector + dex_leg)
- **Bug:** the name `fee_bps` actively misleads (500 means 0.05%, not 500 bps). Any new caller will hit the same trap.
- **Fix:** rename across PoolConfig, PreparedSwap.fee_tier_bps, detector_main callsite. Add `to_bps()` helper or a runtime guard at `detect_opportunity` entry: `if pool_fee_bps > 100: raise ValueError(...)`.

### B.3 — Add `joblib.load` integrity check
- **File:** `src/ml/hist_gbt.py:159`
- **Bug:** `joblib.load(p)` deserializes arbitrary Python objects (RCE if file substituted).
- **Fix:** SHA-256 manifest alongside artifact; verify on load. `isinstance(result, HistGBTArtifact)` post-load.

### B.4 — Auto-HALT on `stuck_leg_unrecoverable`
- **File:** `src/exec/coordinator.py:268-269`
- **Bug:** unrecoverable stuck leg → bot holds unhedged DEX exposure; next cycle fires normally.
- **Fix:** `risk.halt_set(f"stuck_leg_unrecoverable: {trade_id}")` immediately after setting that outcome.

### B.5 — Drop label-leaking features
- **File:** `src/ml/feature_pipeline.py:27`
- **Bug:** `expected_net_bps`, `gross_bps`, `gas_cost_bps`, `slippage_haircut_bps` ARE the decision rule fed back as inputs. Model learns the rules, not edge.
- **Fix:** drop these 4 from `FEATURE_COLUMNS`. Retrain to get the honest baseline AUC.

### B.6 — Add PurgedKFold with embargo
- **File:** `src/ml/hist_gbt.py:96-107`
- **Bug:** contiguous walk-forward at 6 opps/sec → train/holdout boundary samples are autocorrelated. Random-shuffle fallback (line 100) outright destroys time order.
- **Fix:** import `D:\test 2\AI trading assistance\src\utils\purged_kfold.py`. Embargo ≥ max holding period (~1 minute for atomic arb).

### B.7 — Defer HistGBT training until N ≥ 1500 GO trades
- **File:** `src/ml/hist_gbt.py:97` (the `n_samples < 20` guard)
- **Bug:** 148-sample dataset / 14 features = 8.6 rows/feature, well below 100/feature floor. Holdout AUC ±0.18 CI is uninformative.
- **Fix:** raise the floor to 1500. Document the rules-based veto IS the model until then.

---

## Phase C — DECIDE (architectural)

### C.1 — Namespace refactor: `src/` → `arb/`
- **Recommendation:** architect agent says do it NOW (~45 files, mechanical) before more code accumulates.
- **Alternative:** formally accept reverse-proxy as permanent. Two processes, two venvs, single-URL UX via tab. Working today.
- **Cost of deferring:** every new file widens the eventual refactor surface; the editable-dep line in `pyproject.toml:54` will never actually work without it.

### C.2 — Process-model split before live
- **Recommendation:** executor in its own OS process before any TESTNET round-trip, so an ingestion crash can't leave half a position open.
- **Today:** ingestion + dashboard + (eventual) executor all under one `restart_all.ps1` umbrella.

---

## Phase D — Before MAINNET (deeper hardening)

### D.1 — GoPlus error-result TTL (cache poisoning)
- Reduce TTL on error responses from 1h → 60s OR don't cache errors at all.

### D.2 — Idempotency ledger raw-field stripping
- `Fill.raw` (ccxt response) contains account internals. Strip before `safe_json.write_json`. Audit log separately.

### D.3 — Cross-process Parquet write atomicity
- Ingestion writes Parquet files while dashboard reads via DuckDB glob. Partial writes can be observed. Add `.tmp + rename` pattern.

### D.4 — Inventory apply ordering on stuck-leg
- Apply on Bybit fill, reverse on DEX fail (current: apply only on joint success).

### D.5 — Schema versioning on Opportunity + HistGBTArtifact
- Add `schema_version: int` so old parquet rows with raw-tier `dex_fee_bps` don't silently break replay.

### D.6 — Type invariants via `__post_init__`
- `bybit_ask >= bybit_mid >= bybit_bid`, `fill_pct ∈ [0,1]`, `avg_price > 0 if status==filled`, `cancellation_rate ∈ [0,1]`.

---

## Per-agent agent IDs (for SendMessage follow-ups)

| Agent | ID | Focus |
|---|---|---|
| architect | `a196b9e4c72cd6486` | Merge strategy, namespace conflict, process model |
| python-reviewer | `ad419aeb1ed223e7c` | Code quality, P0/P1/P2 ranking |
| security-reviewer | `afd9ca59ca661effa` | Wallet, keys, HALT, fail-closed paths |
| ml-engineer | `a590077bd433363ad` | HistGBT/TFT/features (AFML lens) |
| silent-failure-hunter | `a64656facb584c2c5` | Swallowed errors, bad fallbacks |
| type-design-analyzer | `a94fd7f66828474ed` | Invariants, frozen vs mutable, unit confusion |
| pr-test-analyzer | `a78e49c3a84f26529` | Behavioral coverage gaps |

Each can be continued via `SendMessage` if a finding needs deeper exploration.

---

## What the agents AGREED is GOOD

- Reverse-proxy boundary: hop-by-hop headers stripped, no leaks (`AI trading assistance/src/dashboard/app.py:7273-7339`)
- Coordinator dependency direction: one-way, no upward imports
- GoPlus scanner: correctly fails closed
- Test isolation pattern: `tmp_path` + named prefixes, no string-match anti-patterns
- Replay determinism: RNG-seeded
- Idempotency cross-instance persistence: tested + works

---

## Recommended execution order

1. **Phase A in full** before any more code lands (real production bugs exist today)
2. **Phase B** before any testnet key drops into `.env`
3. **Phase C decision** before too much new code accumulates
4. **Phase D** before live mainnet capital

Total estimated effort: ~10–15 hrs for Phase A + B + C decision; D is incremental.

This plan supersedes the deferred items listed in `core/PLAN.md` §6 (Open work). Update PLAN.md status section after each phase completes.
