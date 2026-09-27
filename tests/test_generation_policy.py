"""Generation policy tests (Generator V3 P3/P4/P5): mode budgets, recipe independence and
the archive-informed campaign planner.
"""
from __future__ import annotations

import math
import random

import pytest

import archive
import generation_policy as policy
import generator
import research_db


@pytest.fixture()
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


@pytest.fixture(scope="module")
def catalog():
    return generator.Catalog()


def test_mode_allocation_is_exact_and_interleaved():
    for budget in (0, 1, 7, 40, 101):
        modes = policy.mode_allocation(budget, "mixed")
        assert len(modes) == budget
    modes = policy.mode_allocation(40, "mixed")
    assert modes.count("explore") == 16
    assert modes.count("exploit") == 10
    assert modes.count("mutate") == 10
    assert modes.count("crossover") == 4
    # Interleaved: the first four slots contain three distinct modes, not four explores.
    assert len(set(modes[:4])) >= 3


def test_single_strategy_repeats_a_single_mode():
    assert policy.mode_allocation(5, "explore") == ["explore"] * 5
    assert policy.resolve_strategy("coverage") == "explore"
    with pytest.raises(ValueError):
        policy.resolve_strategy("nope")


def test_recipe_seed_is_local_to_one_proposal():
    base = policy.recipe_seed("campaign", 7, ["close"], "change", 0, ())
    assert base == policy.recipe_seed("campaign", 7, ["close"], "change", 0, ())
    assert base != policy.recipe_seed("campaign", 7, ["close"], "change", 1, ())
    assert base != policy.recipe_seed("campaign", 7, ["open"], "change", 0, ())
    assert base != policy.recipe_seed("campaign", 8, ["close"], "change", 0, ())
    # Adding an unrelated proposal elsewhere changes none of these inputs, so the seed holds.


def test_recipes_are_deterministic_and_independent():
    first = policy.sample_recipe(random.Random(123), "change")
    second = policy.sample_recipe(random.Random(123), "change")
    assert first == second
    variants = {
        policy.sample_recipe(random.Random(policy.recipe_seed("c", 7, ["close"], "change", index, ())), "change")
        for index in range(8)
    }
    assert len(variants) >= 4  # independent dimensions, not one phase-locked recipe


def test_plan_budget_is_exact_and_caps_are_respected(db, catalog):
    plan = policy.plan_campaign(db, catalog, "plan-1", budget=30, seed=5, mode="mixed")
    assert plan.planned_budget == 30
    assert len(plan.slots) == 30
    assert sum(int(row["budget"]) for row in plan.family_allocation) == 30
    assert max(float(row["share"]) for row in plan.family_allocation) <= policy.DEFAULT_MAX_FAMILY_SHARE + 1e-9


def test_plan_is_deterministic_for_a_fixed_snapshot(db, catalog):
    first = policy.plan_campaign(db, catalog, "plan-determinism", budget=20, seed=3, mode="mixed")
    second = policy.plan_campaign(db, catalog, "plan-determinism", budget=20, seed=3, mode="mixed")
    assert [slot.as_dict() for slot in first.slots] == [slot.as_dict() for slot in second.slots]
    assert first.as_dict()["family_allocation"] == second.as_dict()["family_allocation"]


def test_plan_reserves_exploration_for_untested_families(db, catalog):
    plan = policy.plan_campaign(db, catalog, "plan-explore", budget=80, seed=9, mode="mixed")
    exploration = [row for row in plan.family_allocation if int(row.get("exploration") or 0) == 1]
    assert exploration  # an untouched database must reserve exploration slots
    assert all(int(row["budget"]) >= 1 for row in exploration)


def test_plan_records_mode_family_motif_and_recipe():
    from collections import Counter

    import expression_grammar as grammar

    plan = policy.plan_campaign(None, generator.Catalog(), "plan-nowrite", budget=12, seed=1, mode="explore")
    assert plan.planned_budget == 12
    assert {slot.generation_mode for slot in plan.slots} == {"explore"}
    for slot in plan.slots:
        assert slot.family
        assert slot.motif_id in grammar.MOTIF_BY_ID
        assert slot.reason
        assert slot.recipe_index >= 0
    assert set(plan.distribution("generation_mode")) == {"explore"}
    assert Counter(plan.distribution("family")).total() == 12


def test_archive_parents_influence_crossover_planning(db, catalog):
    def settle(expression, family, sharpe):
        outcome = db.queue_candidate(expression, {"decay": 6}, signal_family=family)
        claimed = db.claim_simulation("plan", candidate_id=outcome.candidate_id)
        db.record_simulation_result(
            candidate_id=claimed["id"], status="DONE",
            metrics={"sharpe": sharpe, "fitness": 1.0, "turnover": 0.1},
            checks=[{"name": "IS", "result": "PASS"}], brain_alpha_id=f"A{outcome.candidate_id}",
        )

    settle("group_rank(ts_rank(close,60),subindustry)", "pv1", 1.6)
    settle("group_rank(ts_rank(free_cash_flow_reported_value,60),industry)", "fundamental6", 1.4)
    archive.rebuild(db)

    plan = policy.plan_campaign(db, catalog, "plan-archive", budget=40, seed=4, mode="crossover")
    assert any(slot.parent_ids for slot in plan.slots)
    assert any(len(slot.parent_ids) >= 2 for slot in plan.slots)


# ---------------------------------------------------------------------------
# Bounded adaptive motif allocation (P9)
# ---------------------------------------------------------------------------


def test_motif_allocation_is_exact_bounded_and_reserves_exploration():
    motifs = [f"motif_{index}" for index in range(6)]
    allocation = policy.allocate_motifs(motifs, 60, {}, seed=4)
    assert sum(allocation.values()) == 60
    assert set(allocation) == set(motifs)
    # Every motif is untested, so the exploration floor spreads the budget across all of them.
    assert all(allocation[motif] > 0 for motif in motifs)
    assert max(allocation.values()) <= int(math.ceil(60 * policy.DEFAULT_MOTIF_MAX_SHARE)) + 1


def test_motif_allocation_rewards_evidence_without_monopoly():
    motifs = [f"motif_{index}" for index in range(4)]
    stats = {motifs[0]: (50, 30), motifs[1]: (50, 2), motifs[2]: (0, 0), motifs[3]: (0, 0)}
    allocation = policy.allocate_motifs(motifs, 40, stats, seed=7, max_share=0.4)
    assert sum(allocation.values()) == 40
    assert max(allocation.values()) <= int(math.ceil(40 * 0.4)) + 1  # no motif monopolizes
    assert allocation[motifs[2]] > 0 and allocation[motifs[3]] > 0  # untested still explored
    assert allocation[motifs[0]] >= allocation[motifs[1]]  # proven motif earns more


def test_plan_records_the_bounded_motif_allocation(db, catalog):
    plan = policy.plan_campaign(db, catalog, "plan-motifs", budget=40, seed=3, mode="mixed")
    assert sum(plan.motif_allocation.values()) == 40
    assert max(plan.motif_allocation.values()) <= int(math.ceil(40 * policy.DEFAULT_MOTIF_MAX_SHARE)) + 1
    # The planner consumes the allocation: exploit/mutate/crossover motifs come from the plan.
    assert plan.distribution("motif_id")
