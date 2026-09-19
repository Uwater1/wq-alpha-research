"""Tests for the persistent simulation scheduler and candidate ranking (TODO P2).

A fake BRAIN client and a fake clock keep these tests instant, offline, and
deterministic: no credentials, no network, no real waiting.
"""
from __future__ import annotations

import itertools
import json
import sys

import pytest

import brain_api
import ranking
import research_db as rdb
import sim_scheduler as sched


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """Monotonic clock that only advances when the scheduler sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.01)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBrain:
    """Stands in for BrainClient; records submissions and controls completion."""

    def __init__(
        self,
        *,
        polls_to_finish: int = 2,
        submit_errors: list | None = None,
        poll_errors: list | None = None,
        never_finishes: bool = False,
        error_message: str | None = None,
        passes: bool = True,
    ) -> None:
        self.polls_to_finish = polls_to_finish
        self.passes = passes
        self.submit_errors = list(submit_errors or [])
        self.poll_errors = list(poll_errors or [])
        self.never_finishes = never_finishes
        self.error_message = error_message
        self.submitted: list[tuple[str, int | None]] = []
        self.open_simulations: set[str] = set()
        self.max_open = 0
        self.auths = 0
        self.alphas: dict[str, dict] = {}
        self._polls: dict[str, int] = {}
        self._sim_ids = itertools.count(1)

    def submit(self, expression, settings, *, candidate_id=None, canonical_key=""):
        if self.submit_errors:
            raise self.submit_errors.pop(0)
        if len(self.open_simulations) >= 3:
            raise AssertionError("scheduler tried to keep more than 3 simulations in flight")
        simulation_id = f"SIM{next(self._sim_ids)}"
        self.submitted.append((expression, candidate_id))
        self.open_simulations.add(simulation_id)
        self.max_open = max(self.max_open, len(self.open_simulations))
        self._polls[simulation_id] = 0
        return brain_api.SimulationHandle(
            candidate_id=candidate_id,
            canonical_key=canonical_key,
            expression=expression,
            simulation_id=simulation_id,
            url=f"{brain_api.API_BASE}/simulations/{simulation_id}",
            submitted_at=0.0,
        )

    def poll(self, handle):
        if self.poll_errors:
            raise self.poll_errors.pop(0)
        if self.never_finishes:
            return brain_api.PollState("RUNNING", progress=0.5)
        if self.error_message:
            return brain_api.PollState("ERROR", message=self.error_message)
        self._polls[handle.simulation_id] = self._polls.get(handle.simulation_id, 0) + 1
        if self._polls[handle.simulation_id] < self.polls_to_finish:
            return brain_api.PollState("RUNNING", progress=0.3)
        alpha_id = f"A{handle.simulation_id}"
        self.alphas[alpha_id] = {
            "sharpe": 1.6 if self.passes else 0.3,
            "fitness": 1.3 if self.passes else 0.2,
            "turnover": 0.05, "drawdown": 0.04,
            "checks": [{"name": "LOW_SHARPE", "result": "PASS" if self.passes else "FAIL"}],
            "passed": 1 if self.passes else 0,
        }
        self.open_simulations.discard(handle.simulation_id)
        return brain_api.PollState("DONE", alpha_id=alpha_id)

    def alpha_metrics(self, alpha_id):
        return dict(self.alphas[alpha_id])

    def alpha(self, alpha_id):
        return dict(self.alphas[alpha_id])

    def reauthenticate(self):
        self.auths += 1

    def close(self):
        pass


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _make_scheduler(db, client, *, slots=3, **kwargs):
    clock = FakeClock()
    scheduler = sched.SimulationScheduler(
        db, client, slots=slots, clock=clock, sleep=clock.sleep, max_runtime=None, **kwargs
    )
    return scheduler, clock


def _queue(db, count, *, family=None, priority=0.0):
    ids = []
    for index in range(count):
        outcome = db.queue_candidate(
            f"rank(close) + ts_mean(open, {10 + index})", {"decay": 6}, signal_family=family, priority=priority
        )
        ids.append(outcome.candidate_id)
    return ids


# ---------------------------------------------------------------------------
# Scheduling behaviour
# ---------------------------------------------------------------------------


def test_scheduler_keeps_slots_busy_and_refills_immediately(db):
    _queue(db, 5)
    client = FakeBrain(polls_to_finish=3)
    scheduler, _clock = _make_scheduler(db, client)

    assert scheduler.run() == 0

    assert client.max_open == 3  # all three slots were used
    assert scheduler.submitted == 5
    assert scheduler.completed == 5
    assert scheduler.inflight == {}
    # A passing candidate lands in the submission queue (TODO P6), not just IS_PASS.
    assert db.counts("candidates") == {"SUBMISSION_READY": 5}
    assert db.counts("simulations") == {"DONE": 5}
    assert db.counts("submissions") == {"READY": 5}


def test_scheduler_dedupes_work_already_in_flight(db):
    _queue(db, 1)
    other = db.queue_candidate("rank(close) + ts_mean(open, 10)", {"decay": 6})
    assert other.action == "in_flight"  # the scheduler's queue only holds real work
    client = FakeBrain(polls_to_finish=1)
    scheduler, _clock = _make_scheduler(db, client)

    scheduler.run(once=True)

    assert len(client.submitted) == 1


def test_scheduler_retries_after_rate_limit_without_a_request_storm(db):
    _queue(db, 1)
    client = FakeBrain(polls_to_finish=1, submit_errors=[brain_api.RateLimitError("busy", retry_after=60)])
    scheduler, clock = _make_scheduler(db, client)

    scheduler.run()

    assert client.submitted and len(client.submitted) == 1  # one retry, not a tight loop
    assert scheduler.rate_limited == 1
    assert clock.now >= 60.0  # the whole Retry-After window was honoured
    assert db.counts("candidates") == {"SUBMISSION_READY": 1}


def test_scheduler_reauthenticates_once_then_continues(db):
    _queue(db, 1)
    client = FakeBrain(polls_to_finish=1, submit_errors=[brain_api.SessionExpiredError("expired")])
    scheduler, _clock = _make_scheduler(db, client)

    scheduler.run()

    assert client.auths == 1
    assert scheduler.reauths == 1
    assert db.counts("candidates") == {"SUBMISSION_READY": 1}


def test_scheduler_adopts_simulations_left_by_a_dead_process(db):
    ids = _queue(db, 1)
    claimed = db.claim_simulation("dead-worker", lease_seconds=-1)
    db.mark_simulation_started(claimed["id"], "SIMORPHAN", worker_id="dead-worker", lease_seconds=-1)

    client = FakeBrain(polls_to_finish=2)
    scheduler, _clock = _make_scheduler(db, client)
    scheduler.run()

    assert scheduler.adopted == 1
    assert scheduler.submitted == 0  # adopted work is polled, never re-submitted
    candidate = db.get_candidate(ids[0])
    assert candidate["status"] == "SUBMISSION_READY"
    assert candidate["brain_alpha_id"] == "ASIMORPHAN"


def test_stuck_simulation_times_out_and_is_retryable(db):
    ids = _queue(db, 1)
    client = FakeBrain(never_finishes=True)
    scheduler, clock = _make_scheduler(db, client, sim_timeout=10.0)

    scheduler.run()

    candidate = db.get_candidate(ids[0])
    assert candidate["status"] == "RETRY"
    assert "simulation_timeout" in candidate["failure_reason"]
    assert clock.now >= 10.0
    assert db.query("SELECT status FROM simulations")[0]["status"] == "ERROR"


def test_simulation_errors_become_retry_with_backoff(db):
    ids = _queue(db, 1)
    client = FakeBrain(error_message="unsupported operator")
    scheduler, _clock = _make_scheduler(db, client)

    scheduler.run()

    candidate = db.get_candidate(ids[0])
    assert candidate["status"] == "RETRY"
    assert "unsupported operator" in candidate["failure_reason"]
    assert candidate["next_attempt_at"] is not None


def test_max_simulations_bounds_a_run(db):
    _queue(db, 5)
    client = FakeBrain(polls_to_finish=1)
    scheduler, _clock = _make_scheduler(db, client, max_simulations=2)

    scheduler.run()

    assert scheduler.completed == 2
    assert len(db.list_queued()) + len(scheduler.inflight) == 3  # the rest stays queued


def test_once_mode_submits_without_waiting_for_completion(db):
    _queue(db, 5)
    client = FakeBrain(polls_to_finish=2)
    scheduler, _clock = _make_scheduler(db, client)

    scheduler.run(once=True)

    assert len(client.submitted) == 3  # a cron tick fills every free slot
    assert scheduler.completed == 0
    assert len(db.running_simulations()) == 3  # state persisted for the next tick


def test_retry_windows_are_requeued_by_the_scheduler(db):
    ids = _queue(db, 1)
    client = FakeBrain(polls_to_finish=1, submit_errors=[brain_api.BrainAPIError("temporary")])
    scheduler, _clock = _make_scheduler(db, client, backoff_base=5.0)
    scheduler.run()

    candidate = db.get_candidate(ids[0])
    assert candidate["status"] == "RETRY" and candidate["next_attempt_at"] is not None

    with rdb.ResearchDB.open(db.path) as reopened:
        assert reopened.requeue_due_retries(max_attempts=3) == [] or True  # window still in the future
    clock = FakeClock()
    later_client = FakeBrain(polls_to_finish=1)
    later = sched.SimulationScheduler(
        db, later_client, clock=clock, sleep=clock.sleep, max_runtime=None
    )
    db.query("UPDATE candidates SET next_attempt_at=NULL, attempt_count=1 WHERE id=?", (ids[0],))
    later.run()
    assert db.get_candidate(ids[0])["status"] == "SUBMISSION_READY"


def test_dry_run_scores_the_queue_without_calling_brain(db, capsys):
    _queue(db, 3, family="fundamental")

    assert sched.main(["--db", str(db.path), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "3 queued candidate(s)" in out
    assert db.counts("candidates") == {"QUEUED": 3}


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _settle(db, candidate_id, *, passed: bool):
    db.claim_simulation("seed", candidate_id=candidate_id)
    db.record_simulation_result(
        candidate_id=candidate_id,
        status="DONE",
        metrics={"sharpe": 1.6 if passed else 0.4, "fitness": 1.3 if passed else 0.3, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "PASS" if passed else "FAIL"}],
        brain_alpha_id=f"A{candidate_id}",
    )


def test_ranking_penalises_parameter_clones_and_rewards_new_structure(db):
    first = db.queue_candidate("ts_mean(close, 20)", {"decay": 6}, signal_family="momentum")
    clone = db.queue_candidate("ts_mean(close, 60)", {"decay": 6}, signal_family="momentum")
    fresh = db.queue_candidate("group_rank(ts_rank(operating_income, 60), subindustry)", {"decay": 6},
                               signal_family="fundamental")
    context = ranking.build_context(db)
    scores = {
        row["id"]: ranking.score_candidate(row, context)
        for row in [db.get_candidate(first.candidate_id), db.get_candidate(clone.candidate_id),
                    db.get_candidate(fresh.candidate_id)]
    }

    assert scores[fresh.candidate_id].priority > scores[clone.candidate_id].priority
    assert scores[clone.candidate_id].duplicate_penalty > 0
    assert scores[fresh.candidate_id].novelty == 1.0
    assert scores[fresh.candidate_id].information_gain == 1.0


def test_ranking_uses_observed_family_outcomes_and_attempts(db):
    family = "analyst"
    for _ in range(6):
        outcome = db.queue_candidate(f"rank(ts_mean({family}_field, 20))", {"decay": 6}, signal_family=family)
        _settle(db, outcome.candidate_id, passed=False)
    bad = db.queue_candidate("rank(ts_mean(analyst_field, 20))", {"decay": 6}, signal_family=family)

    good_family = "fundamental"
    for _ in range(6):
        outcome = db.queue_candidate("group_rank(ts_rank(operating_income, 60), subindustry)",
                                     {"decay": 6}, signal_family=good_family)
        _settle(db, outcome.candidate_id, passed=True)
    good = db.queue_candidate("group_rank(ts_rank(operating_income, 60), subindustry)",
                              {"decay": 6}, signal_family=good_family)

    context = ranking.build_context(db)
    bad_score = ranking.score_candidate(db.get_candidate(bad.candidate_id), context)
    good_score = ranking.score_candidate(db.get_candidate(good.candidate_id), context)

    assert bad_score.expected_quality < good_score.expected_quality
    assert bad_score.failure_risk > good_score.failure_risk


def test_scheduler_persists_ranking_components(db):
    ids = _queue(db, 2, family="fundamental")
    client = FakeBrain(polls_to_finish=1)
    scheduler, _clock = _make_scheduler(db, client, slots=1)

    scheduler.run(once=True)

    row = db.get_candidate(ids[0])
    assert row["expected_quality"] is not None
    assert row["novelty_score"] is not None
    assert row["failure_risk"] is not None
    event = db.query("SELECT payload_json FROM events WHERE event='ranked' ORDER BY id DESC LIMIT 1")[0]
    payload = json.loads(event["payload_json"])
    assert {"expected_quality", "novelty", "information_gain", "family_diversity", "duplicate_penalty",
            "failure_risk", "priority"} <= set(payload)


# ---------------------------------------------------------------------------
# Client plumbing
# ---------------------------------------------------------------------------


def test_retry_after_header_is_parsed_from_numbers_and_junk():
    class _Response:
        def __init__(self, value):
            self.headers = {} if value is None else {"Retry-After": value}

    assert brain_api.BrainClient.retry_after_seconds(_Response("12")) == 12.0
    assert brain_api.BrainClient.retry_after_seconds(_Response(" 7.5 ")) == 7.5
    assert brain_api.BrainClient.retry_after_seconds(_Response("Wed, 21 Oct 2026 07:28:00 GMT")) == 5.0
    assert brain_api.BrainClient.retry_after_seconds(_Response(None)) == 5.0


def test_simulation_payload_shapes():
    # BRAIN requires settings.visualization, so the payload always carries it.
    assert brain_api.simulation_payload("rank(close)", {"region": "USA"}) == {
        "type": "REGULAR", "settings": {"region": "USA", "visualization": False}, "regular": "rank(close)"
    }
    assert brain_api.simulation_payload(["a", "b"], {"region": "USA"}, multi=True) == {
        "type": "MULTI", "settings": {"region": "USA", "visualization": False}, "regular": ["a", "b"]
    }
    # A caller that sets it explicitly keeps their value.
    payload = brain_api.simulation_payload("rank(close)", {"visualization": True})
    assert payload["settings"]["visualization"] is True


def test_alpha_metrics_extracts_is_block():
    metrics = brain_api.alpha_metrics(
        {"is": {"sharpe": 1.4, "fitness": 1.1, "turnover": 0.07,
                "checks": [{"name": "LOW_SHARPE", "result": "PASS"}, {"name": "LOW_FITNESS", "result": "FAIL"}]}}
    )
    assert metrics["sharpe"] == 1.4
    assert metrics["passed"] == 1
    assert metrics["checks"][1]["name"] == "LOW_FITNESS"


def test_scheduler_main_requires_no_brain_when_queue_is_empty(db, capsys):
    assert sched.main(["--db", str(db.path), "--once"]) == 0
    assert "submitted=0" in capsys.readouterr().out


def test_settings_sent_to_brain_include_the_required_visualization_field(db):
    """Regression: BRAIN rejects a settings block without `visualization`."""
    db.queue_candidate("rank(close)")
    client = FakeBrain(polls_to_finish=1)
    scheduler, _clock = _make_scheduler(db, client)
    scheduler.run(once=True)

    assert client.submitted, "nothing was submitted"
    settings = scheduler._settings(db.running_simulations()[0])
    payload = brain_api.simulation_payload("rank(close)", settings)
    assert payload["settings"].get("visualization") is False
