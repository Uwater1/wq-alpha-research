"""Offline tests for the advisory simulation surrogate."""
from __future__ import annotations

import pytest

import research_db as rdb
import surrogate


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _settle(db, expression, passed, family):
    outcome = db.queue_candidate(expression, {"decay": 6}, signal_family=family)
    row = db.claim_simulation("seed", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=row["id"], status="DONE",
        metrics={"sharpe": 1.6 if passed else 0.3, "fitness": 1.3 if passed else 0.2,
                 "turnover": 0.05 if passed else 0.35},
        checks=[{"name": "LOW_SHARPE", "result": "PASS" if passed else "FAIL"}],
        brain_alpha_id=f"LOCAL{row['id']}",
    )


def test_fit_persists_structural_model_and_diagnostics(db):
    for index in range(6):
        _settle(db, f"group_rank(ts_rank(operating_income, {20 + index}), subindustry)", index % 2 == 0,
                "fundamental")
    model = surrogate.fit(db, min_samples=5)
    assert model["samples"] == 6
    assert "field:operating_income" in model["features"]
    assert "is_pass" in model["targets"]
    assert surrogate.load(db)["version"] == 1
    assert surrogate.evaluate(db)["available"] is True


def test_predict_and_advisory_ranking_keep_candidates_in_play(db):
    for index in range(6):
        _settle(db, f"rank(close) + {index}", index < 3, "technical")
    model = surrogate.fit(db, min_samples=5)
    candidate = db.queue_candidate("group_rank(ts_rank(free_cash_flow_reported_value, 60), industry)",
                                   {"decay": 6}, signal_family="cashflow")
    prediction = surrogate.predict(model, db.get_candidate(candidate.candidate_id))
    assert 0.0 <= prediction["is_pass"] <= 1.0
    assert surrogate.rank_advisory(db, limit=10)  # no candidate is rejected by prediction


def test_insufficient_history_is_explicit(db):
    _settle(db, "rank(close)", True, "technical")
    with pytest.raises(ValueError, match="need at least"):
        surrogate.fit(db, min_samples=5)
    queued = db.queue_candidate("rank(open)", {"decay": 6}, signal_family="technical")
    assert surrogate.rank_advisory(db)[0]["id"] == queued.candidate_id
    assert surrogate.rank_advisory(db)[0]["prediction"] is None
