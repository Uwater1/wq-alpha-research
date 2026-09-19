"""Offline tests for the local self-correlation pipeline (TODO P9/P14).

The pipeline is the one place where a wrong answer silently spends a submission slot, so
these tests pin the failure modes: cumulative curves aligned by position, missing/short/
degenerate PnL read as low correlation, a truncated ACTIVE book, and a stale check that
survives a portfolio change.
"""
from __future__ import annotations

import json

import pytest

import brain_api
import correlation as corr
import research_db as rdb
import submission_worker as sw
from test_scheduler import FakeClock

PAGE = 100


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _pnl_payload(dates, values):
    return {
        "schema": {"properties": [{"name": "date"}, {"name": "pnl"}]},
        "records": [[d, v] for d, v in zip(dates, values)],
    }


def _series(returns):
    """Cumulative PnL from a return path, with one date per observation."""
    values, total = [], 0.0
    for ret in returns:
        total += ret
        values.append(total)
    dates = [f"d{i:03d}" for i in range(len(values))]
    return dates, values


def _wave(n=80, amplitude=1.0):
    return [amplitude * (1.0 if i % 2 == 0 else -1.0) for i in range(n)]


def _flat(n=80):
    return [0.0] * n


class FakeCorrelationBrain:
    """Stand-in for BrainClient covering the correlation and submission calls."""

    def __init__(self, *, active_ids=(), pnl=None, status="ACTIVE", submit_outcome="submitted",
                 checks=None, book_error=None):
        self.active_ids = [str(a) for a in active_ids]
        self.pnl = dict(pnl or {})
        self.status = status
        self.submit_outcome = submit_outcome
        self.checks = list(checks or [{"name": "SELF_CORRELATION", "result": "PASS"}])
        self.book_error = book_error
        self.posts: list[str] = []
        self.book_calls = 0

    def list_active_alphas(self, *, page_size=PAGE, max_pages=200):
        self.book_calls += 1
        if self.book_error:
            raise brain_api.BrainAPIError(self.book_error)
        return [{"id": alpha_id} for alpha_id in self.active_ids]

    def get(self, url, **kwargs):
        alpha_id = str(url).split("/alphas/")[1].split("/")[0]
        series = self.pnl.get(alpha_id)
        return _Resp(_pnl_payload(*series) if series else {"schema": {}, "records": []})

    def submit_alpha(self, alpha_id):
        self.posts.append(alpha_id)
        return {"outcome": self.submit_outcome, "detail": ""}

    def submit_checks(self, alpha_id):
        return list(self.checks)

    def alpha_status(self, alpha_id):
        return self.status

    def close(self):
        pass


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _ready_candidate(db, expression="rank(close)", *, family="fundamental"):
    outcome = db.queue_candidate(expression, {"decay": 6}, signal_family=family)
    db.claim_simulation("worker", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=outcome.candidate_id,
        status="DONE",
        metrics={"sharpe": 1.6, "fitness": 1.3, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "PASS"}],
        brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return db.get_candidate(outcome.candidate_id)


# ---------------------------------------------------------------------------
# Pure correlation
# ---------------------------------------------------------------------------


def test_correlate_uses_aligned_daily_returns_not_cumulative_curves():
    dates, values = _series(_wave())
    identical = corr.correlate_against_book(dates, values, [("OLD", dates, values)])
    assert identical.status == corr.STATUS_OK
    assert identical.max_corr == pytest.approx(1.0)

    # A cumulative curve that only *looks* similar (shifted history) must not be compared
    # positionally: with no shared dates the check is explicitly insufficient.
    shifted = ([f"x{i}" for i in range(len(values))], values)
    result = corr.correlate_against_book(dates, values, [("OLD", *shifted)])
    assert result.status == corr.STATUS_INSUFFICIENT
    assert result.max_corr is None


