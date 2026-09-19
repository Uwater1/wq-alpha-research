"""Minimal BRAIN HTTP client for the persistent scheduler (TODO P2).

The legacy `legacy/wq_brain/wq_session.py` helper stays as it is (its scripts and
tests depend on it); this module is the modern client for the queue-driven pipeline:
session refresh, Retry-After-aware requests, and split submit/poll so a scheduler can
hold several simulations open at once.

Usage:
    from brain_api import BrainClient

    client = BrainClient()
    sim = client.submit("rank(close)", {"region": "USA"})   # -> SimulationHandle
    state = client.poll(sim)                                 # running / done / error
    metrics = client.alpha_metrics(state.alpha_id)

All requests raise:
    SessionExpiredError   credentials/session rejected -> call client.reauthenticate()
    RateLimitError        still limited after retries  -> wait retry_after seconds
    BrainAPIError         anything else, with the HTTP status attached

Nothing here touches a database or prints secrets; the scheduler decides what to do.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests
from requests.auth import HTTPBasicAuth

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
API_BASE = "https://api.worldquantbrain.com"

HEADERS = {
    "Accept": "application/json;version=2.0",
    "Content-Type": "application/json",
}

DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_BASE = 0.5


class BrainAPIError(RuntimeError):
    """Non-retryable BRAIN failure; carries the HTTP status when there was one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SessionExpiredError(BrainAPIError):
    """BRAIN no longer accepts the session (401/403 or an expired-credentials payload)."""


class RateLimitError(BrainAPIError):
    """Rate limiting persisted through the retries; ``retry_after`` is the server hint."""

    def __init__(self, message: str, retry_after: float = 5.0) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


@dataclass
class SimulationHandle:
    """A submitted simulation that still has to be polled."""

    candidate_id: int | None
    canonical_key: str
    expression: str
    simulation_id: str
    url: str
    submitted_at: float
    polls: int = 0
    next_poll_at: float = 0.0
    last_progress: float = 0.0

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.submitted_at


@dataclass
class PollState:
    """One poll result: still running, finished with an alpha, or failed."""

    status: str  # RUNNING | DONE | ERROR
    progress: float | None = None
    alpha_id: str | None = None
    message: str | None = None
    # Multi-simulation progress reports one entry per child expression (TODO P3).
    children: list["PollState"] = field(default_factory=list)


def load_credentials() -> tuple[str, str]:
    """Env vars first, then the encrypted credential.txt (same order as the legacy tooling)."""
    env_user = os.getenv("WQ_BRAIN_USERNAME")
    env_password = os.getenv("WQ_BRAIN_PASSWORD")
    if env_user and env_password:
        return env_user, env_password

    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from credential_crypto import load_credentials_from_disk

    return load_credentials_from_disk(REPO_ROOT)


def simulation_payload(
    expression: str | Sequence[str], settings: Mapping[str, Any], *, multi: bool = False
) -> dict[str, Any]:
    """Request body for POST /simulations (``multi`` sends a list of expressions).

    BRAIN validates ``settings.visualization`` as required, so it is always present:
    the canonical settings deliberately drop it (it cannot change a result), which is
    why the request body is assembled here rather than by the caller.
    """
    body_settings = {"visualization": False, **dict(settings)}
    if multi:
        expressions = [expression] if isinstance(expression, str) else list(expression)
        return {"type": "MULTI", "settings": body_settings, "regular": expressions}
    return {"type": "REGULAR", "settings": body_settings, "regular": str(expression)}


