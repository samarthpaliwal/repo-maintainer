# repo-maintainer

Runs daily at 10:00 America/Phoenix (17:00 UTC) on GitHub Actions, looks for small, verifiable
improvements in an allowlist of your repos, validates them, and opens a pull request (or pushes
directly, if you opt in). If nothing safe exists, it logs a no-change day. It never makes empty
or filler commits.

## What it can fix (deliberately narrow)
- `typos`: spelling errors in `*.md`, `*.rst`, `*.txt` (codespell)
- `unused_imports`: unused imports in Python files, never `__init__.py` (ruff F401)

Every change is size-capped, checked for sensitive filenames, `py_compile`d, and run through the
repo's optional `test_command`. Add more fixers in `maintainer/fixers.py`.

## Deploy
1. Create a **private** repo `repo-maintainer` under your account and push this folder to it.
2. Create a **fine-grained personal access token** (Settings > Developer settings):
   - Resource owner: you. Repository access: **Only select repositories**, the same ones as `config.yml`.
   - Permissions: Contents: Read and write; Pull requests: Read and write; Metadata: Read.
   - Set an expiry (e.g. 90 days) and calendar a renewal.
3. In `repo-maintainer` > Settings > Secrets and variables > Actions, add secret `MAINTAINER_TOKEN`.
4. Edit `config.yml` (allowlist, mode). Default mode is `pr`.
5. Actions tab > **daily-maintenance** > Run workflow with `dry_run` checked. Read the job summary.
6. Run once with `dry_run` unchecked on one repo to verify a real PR opens.
7. Go live for the schedule: add repository **variable** `LIVE` = `true`. Until then scheduled
   runs are dry runs.

## Operate
- **Pause:** variable `MAINTAINER_PAUSED=true` (or `paused: true` in config). **Resume:** delete/false.
- **Add a repo:** add it to `config.yml` *and* to the token's repository list.
- **Remove a repo:** delete it from both, or add it to `exclude`.
- **Logs:** each run's job summary, plus a `run-log` artifact (`runs.jsonl`, 90 days) with repo,
  files, commit SHA, PR URL, validation result, and whether it should count toward the graph.
  Logs are not committed to any repo (that would itself be filler activity).

## Test procedure
1. `pip install -r requirements.txt && python -m pytest -q` (6 unit tests).
2. Dry run locally: `GH_TOKEN=... DRY_RUN=true python -m maintainer.main`; check `logs/runs.jsonl`.
3. Plant a typo in a throwaway allowlisted repo's README, run the workflow live, confirm the PR,
   merge it, confirm the commit shows on your profile within a few minutes.

## Limits you should know
- **Timing:** GitHub cron is best-effort; runs can start minutes late and are occasionally dropped
  under load. Scheduled workflows are also auto-disabled after 60 days of **no repo activity** in
  the workflow's own repo; re-enable in the Actions tab if that happens.
- **Graph:** a commit counts only if it is on the default branch (or `gh-pages`), the author email is
  linked to your account, and the repo isn't a fork. PR-mode commits count **after you merge**.
  Private-repo contributions show only if "Private contributions" is enabled in your profile
  settings. Many days will have no qualifying fix, so the graph will not be solid green, and
  that's by design.
- **Dependencies:** this tool never changes dependencies or config files.