def test_correlate_reports_unavailable_short_and_degenerate_instead_of_zero():
    dates, values = _series(_wave())
    assert corr.correlate_against_book([], [], [("OLD", dates, values)]).status == corr.STATUS_UNAVAILABLE

    short_dates, short_values = _series(_wave(n=10))
    short = corr.correlate_against_book(short_dates, short_values, [("OLD", dates, values)])
    assert short.status == corr.STATUS_INSUFFICIENT

    degenerate = corr.correlate_against_book(dates, values, [("OLD", *(_series(_flat())))])
    assert degenerate.status == corr.STATUS_DEGENERATE


def test_correlate_reports_an_empty_book_explicitly():
    dates, values = _series(_wave())
    assert corr.correlate_against_book(dates, values, []).status == corr.STATUS_EMPTY_BOOK


# ---------------------------------------------------------------------------
# ACTIVE book pagination
# ---------------------------------------------------------------------------


class _PagedClient(brain_api.BrainClient):
    def __init__(self, pages):
        super().__init__()
        self.pages = pages
        self.calls = 0

    def get(self, url, **kwargs):
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return _Resp({"results": page})


def test_active_book_pagination_reads_every_page():
    client = _PagedClient([[{"id": "A"}, {"id": "B"}], [{"id": "C"}], [{"id": "IGNORED"}]])
    assert [a["id"] for a in client.list_active_alphas(page_size=2, max_pages=5)] == ["A", "B", "C"]


def test_active_book_pagination_refuses_a_truncated_book():
    client = _PagedClient([[{"id": "A"}, {"id": "B"}], [{"id": "C"}, {"id": "D"}],
                           [{"id": "E"}, {"id": "F"}]])
    with pytest.raises(brain_api.BrainAPIError):
        client.list_active_alphas(page_size=2, max_pages=2)


# ---------------------------------------------------------------------------
# ACTIVE book sync + versioning
# ---------------------------------------------------------------------------


def _service(db, client):
    return corr.CorrelationService(db, client, sleep=lambda _s: None, log=lambda *_a: None)


def test_sync_caches_pnl_and_versions_the_book(db):
    dates, values = _series(_wave())
    client = FakeCorrelationBrain(active_ids=["OLD"], pnl={"OLD": (dates, values)})

    sync = _service(db, client).sync_active_book()

    assert (sync.fetched, sync.added, sync.removed) == (1, 1, 0)
    assert sync.pnl_cached == 1
    assert sync.version == 1
    assert db.active_set_version() == 1
    assert db.active_pnl_ids() == ["OLD"]


def test_membership_change_bumps_version_and_marks_checks_stale(db):
    dates, values = _series(_wave())
    client = FakeCorrelationBrain(active_ids=["OLD"], pnl={"OLD": (dates, values)})
    service = _service(db, client)
    candidate = _ready_candidate(db)
    client.pnl[candidate["brain_alpha_id"]] = _series(_wave())
    service.sync_active_book()
    service.check_candidate(candidate, force=True)
    assert db.get_candidate(candidate["id"])["corr_status"] == corr.STATUS_OK

    # A new ACTIVE alpha appears: the cached check is no longer trustworthy.
    client.active_ids.append("NEW")
    client.pnl["NEW"] = _series(_wave())
    sync = service.sync_active_book()

    assert sync.version == 2
    assert db.mark_stale_correlations() == 0  # sync already flagged it
    assert db.get_candidate(candidate["id"])["corr_status"] == corr.STATUS_STALE

    # An unchanged book must not churn the version.
    assert service.sync_active_book().version == 2


def test_sync_refuses_to_proceed_on_a_failed_book(db):
    client = FakeCorrelationBrain(active_ids=["OLD"], book_error="pagination looked truncated")
    sync = _service(db, client).sync_active_book()
    assert sync.error and sync.fetched == 0
    assert db.active_set_version() == 0


def test_check_candidate_records_a_hold_when_book_refresh_fails(db):
    dates, values = _series(_wave())
    client = FakeCorrelationBrain(active_ids=["OLD"], pnl={"OLD": (dates, values)})
    candidate = _ready_candidate(db)
    service = _service(db, client)
    service.sync_active_book()
    client.book_error = "temporary ACTIVE-book outage"

    result = service.check_candidate(candidate, force=True)

    assert result.status == corr.STATUS_INCOMPLETE
    assert db.get_candidate(candidate["id"])["corr_status"] == corr.STATUS_INCOMPLETE


