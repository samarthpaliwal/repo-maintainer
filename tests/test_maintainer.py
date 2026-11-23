import datetime as dt
import subprocess
from pathlib import Path

from maintainer import fixers, main

CFG = {"max_files_per_change": 5, "max_changed_lines": 60, "typo_ignore_words": [],
       "abandoned_after_days": 730, "repos": [], "exclude": ["o/skip"]}


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    for n, c in files.items():
        (r / n).write_text(c)
    for cmd in (["init", "-q"], ["add", "-A"],
                ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=r, check=True)
    return r


def test_typo_fixer(tmp_path):
    r = make_repo(tmp_path, {"README.md": "This projcet recieves data.\n"})
    fix = fixers.fix_typos(r, CFG)
    assert fix and fix.kind == "docs"
    assert "project" in (r / "README.md").read_text()


def test_typo_fixer_no_change(tmp_path):
    r = make_repo(tmp_path, {"README.md": "All good here.\n"})
    assert fixers.fix_typos(r, CFG) is None


def test_unused_imports_skips_init(tmp_path):
    r = make_repo(tmp_path, {"a.py": "import os\nprint(1)\n", "__init__.py": "import os\n"})
    fix = fixers.fix_unused_imports(r, CFG)
    assert fix and fixers.changed_files(r) == ["a.py"]
    ok, msg = main.validate(r, ["a.py"], CFG, {})
    assert ok, msg


def test_validate_rejects_secret_and_large(tmp_path):
    r = make_repo(tmp_path, {"a.txt": "x\n"})
    ok, _ = main.validate(r, [".env"], CFG, {})
    assert not ok
    ok, _ = main.validate(r, ["a.txt"], dict(CFG, max_files_per_change=0), {})
    assert ok  # no diff -> 0 files, within limit


def test_validate_failing_test_command(tmp_path):
    r = make_repo(tmp_path, {"a.txt": "x\n"})
    ok, msg = main.validate(r, ["a.txt"], CFG, {"test_command": "false"})
    assert not ok and "test_command" in msg


class FakeGH:
    def __init__(self, metas): self.metas = metas
    def get_repo(self, n): return self.metas[n]


def meta(**kw):
    base = {"archived": False, "fork": False, "permissions": {"push": True},
            "pushed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")}
    return {**base, **kw}


def test_candidates_filtering(tmp_path):
    names = ["o/ok", "o/arch", "o/fork", "o/ro", "o/old", "o/skip"]
    gh = FakeGH({"o/ok": meta(), "o/arch": meta(archived=True), "o/fork": meta(fork=True),
                 "o/ro": meta(permissions={"push": False}),
                 "o/old": meta(pushed_at="2020-01-01T00:00:00Z"), "o/skip": meta()})
    cfg = dict(CFG, repos=[{"name": n} for n in names])
    out = main.candidates(cfg, gh, tmp_path)
    assert [c["entry"]["name"] for c in out] == ["o/ok"]
