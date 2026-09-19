"""Offline tests for submission idempotency and recovery (Priority 4 regression coverage).

The scenarios are the ones that used to require hand-editing research.db: a crash
immediately before the POST, a crash immediately after it, a run that times out with
checks pending, an expired lease, a transient submit error, and a worker restart.
Nothing here touches BRAIN: a fake client records what the worker actually POSTed.
"""
from __future__ import annotations

import pytest

import brain_api
import research_db as rdb
import submission_worker as sw
from test_scheduler import FakeClock


class FakeSubmitClient:
    """Configurable stand-in for the submission half of BrainClient."""

    def __init__(
        self,
        *,
        alpha_id=None,
        status="UNSUBMITTED",
        checks=None,
        submit_outcome="submitted",
        submit_errors=None,
    ) -> None:
        self.alpha_id = alpha_id
        self.status = status
        self.checks = list(checks or [])
        self.submit_outcome = submit_outcome
        self.submit_errors = list(submit_errors or [])
        self.posts: list[str] = []
        self.check_reads = 0

    def submit_alpha(self, alpha_id):
        if self.submit_errors:
            return {"outcome": "error", "detail": str(self.submit_errors.pop(0))}
        self.posts.append(alpha_id)
        return {"outcome": self.submit_outcome, "detail": ""}

    def submit_checks(self, alpha_id):
        self.check_reads += 1
        return list(self.checks)

    def alpha_status(self, alpha_id):
        return self.status

    def close(self):
        pass


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _ready_candidate(db, expression="rank(close)", *, settings=None, family="fundamental"):
    """A simulated, IS-passing candidate sitting in the submission queue."""
    settings = {"decay": 20, "neutralization": "INDUSTRY", **(settings or {})}
    outcome = db.queue_candidate(expression, settings, signal_family=family)
    db.claim_simulation("worker", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=outcome.candidate_id,
        status="DONE",
        metrics={"sharpe": 1.6, "fitness": 1.3, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "PASS"}],
        brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return db.get_candidate(outcome.candidate_id)


def _worker(db, client, **kwargs):
    clock = FakeClock()
    kwargs.setdefault("max_runtime", None)
    kwargs.setdefault("poll_interval", 0.0)
    kwargs.setdefault("max_polls", 2)
    return sw.SubmissionWorker(db, client, clock=clock, sleep=clock.sleep, **kwargs)


def _clear_backoff(db):
    """Operator/test convenience: pretend a retry window already elapsed."""
    db.query("UPDATE submissions SET next_attempt_at=NULL")


def _open_submission(db):
    return db.query("SELECT * FROM submissions ORDER BY id DESC LIMIT 1")[0]


# ---------------------------------------------------------------------------
# Crash before vs. after the POST
# ---------------------------------------------------------------------------


def test_expired_lease_before_post_returns_to_ready_and_retries(db):
    candidate = _ready_candidate(db)
    submission_id = db.enqueue_submission(candidate["id"])
    db.claim_submission("worker-1", lease_seconds=-1)  # crashed before mark_submission_posted

    recovered = db.recover_expired_leases()

    assert recovered["submissions"] == 1
    assert _open_submission(db)["status"] == "READY"
    assert _open_submission(db)["post_attempted_at"] is None

    client = FakeSubmitClient(status="ACTIVE", checks=[{"name": "SELF_CORRELATION", "result": "PASS"}])
    _worker(db, client, max_submissions=1).run()

    assert client.posts == [candidate["brain_alpha_id"]]  # the POST happened exactly once
    assert db.get_candidate(candidate["id"])["status"] == "ACTIVE"
    assert db.counts("submissions") == {"ACTIVE": 1}


def test_expired_lease_after_post_is_reconciled_and_not_resubmitted(db):
    candidate = _ready_candidate(db)
    submission_id = db.enqueue_submission(candidate["id"])
    db.claim_submission("worker-1", lease_seconds=-1)
    db.mark_submission_posted(submission_id)

    recovered = db.recover_expired_leases()
    assert recovered["submissions"] == 1
    assert _open_submission(db)["status"] == "CHECK_PENDING"

    client = FakeSubmitClient(status="ACTIVE", checks=[{"name": "SELF_CORRELATION", "result": "PASS"}])
    _worker(db, client, max_submissions=1).run()

    assert client.posts == []  # BRAIN already had it — no blind resubmit
    assert db.counts("submissions") == {"ACTIVE": 1}
    assert db.get_candidate(candidate["id"])["status"] == "ACTIVE"