# ---------------------------------------------------------------------------
# Candidate checks + gate
# ---------------------------------------------------------------------------


def test_check_candidate_persists_the_full_verdict(db):
    dates, values = _series(_wave())
    client = FakeCorrelationBrain(active_ids=["OLD"], pnl={"OLD": (dates, values)})
    candidate = _ready_candidate(db)
    client.pnl[candidate["brain_alpha_id"]] = _series(_wave(amplitude=0.5))  # highly correlated

    result = _service(db, client).check_candidate(candidate, force=True)

    assert result.status == corr.STATUS_OK and result.max_corr_alpha_id == "OLD"
    stored = db.get_candidate(candidate["id"])
    assert stored["self_corr"] == pytest.approx(result.max_corr)
    assert stored["max_corr_alpha_id"] == "OLD"
    assert stored["corr_checked_at"] is not None
    assert stored["active_set_version"] == db.active_set_version()
    assert stored["corr_status"] == corr.STATUS_OK


def test_gate_holds_when_correlation_is_missing_or_stale(db):
    candidate = {"brain_alpha_id": "A1", "sharpe": 1.6, "fitness": 1.3, "turnover": 0.05}

    allowed, reasons = rdb.submission_gate(candidate, require_correlation=True)
    assert not allowed and any("self-correlation" in reason for reason in reasons)

    checked = {**candidate, "self_corr": 0.1, "corr_status": "ok", "active_set_version": 3}
    assert rdb.submission_gate(checked, require_correlation=True, active_set_version=3)[0] is True
    stale = rdb.submission_gate(checked, require_correlation=True, active_set_version=4)
    assert not stale[0] and any("stale" in reason for reason in stale[1])

    high = {**candidate, "self_corr": 0.85, "corr_status": "ok", "active_set_version": 3}
    assert not rdb.submission_gate(high, require_correlation=True, active_set_version=3)[0]


def test_unavailable_and_incomplete_checks_are_explicit_holds(db):
    candidate = {"brain_alpha_id": "A1", "sharpe": 1.6, "fitness": 1.3, "turnover": 0.05,
                 "corr_status": "unavailable", "active_set_version": 1}
    allowed, reasons = rdb.submission_gate(candidate, require_correlation=True, active_set_version=1)
    assert not allowed and "PnL was unavailable" in reasons[0]

    incomplete = {**candidate, "corr_status": "incomplete"}
    assert not rdb.submission_gate(incomplete, require_correlation=True, active_set_version=1)[0]

    # An empty ACTIVE book is a legitimate pass: there is nothing to be correlated with.
    empty = {**candidate, "corr_status": "empty_book", "self_corr": None}
    assert rdb.submission_gate(empty, require_correlation=True, active_set_version=1)[0] is True


def test_incomplete_book_prevents_a_false_low_correlation(db):
    """An ACTIVE alpha with no cached PnL could be the one the candidate duplicates."""
    dates, values = _series(_wave())
    client = FakeCorrelationBrain(active_ids=["CACHED", "UNCACHED"], pnl={"CACHED": (dates, values)})
    candidate = _ready_candidate(db)
    client.pnl[candidate["brain_alpha_id"]] = _series(_wave(n=80, amplitude=1.0))
    service = _service(db, client)
    service.sync_active_book()
    # Simulate a PnL fetch that never succeeded for the second alpha.
    db.query("DELETE FROM active_pnl WHERE brain_alpha_id='CACHED'")

    result = service.check_candidate(candidate, force=True)

    assert result.status == corr.STATUS_INCOMPLETE
    assert db.get_candidate(candidate["id"])["corr_status"] == corr.STATUS_INCOMPLETE


