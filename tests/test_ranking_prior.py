"""P24.1 regression: the scheduler follows measured cells, and only reorders.

The cheapest useful pre-simulation model is the ledger's own conditional outcome counts. It is
wired into ranking as a *bounded* minority share of ``expected_quality`` — enough to prefer a
cell where alphas actually pass over one where they never have, never enough to outvote the
structural and family terms, and never a rejection of anything.
"""
from __future__ import annotations

import pytest

import quality_prior
import ranking
import research_db


@pytest.fixture()
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _settle(db, expression, *, is_pass, settings=None, motif_id="ratio"):
    outcome = db.queue_candidate(
        expression, settings or {"decay": 8, "truncation": 0.08}, signal_family="pv1",
        motif_id=motif_id, generator_version="catalog-generator-v2", generation_mode="explore",
        campaign_id="ledger",
    )
    claimed = db.claim_simulation("t", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": 2.0 if is_pass else 0.1, "fitness": 1.5 if is_pass else 0.2,
                 "turnover": 0.1},
        checks=[{"name": "IS", "result": "PASS" if is_pass else "FAIL"}],
        brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return outcome.candidate_id


# ---------------------------------------------------------------------------
# The context the prior is asked about
# ---------------------------------------------------------------------------


def test_context_from_row_reads_the_source_and_the_reconstructed_recipe():
    row = {
        "normalized_expression": "group_rank(ts_rank(ebit,126),industry)",
        "settings_json": '{"decay": 8, "truncation": 0.08, "neutralization": "SUBINDUSTRY"}',
        "motif_id": "ratio",
    }
    context = quality_prior.context_from_row(row)
    assert context.dataset == "fundamental6"
    assert context.motif_id == "ratio"
    assert context.outer_operator == "group_rank"
    assert context.recipe_bucket.startswith("t0.08")
    assert context.role_signature


def test_context_from_row_tolerates_a_row_with_no_settings():
    context = quality_prior.context_from_row({"normalized_expression": "rank(close)"})
    assert context.dataset == "pv1"
    assert context.recipe_bucket == "unknown"
    assert context.motif_id == "none"


# ---------------------------------------------------------------------------
# Reordering
# ---------------------------------------------------------------------------


def test_a_proven_cell_outranks_an_unproven_one(db):
    for window in (120, 126, 130):
        _settle(db, f"group_rank(ts_rank(ebit,{window}),industry)", is_pass=True)
        _settle(db, f"group_rank(ts_rank(close,{window}),industry)", is_pass=False)
    context = ranking.build_context(db)
    assert context.conditional_prior is not None

    good = db.get_candidate(db.queue_candidate(
        "group_rank(ts_rank(assets,126),industry)", {"decay": 8, "truncation": 0.08},
        signal_family="pv1", motif_id="ratio",
    ).candidate_id)
    bad = db.get_candidate(db.queue_candidate(
        "group_rank(ts_rank(volume,126),industry)", {"decay": 8, "truncation": 0.08},
        signal_family="pv1", motif_id="ratio",
    ).candidate_id)

    good_score = ranking.score_candidate(good, context)
    bad_score = ranking.score_candidate(bad, context)
    assert good_score.expected_quality > bad_score.expected_quality
    assert good_score.reasons["conditional_level"] == "motif+dataset"
    assert bad_score.reasons["conditional_mean"] < good_score.reasons["conditional_mean"]


def test_the_prior_only_reorders_and_never_rejects(db):
    _settle(db, "group_rank(ts_rank(ebit,126),industry)", is_pass=False)
    context = ranking.build_context(db)
    for expression in ("rank(close)", "group_rank(ts_rank(close,126),subindustry)",
                       "0.5 * group_rank(ts_rank(ebit,126),industry) + 0.5 * rank(open)"):
        score = ranking.score_candidate({"id": 1, "normalized_expression": expression}, context)
        assert 0.0 <= score.expected_quality <= 1.0
        assert score.priority == score.priority  # never NaN


def test_without_cell_evidence_the_prior_changes_nothing(db):
    """A queue with nothing settled yet must rank exactly as it did without a prior."""
    db.queue_candidate("group_rank(ts_rank(ebit,126),industry)", {"decay": 8},
                       signal_family="pv1")
    db.queue_candidate("rank(ts_delta(open,60))", {"decay": 8}, signal_family="pv1")
    context = ranking.build_context(db)
    plain = ranking.RankingContext()
    for column in ("family_outcomes", "expression_attempts", "skeleton_counts",
                   "grammar_counts", "semantic_counts", "motif_counts", "family_queue_counts",
                   "family_active_counts", "field_counts", "archive_occupancy", "queued_total",
                   "total_candidates"):
        setattr(plain, column, getattr(context, column))
    rows = db.query("SELECT * FROM candidates WHERE status='QUEUED'")
    assert rows
    for row in rows:
        assert ranking.score_candidate(row, context).priority == pytest.approx(
            ranking.score_candidate(row, plain).priority, abs=1e-12
        )
        assert "conditional_level" not in ranking.score_candidate(row, context).reasons


def test_a_global_level_answer_is_not_used_to_rank(db):
    """`alphas pass 19% of the time overall` is not a reason to prefer one queued row."""
    prior = quality_prior.QualityPrior.from_rows([
        {"decision": "KEEP", "simulated": True, "is_pass": True} for _ in range(20)
    ] + [
        {"decision": "KEEP", "simulated": True, "is_pass": False} for _ in range(20)
    ])
    assert prior.global_evidence.simulations == 40
    context = ranking.RankingContext(conditional_prior=prior)
    score = ranking.score_candidate({"id": 1, "normalized_expression": "rank(close)"}, context)
    assert "conditional_level" not in score.reasons
