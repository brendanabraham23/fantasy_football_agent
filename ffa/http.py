"""Tiny HTTP helper with an on-disk cache so repeated runs don't hammer APIs."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import requests

CACHE_DIR = Path(os.environ.get("FFA_CACHE_DIR", Path.home() / ".cache" / "ffa"))

_session = requests.Session()
_session.headers["User-Agent"] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ffa-fantasy-analyzer/1.0"
)


def _cache_path(key: str) -> Path:
    return CACHE_DIR / hashlib.sha1(key.encode()).hexdigest()


def get(url: str, params=None, ttl: int = 0, timeout: int = 20) -> bytes:
    """GET a URL. If ttl > 0, responses are cached on disk for ttl seconds."""
    key = url + json.dumps(params, sort_keys=True, default=str)
    path = _cache_path(key)
    if ttl and path.exists() and time.time() - path.stat().st_mtime < ttl:
        return path.read_bytes()
    resp = _session.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    if ttl:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return resp.content


def get_json(url: str, params=None, ttl: int = 0, timeout: int = 20):
    return json.loads(get(url, params=params, ttl=ttl, timeout=timeout) or b"null")


def get_text(url: str, params=None, ttl: int = 0, timeout: int = 20) -> str:
    return get(url, params=params, ttl=ttl, timeout=timeout).decode("utf-8", "replace")