def test_correlation_status_reports_the_pipeline_state(db):
    dates, values = _series(_wave())
    client = FakeCorrelationBrain(active_ids=["OLD"], pnl={"OLD": (dates, values)})
    service = _service(db, client)
    candidate = _ready_candidate(db)
    service.sync_active_book()
    service.check_candidate(candidate, force=True)

    state = db.correlation_status()
    assert state["active_set_version"] == 1
    assert state["active_alphas"] == 1
    assert state["cached_pnl_series"] == 1


# ---------------------------------------------------------------------------
# Worker integration
# ---------------------------------------------------------------------------


def _worker(db, client, **kwargs):
    clock = FakeClock()
    kwargs.setdefault("max_runtime", None)
    kwargs.setdefault("poll_interval", 0.0)
    kwargs.setdefault("max_polls", 2)
    kwargs.setdefault("require_correlation", True)
    return sw.SubmissionWorker(db, client, clock=clock, sleep=clock.sleep, **kwargs)


def test_worker_holds_a_redundant_candidate_without_posting(db):
    dates, values = _series(_wave())
    client = FakeCorrelationBrain(active_ids=["OLD"], pnl={"OLD": (dates, values)})
    candidate = _ready_candidate(db)
    client.pnl[candidate["brain_alpha_id"]] = _series(_wave())  # same shape as OLD

    _worker(db, client, max_submissions=1).run()

    assert client.posts == []
    assert db.counts("submissions") == {"RETRY": 1}
    assert db.get_candidate(candidate["id"])["corr_status"] == corr.STATUS_OK
    assert db.get_candidate(candidate["id"])["self_corr"] == pytest.approx(1.0)


def test_worker_submits_a_diverse_candidate(db):
    dates, values = _series(_wave())
    client = FakeCorrelationBrain(active_ids=["OLD"], pnl={"OLD": (dates, values)})
    candidate = _ready_candidate(db)
    client.pnl[candidate["brain_alpha_id"]] = _series([0.0] * 40 + [1.0] * 40)  # uncorrelated shape

    _worker(db, client, max_submissions=1).run()

    assert client.posts == [candidate["brain_alpha_id"]]
    assert db.counts("submissions") == {"ACTIVE": 1}
    assert db.active_alpha_ids() == [candidate["brain_alpha_id"], "OLD"]


def test_worker_refreshes_a_stale_check_immediately_before_submitting(db):
    dates, values = _series(_wave())
    client = FakeCorrelationBrain(active_ids=["OLD"], pnl={"OLD": (dates, values)})
    candidate = _ready_candidate(db)
    client.pnl[candidate["brain_alpha_id"]] = _series([0.0] * 40 + [1.0] * 40)
    # A cached check from an older ACTIVE book, marked usable but out of date.
    db.record_correlation_check(candidate["id"], 0.1, "GONE", active_set_version=0, status="ok")

    _worker(db, client, max_submissions=1).run()

    refreshed = db.get_candidate(candidate["id"])
    # The check now carries the ACTIVE book it saw (v1); submitting then grows the book to v2.
    assert refreshed["active_set_version"] == 1
    assert refreshed["max_corr_alpha_id"] == "OLD"
    assert db.active_set_version() == 2
    assert client.posts == [candidate["brain_alpha_id"]]


def test_worker_holds_submissions_when_active_book_refresh_fails(db):
    dates, values = _series(_wave())
    client = FakeCorrelationBrain(active_ids=["OLD"], pnl={"OLD": (dates, values)})
    candidate = _ready_candidate(db)
    client.pnl[candidate["brain_alpha_id"]] = _series([0.0] * 40 + [1.0] * 40)
    service = _service(db, client)
    service.sync_active_book()
    service.check_candidate(candidate, force=True)

    # The cached check is valid only for the last successfully synchronized book.
    client.book_error = "temporary ACTIVE-book outage"
    _worker(db, client, max_submissions=1).run()

    assert client.posts == []
    assert db.counts("submissions") == {"READY": 1}


def test_gate_is_a_no_op_without_require_correlation(db):
    candidate = {"brain_alpha_id": "A1", "sharpe": 1.6, "fitness": 1.3, "turnover": 0.05}
    assert rdb.submission_gate(candidate)[0] is True
