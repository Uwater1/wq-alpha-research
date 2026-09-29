"""P21/P20.2 regression: conditional evidence, backoff discipline, bounded scoring.

The two failure modes this module exists to prevent are a two-sample bucket steering a campaign
and novelty compensating for a region that keeps failing. Both are pinned here.
"""
from __future__ import annotations

import json

import pytest

import quality_prior as qp
import research_db


@pytest.fixture()
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _row(**overrides):
    row = {
        "motif_id": "ratio", "mutation_operation": "none", "primary_dataset": "analyst4",
        "primary_category": "analyst", "semantic_role_signature": "estimate",
        "recipe_bucket": "t0.08|d8|lb126|SUBINDUSTRY", "parent_quality_bucket": "no_parent",
        "decision": "KEEP", "simulated": True, "is_pass": False, "corr_pass": False,
        "sharpe": 0.0, "settled_at": "2026-09-01T00:00:00",
    }
    row.update(overrides)
    return row


def _context(**overrides):
    base = {
        "motif_id": "ratio", "mutation_operation": "none", "dataset": "analyst4",
        "category": "analyst", "role_signature": "estimate",
        "recipe_bucket": "t0.08|d8|lb126|SUBINDUSTRY", "parent_quality_bucket": "no_parent",
    }
    base.update(overrides)
    return qp.Context(**base)


# ---------------------------------------------------------------------------
# Counts stay separate
# ---------------------------------------------------------------------------


def test_skipped_duplicates_are_attempts_not_negative_outcomes():
    rows = [
        _row(decision="SKIP_REDUNDANT", simulated=False, sharpe=None),
        _row(decision="KEEP", simulated=True, is_pass=True, corr_pass=True, sharpe=1.9),
    ]
    prior = qp.QualityPrior.from_rows(rows)
    evidence = prior.tables["motif"]["ratio"]
    assert evidence.attempts == 2
    assert evidence.simulations == 1
    assert evidence.is_pass == 1 and evidence.corr_pass == 1
    assert evidence.skipped == 1
    assert evidence.mean_sharpe == 1.9


def test_a_row_with_no_simulation_is_not_a_loss():
    """A proposal that never reached BRAIN must not lower the observed pass rate."""
    rows = [_row(decision="KEEP", simulated=False, sharpe=None)]
    prior = qp.QualityPrior.from_rows(rows)
    assert prior.tables["motif"]["ratio"].simulations == 0
    assert prior.global_prior == pytest.approx(qp.DEFAULT_PRIOR_ALPHA / (qp.DEFAULT_PRIOR_ALPHA + qp.DEFAULT_PRIOR_BETA))


# ---------------------------------------------------------------------------
# Hierarchical backoff
# ---------------------------------------------------------------------------


def test_a_narrow_bucket_without_evidence_does_not_answer():
    rows = [
        # One lucky pass in the narrow cell, plenty of evidence one level up.
        _row(is_pass=True, sharpe=2.0),
        *[_row(motif_id="ratio", recipe_bucket="t0.1|d6|lb20|SECTOR", sharpe=-0.2) for _ in range(6)],
    ]
    prior = qp.QualityPrior.from_rows(rows, min_evidence=3)
    look = prior.lookup(_context())
    assert look.specific.simulations == 1
    assert look.level == "motif+dataset"
    assert look.simulations == 7
    assert look.mean < 0.5, "one pass in a one-sample cell must not read as a 100% hypothesis"


def test_backoff_reports_when_nothing_met_the_bar():
    rows = [_row(is_pass=True, sharpe=2.0)]
    prior = qp.QualityPrior.from_rows(rows, min_evidence=3)
    look = prior.lookup(_context())
    assert look.level == "global"
    assert look.backed_off is True
    assert look.mean == prior.global_prior


