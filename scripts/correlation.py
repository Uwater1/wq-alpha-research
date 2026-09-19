"""Local daily-return self-correlation pipeline (TODO P9).

BRAIN's SELF_CORRELATION check is the final word, but it is a black box that only speaks
once per submission attempt. This module makes the same comparison locally and cheaply so
an obviously-redundant alpha never spends a submission slot:

    sync_active_book()   fetch the whole paginated ACTIVE book, version it, cache its PnL
    check_candidate()    fetch a candidate's PnL, correlate aligned daily returns, persist
    correlate_reasons()  the gate reads the persisted verdict, never a bare number

Two rules the pipeline refuses to bend:

* correlations are computed on **aligned daily returns**, never on cumulative PnL curves,
  and never by positional alignment of series with different histories;
* an unusable input (missing/short PnL, a flat series, an incompletely cached book) is an
  explicit status, not a silent 0.0 that would look like perfect diversification.

The pure correlation helpers live here and are re-exported by `evolve_skill.py`, which
keeps its session-based fetchers for the legacy reporting path.

CLI:
    ./.venv/bin/python scripts/correlation.py sync     # refresh the ACTIVE book + cached PnL
    ./.venv/bin/python scripts/correlation.py status   # local pipeline state as JSON
    ./.venv/bin/python scripts/correlation.py check 12 # (re)compute one candidate's check
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import brain_api  # noqa: E402
import research_db  # noqa: E402

#: Daily returns required before a correlation is meaningful (roughly two months).
MIN_RECORDS = 50
DEFAULT_PAGE_SIZE = 100
MAX_BOOK_PAGES = 200

# Correlation verdicts persisted on the candidate row.
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_INSUFFICIENT = "insufficient"
STATUS_DEGENERATE = "degenerate"
STATUS_INCOMPLETE = "incomplete"
STATUS_STALE = "stale"
STATUS_EMPTY_BOOK = "empty_book"

#: Statuses that count as a usable check when the pinned ACTIVE version still matches.
FRESH_STATUSES = frozenset({STATUS_OK, STATUS_EMPTY_BOOK})


def _warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Pure correlation helpers (moved from evolve_skill; reused by both pipelines)
# ---------------------------------------------------------------------------


def daily_returns(cum_pnl: Sequence[float]) -> list[float]:
    return [cum_pnl[i + 1] - cum_pnl[i] for i in range(len(cum_pnl) - 1)]


def safe_corrcoef(a: Any, b: Any) -> float | None:
    """Pearson correlation, or None when a side is constant, mis-sized, or non-finite."""
    x = np.asarray(a, dtype=float)
    y = np.asarray(b, dtype=float)
    if len(x) != len(y) or len(x) < 2:
        return None
    if not (np.isfinite(x).all() and np.isfinite(y).all()):
        return None
    if x.std() == 0 or y.std() == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def aligned_daily_returns(
    new_dates: Sequence[str],
    new_pnl: Sequence[float],
    old_dates: Sequence[str],
    old_pnl: Sequence[float],
) -> tuple[list[float], list[float]]:
    """Daily changes of two PnL series, paired on their common dates.

    Falls back to length alignment only when a side carries no date stamps (rows written
    to alpha_db.json by an older version have values only).
    """
    if new_dates and old_dates and len(new_dates) == len(new_pnl) and len(old_dates) == len(old_pnl):
        new_map = dict(zip(new_dates, new_pnl))
        old_map = dict(zip(old_dates, old_pnl))
        common = [d for d in new_dates if d in old_map]
        if len(common) < 2:
            return [], []
        return daily_returns([new_map[d] for d in common]), daily_returns([old_map[d] for d in common])
    if len(new_pnl) != len(old_pnl):
        return [], []
    return daily_returns(new_pnl), daily_returns(old_pnl)


def recordset_columns(props: Any) -> tuple[int, int]:
    """Resolve (date_index, pnl_index) from a recordset schema, list- or dict-shaped."""
    if isinstance(props, list):
        entries = [p if isinstance(p, Mapping) else {} for p in props]

        def index_of(names: set[str], default: int) -> int:
            for i, entry in enumerate(entries):
                if str(entry.get("name", "")).lower() in names:
                    return i
            return default
    else:
        mapping = props if isinstance(props, Mapping) else {}

        def index_of(names: set[str], default: int) -> int:
            for key, value in mapping.items():
                if str(key).lower() in names:
                    idx = value.get("index") if isinstance(value, Mapping) else None
                    return int(idx) if isinstance(idx, int) else default
            return default

    return index_of({"date"}, 0), index_of({"pnl", "cum_pnl", "returns", "ret"}, 1)


def pnl_from_payload(payload: Any) -> tuple[list[str], list[float]]:
    """Parse a PnL recordset body into (dates, values), oldest record first."""
    if not isinstance(payload, Mapping):
        return [], []
    schema = payload.get("schema")
    properties = schema.get("properties", []) if isinstance(schema, Mapping) else []
    date_idx, pnl_idx = recordset_columns(properties)
    records = payload.get("records") or []
    try:
        records = sorted(records, key=lambda row: row[date_idx])
    except Exception:
        pass

    dates: list[str] = []
    values: list[float] = []
    for row in records:
        rec = row[0] if isinstance(row, list) and len(row) == 1 and isinstance(row[0], list) else row
        try:
            values.append(float(rec[pnl_idx]))
            dates.append(str(rec[date_idx]))
        except Exception:
            continue
    return dates, values


def fetch_pnl_series(client: Any, alpha_id: str) -> tuple[list[str], list[float]]:
    """Cumulative PnL for one alpha via a BrainClient-like ``.get``.

    Returns ([], []) when the recordset is genuinely empty, but says *why* on stderr
    otherwise: a silent empty return used to disable the correlation check entirely.
    """
    url = f"{brain_api.API_BASE}/alphas/{alpha_id}/recordsets/pnl"
    try:
        response = client.get(url, timeout=(10, 60))
    except Exception as exc:
        _warn(f"{alpha_id}: PnL recordset request failed: {exc}")
        return [], []
    if getattr(response, "status_code", 200) != 200:
        _warn(f"{alpha_id}: PnL recordset -> HTTP {getattr(response, 'status_code', '?')}")
        return [], []
    if not (getattr(response, "text", "") or "").strip():
        return [], []  # BRAIN has not finished computing it yet
    try:
        payload = response.json()
    except ValueError as exc:
        _warn(f"{alpha_id}: PnL recordset was not JSON ({exc})")
        return [], []
    return pnl_from_payload(payload)


def fetch_pnl(client: Any, alpha_id: str) -> list[float]:
    """Cumulative PnL values only (for callers that do not need the dates)."""
    return fetch_pnl_series(client, alpha_id)[1]


def fetch_pnl_for_new_alpha(
    client: Any,
    alpha_id: str,
    *,
    tries: int = 3,
    delay: float = 5.0,
    sleep: Any = time.sleep,
) -> tuple[list[str], list[float]]:
    """Fetch a fresh alpha's PnL, waiting out the window where BRAIN still computes it."""
    for attempt in range(tries):
        dates, values = fetch_pnl_series(client, alpha_id)
        if values:
            return dates, values
        if attempt < tries - 1:
            sleep(delay)
    _warn(f"{alpha_id}: PnL still empty after {tries} attempts; correlation left unchecked")
    return [], []


