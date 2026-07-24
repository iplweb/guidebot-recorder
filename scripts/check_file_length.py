#!/usr/bin/env python3
"""Fail if any given source file exceeds the hard line-count limit.

Ruff has no file-length rule, so this stdlib-only pre-commit hook enforces the
second of the cleanup's two machine-checked limits (the first, cyclomatic
complexity ≤ 10, is ruff's ``C901``). See ``CLAUDE.md`` and
``docs/superpowers/specs/2026-07-22-code-cleanup-design.md`` (faza 6) for the why.

The limit is 600 lines per file, no exceptions and no per-file allowlist — a file
that grows past it is meant to be *split*, not exempted. The list of files to
check is passed on argv (pre-commit supplies the staged paths); the *scope* — which
extensions and directories are checked — is the hook's ``files:`` regex in
``.pre-commit-config.yaml``, not anything in here, so widening scope (e.g. adding
``.js`` after phase 7) is a one-line config edit and needs no change to this script.

Offenders are printed as ``path:NNN lines (limit 600)`` and the process exits 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

LIMIT = 600


def line_count(path: Path) -> int:
    """Number of lines in ``path``, counting a final unterminated line."""
    with path.open("rb") as handle:
        data = handle.read()
    if not data:
        return 0
    count = data.count(b"\n")
    if not data.endswith(b"\n"):
        count += 1
    return count


def offenders(paths: list[str]) -> list[str]:
    """Format one ``path:NNN lines (limit 600)`` message per file over the limit."""
    messages: list[str] = []
    for name in paths:
        path = Path(name)
        if not path.is_file():
            continue
        count = line_count(path)
        if count > LIMIT:
            messages.append(f"{name}:{count} lines (limit {LIMIT})")
    return messages


def main(argv: list[str]) -> int:
    """Print offenders and return a non-zero exit code when any file is too long."""
    messages = offenders(argv)
    for message in messages:
        print(message)
    return 1 if messages else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
