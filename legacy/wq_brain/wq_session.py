"""Shared WorldQuant BRAIN session for this repo.

Credential resolution order (first available wins):
    1. WQ_BRAIN_USERNAME / WQ_BRAIN_PASSWORD environment variables
    2. credential.txt at the project root — JSON array ["user", "pass"]
    3. legacy/wq_brain/credentials.json — JSON object {"email": ..., "password": ...}
       (same format the old WQ-Brain project used; still git-ignored)

Usage:
    from wq_session import BRAIN_SESSION, API_BASE
    r = BRAIN_SESSION.get(f"{API_BASE}/users/self/alphas", params={...})

Self-test (no credentials needed):
    ./.venv/bin/python legacy/wq_brain/wq_session.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

API_BASE = "https://api.worldquantbrain.com"

REPO_ROOT = Path(__file__).resolve().parents[2]
CREDENTIAL_TXT = REPO_ROOT / "credential.txt"
LEGACY_CREDENTIALS = Path(__file__).resolve().parent / "credentials.json"

HEADERS = {
    "Accept": "application/json;version=2.0",
    "Content-Type": "application/json",
}


def load_credentials() -> tuple[str, str]:
    """Resolve BRAIN credentials from env vars, credential.txt, or credentials.json."""
    env_user = os.getenv("WQ_BRAIN_USERNAME")
    env_pass = os.getenv("WQ_BRAIN_PASSWORD")
    if env_user and env_pass:
        return env_user, env_pass

    if CREDENTIAL_TXT.exists():
        username, password = json.loads(
            CREDENTIAL_TXT.read_text(encoding="utf-8")
        )
        return str(username), str(password)

    if LEGACY_CREDENTIALS.exists():
        creds = json.loads(LEGACY_CREDENTIALS.read_text(encoding="utf-8"))
        return str(creds["email"]), str(creds["password"])

    raise FileNotFoundError(
        "BRAIN credentials not found. Set WQ_BRAIN_USERNAME / WQ_BRAIN_PASSWORD, "
        'create credential.txt at the repo root as ["user", "pass"], or create '
        'legacy/wq_brain/credentials.json as {"email": ..., "password": ...}. '
        "All three locations are git-ignored."
    )


class RateLimitError(RuntimeError):
    """Raised when a simulation POST is rejected by rate limiting / concurrency limits."""


class SessionExpiredError(RuntimeError):
    """Raised when BRAIN reports expired or invalid credentials."""


def create_session() -> requests.Session:
    username, password = load_credentials()
    session = requests.Session()
    session.auth = HTTPBasicAuth(username, password)
    session.headers.update(HEADERS)

    resp = session.post(f"{API_BASE}/authentication")
    if resp.status_code != 201:
        raise RuntimeError(f"BRAIN auth failed: {resp.status_code} {resp.text}")
    return session


def api_get(session: requests.Session, url: str, retries: int = 4, **kwargs: Any) -> requests.Response:
    """GET with retry and exponential backoff; raises with a real message on 4xx/5xx."""
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=(10, 60), **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"GET {url} failed: {exc}") from exc
            time.sleep(2**attempt)
            continue
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "5")
            wait = float(retry_after) if retry_after.isdigit() else 5.0
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp
    raise RuntimeError(f"GET {url} still rate-limited after {retries} attempts")


def api_post(session: requests.Session, url: str, retries: int = 4, **kwargs: Any) -> requests.Response:
    """POST with retry/backoff; maps BRAIN auth and concurrency errors to typed exceptions."""
    for attempt in range(retries):
        try:
            resp = session.post(url, timeout=(10, 60), **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"POST {url} failed: {exc}") from exc
            time.sleep(2**attempt)
            continue
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "5")
            wait = float(retry_after) if retry_after.isdigit() else 5.0
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))[:300]
            except Exception:
                detail = resp.text[:300]
            if "credentials" in detail.lower():
                raise SessionExpiredError(f"BRAIN rejected the request (credentials expired?): {detail}")
            raise RuntimeError(f"POST {url} -> HTTP {resp.status_code}: {detail}")
        return resp
    raise RateLimitError(f"POST {url} still rate-limited after {retries} attempts")


# Create one lazily-initialized shared session.
BRAIN_SESSION: requests.Session | None = None


def get_session() -> requests.Session:
    global BRAIN_SESSION
    if BRAIN_SESSION is None:
        BRAIN_SESSION = create_session()
    return BRAIN_SESSION


def _self_test() -> int:
    """Verify structure without needing credentials."""
    import ast

    for fn in Path(__file__).resolve().parent.glob("*.py"):
        ast.parse(fn.read_text(encoding="utf-8"), filename=str(fn))
    print("All legacy/wq_brain scripts parse OK.")

    try:
        load_credentials()
    except FileNotFoundError as exc:
        print(f"Credential check (expected until you add keys): {exc}")
    else:
        print("Credentials found. Testing login...")
        session = get_session()
        me = api_get(session, f"{API_BASE}/users/self").json()
        print(f"Login OK as: {me.get('id', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