# ---------------------------------------------------------------------------
# Correlation against the ACTIVE book
# ---------------------------------------------------------------------------


@dataclass
class CorrelationResult:
    """Outcome of a local self-correlation check."""

    status: str
    max_corr: float | None = None
    max_corr_alpha_id: str | None = None
    compared: int = 0
    reason: str = ""
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.status in FRESH_STATUSES

    def exceeds(self, limit: float) -> bool:
        return self.max_corr is not None and abs(self.max_corr) >= limit

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "max_corr": self.max_corr,
            "max_corr_alpha_id": self.max_corr_alpha_id,
            "compared": self.compared,
            "reason": self.reason,
            "from_cache": self.from_cache,
        }


def correlate_against_book(
    candidate_dates: Sequence[str],
    candidate_pnl: Sequence[float],
    book: Iterable[tuple[str, Sequence[str], Sequence[float]]],
    *,
    min_records: int = MIN_RECORDS,
) -> CorrelationResult:
    """Highest absolute daily-return correlation of a candidate against the ACTIVE book."""
    book = list(book)
    if not candidate_pnl:
        return CorrelationResult(STATUS_UNAVAILABLE, reason="candidate PnL series is empty")
    if not book:
        return CorrelationResult(STATUS_EMPTY_BOOK, reason="no ACTIVE alphas to compare against")
    if len(candidate_pnl) < min_records:
        return CorrelationResult(
            STATUS_INSUFFICIENT, compared=0,
            reason=f"candidate PnL has only {len(candidate_pnl)} records",
        )

    best_id: str | None = None
    best_corr: float | None = None
    compared = 0
    degenerate = 0
    for alpha_id, old_dates, old_pnl in book:
        if not old_pnl:
            continue
        new_ret, old_ret = aligned_daily_returns(candidate_dates, candidate_pnl, old_dates, old_pnl)
        if len(new_ret) < min_records or len(new_ret) != len(old_ret):
            continue
        corr = safe_corrcoef(new_ret, old_ret)
        if corr is None:
            degenerate += 1
            continue
        compared += 1
        if best_corr is None or abs(corr) > abs(best_corr):
            best_id, best_corr = str(alpha_id), corr

    if compared == 0:
        if degenerate:
            return CorrelationResult(
                STATUS_DEGENERATE, reason="every comparison had a flat or non-finite series"
            )
        return CorrelationResult(
            STATUS_INSUFFICIENT, reason="no ACTIVE alpha shared enough aligned daily returns"
        )
    return CorrelationResult(STATUS_OK, max_corr=best_corr, max_corr_alpha_id=best_id, compared=compared)