def test_the_most_specific_sufficient_level_answers():
    rows = [
        _row(is_pass=True, sharpe=2.0) for _ in range(4)
    ] + [
        _row(motif_id="change", sharpe=-0.3) for _ in range(6)
    ]
    prior = qp.QualityPrior.from_rows(rows, min_evidence=3)
    assert prior.lookup(_context()).level == "motif+dataset+recipe"
    # Four passes in a four-sample cell lift the estimate above the global prior, and nowhere
    # near certainty: the Beta(1, 19) prior is what keeps a tiny bucket from reading as a fact.
    assert prior.lookup(_context()).mean > prior.global_prior
    assert prior.lookup(_context()).mean < 0.5
    # A different recipe cell has no evidence of its own, so the answer backs off exactly one
    # level instead of borrowing the narrow cell's luck.
    backed_off = prior.lookup(_context(recipe_bucket="t0.2|d20|lb252|SECTOR"))
    assert backed_off.level == "motif+dataset"
    assert backed_off.simulations == 4
    other = prior.lookup(_context(motif_id="change"))
    assert other.level == "motif+dataset+recipe"
    assert other.specific.simulations == 6
    assert other.mean < 0.5


def test_a_missing_cell_falls_back_rather_than_failing():
    prior = qp.QualityPrior.from_rows([_row(is_pass=True) for _ in range(5)])
    look = prior.lookup(_context(motif_id="never_seen", dataset="nowhere"))
    assert look.level in {name for name, _ in qp.HIERARCHY}
    assert look.level != "motif+dataset+recipe"
    # Nothing is known about the unseen motif: the narrowest matching cell is the operation
    # level, and it says which level it came from rather than pretending to be the motif.
    assert look.specific_level not in {"motif+dataset+recipe", "motif+dataset", "motif"}
    assert look.specific.simulations >= 1
    assert look.simulations >= 1


# ---------------------------------------------------------------------------
# Bounded scoring (P20.2)
# ---------------------------------------------------------------------------


def test_quality_uses_the_upper_bound_but_is_clamped():
    confident_good = qp.Look(mean=0.9, lower=0.8, upper=0.95, simulations=60, level="motif",
                             specific=qp.Evidence(), global_prior=0.05)
    confident_bad = qp.Look(mean=0.0, lower=0.0, upper=0.01, simulations=60, level="motif",
                            specific=qp.Evidence(), global_prior=0.05)
    thin = qp.Look(mean=0.5, lower=0.02, upper=0.75, simulations=1, level="global",
                   specific=qp.Evidence(), global_prior=0.05)
    good = qp.quality_conditioned_score(confident_good)
    bad = qp.quality_conditioned_score(confident_bad)
    thin_score = qp.quality_conditioned_score(thin)
    # With the uncertainty bonus held equal, the quality term orders the three strictly.
    at_equal_uncertainty = {
        name: qp.quality_conditioned_score(look, uncertainty=0.2)
        for name, look in (("good", confident_good), ("thin", thin), ("bad", confident_bad))
    }
    assert at_equal_uncertainty["good"] > at_equal_uncertainty["thin"] > at_equal_uncertainty["bad"]
    # The thin cell may out-score a confident-but-worse cell: that is the exploration bonus
    # paying for genuine uncertainty rather than for quality.
    assert thin_score > bad and good > bad
    assert bad > 0.0, "strongly negative evidence downweights a region, it never deletes it"
    assert bad <= qp.QUALITY_FLOOR * (1 + qp.DEFAULT_EXPLORATION) / qp.COST_FLOOR


def test_novelty_is_bounded_so_it_cannot_compensate_for_quality():
    look = qp.Look(mean=0.02, lower=0.0, upper=0.02, simulations=80, level="motif",
                   specific=qp.Evidence(), global_prior=0.05)
    flat = qp.quality_conditioned_score(look, novelty=0.0)
    maxed = qp.quality_conditioned_score(look, novelty=1.0)
    assert flat > 0.0 and maxed > flat
    # Even at maximum novelty the score stays near the quality floor: novelty is a multiplier,
    # not a substitute for evidence.
    assert maxed / flat <= 1.0 / qp.NOVELTY_FLOOR