def test_reconcile_keeps_pending_checks_reconcilable(db):
    candidate = _ready_candidate(db)
    submission_id = db.enqueue_submission(candidate["id"])
    db.claim_submission("worker-1", lease_seconds=-1)
    db.mark_submission_posted(submission_id)
    db.recover_expired_leases()
    _clear_backoff(db)

    client = FakeSubmitClient(status="UNSUBMITTED", checks=[{"name": "SELF_CORRELATION", "result": "PENDING"}])
    _worker(db, client, max_submissions=1).run()

    assert client.posts == []
    assert db.counts("submissions") == {"CHECK_PENDING": 1}
    assert db.get_candidate(candidate["id"])["status"] == "SUBMITTING"


def test_reconcile_returns_to_ready_only_when_brain_proves_not_submitted(db):
    candidate = _ready_candidate(db)
    submission_id = db.enqueue_submission(candidate["id"])
    db.claim_submission("worker-1", lease_seconds=-1)
    db.mark_submission_posted(submission_id)
    db.recover_expired_leases()

    client = FakeSubmitClient(status="UNSUBMITTED", checks=[])
    _worker(db, client, max_submissions=1).run()

    # Reconcile put the row back to READY, then the worker submitted it once.
    assert client.posts == [candidate["brain_alpha_id"]]
    assert db.counts("submissions") == {"CHECK_PENDING": 1}  # the new POST is pending checks


