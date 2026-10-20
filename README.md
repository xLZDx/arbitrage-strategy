# Arbitrage Strategy

Research and engineering project for a proposed trading-arbitrage pipeline, combining market-data ingestion, opportunity detection, simulation, and a guarded execution path.

**Status:** The [architecture document](core/ARCHITECTURE.md) describes a Phase 0 design. The presence of source code is not proof that a profitable or production-safe strategy has been validated.

## Project components

- `src/` — application and strategy implementation.
- `models/` — model-related assets and definitions.
- `scripts/` — operational and development helpers.
- `tests/` — automated test cases.
- `core/` — architecture, risk, plan, decisions, and reviews.
- `restart_all.ps1` and `stop_all.ps1` — Windows process-control helpers.

The documented architecture includes Bybit spot-market data, DEX quotes, DuckDB/Parquet data storage, an opportunity process, and an optional execution pipeline. Some components depend on a separate trading-assistance project; consult the exact dependency configuration in `pyproject.toml`.

## Development

Read [core/PLAN.md](core/PLAN.md), [core/ARCHITECTURE.md](core/ARCHITECTURE.md), and [core/RISK.md](core/RISK.md) before executing any strategy. Check `pyproject.toml` for the supported Python environment, local path dependencies, and test tooling.

## Historical research translation

The original [mixed-language arbitrage research notebook](arbitrage.txt) remains unchanged. Its [English translation of the Russian passages](docs/ARBITRAGE_RUSSIAN_PASSAGES_EN_2026-10-10.md) covers Weighted OBI, HistGBT/TFT/DRL proposals, DuckDB/QuestDB notes, and the proposed execution pipeline. It is **historical research**, not implementation evidence or approval to trade.

## Safety

Treat all outputs as research until independently validated with realistic fees, slippage, latency, and out-of-sample evidence. Do not enable real orders, access live credentials, or claim positive expected value from a backtest alone. Keep live execution off unless separately authorized and approved.

## Language policy

Use English for all new GitHub-facing text, documentation, comments, commits, issues, and PRs. Keep exchange identifiers and structured market data unchanged.
