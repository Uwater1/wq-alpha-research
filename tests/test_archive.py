from __future__ import annotations

import archive
import pytest
import research_db


@pytest.fixture
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _settled(db, expression, family, sharpe, fitness, turnover, status="IS_PASS"):
    outcome = db.queue_candidate(expression, {"decay": 6}, signal_family=family)
    claimed = db.claim_simulation("test", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": sharpe, "fitness": fitness, "turnover": turnover},
        checks=[{"name": "IS", "result": "PASS"}], brain_alpha_id=f"A{outcome.candidate_id}",
    )
    if status == "REJECTED":
        return outcome.candidate_id
    return outcome.candidate_id


def test_rebuild_keeps_one_elite_per_niche(db):
    first = _settled(db, "rank(close)", "technical", 1.2, 1.0, 0.1)
    second = _settled(db, "group_rank(ts_mean(open, 20), industry)", "technical", 1.8, 1.3, 0.1)
    report = archive.rebuild(db)
    assert report["members"] == 2
    cells = db.query("SELECT elite_candidate_id, member_count FROM archive_cells")
    assert len(cells) == 2  # different fields/operators make different niches
    assert {row["elite_candidate_id"] for row in cells} == {first, second}


def test_parent_selection_is_seeded_and_diverse(db):
    _settled(db, "rank(close)", "technical", 1.2, 1.0, 0.1)
    _settled(db, "group_rank(ts_mean(free_cash_flow_reported_value, 60), industry)", "fundamental", 1.4, 1.1, 0.08)
    archive.rebuild(db)
    first = archive.parents(db, count=2, seed=11)
    second = archive.parents(db, count=2, seed=11)
    assert [row["elite_candidate_id"] for row in first] == [row["elite_candidate_id"] for row in second]
    assert {row["signal_family"] for row in first} == {"technical", "fundamental"}


def test_family_allocator_preserves_exploration_reserve(db):
    _settled(db, "rank(close)", "proven", 1.5, 1.2, 0.1)
    for index in range(3):
        _settled(db, f"rank(open) + {index + 1}", "proven", 0.2, 0.1, 0.1, status="REJECTED")
    result = archive.allocate_families(db, ["proven", "unexplored", "new"], budget=6, seed=3, allocation_key="test")
    assert sum(row["budget"] for row in result) == 6
    assert all(row["budget"] >= 1 for row in result)
    assert any(row["family"] in {"unexplored", "new"} and row["exploration"] for row in result)
    assert db.query("SELECT COUNT(*) AS n FROM family_allocations")[0]["n"] == 3
