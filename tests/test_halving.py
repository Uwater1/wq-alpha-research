"""Tests for the P5 variant gate and the P6 submission queue worker.

Both features exist to change what the pipeline spends capacity on, so the tests check
the spending decisions: an unproven structure gets one slot at a time, a proven one gets
its grid, and the submission queue holds candidates instead of dropping them.
"""
from __future__ import annotations

import pytest

import brain_api
import ranking
import research_db as rdb
import submission_worker as sw
import successive_halving as halving
from test_scheduler import FakeBrain, FakeClock, _make_scheduler


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _grid(db, windows=(20, 40, 60), *, decay=6, family="momentum"):
    """A parameter grid: one structure, three variants."""
    return [
        db.queue_candidate(f"ts_mean(close, {window})", {"decay": decay}, signal_family=family).candidate_id
        for window in windows
    ]


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_untried_structures_are_not_deferred(db):
    _grid(db)

    counters = halving.review(db)

    assert counters["deferred"] == 0
    assert len(db.list_queued(due_only=True)) == 3


def test_variants_of_a_failed_structure_wait_for_the_horizon(db):
    ids = _grid(db)
    scheduler, _clock = _make_scheduler(db, FakeBrain(polls_to_finish=1, passes=False), max_simulations=1)

    scheduler.run()  # one representative simulation, which fails the IS gate

    assert db.get_candidate(ids[0])["status"] == "REJECTED"
    waiters = [db.get_candidate(candidate_id) for candidate_id in ids[1:]]
    assert all(row["status"] == "QUEUED" for row in waiters)
    assert all(row["gate_reason"] == "variant_search" for row in waiters)
    # Deferred rows stay out of the scheduler's claimable set but are not lost.
    assert len(db.list_queued(due_only=True)) == 0
    assert len(db.list_queued()) == 2


def test_a_proven_structure_expands_into_its_grid(db):
    ids = _grid(db)
    scheduler, _clock = _make_scheduler(db, FakeBrain(polls_to_finish=1), max_simulations=3)

    scheduler.run()

    assert scheduler.completed == 3  # the base signal passed, so the grid was worth it
    assert db.counts("candidates") == {"SUBMISSION_READY": 3}
    assert halving.status(db)["deferred_candidates"] == 0


def test_a_passing_sibling_promotes_deferred_variants(db):
    ids = _grid(db)
    # Defer the variants as if the structure had failed once.
    db.defer_candidate(ids[1], reason="variant_search", until="2999-01-01T00:00:00+00:00")
    db.defer_candidate(ids[2], reason="variant_search", until="2999-01-01T00:00:00+00:00")
    assert len(db.list_queued(due_only=True)) == 1

    # The representative variant passes, which proves the structure.
    db.claim_simulation("seed", candidate_id=ids[0])
    db.record_simulation_result(
        candidate_id=ids[0], status="DONE", metrics={"sharpe": 1.6, "fitness": 1.3, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "PASS"}], brain_alpha_id="A1",
    )
    counters = halving.review(db)

    assert counters["promoted_proven"] == 2
    assert len(db.list_queued(due_only=True)) == 2
    assert all(db.get_candidate(candidate_id)["gate_reason"] is None for candidate_id in ids[1:])


def test_elapsed_horizon_releases_a_variant_even_without_a_verdict(db):
    ids = _grid(db)
    db.defer_candidate(ids[1], reason="variant_search", until="2000-01-01T00:00:00+00:00")

    counters = halving.review(db)

    assert counters["promoted_horizon"] == 1
    assert db.get_candidate(ids[1])["gate_reason"] is None
    assert len(db.list_queued(due_only=True)) == 3  # the released variant joins the grid again


def test_skeleton_outcomes_count_attempts_and_passes(db):
    ids = _grid(db)
    db.claim_simulation("seed", candidate_id=ids[0])
    db.record_simulation_result(
        candidate_id=ids[0], status="DONE", metrics={"sharpe": 0.3, "fitness": 0.2, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "FAIL"}],
    )

    outcomes = db.skeleton_outcomes()
    assert len(outcomes) == 1
    stats = next(iter(outcomes.values()))
    assert stats["attempted"] == 1 and stats["passed"] == 0 and stats["total"] == 3


