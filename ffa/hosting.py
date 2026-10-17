"""Hosted mode: reports live on a git `data` branch and pipeline runs happen in GitHub Actions.

Enabled when FFA_DATA_REPO (owner/repo) is set. The server keeps a shallow clone of the data branch,
pulls it every `hosting.sync_minutes`, and "Run pipeline" dispatches the predict workflow instead of
running locally (free hosts have small, ephemeral disks and sleep between visits).

Environment:
  FFA_DATA_REPO      owner/repo holding the data branch and the workflow (required to enable)
  FFA_GITHUB_TOKEN   token with Actions read/write on that repo; needed for Run pipeline (and for a
                     private repo's data branch). Without it the UI is read-only.
  FFA_WORKFLOW_REF   branch the workflow file lives on (default: the repo's default branch)
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://api.github.com"


@dataclass
class Hosting:
    repo: str
    clone_dir: Path
    token: str | None = None
    branch: str = "data"
    workflow: str = "predict.yml"
    ref: str | None = None
    sync_minutes: float = 5
    remote_url: str | None = None  # override (tests, mirrors); default github.com/<repo>
    synced_at: float | None = None
    last_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_env(cls, cfg: dict, clone_dir: str | Path) -> "Hosting | None":
        repo = os.environ.get("FFA_DATA_REPO", "").strip()
        if not repo:
            return None
        h = cfg.get("hosting", {})
        return cls(repo=repo, clone_dir=Path(clone_dir), token=os.environ.get("FFA_GITHUB_TOKEN") or None,
                   branch=h.get("data_branch", "data"), workflow=h.get("workflow", "predict.yml"),
                   ref=os.environ.get("FFA_WORKFLOW_REF") or None, sync_minutes=h.get("sync_minutes", 5))

    @property
    def reports_dir(self) -> Path:
        return self.clone_dir / "reports"

    @property
    def can_run(self) -> bool:
        return bool(self.token)

    # ---- data branch -------------------------------------------------------------
    def _remote(self) -> str:
        if self.remote_url:
            return self.remote_url
        auth = f"x-access-token:{self.token}@" if self.token else ""
        return f"https://{auth}github.com/{self.repo}.git"

    def sync(self) -> bool:
        """Clone or fast-forward the data branch. Returns True if it now holds data."""
        with self._lock:
            d = self.clone_dir
            try:
                if (d / ".git").is_dir():
                    git(["fetch", "--depth", "1", "origin", self.branch], d)
                    git(["reset", "--hard", "FETCH_HEAD"], d)
                else:
                    d.parent.mkdir(parents=True, exist_ok=True)
                    git(["clone", "--depth", "1", "--branch", self.branch, self._remote(), str(d)], d.parent)
                self.synced_at, self.last_error = time.time(), None
                return True
            except subprocess.CalledProcessError as exc:
                msg = (exc.stderr or "").replace(self.token or "\0", "***").strip().splitlines()
                self.last_error = msg[-1] if msg else str(exc)
                print(f"[warn] couldn't sync the '{self.branch}' branch of {self.repo}: {self.last_error}")
                return False

    def start_sync_loop(self, on_change=None) -> threading.Thread:
        def loop():
            while True:
                time.sleep(max(30.0, self.sync_minutes * 60))
                before = head(self.clone_dir)
                if self.sync() and on_change and head(self.clone_dir) != before:
                    on_change()
        t = threading.Thread(target=loop, daemon=True, name="ffa-data-sync")
        t.start()
        return t

    # ---- workflow ----------------------------------------------------------------
    def _call(self, method: str, path: str, body: dict | None = None):
        return gh_request(method, f"{API}/repos/{self.repo}{path}", self.token, body)

    def dispatch(self, news: bool, weather: bool) -> None:
        ref = self.ref or self._call("GET", "")["default_branch"]
        self._call("POST", f"/actions/workflows/{self.workflow}/dispatches",
                   {"ref": ref, "inputs": {"news": str(news).lower(), "weather": str(weather).lower()}})

    def latest_run(self) -> dict | None:
        runs = self._call("GET", f"/actions/workflows/{self.workflow}/runs?per_page=1") or {}
        return (runs.get("workflow_runs") or [None])[0]


def git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).stdout


def head(d: Path) -> str | None:
    try:
        return git(["rev-parse", "HEAD"], d).strip()
    except Exception:
        return None


def gh_request(method: str, url: str, token: str | None, body: dict | None = None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, url, headers=headers, data=json.dumps(body) if body else None, timeout=20)
    if resp.status_code >= 400:
        detail = resp.json().get("message", resp.text) if resp.content else resp.reason
        raise RuntimeError(f"GitHub API {resp.status_code}: {detail}")
    return resp.json() if resp.content else None


def run_view(run: dict | None, since: str | None) -> dict:
    """A GitHub Actions run as the UI's job view (idle | running | done | error)."""
    if not run or (since and run["created_at"] < since):  # both "YYYY-MM-DDTHH:MM:SSZ"
        return {"state": "running" if since else "idle",
                "log": ["Waiting for GitHub Actions to start the run…"] if since else [],
                "started_at": since, "finished_at": None, "options": {}, "error": None}
    url = run.get("html_url")
    if run["status"] != "completed":
        step = "Queued on GitHub Actions" if run["status"] in ("queued", "waiting", "pending") else \
            "Running on GitHub Actions (a few minutes)"
        return {"state": "running", "log": [url or "", step], "started_at": run["created_at"],
                "finished_at": None, "options": {}, "error": None, "url": url}
    ok = run.get("conclusion") == "success"
    return {"state": "done" if ok else "error", "log": [url or ""], "started_at": run["created_at"],
            "finished_at": run.get("updated_at"), "options": {}, "url": url,
            "error": None if ok else f"GitHub Actions run {run.get('conclusion')} (see {url})"}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