def test_uncertainty_pays_a_bounded_bonus_and_cost_is_a_divisor():
    look = qp.Look(mean=0.2, lower=0.05, upper=0.35, simulations=10, level="motif",
                   specific=qp.Evidence(), global_prior=0.05)
    assert qp.quality_conditioned_score(look, cost=2.0) < qp.quality_conditioned_score(look, cost=1.0)
    assert qp.quality_conditioned_score(look, cost=0.0) == qp.quality_conditioned_score(look, cost=qp.COST_FLOOR)
    bounded = qp.quality_conditioned_score(look, uncertainty=1e9)
    assert bounded < qp.QUALITY_CAP * 1.0 * (1.0 + qp.DEFAULT_EXPLORATION * 40000.0)
    assert bounded > 0.0


def test_prior_score_is_monotone_in_evidence_of_success():
    weak = qp.QualityPrior.from_rows([_row(is_pass=True, sharpe=1.5)], min_evidence=1)
    strong = qp.QualityPrior.from_rows([_row(is_pass=True, sharpe=1.5) for _ in range(20)], min_evidence=1)
    assert strong.score(_context())[0] > weak.score(_context())[0]


# ---------------------------------------------------------------------------
# Point-in-time safety and reporting
# ---------------------------------------------------------------------------


def _settle(db, expression, *, version="catalog-generator-v2", is_pass=True, motif_id="ratio"):
    outcome = db.queue_candidate(
        expression, {"decay": 8, "truncation": 0.08}, signal_family="pv1",
        generator_version=version, motif_id=motif_id,
    )
    claimed = db.claim_simulation("t", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": 2.0 if is_pass else 0.1, "fitness": 1.5, "turnover": 0.1},
        checks=[{"name": "IS", "result": "PASS" if is_pass else "FAIL"}],
        brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return outcome.candidate_id


def test_build_is_point_in_time_safe(db):
    _settle(db, "group_rank(ts_rank(close,60),subindustry)")
    cold = qp.QualityPrior.build(db, as_of="2000-01-01T00:00:00")
    assert cold.global_evidence.simulations == 0
    assert cold.global_prior == pytest.approx(0.05)
    warm = qp.QualityPrior.build(db)
    assert warm.global_evidence.simulations == 1
    assert warm.global_prior > cold.global_prior
    assert warm.as_dict()["version"] == qp.QUALITY_PRIOR_VERSION


def test_rank_prefers_the_better_evidenced_context(db):
    _settle(db, "group_rank(ts_rank(close,60),subindustry)", motif_id="ratio")
    for index in range(4):
        _settle(db, f"rank(ts_delta(open,{20 + index}))", is_pass=False, motif_id="change")
    prior = qp.QualityPrior.build(db, min_evidence=3)
    ranked = prior.rank([_context(motif_id="change"), _context(motif_id="ratio")])
    assert ranked[0]["context"]["motif_id"] == "ratio"
    # One passing simulation is not enough to answer at the motif level, so the ratio lookup
    # backs off to the coarsest informative level while still reporting its own single sample.
    assert ranked[0]["look"]["level"] in {"operation+parent_quality", "global"}
    assert ranked[0]["look"]["specific"]["simulations"] == 1
    assert ranked[0]["look"]["specific_level"] == "motif+operation"
    assert ranked[1]["score"] < ranked[0]["score"]
    assert ranked[1]["look"]["level"] == "motif+operation"
    assert ranked[1]["look"]["simulations"] == 4


def test_ladder_ranks_cheap_structure_preserving_steps_first():
    prior = qp.QualityPrior.from_rows([_row(is_pass=True, sharpe=2.0) for _ in range(4)], min_evidence=3)
    ladder = qp.ladder_prior(prior, motif_id="ratio", dataset="analyst4")
    assert [row["operation"] for row in ladder] != []
    assert {row["operation"] for row in ladder} == set(qp.MUTATION_LADDER)
    assert ladder[0]["rank"] <= ladder[-1]["rank"]
    assert ladder[0]["score"] >= ladder[-1]["score"]


def test_quality_prior_cli_reports_the_cells(db, capsys):
    _settle(db, "group_rank(ts_rank(close,60),subindustry)")
    assert qp.main(["--db", str(db.path), "--min-evidence", "1", "--level", "motif"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["prior"]["version"] == qp.QUALITY_PRIOR_VERSION
    assert report["cells"][0]["simulations"] == 1
    assert "enough_evidence" in report["cells"][0]