def test_scheduler_can_disable_the_gate(db):
    _grid(db)
    scheduler, _clock = _make_scheduler(db, FakeBrain(polls_to_finish=1, passes=False),
                                        max_simulations=1, halving=False)

    scheduler.run()

    assert len(db.list_queued(due_only=True)) == 2  # no deferral without the gate


# ---------------------------------------------------------------------------
# Submission queue: gates and ordering
# ---------------------------------------------------------------------------


def _ready_candidate(db, expression="rank(close)", *, sharpe=1.6, fitness=1.3, turnover=0.05,
                     family="fundamental", checks=None):
    outcome = db.queue_candidate(expression, {"decay": 6}, signal_family=family)
    db.claim_simulation("worker", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=outcome.candidate_id, status="DONE",
        metrics={"sharpe": sharpe, "fitness": fitness, "turnover": turnover},
        checks=checks or [{"name": "LOW_SHARPE", "result": "PASS"}],
        brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return db.get_candidate(outcome.candidate_id)


def test_passing_candidate_enters_the_submission_queue(db):
    candidate = _ready_candidate(db)

    assert candidate["status"] == "SUBMISSION_READY"
    assert db.counts("submissions") == {"READY": 1}


def test_failing_candidates_never_reach_the_submission_queue(db):
    # BRAIN's own checks are authoritative for the IS gate, so a failing check rejects.
    candidate = _ready_candidate(db, sharpe=0.3, checks=[{"name": "LOW_SHARPE", "result": "FAIL"}])

    assert candidate["status"] == "REJECTED"
    assert db.counts("submissions") == {}


def test_a_passing_check_with_weak_metrics_is_still_gated_at_submission(db):
    candidate = _ready_candidate(db, sharpe=1.0)

    # The IS gate passed, but the submission floors are stricter: the candidate waits.
    assert candidate["status"] == "IS_PASS"
    assert db.counts("submissions") == {}
    allowed, reasons = rdb.submission_gate(candidate)
    assert not allowed and any("sharpe" in reason for reason in reasons)
    assert db.query("SELECT event FROM events WHERE event='submission_gate_failed'")


@pytest.mark.parametrize(
    "override, fragment",
    [
        ({"sharpe": 1.0}, "sharpe"),
        ({"fitness": 0.5}, "fitness"),
        ({"turnover": 0.45}, "turnover"),
    ],
)
def test_submission_gates_check_every_floor(override, fragment):
    candidate = {"brain_alpha_id": "A1", "sharpe": 1.6, "fitness": 1.3, "turnover": 0.05, **override}

    allowed, reasons = rdb.submission_gate(candidate)

    assert not allowed and any(fragment in reason for reason in reasons)


def test_submission_gate_blocks_an_identical_active_alpha():
    candidate = {"brain_alpha_id": "A1", "canonical_key": "key1", "sharpe": 1.6, "fitness": 1.3, "turnover": 0.05}

    allowed, reasons = rdb.submission_gate(candidate, active_keys={"key1"})

    assert not allowed and any("already ACTIVE" in reason for reason in reasons)
    assert rdb.submission_gate(candidate, active_keys={"A9"})[0] is True  # a different alpha is fine


def test_correlation_gate_is_opt_in_and_holds_unchecked_candidates():
    candidate = {"brain_alpha_id": "A1", "sharpe": 1.6, "fitness": 1.3, "turnover": 0.05}

    assert rdb.submission_gate(candidate)[0] is True
    allowed, reasons = rdb.submission_gate(candidate, require_correlation=True)
    assert not allowed and any("self-correlation" in reason for reason in reasons)


def test_submission_priority_rewards_underrepresented_families(db):
    crowded = _ready_candidate(db, "rank(close)", family="momentum")
    db.upsert_active_alpha("ACTIVE1", expression="rank(close)", settings={"decay": 6})
    db.query("UPDATE candidates SET status='ACTIVE', signal_family='momentum' WHERE id=?", (crowded["id"],))
    fresh = db.queue_candidate("group_rank(ts_rank(operating_income, 60), subindustry)", {"decay": 6},
                               signal_family="fundamental")
    context = ranking.build_context(db)

    crowded_score = ranking.submission_priority(db.get_candidate(crowded["id"]), context)
    fresh_score = ranking.submission_priority(db.get_candidate(fresh.candidate_id), context)

    assert fresh_score.reasons["portfolio_diversification"] > crowded_score.reasons["portfolio_diversification"]


# ---------------------------------------------------------------------------
# Submission worker
# ---------------------------------------------------------------------------


class FakeSubmitBrain:
    """Stands in for the submission half of BrainClient."""

    def __init__(self, *, self_correlation="PASS", status="ACTIVE", submit_outcome="submitted",
                 checks_rounds=1, max_correlation=None):
        self.self_correlation = self_correlation
        self.status = status
        self.submit_outcome = submit_outcome
        self.checks_rounds = checks_rounds
        self.max_correlation = max_correlation
        self.submitted_alpha_ids: list[str] = []
        self.status_reads = 0

    def submit_alpha(self, alpha_id):
        if self.submit_outcome == "submitted":
            self.submitted_alpha_ids.append(alpha_id)
        return {"outcome": self.submit_outcome, "detail": ""}

    def submit_checks(self, alpha_id):
        self.checks_rounds -= 1
        if self.checks_rounds > 0 or self.self_correlation is None:
            return []
        checks = [{"name": "SELF_CORRELATION", "result": self.self_correlation}]
        if self.max_correlation is not None:
            checks[0]["value"] = self.max_correlation
        return checks

    def alpha_status(self, alpha_id):
        self.status_reads += 1
        return self.status

    def close(self):
        pass


def _worker(db, client, **kwargs):
    clock = FakeClock()
    return sw.SubmissionWorker(db, client, clock=clock, sleep=clock.sleep, max_runtime=None,
                               poll_interval=0.0, max_polls=3, **kwargs)


def test_worker_submits_a_ready_candidate_and_confirms_active(db):
    candidate = _ready_candidate(db)
    client = FakeSubmitBrain()

    assert _worker(db, client).run() == 0

    assert client.submitted_alpha_ids == [candidate["brain_alpha_id"]]
    assert db.counts("submissions") == {"ACTIVE": 1}
    assert db.get_candidate(candidate["id"])["status"] == "ACTIVE"
    assert db.active_alpha_ids() == [candidate["brain_alpha_id"]]
    # The live-book snapshot must be complete, not just an id: P9 re-checks correlation against it.
    active = db.query("SELECT * FROM active_alphas WHERE brain_alpha_id=?", (candidate["brain_alpha_id"],))[0]
    assert active["canonical_key"] == candidate["canonical_key"]
    assert active["sharpe"] == 1.6 and active["fitness"] == 1.3


def test_worker_records_a_platform_correlation_failure(db):
    candidate = _ready_candidate(db)
    client = FakeSubmitBrain(self_correlation="FAIL", status="UNSUBMITTED", max_correlation=0.83)

    _worker(db, client).run()

    submission = db.query("SELECT * FROM submissions")[0]
    assert submission["status"] == "SELF_CORR_FAIL"
    assert submission["max_corr"] == 0.83
    assert db.get_candidate(candidate["id"])["status"] == "REJECTED"


def test_worker_keeps_check_pending_instead_of_guessing(db):
    _ready_candidate(db)
    client = FakeSubmitBrain(self_correlation=None, status="UNSUBMITTED", checks_rounds=99)

    _worker(db, client).run()

    assert db.counts("submissions") == {"CHECK_PENDING": 1}


def test_worker_reconciles_a_pending_submission_that_became_active(db):
    candidate = _ready_candidate(db)
    submission_id = db.enqueue_submission(candidate["id"])
    db.query("UPDATE submissions SET status='CHECK_PENDING', brain_alpha_id=? WHERE id=?",
             (candidate["brain_alpha_id"], submission_id))

    _worker(db, FakeSubmitBrain(status="ACTIVE")).run()

    assert db.counts("submissions") == {"ACTIVE": 1}
    assert db.get_candidate(candidate["id"])["status"] == "ACTIVE"


def test_queue_does_not_stop_at_the_first_success(db):
    first = _ready_candidate(db, "rank(close)")
    second = _ready_candidate(db, "rank(open)", family="analyst")
    client = FakeSubmitBrain()

    worker = _worker(db, client, max_submissions=2)
    worker.run()

    assert len(client.submitted_alpha_ids) == 2
    assert {db.get_candidate(row["id"])["status"] for row in (first, second)} == {"ACTIVE"}
    assert db.counts("submissions") == {"ACTIVE": 2}


def test_a_held_candidate_does_not_block_the_next_one(db):
    held = _ready_candidate(db, "rank(close)", sharpe=0.2)  # fails the floor at claim time
    db.enqueue_submission(held["id"], priority=99.0)  # force it to the front of the queue
    good = _ready_candidate(db, "rank(open)")
    client = FakeSubmitBrain()

    worker = _worker(db, client, max_submissions=1)
    worker.run()

    # The held candidate was claimed first, failed its gate, and the run moved on.
    assert client.submitted_alpha_ids == [good["brain_alpha_id"]]
    assert set(db.counts("submissions")) == {"RETRY", "ACTIVE"}


def test_already_submitted_alpha_is_still_verified(db):
    candidate = _ready_candidate(db)
    client = FakeSubmitBrain(submit_outcome="already_submitted")

    _worker(db, client).run()

    assert db.counts("submissions") == {"ACTIVE": 1}
    assert db.get_candidate(candidate["id"])["status"] == "ACTIVE"


def test_two_workers_cannot_submit_the_same_candidate(db):
    _ready_candidate(db)
    first = _worker(db, FakeSubmitBrain(submit_outcome="in_progress", self_correlation="PASS", status="ACTIVE"),
                    max_submissions=1, worker_id="w1", lease_seconds=300)
    first.run()
    second = _worker(db, FakeSubmitBrain(), max_submissions=1, worker_id="w2")

    second.run()

    assert db.counts("submissions") == {"ACTIVE": 1}  # the lease kept the second worker out


def test_worker_thresholds_are_configurable(db):
    candidate = _ready_candidate(db, sharpe=0.2)
    db.enqueue_submission(candidate["id"], priority=5.0)
    client = FakeSubmitBrain()

    worker = _worker(db, client, max_submissions=1, thresholds={"sharpe": 0.1, "fitness": 0.1})
    worker.run()

    assert client.submitted_alpha_ids == [candidate["brain_alpha_id"]]  # the operator lowered the floor
    assert db.counts("submissions") == {"ACTIVE": 1}


def test_worker_dry_run_lists_the_queue_without_calling_brain(db, capsys):
    _ready_candidate(db)

    assert sw.main(["--db", str(db.path), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "READY submission(s)" in out
    assert db.counts("submissions") == {"READY": 1}


def test_worker_main_with_empty_queue_is_a_no_op(db, capsys):
    assert sw.main(["--db", str(db.path)]) == 0
    assert "submitted=0" in capsys.readouterr().out


def test_named_candidates_are_submitted_before_the_queue(db):
    """`--submit-candidate` means "this next", not "requeue somewhere in the middle"."""
    low = _ready_candidate(db, expression="rank(close)", sharpe=1.9, fitness=1.8)
    named = _ready_candidate(db, expression="rank(open)", sharpe=1.3, fitness=1.2)
    db.enqueue_submission(low["id"], priority=9.0)  # the stronger candidate owns the queue head

    worker = _worker(db, FakeSubmitBrain(), max_submissions=1)
    worker.enqueue_first([named["id"]])
    worker.run()

    assert db.counts("submissions")["ACTIVE"] == 1
    assert db.get_candidate(named["id"])["status"] == "ACTIVE"
    assert db.get_candidate(low["id"])["status"] == "SUBMISSION_READY"