def alpha_metrics(alpha: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the IS metrics and checks a candidate row needs from an /alphas payload."""
    is_block = alpha.get("is") if isinstance(alpha.get("is"), Mapping) else {}
    checks = [c for c in (is_block.get("checks") or []) if isinstance(c, Mapping)]
    return {
        "sharpe": is_block.get("sharpe"),
        "fitness": is_block.get("fitness"),
        "turnover": is_block.get("turnover"),
        "drawdown": is_block.get("drawdown"),
        "checks": [
            {k: c.get(k) for k in ("name", "result", "value", "limit") if k in c}
            for c in checks
        ],
        "passed": sum(1 for c in checks if c.get("result") == "PASS"),
    }


class BrainClient:
    """Thin wrapper around ``requests`` that knows BRAIN's rate-limit conventions."""

    def __init__(self, session: requests.Session | None = None, *, retries: int = DEFAULT_RETRIES) -> None:
        self._session = session
        self.retries = retries
        self.last_latency: float = 0.0
        self.auth_count = 0

    # -- session -----------------------------------------------------------

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = self.create_session()
        return self._session

    def create_session(self) -> requests.Session:
        username, password = load_credentials()
        session = requests.Session()
        session.auth = HTTPBasicAuth(username, password)
        session.headers.update(HEADERS)
        response = session.post(f"{API_BASE}/authentication", timeout=(10, 60))
        self.auth_count += 1
        if response.status_code != 201:
            raise BrainAPIError(f"BRAIN auth failed: HTTP {response.status_code}", response.status_code)
        self._session = session
        return session

    def reauthenticate(self) -> None:
        """Drop the current session and log in again (TODO P2: session expiry)."""
        self._session = None
        self._session = self.create_session()

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    # -- HTTP --------------------------------------------------------------

    @staticmethod
    def retry_after_seconds(response: requests.Response, default: float = 5.0) -> float:
        """BRAIN's Retry-After is sometimes a plain number, sometimes HTTP-date-ish."""
        raw = response.headers.get("Retry-After")
        if not raw:
            return default
        try:
            return max(0.0, float(str(raw).strip()))
        except (TypeError, ValueError):
            return default

    def request(self, method: str, url: str, *, retries: int | None = None, **kwargs: Any) -> requests.Response:
        """HTTP with Retry-After-aware 429 handling, backoff, and typed auth errors."""
        attempts = self.retries if retries is None else retries
        kwargs.setdefault("timeout", (10, 60))
        last_retry_after = 5.0
        for attempt in range(attempts + 1):
            started = time.monotonic()
            try:
                response = self.session.request(method, url, **kwargs)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                self.last_latency = time.monotonic() - started
                if attempt >= attempts:
                    raise BrainAPIError(f"{method} {url} failed: {exc}") from exc
                time.sleep(DEFAULT_BACKOFF_BASE * (2**attempt))
                continue
            self.last_latency = time.monotonic() - started

            if response.status_code == 429:
                last_retry_after = self.retry_after_seconds(response)
                if attempt >= attempts:
                    raise RateLimitError(f"{method} {url} rate-limited", last_retry_after)
                time.sleep(min(last_retry_after, 30.0))
                continue

            if response.status_code in (401, 403):
                raise SessionExpiredError(f"{method} {url} -> HTTP {response.status_code}", response.status_code)

            if response.status_code >= 400:
                detail = response.text[:300]
                try:
                    detail = str(response.json().get("detail") or detail)[:300]
                except (ValueError, AttributeError):
                    pass
                if "credentials" in detail.lower():
                    raise SessionExpiredError(f"BRAIN rejected the request: {detail}", response.status_code)
                raise BrainAPIError(f"{method} {url} -> HTTP {response.status_code}: {detail}", response.status_code)

            return response
        raise RateLimitError(f"{method} {url} still rate-limited after {attempts} retries", last_retry_after)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    # -- simulations -------------------------------------------------------

    def submit(self, expression: str, settings: Mapping[str, Any], *, candidate_id: int | None = None,
               canonical_key: str = "") -> SimulationHandle:
        """POST one REGULAR simulation and return the handle to poll."""
        response = self.post(f"{API_BASE}/simulations", json=simulation_payload(expression, settings))
        location = str(response.headers.get("Location", "")).rstrip("/")
        if not location:
            raise BrainAPIError(f"BRAIN returned no simulation Location (HTTP {response.status_code})")
        if location.startswith("http"):
            url = location
        else:
            url = f"{API_BASE}{location if location.startswith('/') else '/' + location}"
        simulation_id = location.rsplit("/", 1)[-1] or location
        return SimulationHandle(
            candidate_id=candidate_id,
            canonical_key=canonical_key,
            expression=expression,
            simulation_id=simulation_id,
            url=url,
            submitted_at=time.monotonic(),
        )

    def submit_multi(self, expressions: list[str], settings: Mapping[str, Any]) -> SimulationHandle:
        """POST a MULTI simulation (TODO P3); callers must fall back on BrainAPIError."""
        response = self.post(f"{API_BASE}/simulations", json=simulation_payload(expressions, settings, multi=True))
        location = str(response.headers.get("Location", "")).rstrip("/")
        if not location:
            raise BrainAPIError(f"BRAIN returned no simulation Location (HTTP {response.status_code})")
        url = location if location.startswith("http") else f"{API_BASE}{location if location.startswith('/') else '/' + location}"
        return SimulationHandle(
            candidate_id=None,
            canonical_key="",
            expression=json.dumps(expressions),
            simulation_id=location.rsplit("/", 1)[-1] or location,
            url=url,
            submitted_at=time.monotonic(),
        )

    def poll(self, handle: SimulationHandle) -> PollState:
        """One poll of a running simulation."""
        payload = self.get(handle.url).json()
        handle.polls += 1
        if not isinstance(payload, Mapping):
            return PollState("RUNNING", progress=None)
        if "alpha" in payload:
            return PollState("DONE", alpha_id=str(payload["alpha"]))
        children = payload.get("children")
        if isinstance(children, list) and children:
            return PollState("RUNNING", progress=payload.get("progress"), children=[
                PollState("DONE", alpha_id=str(child["alpha"])) if isinstance(child, Mapping) and "alpha" in child
                else PollState("ERROR", message=str(child.get("message", "child failed"))) if isinstance(child, Mapping)
                and str(child.get("status", "")).upper() in ("ERROR", "FAILED")
                else PollState("RUNNING")
                for child in children
            ])
        status = str(payload.get("status", "")).upper()
        if status in ("ERROR", "FAILED"):
            return PollState("ERROR", message=str(payload.get("message", "simulation error")))
        progress = payload.get("progress")
        return PollState("RUNNING", progress=progress if isinstance(progress, (int, float)) else None)

    def alpha(self, alpha_id: str) -> dict[str, Any]:
        return dict(self.get(f"{API_BASE}/alphas/{alpha_id}").json())

    def alpha_metrics(self, alpha_id: str) -> dict[str, Any]:
        return alpha_metrics(self.alpha(alpha_id))

    # -- submission (TODO P6) ---------------------------------------------

    def submit_alpha(self, alpha_id: str) -> dict[str, Any]:
        """Ask BRAIN to submit an alpha.

        Mirrors the legacy tool's reading of the platform: 404 means it was already
        submitted, 403/409 means a previous request is still being evaluated (the
        server-side correlation check can stay pending for minutes).
        """
        try:
            self.post(f"{API_BASE}/alphas/{alpha_id}/submit", retries=1)
        except BrainAPIError as exc:
            if exc.status == 404:
                return {"outcome": "already_submitted", "detail": "HTTP 404"}
            if exc.status in (403, 409):
                return {"outcome": "in_progress", "detail": f"HTTP {exc.status}"}
            return {"outcome": "error", "detail": str(exc)[:200]}
        return {"outcome": "submitted", "detail": ""}

    def submit_checks(self, alpha_id: str) -> list[dict[str, Any]]:
        """Checks reported by GET /alphas/{id}/submit while submission is pending."""
        try:
            response = self.get(f"{API_BASE}/alphas/{alpha_id}/submit", retries=1)
        except BrainAPIError:
            return []
        if not response.content:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        checks = (payload.get("is") or {}).get("checks") if isinstance(payload, Mapping) else None
        return [c for c in (checks or []) if isinstance(c, Mapping)]

    def alpha_status(self, alpha_id: str) -> str | None:
        try:
            return str(self.alpha(alpha_id).get("status") or "")
        except BrainAPIError:
            return None

    # -- ACTIVE portfolio (TODO P9) ---------------------------------------

    def list_active_alphas(self, *, page_size: int = 100, max_pages: int = 200) -> list[dict[str, Any]]:
        """Every ACTIVE alpha on the book, fully paginated.

        Raises instead of returning a silently truncated book: correlating against a partial
        portfolio would hide exactly the alpha a candidate duplicates.
        """
        alphas: list[dict[str, Any]] = []
        offset = 0
        for _page in range(max_pages):
            payload = self.get(
                f"{API_BASE}/users/self/alphas",
                params={"limit": page_size, "offset": offset, "status": "ACTIVE"},
            ).json()
            batch = payload.get("results", payload.get("alphas", [])) if isinstance(payload, Mapping) else []
            if not isinstance(batch, list):
                raise BrainAPIError("BRAIN returned an unexpected alpha-list payload")
            alphas.extend(alpha for alpha in batch if isinstance(alpha, Mapping))
            if len(batch) < page_size:
                return alphas
            offset += page_size
        raise BrainAPIError(
            f"ACTIVE alpha pagination exceeded {max_pages} pages; refusing to proceed with a partial book"
        )