@dataclass
class BookSync:
    """What one ACTIVE-book sync changed."""

    fetched: int = 0
    added: int = 0
    removed: int = 0
    version: int = 0
    pnl_cached: int = 0
    stale_checks: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched, "added": self.added, "removed": self.removed, "version": self.version,
            "pnl_cached": self.pnl_cached, "stale_checks": self.stale_checks, "error": self.error,
        }


class CorrelationService:
    """Keeps the local ACTIVE book, cached PnL, and per-candidate checks in sync (P9)."""

    def __init__(
        self,
        db: research_db.ResearchDB,
        client: Any,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        min_records: int = MIN_RECORDS,
        pnl_tries: int = 2,
        pnl_delay: float = 2.0,
        sleep: Any = time.sleep,
        log: Any = print,
    ) -> None:
        self.db = db
        self.client = client
        self.page_size = int(page_size)
        self.min_records = int(min_records)
        self.pnl_tries = int(pnl_tries)
        self.pnl_delay = float(pnl_delay)
        self._sleep = sleep
        self._log = log

    # -- ACTIVE book -------------------------------------------------------

    def sync_active_book(self, *, force: bool = False) -> BookSync:
        """Fetch the entire paginated ACTIVE book, version it, and cache missing PnL."""
        sync = BookSync()
        try:
            alphas = self.client.list_active_alphas(page_size=self.page_size)
        except brain_api.BrainAPIError as exc:
            # Refuse to proceed on a partial book: a truncated book would silently hide
            # the very alpha a candidate correlates with.
            sync.error = str(exc)[:300]
            _warn(f"ACTIVE book sync failed: {sync.error}")
            return sync

        alpha_ids = [str(alpha.get("id")) for alpha in alphas if isinstance(alpha, Mapping) and alpha.get("id")]
        sync.fetched = len(alpha_ids)
        result = self.db.sync_active_set(alpha_ids)
        sync.added, sync.removed, sync.version = result["added"], result["removed"], result["version"]
        sync.stale_checks = self.db.mark_stale_correlations()

        cached = set(self.db.active_pnl_ids()) if not force else set()
        for alpha_id in alpha_ids:
            if alpha_id in cached:
                continue
            dates, values = fetch_pnl_for_new_alpha(
                self.client, alpha_id, tries=self.pnl_tries, delay=self.pnl_delay, sleep=self._sleep
            )
            if values:
                self.db.cache_active_pnl(alpha_id, dates, values)
                sync.pnl_cached += 1
        return sync

    def book(self) -> tuple[list[tuple[str, list[str], list[float]]], list[str]]:
        """Cached ACTIVE PnL entries plus the ids that have no usable series yet."""
        cached = self.db.active_pnl()
        entries: list[tuple[str, list[str], list[float]]] = []
        missing: list[str] = []
        for row in self.db.query("SELECT brain_alpha_id FROM active_alphas ORDER BY brain_alpha_id"):
            alpha_id = str(row["brain_alpha_id"])
            series = cached.get(alpha_id)
            if series and series[1]:
                entries.append((alpha_id, series[0], series[1]))
            else:
                missing.append(alpha_id)
        return entries, missing

    # -- candidates --------------------------------------------------------

    def check_candidate(self, candidate: Mapping[str, Any], *, force: bool = False) -> CorrelationResult:
        """Compute (or reuse) a candidate's local self-correlation (TODO P9)."""
        candidate_id = int(candidate["id"])
        if not force and str(candidate.get("corr_status") or "") in FRESH_STATUSES \
                and candidate.get("active_set_version") == self.db.active_set_version():
            return CorrelationResult(
                str(candidate.get("corr_status")), max_corr=candidate.get("self_corr"),
                max_corr_alpha_id=candidate.get("max_corr_alpha_id"), from_cache=True,
            )

        self.sync_active_book()
        # Version *after* the sync: the check must be pinned to the book it actually saw.
        version = self.db.active_set_version()
        alpha_id = candidate.get("brain_alpha_id")
        if not alpha_id:
            return self._record(
                candidate_id, version,
                CorrelationResult(STATUS_UNAVAILABLE, reason="candidate has no BRAIN alpha id"),
            )
        dates, values = fetch_pnl_for_new_alpha(
            self.client, str(alpha_id), tries=self.pnl_tries, delay=self.pnl_delay, sleep=self._sleep
        )
        if not values:
            return self._record(
                candidate_id, version,
                CorrelationResult(STATUS_UNAVAILABLE, reason="candidate PnL is unavailable"),
            )

        entries, missing = self.book()
        result = correlate_against_book(dates, values, entries, min_records=self.min_records)
        if missing:
            # A missing series might be the most correlated one, so no verdict is honest.
            result = CorrelationResult(
                STATUS_INCOMPLETE, compared=result.compared,
                reason=f"{len(missing)} ACTIVE alpha(s) have no cached PnL",
            )
        return self._record(candidate_id, version, result)

    def _record(self, candidate_id: int, version: int, result: CorrelationResult) -> CorrelationResult:
        self.db.record_correlation_check(
            candidate_id,
            result.max_corr,
            result.max_corr_alpha_id,
            active_set_version=version,
            status=result.status,
        )
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _service(args: argparse.Namespace) -> CorrelationService:
    db = research_db.ResearchDB.open(args.db)
    return CorrelationService(db, brain_api.BrainClient())


