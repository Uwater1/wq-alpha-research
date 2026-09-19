"""Offline tests for the staged-search funnel (TODO P5/P14).

The funnel only matters when generation volume is real, so the tests check the spending
decisions: a fresh grid keeps one slot, a failed structure keeps its siblings waiting, a
proven structure expands, a saturated one stops, and the budget ledger records why.
"""
from __future__ import annotations

import pytest

import research_db as rdb
import staged_search as staged
from test_scheduler import FakeBrain, _make_scheduler


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _grid(db, windows, *, decay=6, family="momentum"):
    """A parameter grid: one structure, many variants (windows collapse to one skeleton)."""
    return [
        db.queue_candidate(f"ts_mean(close, {window})", {"decay": decay}, signal_family=family).candidate_id
        for window in windows
    ]


def _settle(db, candidate_id, *, passed: bool):
    db.claim_simulation("seed", candidate_id=candidate_id)
    db.record_simulation_result(
        candidate_id=candidate_id,
        status="DONE",
        metrics={"sharpe": 1.6 if passed else 0.4, "fitness": 1.3 if passed else 0.3, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "PASS" if passed else "FAIL"}],
        brain_alpha_id=f"A{candidate_id}",
    )


# ---------------------------------------------------------------------------
# Budget decisions (no fixed funnel ratios)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attempted, passed, expected",
    [
        (0, 0, ("unproven", "baseline", 1)),
        (1, 0, ("unproven", "hold", 0)),
        (6, 0, ("saturated", "stop", 0)),
        (1, 1, ("proven", "expand", 7)),
        (8, 8, ("proven", "stop", 0)),
        (10, 1, ("saturated", "stop", 0)),  # pass ratio 0.1 < 0.25 after enough attempts
    ],
)
def test_budget_decision_is_evidence_driven(attempted, passed, expected):
    assert staged.decide(attempted, passed) == expected


# ---------------------------------------------------------------------------
# Volume gate
# ---------------------------------------------------------------------------


def test_untried_structure_is_not_deferred_below_the_volume_threshold(db):
    _grid(db, range(4))

    counters = staged.review(db, volume_threshold=30)

    assert counters["engaged"] is False
    assert counters["deferred"] == 0
    assert len(db.list_queued(due_only=True)) == 4


def test_fresh_grid_cannot_monopolize_slots_above_the_threshold(db):
    ids = _grid(db, range(8))

    counters = staged.review(db, volume_threshold=5)

    assert counters["engaged"] is True
    assert counters["deferred"] == 7
    assert len(db.list_queued(due_only=True)) == 1  # exactly one baseline holds the structure
    deferred = [db.get_candidate(candidate_id) for candidate_id in ids]
    assert sum(1 for row in deferred if row["gate_reason"]) == 7


def test_failed_structure_keeps_its_siblings_waiting(db):
    ids = _grid(db, range(6))
    _settle(db, ids[0], passed=False)  # the representative spent one slot and failed

    counters = staged.review(db, volume_threshold=5)

    assert counters["deferred"] == 5
    assert len(db.list_queued(due_only=True)) == 0
    assert all(db.get_candidate(candidate_id)["gate_reason"] == "staged:hold" for candidate_id in ids[1:])


def test_proven_structure_expands_up_to_its_budget(db):
    ids = _grid(db, range(8))
    staged.review(db, volume_threshold=5)  # one baseline claimable, seven deferred
    _settle(db, ids[0], passed=True)  # the base signal proved useful

    counters = staged.review(db, volume_threshold=5, max_variants=3)

    assert counters["promoted"] == 2  # 3 - 1 already attempted
    assert len(db.list_queued(due_only=True)) == 2


def test_saturated_structure_stops_consuming_capacity(db):
    ids = _grid(db, range(10))
    # Six attempts, none of them passing: the family is a bad bet.
    for candidate_id in ids[:6]:
        db.query("UPDATE candidates SET attempt_count=1, status='REJECTED' WHERE id=?", (candidate_id,))

    counters = staged.review(db, volume_threshold=3)

    assert counters["engaged"] is True
    assert counters["stopped"] == 1
    assert counters["deferred"] == 4
    assert len(db.list_queued(due_only=True)) == 0
    ledger = db.structure_budgets()
    assert ledger and ledger[0]["status"] == "saturated" and ledger[0]["attempts"] == 6


def test_budget_ledger_records_lineage_and_spend(db):
    ids = _grid(db, range(6), family="fundamental")
    _settle(db, ids[0], passed=True)

    staged.review(db, volume_threshold=2, max_variants=4)

    ledger = db.structure_budgets()
    assert len(ledger) == 1
    row = ledger[0]
    assert row["family"] == "fundamental"
    assert (row["attempts"], row["passes"]) == (1, 1)
    assert row["status"] == "proven" and row["budget"] == 3
    assert staged.lineage(db)[0]["family"] == "fundamental"
    assert staged.lineage(db)[0]["passes"] == 1


def test_status_is_read_only(db):
    _grid(db, range(6))
    before = db.counts("candidates")

    payload = staged.status(db)

    assert payload["queued"] == 6
    assert payload["structures"]
    assert db.counts("candidates") == before
    assert all(db.get_candidate(c["id"])["gate_reason"] is None for c in
               db.query("SELECT id FROM candidates"))


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


def test_scheduler_spends_a_fresh_grid_one_slot_at_a_time(db):
    _grid(db, range(20))
    client = FakeBrain(polls_to_finish=1)
    scheduler, _clock = _make_scheduler(db, client, slots=3, max_simulations=1,
                                        staged_volume_threshold=5)

    scheduler.run()

    assert scheduler.submitted == 1
    assert client.max_open == 1  # while unproven the grid held exactly one slot
    assert len(db.list_queued()) == 19
    # That one pass proved the structure, so the funnel opened its expansion budget.
    assert len(db.list_queued(due_only=True)) == staged.DEFAULT_MAX_VARIANTS - 1


def test_scheduler_can_disable_the_staged_funnel(db):
    _grid(db, range(20))
    client = FakeBrain(polls_to_finish=1)
    scheduler, _clock = _make_scheduler(db, client, slots=3, staged_search=False,
                                        staged_volume_threshold=5)

    scheduler.run(once=True)

    # Without the funnel all three slots can go to the same unproven structure.
    assert len(client.submitted) == 3


def test_scheduler_expands_a_proven_structure_into_its_grid(db):
    ids = _grid(db, range(6))
    _settle(db, ids[0], passed=True)
    client = FakeBrain(polls_to_finish=1)
    scheduler, _clock = _make_scheduler(db, client, slots=3, staged_volume_threshold=2,
                                        staged_max_variants=3)

    scheduler.run(once=True)

    assert len(client.submitted) == 2  # budget 3 minus the one already attempted
