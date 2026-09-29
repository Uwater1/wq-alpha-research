"""P4.4/P22.1 regression: a child budget is parent + growth, never a ceiling below the parent.

The gate-reaching alphas in the live ledger are 73-113 nodes against a 40-node fresh-candidate
ceiling. While the child budget was ``min(ceiling, parent + slack)``, the parent's *own* structure
was illegal, so every structural edit of it failed validation and the whole lineage machinery
(mutation, crossover, warm-start D2) silently degraded to fresh exploration on exactly the
parents worth exploiting. These tests pin the fix at the boundary that made it invisible.
"""
from __future__ import annotations

import pytest

import archive
import diversity
import expression_grammar as grammar
import generator
import research_db


def _parse(expression, catalog=None):
    metadata = diversity.catalog_metadata(catalog) if catalog is not None else diversity.load_field_metadata()
    return grammar.parse_expression(expression, metadata)


@pytest.fixture()
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


@pytest.fixture(scope="module")
def catalog():
    return generator.Catalog()


#: A composite in the shape and size class of the proven population: nested sums over several
#: datasets, ~70+ nodes, far above the fresh-candidate ceiling.
BIG = (
    "add(add(add(group_rank(ts_rank(ebit,126),industry),group_rank(ts_rank(sales,126),industry)),"
    "add(group_rank(ts_rank(close,126),industry),group_rank(ts_rank(volume,126),industry))),"
    "add(add(group_rank(ts_rank(assets,126),industry),group_rank(ts_rank(capex,126),industry)),"
    "add(group_rank(ts_rank(cashflow_op,126),industry),group_rank(ts_rank(debt,126),industry))))"
)


def _budget(metric, cap, slack):
    return generator._child_budget(metric, cap, slack)


def test_a_parent_smaller_than_the_ceiling_keeps_its_previous_budget():
    """The fix must not loosen anything the old rule could already handle."""
    for metric in (0, 4, 10, 16, 34):
        assert _budget(metric, 40, 6) == min(40, metric + 6)


def test_a_parent_at_or_above_the_ceiling_is_never_below_itself():
    for metric in (40, 73, 113):
        assert _budget(metric, 40, 6) == metric + 6


def test_the_growth_budget_always_admits_the_parent_structure(catalog):
    parent = _parse(BIG, catalog)
    limits = generator._mutation_limits(parent)
    assert grammar.node_count(parent) >= 40, "this fixture must be in the oversized class"
    grammar.validate_tree(parent, limits)  # must not raise: the parent is legal for its own edit


def test_a_structural_edit_of_an_oversized_parent_succeeds(db, catalog):
    outcome = db.queue_candidate(
        BIG, {"decay": 8, "truncation": 0.08}, signal_family="pv1", motif_id="ratio",
        generator_version="catalog-generator-v2", generation_mode="explore", campaign_id="ledger",
    )
    claimed = db.claim_simulation("t", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": 2.0, "fitness": 1.5, "turnover": 0.1},
        checks=[{"name": "IS", "result": "PASS"}], brain_alpha_id=f"A{outcome.candidate_id}",
    )
    parent = db.get_candidate(outcome.candidate_id)
    service = generator.CandidateGenerator(db, catalog, seed=5)
    for operation in ("normalization_change", "subtree_replace", "dataset_swap", "add_component"):
        child = service.structural_mutation(parent, operation=operation, campaign_id="edits")
        assert child is not None, operation
        assert child.parameters["operation"] == operation
        assert child.expression != BIG
        assert grammar.node_depth(_parse(child.expression, catalog)) >= \
            grammar.node_depth(_parse(BIG, catalog)) - 1


def test_crossover_of_two_oversized_parents_is_constructible(db, catalog):
    left = _parse(BIG, catalog)
    right = _parse(BIG.replace("ebit", "operating_income"), catalog)
    limits = generator._crossover_limits(left, right)
    assert limits.max_nodes >= grammar.node_count(left) + grammar.node_count(right)
    grammar.validate_tree(grammar.make_call("add", [left, right]), limits)


def test_the_lineage_pool_of_a_proven_composite_is_reachable(db, catalog):
    """End to end: a proven oversized composite is archived, then mutated into a child that is
    still inside the same depth class instead of falling back to exploration."""
    service = generator.CandidateGenerator(db, catalog, seed=6)
    outcome = db.queue_candidate(
        BIG, {"decay": 8, "truncation": 0.08}, signal_family="pv1", motif_id="ratio",
        generator_version="catalog-generator-v2", generation_mode="explore", campaign_id="ledger",
    )
    claimed = db.claim_simulation("t", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": 2.0, "fitness": 1.5, "turnover": 0.1},
        checks=[{"name": "IS", "result": "PASS"}], brain_alpha_id=f"A{outcome.candidate_id}",
    )
    archive.rebuild(db)
    plan = service.plan(campaign_id="lineage-big", budget=8, seed=6, mode="mutate")
    mutate_slots = [slot for slot in plan.slots if slot.generation_mode == "mutate"]
    assert mutate_slots
    assert {slot.parent_ids[0] for slot in mutate_slots if slot.parent_ids} == {outcome.candidate_id}
    materialized = [service.materialize(slot, campaign_id="lineage-big") for slot in mutate_slots]
    realized = [p for p in materialized if p is not None]
    assert realized
    assert all(p.generation_mode == "mutate" for p in realized), (
        "an oversized proven parent must be mutated, not silently replaced by exploration"
    )