def cmd_sync(args: argparse.Namespace) -> int:
    service = _service(args)
    try:
        sync = service.sync_active_book(force=args.force)
    finally:
        service.client.close()
        service.db.close()
    print(json.dumps(sync.as_dict(), indent=2, sort_keys=True))
    return 0 if sync.error is None else 1


def cmd_status(args: argparse.Namespace) -> int:
    with research_db.ResearchDB.open(args.db) as db:
        print(json.dumps(db.correlation_status(), indent=2, sort_keys=True))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    service = _service(args)
    try:
        with research_db.ResearchDB.open(args.db) as db:
            candidate = db.get_candidate(args.candidate)
            if candidate is None:
                print(f"candidate {args.candidate} not found", file=sys.stderr)
                return 1
            service.db = db
            result = service.check_candidate(candidate, force=args.force)
    finally:
        service.client.close()
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help=f"path to research.db (default: ${research_db.DB_ENV_VAR} or repo root)")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="refresh the ACTIVE book and cache missing PnL")
    sync.add_argument("--force", action="store_true", help="re-fetch every cached PnL series")
    sync.set_defaults(func=cmd_sync)

    sub.add_parser("status", help="print local correlation state as JSON").set_defaults(func=cmd_status)

    check = sub.add_parser("check", help="(re)compute one candidate's local correlation")
    check.add_argument("candidate", type=int)
    check.add_argument("--force", action="store_true", help="ignore a fresh cached check")
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
