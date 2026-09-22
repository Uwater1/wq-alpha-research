from __future__ import annotations

import json

import generator
import research_db
import pytest


@pytest.fixture
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def test_catalog_exposes_all_supplied_dataset_families():
    catalog = generator.Catalog()
    assert len(catalog.fields) == 4367
    assert {field.dataset for field in catalog.fields} >= {
        "analyst4", "fundamental2", "fundamental6", "news12", "option8", "pv1", "socialmedia8", "univ1",
    }


def test_generation_is_reproducible_and_records_lineage(db):
    first = generator.CandidateGenerator(db, seed=7)
    proposals_a = first.proposals(count=5, family="all")
    proposals_b = generator.CandidateGenerator(db, seed=7).proposals(count=5, family="all")
    assert proposals_a == proposals_b

    outcomes = first.queue("campaign-1", proposals_a)
    assert len(outcomes) == 5
    row = db.get_candidate(outcomes[0]["candidate_id"])
    assert row["campaign_id"] == "campaign-1"
    assert row["generator_version"] == generator.GENERATOR_VERSION
    assert json.loads(row["mutation_parameters_json"])["catalog_version"] == first.catalog.version
    assert row["mutation_type"] == "dataset_coverage"


def test_numeric_turnover_diagnosis_selects_repair_even_without_named_check(db):
    outcome = db.queue_candidate("rank(close)", {"decay": 4}, signal_family="technical")
    parent = db.get_candidate(outcome.candidate_id)
    db.query("UPDATE candidates SET turnover=? WHERE id=?", (0.39, parent["id"]))
    proposals = generator.CandidateGenerator(db, seed=1).mutate(db.get_candidate(parent["id"]), count=2)
    assert [proposal.mutation_type for proposal in proposals] == ["turnover_repair", "turnover_repair"]
    assert all(proposal.settings["decay"] == 8 for proposal in proposals)


def test_failure_directed_turnover_repair_uses_structured_mutation(db):
    outcome = db.queue_candidate("rank(close)", {"decay": 4}, signal_family="technical")
    parent = db.get_candidate(outcome.candidate_id)
    db.query("UPDATE candidates SET failure_reason=? WHERE id=?", ("HIGH_TURNOVER", parent["id"]))
    parent = db.get_candidate(parent["id"])

    proposals = generator.CandidateGenerator(db, seed=1).mutate(parent, count=2)
    assert [proposal.mutation_type for proposal in proposals] == ["turnover_repair", "turnover_repair"]
    assert all("hump=" in proposal.expression for proposal in proposals)
    outcomes = generator.CandidateGenerator(db, seed=1).queue("repair-campaign", proposals)
    assert all(item["action"] == "queued" for item in outcomes)
    child = db.get_candidate(outcomes[0]["candidate_id"])
    assert child["parent_id"] == parent["id"]
    assert child["generation"] == 1
    assert json.loads(child["parent_ids_json"]) == [parent["id"]]