def test_reconcile_resolves_a_platform_correlation_failure(db):
    candidate = _ready_candidate(db)
    submission_id = db.enqueue_submission(candidate["id"])
    db.claim_submission("worker-1", lease_seconds=-1)
    db.mark_submission_posted(submission_id)
    db.recover_expired_leases()

    client = FakeSubmitClient(
        status="UNSUBMITTED",
        checks=[{"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.91}],
    )
    _worker(db, client, max_submissions=1).run()

    row = _open_submission(db)
    assert row["status"] == "SELF_CORR_FAIL"
    assert row["max_corr"] == 0.91
    assert db.get_candidate(candidate["id"])["status"] == "REJECTED"


def test_timed_out_run_leaves_a_reconcilable_row_not_a_lost_one(db):
    candidate = _ready_candidate(db)
    # Checks never settle: the worker's poll budget runs out.
    client = FakeSubmitClient(status="UNSUBMITTED", checks=[])

    _worker(db, client, max_submissions=1, max_polls=1).run()

    assert db.counts("submissions") == {"CHECK_PENDING": 1}
    assert _open_submission(db)["post_attempted_at"] is not None

    # A later run reconciles the same row instead of resubmitting it.
    client.status = "ACTIVE"
    _worker(db, client, max_submissions=1).run()
    assert client.posts == [candidate["brain_alpha_id"]]  # only the original POST
    assert db.counts("submissions") == {"ACTIVE": 1}


def test_uncertain_submit_response_is_reconciled_before_any_retry(db):
    candidate = _ready_candidate(db)
    # The fake records that the POST reached BRAIN, but gives the worker no response.
    client = FakeSubmitClient(status="ACTIVE", submit_outcome="uncertain")

    _worker(db, client, max_submissions=1).run()

    assert client.posts == [candidate["brain_alpha_id"]]
    assert db.counts("submissions") == {"CHECK_PENDING": 1}

    _worker(db, client, max_submissions=1).run()

    assert client.posts == [candidate["brain_alpha_id"]]
    assert db.counts("submissions") == {"ACTIVE": 1}


@pytest.mark.parametrize(
    "status, expected",
    [(None, "uncertain"), (408, "uncertain"), (429, "uncertain"), (500, "uncertain"), (400, "error")],
)
def test_brain_client_distinguishes_uncertain_submit_failures(monkeypatch, status, expected):
    client = brain_api.BrainClient()

    def lost_post(*_args, **_kwargs):
        raise brain_api.BrainAPIError("submit failed", status=status)

    monkeypatch.setattr(client, "post", lost_post)

    assert client.submit_alpha("A1")["outcome"] == expected


def test_submit_alpha_does_not_retry_a_lost_response_inside_http_layer():
    """A timeout after POST may mean BRAIN accepted it; reconciliation must own retries."""

    class LostResponseSession:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, **kwargs):
            self.calls += 1
            raise brain_api.requests.exceptions.Timeout("response lost after POST")

        def close(self):
            pass

    session = LostResponseSession()
    client = brain_api.BrainClient(session=session, retries=3)

    outcome = client.submit_alpha("A1")

    assert outcome["outcome"] == "uncertain"
    assert session.calls == 1


# ---------------------------------------------------------------------------
# Retry policy: backoff, automatic retry, attempt budget
# ---------------------------------------------------------------------------


def test_transient_submit_error_backs_off_then_retries_automatically(db):
    candidate = _ready_candidate(db)
    client = FakeSubmitClient(submit_errors=["HTTP 500 temporary"])

    _worker(db, client, max_submissions=1).run()

    row = _open_submission(db)
    assert row["status"] == "RETRY"
    assert row["next_attempt_at"] is not None
    assert row["attempt"] == 1
    # The backoff window is honoured: an immediate claim finds nothing.
    assert db.claim_submission("worker-2") is None

    _clear_backoff(db)
    client.status = "ACTIVE"
    client.checks = [{"name": "SELF_CORRELATION", "result": "PASS"}]
    _worker(db, client, max_submissions=1).run()

    assert db.counts("submissions") == {"ACTIVE": 1}
    assert db.get_candidate(candidate["id"])["status"] == "ACTIVE"


def test_submission_attempt_budget_retires_the_row_as_exhausted(db):
    candidate = _ready_candidate(db)
    submission_id = db.enqueue_submission(candidate["id"])

    db.claim_submission("worker-1")
    db.finish_submission(submission_id, "RETRY", message="boom", max_attempts=2)
    assert _open_submission(db)["status"] == "RETRY"

    _clear_backoff(db)
    db.claim_submission("worker-1")
    db.finish_submission(submission_id, "RETRY", message="boom", max_attempts=2)

    row = _open_submission(db)
    assert row["status"] == "EXHAUSTED"
    assert row["next_attempt_at"] is None
    assert "exhausted" in row["message"]
    # The candidate stays re-enqueueable instead of forcing a manual DB edit.
    assert db.get_candidate(candidate["id"])["status"] == "SUBMISSION_READY"
    assert db.expire_exhausted_submissions() == 0


def test_exhausted_sweep_retires_stale_retry_rows(db):
    candidate = _ready_candidate(db)
    db.enqueue_submission(candidate["id"])
    db.query("UPDATE submissions SET status='RETRY', attempt=7")

    assert db.expire_exhausted_submissions(5) == 1
    assert db.counts("submissions") == {"EXHAUSTED": 1}


def test_terminal_submission_rows_never_move_again(db):
    candidate = _ready_candidate(db)
    submission_id = db.enqueue_submission(candidate["id"])
    db.claim_submission("worker-1")
    db.finish_submission(submission_id, "ACTIVE", brain_alpha_id=candidate["brain_alpha_id"])

    with pytest.raises(ValueError):
        db.finish_submission(submission_id, "RETRY", message="zombie")


# ---------------------------------------------------------------------------
# Overlap, restart, and canonical identity
# ---------------------------------------------------------------------------


def test_two_workers_cannot_reach_the_same_submission(db):
    candidate = _ready_candidate(db)
    client = FakeSubmitClient(status="ACTIVE", checks=[{"name": "SELF_CORRELATION", "result": "PASS"}])

    first = _worker(db, client, max_submissions=1, worker_id="w1", lease_seconds=300)
    second = _worker(db, client, max_submissions=1, worker_id="w2")
    first.run()
    second.run()

    assert client.posts == [candidate["brain_alpha_id"]]
    assert db.counts("submissions") == {"ACTIVE": 1}


def test_restart_after_crash_does_not_duplicate_work(db):
    candidate = _ready_candidate(db)
    submission_id = db.enqueue_submission(candidate["id"])
    db.claim_submission("dead-worker", lease_seconds=-1)
    db.mark_submission_posted(submission_id)

    # "Crash": a brand-new worker object opens the same store and recovers.
    client = FakeSubmitClient(status="ACTIVE", checks=[{"name": "SELF_CORRELATION", "result": "PASS"}])
    _worker(db, client, max_submissions=1, worker_id="fresh").run()

    assert client.posts == []
    assert db.counts("submissions") == {"ACTIVE": 1}
    assert db.get_candidate(candidate["id"])["status"] == "ACTIVE"


def test_active_identity_uses_stored_settings_not_reconstructed_defaults(db):
    """Regression: non-default settings must keep their canonical key."""
    candidate = _ready_candidate(db, settings={"decay": 42, "neutralization": "SECTOR"})
    client = FakeSubmitClient(status="ACTIVE", checks=[{"name": "SELF_CORRELATION", "result": "PASS"}])

    _worker(db, client, max_submissions=1).run()

    active = db.query("SELECT * FROM active_alphas")[0]
    assert active["canonical_key"] == candidate["canonical_key"]
    assert active["settings_hash"] == candidate["settings_hash"]
    assert active["expression_hash"] == candidate["expression_hash"]
    # And the stored key really is the non-default one, not a defaults reconstruction.
    import canonical as canon

    assert candidate["canonical_key"] != canon.canonical_key(candidate["expression"], {})
