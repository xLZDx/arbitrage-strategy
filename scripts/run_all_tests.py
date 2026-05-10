"""
Run every tests/test_arb_*.py via its standalone runner.

Per CLAUDE.md regression-test rule: 0 failures gate every commit-after-push.
This script is the canonical way to verify the suite locally.

Run:
  ./venv/Scripts/python.exe scripts/run_all_tests.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
PYTHON = REPO_ROOT / "venv" / "Scripts" / "python.exe"


def main() -> int:
    if not PYTHON.exists():
        print(f"venv missing: {PYTHON}", file=sys.stderr)
        return 2
    test_files = sorted(TESTS_DIR.glob("test_arb_*.py"))
    if not test_files:
        print("no test files found", file=sys.stderr)
        return 1
    failures = 0
    total_files = 0
    for tf in test_files:
        total_files += 1
        print(f"\n=== {tf.name} ===")
        rc = subprocess.run(
            [str(PYTHON), str(tf)],
            cwd=str(REPO_ROOT),
            check=False,
        ).returncode
        if rc != 0:
            failures += 1
    print()
    if failures:
        print(f"{failures} / {total_files} test FILES FAILED")
        return 1
    print(f"All {total_files} test files passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
