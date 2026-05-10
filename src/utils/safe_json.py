"""
Atomic file-locked I/O for state files (HALT flag, inventory snapshots,
config caches). Mirrors the pattern from sister project's src/utils/safe_json.py
but standalone so we don't take a hard import dependency before the editable
install is wired.

Per CLAUDE.md: every state file write must be atomic. No partial-write states.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock


def _lock_for(path: Path) -> FileLock:
    return FileLock(str(path) + ".lock", timeout=10)


def read_json(path: Path | str, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with _lock_for(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)


def write_json(path: Path | str, data: Any) -> None:
    """Atomic JSON write: write to temp, fsync, replace."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(p):
        fd, tmp = tempfile.mkstemp(
            prefix=p.name + ".",
            suffix=".tmp",
            dir=str(p.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def append_jsonl(path: Path | str, record: dict[str, Any]) -> None:
    """Append one JSON line. Used for opportunity / trade / drill logs."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
    with _lock_for(p):
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
