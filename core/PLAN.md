# Arbitrage Strategy — Production Plan (v2)

**Status:** PENDING APPROVAL — all Q1–Q6 answered, plan revised. Awaiting explicit "approved/go/yes" + double-confirm before any implementation.
**Project root:** `D:\test 2\arbitrage_strategy\`
**Source spec:** `arbitrage.txt` (root of this project)
**Reuse source:** `D:\test 2\AI trading assistance\` (existing trading bot)
**Created:** 2026-05-10 (v1)
**Revised:** 2026-05-10 (v2 — Q1–Q5 answers folded in)

---

## 0. Decisions locked in (Q1–Q6)

| # | Decision |
|---|---|
| **Q1** | Scope = **CEX-DEX statistical arbitrage** (Bybit Spot ↔ DEX). NOT atomic-MEV / flashloan / mempool-searcher. Execution layer = **MEV-lite**: private RPC + mandatory bundle simulation + revert protection (`amountOutMin` + `deadline`) + gas bidding (`Profit > Gas + Bribe`). Multi-relay broadcast (Flashbots + MEV Blocker + bloXroute + Eden) is a **Phase-8 upgrade**, not the introduction of MEV. NO `mev_searcher/` module. |
| **Q1.5** | Venue = **Base** (default). User did not explicitly pick; correctable before approval. Solana would change tooling (web3.py → solana-py, Flashbots → Jito). |
| **Q2** | **Separate repo** at `D:\test 2\arbitrage_strategy`. Trading bot installed as path-editable dep via `pyproject.toml`. Two independent git remotes, restart cycles independent. |
| **Q3** | Pilot pairs = **BTC/USDT, ETH/USDT, SOL/USDT**. DEX-side wrapped tokens: WBTC, WETH, wSOL on Base. Bybit-side: BTCUSDT, ETHUSDT, SOLUSDT spot. |
| **Q4** | Bankroll = **$500/side placeholder**. Phases 1–11 use SHADOW/TESTNET with no real capital. Real number revisited before Phase 12. All limits parameterized via `BANKROLL_PER_SIDE_USD` config. |
| **Q5** | **Same dashboard, new "Arbitrage" tab** on port 5000, `/api/arb/*` endpoints. Ingestion runs as its own process. Dashboard reads via `parquet_store` with `_duck_lock`. Fallback to port 5001 if p95 latency rises >20%. |
| **Q6** | Plan saved to `core/PLAN.md` (this file). |

---

## 1. Critique of the source spec — what we keep vs drop

| # | Source-spec idea | Verdict |
|---|---|---|
| 1.1 | Atomic on-chain MEV searcher | **DROPPED** — separate project. (Q1) |
| 1.2 | Per-trade Bybit→Wallet withdrawal | **DROPPED** — uses pre-positioned inventory, periodic rebalance. |
| 1.3 | Eth mainnet / Flashbots default | **DROPPED** — Base L2, MEV-lite. (Q1, Q1.5) |
| 1.4 | DRL pathfinding (PPO multi-hop) | **DEFERRED** to Phase 13. 1inch/0x already solve it deterministically. |
| 1.5 | HistGBT + TFT + DRL day one | **REORDERED** — rules → log → label → train. |
| 1.6 | River as online HistGBT | **REPURPOSED** as drift detector only. |
| 1.7 | Honeypot scanner | **CONDITIONAL** — only outside majors allowlist. Phase 9, deferred since pilot pairs are all majors. |
| 1.8 | Whale tracker as P0 trigger | **DOWNGRADED** to nice-to-have. Phase-N. |
| 1.9 | 0.5% slippage tolerance | **REPLACED** with dynamic OBI + realized-slippage-driven tolerance. |
| 1.10 | Weighted multi-level OBI | **KEPT** as P0 feature. |
| 1.11 | Cancellation-rate spoofing detector | **PROMOTED** to P0 (was buried in source spec). |
| 1.12 | Private RPC + bundle simulation | **KEPT** — Phase 5 from day one. (Q1) |
| 1.13 | DuckDB + Parquet storage | **KEPT** — matches existing trading bot. |
| 1.14 | Bybit Spot reference price | **KEPT**. |

---

## 2. Reuse map from `D:\test 2\AI trading assistance`

| Component | Reuse as-is | Reuse with mods | New |
|---|---|---|---|
| `src/database/parquet_store.py` (DuckDB+Parquet, `_duck_lock`) | ✅ | | |
| `src/utils/safe_json.py` (atomic file-locked I/O) | ✅ | | |
| `src/utils/config.py` | | extend | |
| Bybit REST connector | ✅ | | |
| Bybit WebSocket | | add L2 depth subscription | |
| TFT model + training pipeline | ✅ (weights as feature) | | |
| HistGBT trainer | | retarget feature set | |
| Distributed training orchestrator (port 7700) | ✅ | | |
| Flask dashboard (port 5000) | | add `/api/arb/*` namespace + Arbitrage tab | |
| `tests/test_dashboard.py` patterns | | mirror as `tests/test_arb_*` | |
| `restart_all.ps1` | | add arb services | |
| Web3 / RPC client | | | ✅ Phase 1 |
| DEX router (Uniswap V3 / 0x / 1inch) | | | ✅ Phase 5 |
| Gas oracle | | | ✅ Phase 1 |
| Inventory manager (cross-venue balances) | | | ✅ Phase 4 |
| Private RPC + bundle simulator (MEV-lite) | | | ✅ Phase 5 |
| Contract auditor (GoPlus) | | | ✅ Phase 9 (deferred) |

---

## 3. Repo layout (separate-repo decision per Q2)

```
D:\test 2\arbitrage_strategy\
├── .claude\settings.json          ✅ already created
├── pyproject.toml                 → editable dep: ai_trading_assistance
├── core\
│   ├── PLAN.md                    ✅ this file
│   ├── ARCHITECTURE.md            Phase 0 deliverable
│   ├── RISK.md                    Phase 0 deliverable
│   └── CLAUDE.md                  Phase 0 deliverable (commit-before / commit+push-after lifecycle)
├── src\
│   ├── data\                      Phase 1 — bybit_l2_ws, dex_quote, gas_oracle
│   ├── storage\                   Phase 1 — arb_store wrapper
│   ├── features\                  Phase 1 — obi, spoofing detector
│   ├── strategy\                  Phase 2 — opportunity detector
│   ├── sim\                       Phase 3 — replay, inventory sim
│   ├── risk\                      Phase 4 — limits, kill switch
│   ├── ops\                       Phase 4 — inventory manager, health
│   ├── exec\                      Phase 5 — bybit_leg, dex_leg, coordinator,
│   │                                       private_rpc_router, flashbots_executor,
│   │                                       bundle_simulator
│   └── ml\                        Phase 6+ — hist_gbt, tft_feature_pipe
├── tests\                         test_arb_<phase>.py
├── data\arb\                      isolated DuckDB + Parquet
├── scripts\
├── logs\
├── models\
├── venv\                          (separate from trading bot's venv)
├── restart_all.ps1
└── stop_all.ps1
```

---

## 4. Dashboard architecture (Q5)

- **One dashboard at port 5000.** Add "Arbitrage" tab.
- **`/api/arb/*` namespace** registered as a Flask blueprint imported from this project.
- **Ingestion process is independent.** It writes to `data/arb/db/`. Flask never opens that DB write-side.
- Dashboard READ path uses `parquet_store.query()` with the existing `_duck_lock` (per the 2026-05-08 thread-safety incident memory).
- **Cards:** live OBI sparkline, DEX-vs-Bybit spread heatmap, opportunity feed, simulated PnL ladder, inventory balance, MEV bundle status (post-Phase-5), risk-state strip (HALT, daily-loss, drawdown).
- **Latency budget:** if dashboard p95 endpoint latency rises >20% above current baseline after the arb tab ships → fall back to a separate Flask app on port 5001 (one-line code change: `app = Flask(__name__)` in a new `app_arb.py`).
- **Test gate:** every new endpoint gets an assertion in `tests/test_arb_dashboard.py` before merge.

---

## 5. Phased implementation

**Git lifecycle every phase:** commit-before → implement → tests green + `restart_all.ps1` clean → commit + **push to remote**.

### Phase 0 — Bootstrap (½ day)
- Skeleton dirs (above). `.claude/settings.json` ✅ already present.
- `core/PLAN.md` ✅ this file.
- `core/ARCHITECTURE.md`: process diagram, reuse map, dashboard wiring.
- `core/RISK.md`: HALT flag, daily loss cap, withdrawal-disabled flag, kill-switch drill spec.
- `core/CLAUDE.md`: project rules (commit-before/after lifecycle, D-drive only, approval gate, no-guessing).
- `pyproject.toml` with editable dep.
- `venv\` set up. Pip installs use `--no-cache-dir`.
- `restart_all.ps1` empty service list (extended each phase).
- Git init + first commit + push (creates remote).

### Phase 1 — Read-only market plumbing (2–3 days)
- `src/data/bybit_l2_ws.py`: async WebSocket, 50-level depth, 3 symbols, writes Arrow batches.
- `src/data/dex_quote.py`: 1inch/0x quote poller for the same 3 pairs on Base. No swaps.
- `src/data/gas_oracle.py`: Base gas via public RPC (Alchemy / Infura / public Base RPC).
- `src/storage/arb_store.py`: ParquetClient wrapper, `data/arb/db/` partitioned by `(date, pair)`. DuckDB temp dir set to `data/arb/cache/duckdb_temp/`.
- `src/features/obi.py`: weighted multi-level OBI + OBI delta + cancellation rate.
- Dashboard cards: live OBI sparkline + spread for 3 pairs.
- Tests: WebSocket reconnect, OBI calc parity, store roundtrip.
- **Exit:** 24h continuous capture, no gaps, < 1% packet loss.

### Phase 2 — Opportunity detector (no execution) (2 days)
- `src/strategy/opportunity.py`: rule-based detector. Logs spread bps, OBI both sides, gas, expected slippage, theoretical net PnL.
- `data/arb/opportunities.parquet` — labelable dataset for Phase 6.
- Dashboard card: opportunity feed + would-have-been cumulative PnL.
- Tests: schema, threshold logic.
- **Exit:** 72h log of ≥ 1000 opportunities labeled with simulated outcome.

### Phase 3 — PnL & slippage simulator (1–2 days)
- `src/sim/replay.py`: replays Phase-1 tape, computes realistic Bybit/DEX fees, gas, simulated price impact, partial-fill probability.
- `src/sim/inventory.py`: tracks notional balance both sides; flags inventory violations.
- Dashboard card: simulated equity curve, hit rate, average net bps.
- Tests: replay determinism, fee model parity vs real Bybit history.
- **Kill criterion:** if simulated Sharpe < 1.0 on 1 week of replay, project pauses for venue/pair-set rethink.

### Phase 4 — Risk + ops scaffolding (1 day)
- `src/risk/limits.py`: per-trade max notional (parameterized off `BANKROLL_PER_SIDE_USD`), daily loss cap, drawdown trigger, manual kill switch (file flag `data/arb/HALT`).
- `src/ops/inventory_manager.py`: target ratios per venue; alert on imbalance (no auto-rebalance yet).
- `src/ops/health.py`: services up/down; integrates with existing `/api/monitor/services`.
- `restart_all.ps1` extended; `stop_all.ps1` mirrors.
- Tests: HALT flag halts within 1 cycle; loss cap triggers correctly.
- **Exit:** drill — flip HALT, all execution paths refuse within 2s.

### Phase 5 — Rule-based execution + MEV-lite (4–6 days)
**Now includes the MEV-lite layer per Q1.**
- `src/exec/bybit_leg.py`: spot taker order, retries, idempotency.
- `src/exec/dex_leg.py`: DEX swap with **mandatory `amountOutMin` + `deadline`**.
- `src/exec/private_rpc_router.py`: routes all DEX txs through a private RPC (Flashbots Protect on Eth-compatible L2 or equivalent on Base).
- `src/exec/bundle_simulator.py`: **mandatory `simulate_bundle()`** before every send. Aborts on revert.
- `src/exec/flashbots_executor.py`: bundle construction, signing, submission to relay.
- `src/exec/coordinator.py`: send both legs, monitor fills, log result. CEX hedge fires immediately via API; DEX leg goes through private bundle.
- **Gas-bid logic:** trade only sent if `expected_net_profit > simulated_gas + bribe_floor`.
- **Default mode = SHADOW.** TESTNET via env var. Mainnet flag separate and gated by a second env var.
- Dashboard card: live trade ledger, bundle inclusion rate, simulated-vs-actual gap.
- Tests: idempotency, partial-fill, cancel-on-timeout, both-legs-or-neither (with explicit unwind), bundle simulation parity.
- **Exit:** 100 testnet round-trips, zero stuck-leg incidents, bundle inclusion rate > 80%.

### Phase 6 — HistGBT spread-survival classifier (3 days)
- Label Phase-2 opportunities: success = realized net bps > threshold within window.
- `src/ml/hist_gbt.py`: LightGBM. Features = OBI, OBI-delta, cancellation rate, spread, gas, hour, volatility, recent realized slippage.
- Walk-forward CV; ablation report.
- Hook into `coordinator.py` as **veto** (default threshold 0.7).
- Reuse trading-bot's distributed training cluster for hyperparam sweep.
- Tests: model loads, predict_proba shape, regression test on golden dataset.
- **Exit:** holdout AUC > 0.65 AND simulated Sharpe improves vs Phase-3 baseline.

### Phase 7 — TFT trend overlay (2 days)
- Reuse trading bot's TFT. Use 60s forecast as **HistGBT feature**, not a separate veto.
- Tests: feature-pipeline parity between bot and arb project.
- **Exit:** TFT-as-feature improves HistGBT AUC by ≥ 0.02 (else drop).

### Phase 8 — Multi-relay MEV upgrade (1–2 days)
**Per Q1: this is an UPGRADE, not the introduction of MEV.**
- Add Flashbots + MEV Blocker + bloXroute + Eden as parallel broadcast targets.
- Inclusion-rate-weighted relay selection.
- **Add only if Phase-5 logs show inclusion-rate < 95% on the primary relay.**
- **Exit:** sandwich-attack rate < 0.5% AND inclusion rate > 95%.

### Phase 9 — Security/contract scanner (1 day, conditional)
- **Currently SKIPPED** — pilot pairs are all majors. Activates only if we add long-tail tokens.
- GoPlus API integration with cache; `trust_score < 80` → block.
- Tests: API mock, blocklist propagation.

### Phase 10 — Online drift detection (1 day)
- River-based feature-distribution drift watcher → dashboard alert.
- Triggers nightly retrain.
- **Exit:** drift alert fires correctly on a synthetic regime shift.

### Phase 11 — Paper-trade hardening (1 week real-time)
- Run end-to-end in SHADOW mode. Real fills not submitted but tracked.
- Track every Phase-3-simulator metric vs reality.
- Fix divergences (these are usually the bugs that lose money in production).
- **Exit:** 7 days, simulated-vs-paper PnL within ±15%, zero unhandled exceptions.

### Phase 12 — Live mainnet with caps (gradual)
- Revisit `BANKROLL_PER_SIDE_USD` (currently $500 stub per Q4) based on Phase-3/11 results.
- Start with **per-trade cap = 10% of bankroll, daily loss cap = 5% of bankroll**.
- Ramp 2x per profitable week, halt on any unhandled exception.
- Dashboard alerting tied to existing notification channel.

### Phase 13 (deferred) — DRL pathing
- Only if Phase-2 logs prove aggregator routes leave money on the table.
- Stable-Baselines3 PPO env mirroring the live decision loop.
- Train offline, deploy in shadow, then live behind a feature flag.

---

## 6. Infrastructure & ops

- **All paths on D:\** (per standing rule). DuckDB temp dir set to `data/arb/cache/duckdb_temp/`.
- **No pip cache on C:** every install uses `--no-cache-dir`.
- **Process model:** ingestion (1 proc), opportunity detector (1 proc), executor (1 proc), Flask dashboard (existing trading-bot proc), all monitored by `restart_all.ps1`.
- **Logging:** structured JSON to `logs/arb_<service>_<date>.jsonl`, daily rotation.
- **Secrets:** `.env` not committed; `.claude/settings.json` deny rules block `C:/` writes and force-push.
- **Backups:** `data/arb/db/` snapshotted to a second drive nightly.
- **Observability metrics:** bundle inclusion rate, fill rate, simulated-vs-real PnL gap, p99 cycle latency, OBI cancellation rate, gas-spend ratio, inventory imbalance.
- **Git lifecycle per phase:** commit-before → implement → tests/restart green → commit + push-after.

---

## 7. Improvements over the source spec (final list)

1. Drop atomic-MEV scope; do CEX-DEX statistical arb only.
2. Inventory-positioned model (no per-trade withdrawals).
3. Base L2 default, not Eth mainnet.
4. MEV-lite (private RPC + simulation + revert protection + gas bidding) shipped Phase 5; full multi-relay broadcast deferred to Phase 8 upgrade.
5. Aggregator-routing instead of DRL pathfinding (Phase-13 deferral).
6. Sequence: rules → log → train, not all-models-day-one.
7. Security layer conditional on long-tail tokens (Phase 9 skipped for pilot).
8. Whale tracker downgraded to Phase-N nice-to-have.
9. SHADOW → TESTNET → CAPPED-MAINNET ramp; no testnet-skip.
10. Same dashboard, lazy tab, separate process for ingestion (perf isolation).
11. Cancellation-rate spoofing detector promoted to P0 OBI feature.
12. Risk-first: HALT flag + daily loss cap + position cap before any execution code.
13. Replay simulator before live — non-negotiable.
14. Bundle simulation before every send (Phase 5).
15. `Profit > Gas + Bribe` hard gate.
16. `amountOutMin` + `deadline` always set.

---

## 8. Approval gate

**This v2 plan is awaiting:**
1. Final venue confirmation (Base assumed unless corrected).
2. Explicit approval ("approved" / "go" / "yes" / "implement").
3. Double-confirm step (per CLAUDE.md): I will restate the resolved plan in one paragraph and ask "Confirm to proceed?". Only the second affirmative starts Phase 0.

Sub-steps inside an approved phase are auto-approved. New scope mid-flight requires re-approval.
