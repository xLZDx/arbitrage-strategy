# Architecture — arbitrage_strategy

**Version:** Phase 0 draft. Updated each phase.
**Source of truth for scope:** `core/PLAN.md`.

---

## 1. Process model

```
┌─────────────────────────────────────────────────────────────────┐
│                      Bybit Spot (CEX)                            │
│                  REST + WebSocket L2 (50 lvl)                    │
└──────┬──────────────────────────────────────────┬───────────────┘
       │ ticks                                    │ orders
       ▼                                          ▲
┌──────────────────┐    ┌───────────────────┐    │
│  ingestion proc  │───▶│ data/arb/db (DuckDB)   │
│  (1 process)     │    │  partitioned Parquet  │
│  - bybit_l2_ws   │    └─────┬─────────────┘    │
│  - dex_quote     │          │ READ (lock)      │
│  - gas_oracle    │          ▼                  │
│  - features/obi  │    ┌─────────────────┐      │
└──────────────────┘    │  Flask (5000)   │      │
                        │  /api/arb/*     │      │
                        │  Arbitrage tab  │      │
                        └─────────────────┘      │
                                                 │
┌──────────────────┐                             │
│ opportunity proc │─── reads same DB ───┐       │
│  (1 process)     │                     │       │
│  - rule detector │                     ▼       │
│  - PnL simulator │              ┌──────────────┴─────┐
│  - HistGBT veto  │              │ executor process    │
│  - TFT feature   │──signals────▶│  (1 process)        │
└──────────────────┘              │  - bybit_leg        │
                                  │  - dex_leg          │
                                  │  - bundle_simulator │
                                  │  - private_rpc      │
                                  │  - flashbots_exec   │
                                  │  - coordinator      │
                                  └──┬─────────────┬────┘
                                     │             │
                                     ▼             ▼
                              ┌─────────┐    ┌──────────┐
                              │ Bybit   │    │ Base RPC │
                              │ REST    │    │ private  │
                              └─────────┘    └──────────┘
```

Three independent processes + the existing trading-bot Flask process. Each has its own log file under `logs/arb_<service>_<date>.jsonl`. `restart_all.ps1` orchestrates them.

---

## 2. Storage

- **DuckDB + partitioned Parquet** under `data/arb/db/`, partitioned by `(date, pair)`.
- Wrapper: `src/storage/arb_store.py` — uses `parquet_store.ParquetClient` from sister project. Holds `_duck_lock` during every `con.execute()` (per the 2026-05-08 thread-safety incident in the sister project).
- DuckDB temp dir: `data/arb/cache/duckdb_temp/`.
- Hot data tables: `obi_snapshots`, `dex_quotes`, `gas_history`, `opportunities`, `trades`, `inventory`.

---

## 3. Reuse from sister project

Imported via path-editable install in `pyproject.toml`:
```
ai_trading_assistance = { path = "../AI trading assistance", develop = true }
```

| Sister-project module | How we use it |
|---|---|
| `src.database.parquet_store.ParquetClient` | Storage backend (wrapped by `arb_store`). |
| `src.utils.safe_json` | Atomic file-locked I/O for HALT flag, config snapshots, inventory state. |
| `src.utils.config` | Re-exported constants extended with arb-specific keys. |
| `src.exchanges.bybit.*` (REST + WS) | Bybit REST client; WS extended with L2-depth subscription in Phase 1. |
| `src.models.tft.*` | Phase 7 — load existing TFT weights, expose 60s forecast as a feature. |
| Distributed training cluster (port 7700) | Phase 6 — HistGBT hyperparam sweep. |

---

## 4. Dashboard wiring

- Existing Flask app at port 5000 imports an `/api/arb/*` blueprint exposed by `src.dashboard.arb_blueprint` in this project.
- New tab "Arbitrage" added to the sister project's `templates/` (or via a dynamic tab registration if the sister project supports it — verify in Phase 0 follow-up).
- Cards (added per phase):
  - Phase 1: live OBI sparkline, DEX vs Bybit spread heatmap.
  - Phase 2: opportunity feed table + would-have-been PnL chart.
  - Phase 3: simulated equity curve, hit rate, average net bps.
  - Phase 4: risk-state strip (HALT, daily loss, drawdown).
  - Phase 5: live trade ledger, bundle inclusion rate, sim-vs-actual PnL gap.
- **Latency budget:** dashboard p95 ≤ baseline + 20%. Fallback to port 5001 if breached.

---

## 5. Phase-0 deliverables (this phase)

- [x] `.claude/settings.json` (allow + deny rules)
- [x] `core/PLAN.md` v2
- [x] `core/CLAUDE.md`
- [x] `core/ARCHITECTURE.md` (this file)
- [ ] `core/RISK.md`
- [ ] `pyproject.toml` (skeleton, no install yet)
- [ ] `.gitignore`
- [ ] `restart_all.ps1`, `stop_all.ps1` (empty service list)
- [ ] First commit + push

Phase 1 begins after Phase 0 commit lands and remote is configured.
