"""Regression tests for the Generator V3 P20/P21 audit carryovers."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import generator
import generation_policy as policy
import research_db


class _DatasetPrior:
    """A controlled motif succeeds in only one dataset; other datasets disagree."""

    tables = {}
    global_prior = 0.15
    min_evidence = 3

    def score(self, context):
        # Deliberately opposite winners in the two families.
        success = (context.dataset == "analyst4" and context.motif_id == "ratio") or (
            context.dataset == "fundamental6" and context.motif_id == "ranked_level"
        )
        rate = 0.75 if success else 0.02
        look = SimpleNamespace(
            level="motif+dataset", specific_level="motif+dataset",
            simulations=60, specific=SimpleNamespace(simulations=60),
            mean=rate, upper=rate,
        )
        return rate, look


def test_motif_allocations_are_conditioned_by_family_not_best_other_dataset():
    prior = _DatasetPrior()
    motifs = ["ratio", "ranked_level"]
    analyst, analyst_report = policy.allocate_motifs_conditioned(
        motifs, ["analyst4"], 24, prior,
    )
    fundamental, fundamental_report = policy.allocate_motifs_conditioned(
        motifs, ["fundamental6"], 24, prior,
    )
    assert sum(analyst.values()) == sum(fundamental.values()) == 24
    assert analyst["ratio"] > analyst["ranked_level"]
    assert fundamental["ranked_level"] > fundamental["ratio"]
    assert analyst_report["contexts"]["ratio"]["dataset"] == "analyst4"
    assert fundamental_report["contexts"]["ratio"]["dataset"] == "fundamental6"


def test_plan_reports_separate_family_budgets(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as db:
        plan = generator.CandidateGenerator(db).plan(
            campaign_id="dataset-conditioned-audit", budget=32, seed=4,
            prior=_DatasetPrior(), mode="mixed",
        )
    by_family = plan.quality_allocation["by_family"]
    family_counts = plan.distribution("family")
    assert set(by_family) == set(family_counts)
    assert sum(plan.motif_allocation.values()) == plan.budget
    for family, report in by_family.items():
        assert sum(plan_family_budget["budget"] for plan_family_budget in plan.family_allocation) == plan.budget
        assert all(cell["dataset"] == family for cell in report["contexts"].values())


def test_historical_v3_generation_fails_closed_until_full_snapshot_replay(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as db:
        with pytest.raises(ValueError, match="point-in-time archive"):
            generator.CandidateGenerator(db).generate(
                campaign_id="historical-audit", count=8,
                as_of="2000-01-01T00:00:00",
            )
