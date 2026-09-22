from __future__ import annotations

import json

import pytest
import research_db
import robustness


@pytest.fixture
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _candidate(db, index, family, passed):
    outcome = db.queue_candidate(
        f"rank({'close' if index % 2 == 0 else 'open'}) + {index}", {"decay": 6}, signal_family=family,
        campaign_id="campaign-r", generation=index, mutation_type="field_swap",
    )
    claimed = db.claim_simulation("robustness", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": 1.5 if passed else 0.2, "fitness": 1.2 if passed else 0.1, "turnover": 0.08},
        checks=[{"name": "IS", "result": "PASS" if passed else "FAIL"}],
        brain_alpha_id=f"A{outcome.candidate_id}",
    )


def test_campaign_report_discounts_search_and_preserves_provenance(db):
    for index in range(6):
        _candidate(db, index, "family-a" if index < 5 else "family-b", index == 0)
    report = robustness.campaign_report(db, "campaign-r")
    assert report["provenance"]["trial_count"] == 6
    assert report["provenance"]["generations"] == list(range(6))
    assert report["multiple_testing"]["status"] == "advisory"
    assert report["multiple_testing"]["effective_number_of_trials"] >= 2
    assert len(report["families"]) == 2
    assert db.query("SELECT COUNT(*) AS n FROM robustness_reports")[0]["n"] == 1


def test_report_is_explicit_when_pnl_is_unavailable(db):
    _candidate(db, 1, "family-a", True)
    report = robustness.campaign_report(db, "campaign-r", persist=False)
    assert report["stability"]["status"] == "unavailable"
    assert report["stability"]["candidates"] == []
