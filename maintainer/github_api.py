"""Minimal GitHub REST client with retry/backoff. Never logs the token."""
from __future__ import annotations

import time

import requests

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str, retries: int = 3):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "repo-maintainer",
            }
        )
        self.retries = retries

    def request(self, method: str, path: str, **kw):
        # Only retry idempotent reads; writes are never blindly retried.
        attempts = self.retries if method == "GET" else 1
        for i in range(attempts):
            try:
                r = self.s.request(method, f"{API}{path}", timeout=30, **kw)
            except requests.RequestException as e:
                if i + 1 == attempts:
                    raise GitHubError(f"{method} {path}: {type(e).__name__}") from None
                time.sleep(2**i)
                continue
            if r.status_code in (500, 502, 503, 504) and i + 1 < attempts:
                time.sleep(2**i)
                continue
            if r.status_code >= 400:
                raise GitHubError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
            return r.json() if r.content else {}
        raise GitHubError(f"{method} {path}: exhausted retries")

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def get_user(self, login: str) -> dict:
        return self.get(f"/users/{login}")

    def get_repo(self, full_name: str) -> dict:
        return self.get(f"/repos/{full_name}")

    def get_branch(self, full_name: str, branch: str) -> dict:
        return self.get(f"/repos/{full_name}/branches/{branch}")

    def open_pulls(self, full_name: str) -> list[dict]:
        return self.get(f"/repos/{full_name}/pulls", params={"state": "open", "per_page": 100})

    def recent_commits(self, full_name: str, n: int = 5) -> list[dict]:
        return self.get(f"/repos/{full_name}/commits", params={"per_page": n})

    def create_pull(self, full_name, head, base, title, body) -> dict:
        return self.post(
            f"/repos/{full_name}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
