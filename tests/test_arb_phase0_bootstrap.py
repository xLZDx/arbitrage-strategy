"""
Phase 0 bootstrap regression test.

Verifies all required scaffolding exists. Locks in the project layout so
later refactors that break it must update this test in the same commit
(per CLAUDE.md regression-test rule).

Run: python -m pytest tests/test_arb_phase0_bootstrap.py -v
Or:  python tests/test_arb_phase0_bootstrap.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FILES = [
    "CLAUDE.md",
    "core/PLAN.md",
    "core/ARCHITECTURE.md",
    "core/RISK.md",
    "pyproject.toml",
    ".gitignore",
    ".claude/settings.json",
    "restart_all.ps1",
    "stop_all.ps1",
    "arbitrage.txt",
]

REQUIRED_DIRS = [
    "src/data",
    "src/storage",
    "src/features",
    "src/strategy",
    "src/sim",
    "src/risk",
    "src/ops",
    "src/exec",
    "src/ml",
    "src/dashboard",
    "src/utils",
    "tests",
    "scripts",
    "logs",
    "models",
    "data/arb",
]

REQUIRED_PACKAGE_INITS = [
    "src/__init__.py",
    "src/data/__init__.py",
    "src/storage/__init__.py",
    "src/features/__init__.py",
    "src/strategy/__init__.py",
    "src/sim/__init__.py",
    "src/risk/__init__.py",
    "src/ops/__init__.py",
    "src/exec/__init__.py",
    "src/ml/__init__.py",
    "src/dashboard/__init__.py",
    "src/utils/__init__.py",
    "tests/__init__.py",
]

PLAN_REQUIRED_PHRASES = [
    "CEX-DEX statistical arbitrage",
    "MEV-lite",
    "Base",
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BANKROLL_PER_SIDE_USD",
]

CLAUDE_REQUIRED_PHRASES = [
    "Approval Gate",
    "No Guessing",
    "Regression Test Maintenance",
    "Git lifecycle",
    "All paths on D:",
]

SETTINGS_REQUIRED_DENIES = [
    "Bash(rm -rf",
    "Bash(git push --force",
    "Bash(git reset --hard",
]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_required_files_exist() -> None:
    missing = [f for f in REQUIRED_FILES if not (REPO_ROOT / f).is_file()]
    assert not missing, f"Missing required files: {missing}"


def test_required_dirs_exist() -> None:
    missing = [d for d in REQUIRED_DIRS if not (REPO_ROOT / d).is_dir()]
    assert not missing, f"Missing required dirs: {missing}"


def test_package_inits_exist() -> None:
    missing = [f for f in REQUIRED_PACKAGE_INITS if not (REPO_ROOT / f).is_file()]
    assert not missing, f"Missing __init__.py files: {missing}"


def test_plan_contains_locked_decisions() -> None:
    plan = _read("core/PLAN.md")
    missing = [p for p in PLAN_REQUIRED_PHRASES if p not in plan]
    assert not missing, f"PLAN.md missing locked-in phrases: {missing}"


def test_claude_md_contains_mandatory_rules() -> None:
    claude_md = _read("CLAUDE.md")
    missing = [p for p in CLAUDE_REQUIRED_PHRASES if p not in claude_md]
    assert not missing, f"CLAUDE.md missing mandatory sections: {missing}"


def test_settings_has_destructive_denies() -> None:
    settings = _read(".claude/settings.json")
    missing = [d for d in SETTINGS_REQUIRED_DENIES if d not in settings]
    assert not missing, f"settings.json missing destructive denies: {missing}"


def test_pyproject_declares_python_311() -> None:
    pyproject = _read("pyproject.toml")
    assert 'requires-python = ">=3.11"' in pyproject


def test_gitignore_excludes_secrets_and_data() -> None:
    gi = _read(".gitignore")
    for required in [".env", "venv/", "data/arb/", "logs/", "__pycache__/"]:
        assert required in gi, f".gitignore missing: {required}"


def test_restart_scripts_are_powershell() -> None:
    for script in ("restart_all.ps1", "stop_all.ps1"):
        content = _read(script)
        assert content.startswith("#") or "param(" in content, (
            f"{script} doesn't look like a PowerShell script"
        )


def _run_all() -> int:
    """Standalone runner for `python tests/test_arb_phase0_bootstrap.py`."""
    failures: list[tuple[str, str]] = []
    tests = [
        ("required_files_exist", test_required_files_exist),
        ("required_dirs_exist", test_required_dirs_exist),
        ("package_inits_exist", test_package_inits_exist),
        ("plan_contains_locked_decisions", test_plan_contains_locked_decisions),
        ("claude_md_contains_mandatory_rules", test_claude_md_contains_mandatory_rules),
        ("settings_has_destructive_denies", test_settings_has_destructive_denies),
        ("pyproject_declares_python_311", test_pyproject_declares_python_311),
        ("gitignore_excludes_secrets_and_data", test_gitignore_excludes_secrets_and_data),
        ("restart_scripts_are_powershell", test_restart_scripts_are_powershell),
    ]
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
    print()
    if failures:
        print(f"{len(failures)} / {len(tests)} FAILED")
        return 1
    print(f"{len(tests)} / {len(tests)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
