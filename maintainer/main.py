"""Daily repository maintenance. See README.md.

Env: GH_TOKEN (required), DRY_RUN=true|false (default true), MAINTAINER_PAUSED, CONFIG, LOG_DIR
Exit code is non-zero only on real errors; "no safe change today" is a normal outcome.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from .fixers import FIXERS, Fix, changed_files
from .github_api import GitHub, GitHubError

SECRET_PATTERNS = re.compile(r"(\.env|id_rsa|\.pem$|\.key$|secrets?\.|credentials)", re.I)
RESULTS_TEMPLATE = {"repo": None, "outcome": None, "fix": None, "files": [], "branch": None,
                    "commit": None, "pr": None, "validation": None, "graph": None, "note": None}


def log_event(logdir: Path, event: dict) -> None:
    event = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **event}
    logdir.mkdir(parents=True, exist_ok=True)
    with (logdir / "runs.jsonl").open("a") as f:
        f.write(json.dumps(event) + "\n")
    print(json.dumps(event))


def git(args: list[str], cwd: Path, token: str | None = None, check: bool = True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    pre: list[str] = []
    if token:  # pass credentials via header, never in the URL or on disk
        b = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        pre = ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {b}"]
    r = subprocess.run(["git", *pre, *args], cwd=cwd, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        # stderr can echo the header; scrub before raising.
        msg = r.stderr.replace(token or "\0", "***")
        raise RuntimeError(f"git {args[0]} failed: {msg[:300]}")
    return r


def diff_stats(repo: Path) -> tuple[int, int]:
    out = git(["diff", "--numstat"], repo).stdout.splitlines()
    files = len(out)
    lines = sum(int(a) + int(b) for a, b, _ in (l.split("\t") for l in out) if a.isdigit() and b.isdigit())
    return files, lines


def validate(repo: Path, files: list[str], cfg: dict, repo_cfg: dict) -> tuple[bool, str]:
    nf, nl = diff_stats(repo)
    if nf > cfg["max_files_per_change"] or nl > cfg["max_changed_lines"]:
        return False, f"change too large ({nf} files, {nl} lines)"
    for f in files:
        if SECRET_PATTERNS.search(f):
            return False, f"touches sensitive-looking file {f}"
    for f in files:
        if f.endswith(".py"):
            r = subprocess.run([sys.executable, "-m", "py_compile", f], cwd=repo, capture_output=True, text=True)
            if r.returncode != 0:
                return False, f"py_compile failed for {f}"
    tc = repo_cfg.get("test_command")
    if tc:
        r = subprocess.run(tc, shell=True, cwd=repo, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return False, "test_command failed"
        return True, f"py_compile ok; tests passed ({tc})"
    only_docs = all(f.endswith((".md", ".rst", ".txt")) for f in files)
    return True, "docs-only change" if only_docs else "py_compile ok; no test_command configured"


def candidates(cfg: dict, gh: GitHub, logdir: Path) -> list[dict]:
    """Eligible repos, rotated by day so we don't hammer one project."""
    excl = {e.lower() for e in cfg.get("exclude", [])}
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for entry in cfg["repos"]:
        name = entry["name"]
        if name.lower() in excl:
            continue
        try:
            meta = gh.get_repo(name)
        except GitHubError as e:
            log_event(logdir, {"repo": name, "outcome": "skipped", "note": f"lookup failed: {e}"})
            continue
        why = None
        if meta.get("archived"):
            why = "archived"
        elif meta.get("fork"):
            why = "fork"
        elif not meta.get("permissions", {}).get("push"):
            why = "no push permission"
        else:
            pushed = dt.datetime.fromisoformat(meta["pushed_at"].replace("Z", "+00:00"))
            if (now - pushed).days > cfg.get("abandoned_after_days", 730):
                why = "abandoned"
        if why:
            log_event(logdir, {"repo": name, "outcome": "skipped", "note": why})
            continue
        out.append({"entry": entry, "meta": meta})
    if out:
        k = dt.date.today().toordinal() % len(out)
        out = out[k:] + out[:k]
    return out


