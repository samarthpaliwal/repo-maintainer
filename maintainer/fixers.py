"""Deterministic, conservative fixers. Each runs inside a cloned working tree and
only edits files; the caller diffs, validates, and commits.

A fixer returns a Fix describing what it did, or None if it found nothing.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

DOC_EXT = (".md", ".rst", ".txt")
SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "dist", "build", "vendor", "__pycache__"}


@dataclass
class Fix:
    kind: str          # conventional-commit type, e.g. "docs", "refactor"
    summary: str       # commit subject (without type prefix)
    detail: str        # PR body text


def _run(cmd: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)


def tracked(repo: Path, suffixes: tuple[str, ...]) -> list[str]:
    out = _run(["git", "ls-files"], repo, check=True).stdout.splitlines()
    return [
        f for f in out
        if f.endswith(suffixes) and not (set(Path(f).parts) & SKIP_DIRS)
    ]


def changed_files(repo: Path) -> list[str]:
    return _run(["git", "diff", "--name-only"], repo).stdout.splitlines()


def fix_typos(repo: Path, cfg: dict) -> Fix | None:
    files = tracked(repo, DOC_EXT)
    if not files:
        return None
    ignore = ",".join(cfg.get("typo_ignore_words", []))
    cmd = ["codespell", "-w", "-q", "3"]
    if ignore:
        cmd += ["-L", ignore]
    _run(cmd + files, repo)
    changed = changed_files(repo)
    if not changed:
        return None
    return Fix(
        "docs",
        "fix spelling errors in documentation",
        "Corrects spelling errors found by `codespell` in: " + ", ".join(f"`{f}`" for f in changed),
    )


def fix_unused_imports(repo: Path, cfg: dict) -> Fix | None:
    files = [f for f in tracked(repo, (".py",)) if Path(f).name != "__init__.py"]
    if not files:
        return None
    # --isolated: ignore the project's ruff config so behavior is predictable.
    _run(["ruff", "check", "--isolated", "--select", "F401", "--fix", "-q"] + files, repo)
    changed = changed_files(repo)
    if not changed:
        return None
    return Fix(
        "refactor",
        "remove unused imports",
        "Removes unused imports (ruff `F401`) from: " + ", ".join(f"`{f}`" for f in changed),
    )


FIXERS = {"typos": fix_typos, "unused_imports": fix_unused_imports}
