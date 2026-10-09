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
    for index in range(8):
        _settle(db, f"group_rank(ts_rank(operating_income, {20 + index}), subindustry)", index % 2 == 0,
                "fundamental")
    model = surrogate.fit(db, min_samples=5)
    assert model["samples"] == 8
    assert "field:operating_income" in model["features"]
    assert "is_pass" in model["targets"]
    assert surrogate.load(db)["version"] == 1
    evaluated = surrogate.evaluate(db)
    assert evaluated["available"] is True
    assert evaluated["oos"] is True
    assert evaluated["train_samples"] + evaluated["test_samples"] == 8


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


# ---------------------------------------------------------------------------
# P24.2: specific failure targets, not only the composite outcome
# ---------------------------------------------------------------------------


def test_failure_targets_read_the_named_check_and_are_none_without_checks():
    refused = {"checks_json": '[{"name": "LOW_SHARPE", "result": "FAIL"}]'}
    cleared = {"checks_json": '[{"name": "LOW_SHARPE", "result": "PASS"}]'}
    assert surrogate._target(refused, "low_sharpe") == 1.0
    assert surrogate._target(cleared, "low_sharpe") == 0.0
    assert surrogate._target(refused, "low_fitness") == 0.0
    assert surrogate._target({}, "low_sharpe") is None


def test_fit_learns_the_gate_failure_targets_from_checks(db):
    for index in range(8):
        _settle(db, f"group_rank(ts_rank(operating_income, {20 + index}), subindustry)",
                index % 2 == 0, "fundamental")
    model = surrogate.fit(db, min_samples=5)
    assert "low_sharpe" in model["targets"]
    assert "low_fitness" in model["targets"]
    assert all("checks" not in name for name in model["features"]), (
        "post-outcome checks must never become pre-simulation features"
    )


# ---------------------------------------------------------------------------
# P24.3: calibration and fixed-budget capture
# ---------------------------------------------------------------------------


def test_calibration_helpers_score_a_perfect_and_a_useless_forecast():
    perfect = surrogate.calibration_report([1.0, 0.0, 1.0, 0.0], [1.0, 0.0, 1.0, 0.0])
    assert perfect["brier"] == pytest.approx(0.0)
    assert perfect["base_rate"] == pytest.approx(0.5)
    assert perfect["base_rate_brier"] == pytest.approx(0.25)
    assert perfect["log_loss"] < 1e-4
    table = perfect["reliability"]
    assert table and all("observed_rate" in row and "count" in row for row in table)
    # A constant 0.5 forecast on a 50/50 problem is exactly as bad as the base rate.
    useless = surrogate.calibration_report([1.0, 0.0, 1.0, 0.0], [0.5, 0.5, 0.5, 0.5])
    assert useless["brier"] == pytest.approx(0.25)
    assert useless["base_rate_brier"] == pytest.approx(0.25)


def test_evaluate_reports_calibration_and_passes_per_budget(db):
    for index in range(12):
        _settle(db, f"group_rank(ts_rank(operating_income, {20 + index}), subindustry)",
                index % 3 == 0, "fundamental")
    surrogate.fit(db, min_samples=5)
    evaluated = surrogate.evaluate(db)
    assert "calibration" in evaluated and evaluated["calibration"]["samples"] >= 1
    capture = evaluated["passes_per_budget"]
    assert set(capture) >= {"budget", "surrogate", "fifo", "random", "available_passes"}
    assert capture["budget"] <= capture["test_size"]
    assert 0 <= capture["surrogate"] <= capture["budget"]


def test_walk_forward_is_expanding_and_reports_calibration(db):
    for index in range(16):
        _settle(db, f"group_rank(ts_rank(operating_income, {20 + index}), subindustry)",
                index % 2 == 0, "fundamental")
    surrogate.fit(db, min_samples=5)
    report = surrogate.walk_forward(db, folds=3, min_train=5)
    assert report["available"] is True
    scored = [fold for fold in report["folds"] if not fold.get("skipped")]
    assert len(scored) >= 1
    # The expanding window never shrinks: each fold trains on at least as much as the last.
    train_sizes = [fold["train"] for fold in scored]
    assert train_sizes == sorted(train_sizes)
    assert report["calibration"]["samples"] == sum(fold["test"] for fold in scored)


def test_walk_forward_is_explicit_when_history_is_too_short(db):
    _settle(db, "rank(close)", True, "technical")
    report = surrogate.walk_forward(db, folds=3, min_train=5)
    assert report["available"] is False
    assert report["reason"] == "insufficient_history"
