"""P21.3 regression: the recipe grid learns what the platform accepted.

The P18/P19 diagnosis found the sharpest single explanation of the V3 collapse: 120 of 139
gate-reaching seeds used truncation ``0.08``, a value V3's grid could not express at all
(``{0.05, 0.1, 0.15}``). These tests pin the two halves of the fix — an unrepresentable but
proven value becomes reachable, and evidence conditions sampling without ever closing the grid
(a regime change must stay detectable) or widening it outside the designed search space.
"""
from __future__ import annotations

import random

import pytest

import generation_policy as policy
import generator
import research_db
import seed_bank


@pytest.fixture()
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


@pytest.fixture(scope="module")
def catalog():
    return generator.Catalog()


def _settle(db, expression, *, settings=None, is_pass=True, motif_id="ratio",
            version="catalog-generator-v2"):
    outcome = db.queue_candidate(
        expression, settings or {"decay": 8, "truncation": 0.08},
        signal_family="pv1", generator_version=version, motif_id=motif_id,
        generation_mode="explore", campaign_id="ledger",
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
# The conditioned grid
# ---------------------------------------------------------------------------


def test_a_value_the_grid_cannot_express_becomes_reachable():
    values, weights = policy.recipe_value_weights((0.05, 0.1, 0.15), {"0.08": 120})
    assert 0.08 in values, "the proven value must be reachable, not rounded into the old grid"
    assert all(isinstance(value, float) for value in values)
    best = values[weights.index(max(weights))]
    assert best == 0.08
    assert weights[values.index(0.08)] > weights[values.index(0.1)]


def test_a_value_outside_the_designed_range_is_evidence_but_not_a_grid_member():
    """The ledger may extend a grid between its endpoints, never outside them."""
    values, _ = policy.recipe_value_weights((4, 6, 10, 20), {"8": 69, "0": 12, "2": 5})
    assert 8 in values
    assert 0 not in values and 2 not in values


def test_unobserved_values_keep_the_exploration_floor():
    floor = policy.RECIPE_EXPLORATION_FLOOR
    values, weights = policy.recipe_value_weights((0.05, 0.1, 0.15), {"0.1": 90})
    unobserved = sum(weight for value, weight in zip(values, weights) if value != 0.1)
    assert unobserved == pytest.approx(floor, abs=1e-6), (
        "a regime change must stay detectable: the floor is spent on what was never observed"
    )
    assert weights[values.index(0.1)] == pytest.approx(1.0 - floor, abs=1e-6)


def test_without_evidence_a_draw_is_bit_for_bit_the_old_one():
    """An unconditioned campaign's recipes must not move (P3.1)."""
    for seed in range(12):
        assert policy.recipe_choice(random.Random(seed), policy.TRUNCATIONS, None) == \
            random.Random(seed).choice(policy.TRUNCATIONS)
        assert policy.recipe_choice(random.Random(seed), policy.DECAYS, {}) == \
            random.Random(seed).choice(policy.DECAYS)


def test_conditioned_sampling_follows_the_proven_value():
    prior = {"truncation": {"0.08": 120, "0.05": 12}}
    drawn = [policy.sample_recipe(random.Random(index), "ratio", prior=prior).truncation
             for index in range(200)]
    assert drawn.count(0.08) > drawn.count(0.05) > 0
    assert len(set(drawn)) > 1, "the grid is conditioned, never pinned"


# ---------------------------------------------------------------------------
# From the ledger into the plan
# ---------------------------------------------------------------------------


def test_proven_recipe_counts_ignore_failures_and_respect_the_clock(db):
    _settle(db, "group_rank(ts_rank(ebit,126),industry)",
            settings={"decay": 8, "truncation": 0.08, "neutralization": "SUBINDUSTRY"})
    _settle(db, "rank(ts_delta(open,60))", is_pass=False,
            settings={"decay": 20, "truncation": 0.15})
    counts = seed_bank.proven_recipe_counts(db)
    assert counts["truncation"] == {"0.08": 1}, "a refused simulation is not proven evidence"
    assert counts["decay"] == {"8": 1}
    assert counts["neutralization"] == {"SUBINDUSTRY": 1}
    # A clock before the settlement sees nothing at all.
    assert seed_bank.proven_recipe_counts(db, as_of="2000-01-01T00:00:00") == {}


def test_the_plan_carries_the_recipe_prior_it_sampled_from(db, catalog):
    for index in range(3):
        _settle(db, f"group_rank(ts_rank(ebit,{120 + index}),industry)",
                settings={"decay": 8, "truncation": 0.08, "neutralization": "SUBINDUSTRY"})
    gen = generator.CandidateGenerator(db, catalog, seed=3)
    plan, proposals = gen.generate(campaign_id="recipe-learn", count=12, seed=3, strategy="mixed")
    assert plan.recipe_prior["truncation"]["0.08"] >= 1
    assert "truncation" in plan.as_dict()["recipe_prior_dimensions"]
    assert proposals, "learning the recipe grid must not shrink the campaign"
    assert any(float(p.settings.get("truncation") or 0.0) == pytest.approx(0.08)
               for p in proposals)


def test_planned_structure_hashes_still_describe_the_emitted_tree(db, catalog):
    """Planning and materialization must sample the *same* conditioned recipe (P4.2)."""
    for index in range(3):
        _settle(db, f"group_rank(ts_rank(ebit,{120 + index}),industry)",
                settings={"decay": 8, "truncation": 0.08, "neutralization": "SUBINDUSTRY"})
    _, proposals = generator.CandidateGenerator(db, catalog, seed=5).generate(
        campaign_id="recipe-hash", count=16, seed=5, strategy="mixed",
    )
    described = [p for p in proposals if p.parameters.get("planned_grammar_hash")]
    assert described
    assert all(p.parameters.get("planned_structure_matched") for p in described)


def test_an_unconditioned_arm_keeps_the_local_grid(db, catalog):
    _settle(db, "group_rank(ts_rank(ebit,126),industry)",
            settings={"decay": 8, "truncation": 0.08, "neutralization": "SUBINDUSTRY"})
    gen = generator.CandidateGenerator(db, catalog, seed=9)
    plan = gen.plan(campaign_id="recipe-control", budget=12, seed=9, mode="mixed",
                    recipe_prior={})
    assert plan.recipe_prior == {}
    assert all(float(slot.recipe_index) >= 0 for slot in plan.slots)
