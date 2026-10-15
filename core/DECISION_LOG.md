# Arbitrage Strategy — Decision Log

## 2026-08-30 — W5: add uv.lock (pyproject.toml had no dependency lockfile)

**Decision:** This repo had `pyproject.toml` but no lockfile at all, flagged in a
harness-engineering audit of the whole `D:\Repo` workspace
(`D:\Repo\reports\HARNESS_AUDIT_2026-08-30.ru.html`). Generated `uv.lock` via
`py -3 -m uv lock --system-certs` -- `uv` is installed as a Python package on this
machine (`py -3 -m uv`), not on PATH as a standalone binary; `--system-certs` was required
to route around this machine's Norton antivirus TLS interception on `pypi.org` (a documented
local environment issue, not a security downgrade -- it uses the OS trust store, never disables
certificate verification).

**Fix:** `uv.lock` added, resolving 151 packages across the `requires-python = ">=3.11"` range
declared in `pyproject.toml`. No changes to `pyproject.toml` itself, no dependency version
changes, no code changes.

**Evidence:** `uv lock` completed with `Resolved 151 packages in 5.12s`, no errors. Commit
stages `uv.lock` only; several other files in this repo were left dirty by a different,
concurrent session's in-progress work at the time of this commit and are untouched.

**Known gap, not fixed here (GPT-PM review, MAJOR, verified against source before accepting):**
adding `uv.lock` alone does not make installs reproducible, because the repo's own documented
bootstrap path still bypasses it -- `pyproject.toml:52` says `pip install -e .`, and
`core/PLAN.md:150,253` confirms the venv setup uses plain `pip install --no-cache-dir`. Neither
consumes `uv.lock` (per Astral's own docs, a `uv.lock` is read by `uv sync`/`uv run`, not by
`pip`), so two clean `pip install -e .` runs against the `>=`-constrained versions in
`pyproject.toml` can still resolve different dependency graphs. Deliberately NOT fixed in this
commit: `core/PLAN.md` is a file another concurrent session has actively dirty right now, and
`pyproject.toml`'s install instructions are this project's own tooling choice, not a mechanical
harness-audit gap. Recorded here rather than silently closed; migrating the bootstrap path to
`uv sync --frozen` (or exporting a pip-consumable locked artifact) is left to a session actually
working in this project.

## 2026-10-16 -- SDLC workflow pointer added to CLAUDE.md

Operator request: every project points its sessions to the global skill `sdlc-orchestrator` (virtual scrum team). Change limited to `CLAUDE.md`; no behavior change.