def attempt_repo(c: dict, cfg: dict, gh: GitHub, token: str, author: tuple[str, str],
                 dry: bool, logdir: Path) -> dict | None:
    name = c["entry"]["name"]
    meta = c["meta"]
    base = meta["default_branch"]
    res = dict(RESULTS_TEMPLATE, repo=name)

    # Duplicate protection: skip if a maintainer PR is already open.
    if any(p["head"]["ref"].startswith("maintainer/") for p in gh.open_pulls(name)):
        res.update(outcome="skipped", note="maintainer PR already open")
        return res

    work = Path(tempfile.mkdtemp(prefix="maint-"))
    try:
        git(["clone", "--depth", "1", f"https://github.com/{name}.git", str(work / "r")], work, token)
        repo = work / "r"
        allowed = c["entry"].get("fixers") or cfg["fixers"]
        fix: Fix | None = None
        for fname in allowed:
            fix = FIXERS[fname](repo, cfg)
            if fix:
                break
            git(["checkout", "--", "."], repo)
        if not fix:
            res.update(outcome="no_change", note="no safe improvement found")
            return res

        files = changed_files(repo)
        ok, vmsg = validate(repo, files, cfg, c["entry"])
        res.update(fix=f"{fix.kind}: {fix.summary}", files=files, validation=vmsg)
        if not ok:
            res.update(outcome="rejected", note=vmsg)
            return res
        if dry:
            res.update(outcome="dry_run", note="validated; nothing pushed",
                       diff=git(["diff", "--stat"], repo).stdout.strip())
            return res

        branch_protected = False
        try:
            branch_protected = bool(gh.get_branch(name, base).get("protected"))
        except GitHubError:
            branch_protected = True  # be conservative
        direct = cfg["mode"] == "direct" and not branch_protected
        branch = base if direct else f"maintainer/{fix.kind}-{dt.date.today().isoformat()}"

        if not direct:
            git(["checkout", "-b", branch], repo)
        git(["add", "-A"], repo)
        git(["-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}",
             "commit", "-q", "-m", f"{fix.kind}: {fix.summary}"], repo)
        sha = git(["rev-parse", "HEAD"], repo).stdout.strip()
        git(["push", "origin", f"HEAD:refs/heads/{branch}"], repo, token)

        # Verify the push landed.
        remote = gh.get(f"/repos/{name}/git/ref/heads/{branch}")["object"]["sha"]
        if remote != sha:
            res.update(outcome="error", note="remote sha mismatch after push")
            return res
        res.update(commit=sha, branch=branch)

        if direct:
            res.update(outcome="pushed", graph="likely counts: commit on default branch with linked author email")
        else:
            pr = gh.create_pull(name, branch, base, f"{fix.kind}: {fix.summary}",
                                f"{fix.detail}\n\nValidation: {vmsg}\n\n_Opened by repo-maintainer._")
            res.update(outcome="pr_opened", pr=pr["html_url"],
                       graph="pending: counts toward the graph only after the PR is merged to the default branch")
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


def write_summary(results: list[dict], dry: bool) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as f:
        f.write(f"## repo-maintainer {'(dry run)' if dry else ''}\n\n| repo | outcome | detail |\n|---|---|---|\n")
        for r in results:
            detail = r.get("pr") or r.get("commit") or r.get("note") or ""
            f.write(f"| {r['repo']} | {r['outcome']} | {detail} |\n")


def main() -> int:
    cfg = yaml.safe_load(Path(os.environ.get("CONFIG", "config.yml")).read_text())
    logdir = Path(os.environ.get("LOG_DIR", "logs"))
    dry = os.environ.get("DRY_RUN", "true").lower() != "false"

    if cfg.get("paused") or os.environ.get("MAINTAINER_PAUSED", "").lower() == "true":
        log_event(logdir, {"outcome": "paused"})
        return 0
    token = os.environ.get("GH_TOKEN")
    if not token:
        log_event(logdir, {"outcome": "error", "note": "GH_TOKEN missing"})
        return 1

    gh = GitHub(token)
    email = cfg["author"].get("email")
    if not email:
        u = gh.get_user(cfg["owner"])
        email = f"{u['id']}+{u['login']}@users.noreply.github.com"
    author = (cfg["author"]["name"], email)

    results, errors = [], 0
    for c in candidates(cfg, gh, logdir):
        if len([r for r in results if r["outcome"] in ("pushed", "pr_opened", "dry_run")]) >= cfg["max_repos_per_run"]:
            break
        try:
            r = attempt_repo(c, cfg, gh, token, author, dry, logdir)
        except Exception as e:  # keep going with the next repo
            msg = str(e).replace(token, "***")
            r = dict(RESULTS_TEMPLATE, repo=c["entry"]["name"], outcome="error", note=msg[:300])
            errors += 1
        results.append(r)
        log_event(logdir, r)

    if not any(r["outcome"] in ("pushed", "pr_opened", "dry_run") for r in results):
        log_event(logdir, {"outcome": "no_change_day", "note": "no qualifying contribution today", "graph": "none"})
    write_summary(results, dry)
    return 1 if errors and not any(r["outcome"] in ("pushed", "pr_opened") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
