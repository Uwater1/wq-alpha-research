"""P19 regression: the seed bank must not see the future, and the ladder must be decidable.

Two properties matter more than any single number here. A seed is only a seed if it had
*already* passed before the campaign clock — otherwise warm starting reads the answer. And a
band is decided on independent structural facts, so D2 ("one structural edit") cannot silently
mean "somewhere under a similarity threshold".
"""
from __future__ import annotations

import json

import pytest

import expression_grammar as grammar
import research_db
import seed_bank


@pytest.fixture()
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _settle(db, expression, *, settings=None, sharpe=2.0, fitness=1.5, turnover=0.1,
            is_pass=True, parent_ids=(), motif_id="ratio", version="catalog-generator-v2",
            stage="", checks=None):
    """Simulate one candidate; the platform's own check decides the gate.

    Metrics and the gate are separate knobs on purpose: a candidate can have an absurd Sharpe
    and still be *refused* by BRAIN, and that candidate must never become a seed.
    """
    outcome = db.queue_candidate(
        expression, settings or {"decay": 6}, signal_family="pv1", generator_version=version,
        motif_id=motif_id, parent_ids=list(parent_ids), generation_mode="explore",
    )
    claimed = db.claim_simulation("t", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": sharpe, "fitness": fitness, "turnover": turnover},
        checks=checks or [{"name": "IS", "result": "PASS" if is_pass else "FAIL"}],
        brain_alpha_id=f"A{outcome.candidate_id}",
    )
    if stage:
        # A further stage (correlation reached) is set explicitly and legally.
        db.set_status(outcome.candidate_id, stage)
    return outcome.candidate_id


# ---------------------------------------------------------------------------
# Point-in-time seed bank
# ---------------------------------------------------------------------------


def test_a_candidate_that_passed_later_is_not_a_seed_earlier(db):
    candidate_id = _settle(db, "group_rank(ts_rank(close,60),subindustry)")
    row = db.query("SELECT completed_at FROM simulations WHERE candidate_id=?", (candidate_id,))[0]
    settled = str(row["completed_at"])

    # Before it settled there was nothing to learn from; after it settled it is a seed.
    assert seed_bank.build_seed_bank(db, as_of="2000-01-01T00:00:00") == []
    seeds = seed_bank.build_seed_bank(db, as_of=settled)
    assert [seed.candidate_id for seed in seeds] == [candidate_id]
    # Clearing the IS gate joins the submission queue, so the stage is a gate-reaching one.
    assert seeds[0].stage in seed_bank.PASS_STAGES
    assert seeds[0].settled_at <= settled


def test_seed_bank_tags_the_proven_region_not_just_the_status(db):
    _settle(db, "group_rank(ts_rank(close,60),subindustry)", sharpe=1.9, motif_id="ranked_level")
    _settle(db, "rank(open)", is_pass=False)
    seeds = seed_bank.build_seed_bank(db)
    assert len(seeds) == 1
    seed = seeds[0]
    assert seed.motif_id == "ranked_level"
    assert seed.outer_operator == "group_rank"
    assert seed.fields == ("close",)
    assert seed.grammar_skeleton_hash and seed.semantic_skeleton_hash
    assert seed.generator_version == "catalog-generator-v2"
    # The public view is safe to print; the private expression stays on the record itself.
    public = seed.public()
    assert "expression" not in public and "settings" not in public
    assert "close" in public["fields"]


def test_a_refused_candidate_is_never_a_seed_however_good_its_metrics_look(db):
    passed = _settle(db, "rank(close)")
    # An enormous Sharpe does not make a region proven: BRAIN refused the request.
    refused = _settle(db, "rank(open)", sharpe=99.0, is_pass=False)
    assert db.get_candidate(refused)["status"] == "REJECTED"
    assert {seed.candidate_id for seed in seed_bank.build_seed_bank(db)} == {passed}
    # The stage filter is a real bar, not a relabel: nothing has reached ACTIVE here.
    assert seed_bank.build_seed_bank(db, stages=("ACTIVE",)) == []
    assert {seed.candidate_id for seed in seed_bank.build_seed_bank(db, stages=seed_bank.CORR_STAGES)} == {passed}


def test_scope_filter_keeps_only_the_campaign_scope(db):
    inside = _settle(db, "rank(close)", settings={"decay": 6, "region": "USA", "delay": 1})
    _settle(db, "rank(open)", settings={"decay": 6, "region": "CHN", "delay": 1})
    kept = seed_bank.build_seed_bank(db, scope={"region": "USA", "universe": "TOP3000", "delay": 1})
    assert [seed.candidate_id for seed in kept] == [inside]


def test_seed_bank_orders_by_point_in_time_quality(db):
    weak = _settle(db, "rank(close)", sharpe=1.3)
    strong = _settle(db, "rank(open)", sharpe=2.4)
    assert [seed.candidate_id for seed in seed_bank.build_seed_bank(db)] == [strong, weak]


# ---------------------------------------------------------------------------
# Distance ladder
# ---------------------------------------------------------------------------


def _seed(db, expression, settings=None):
    _settle(db, expression, settings=settings)
    return seed_bank.build_seed_bank(db)[0]


def test_ladder_rungs_are_decided_by_structure(db):
    seed = _seed(db, "group_rank(ts_rank(close,60),subindustry)", {"decay": 6})

    exact = {"normalized_expression": "group_rank(ts_rank(close,60),subindustry)", "settings_json": json.dumps({"decay": 6})}
    assert seed_bank.distance_band(seed, exact) == "D0"

    # Same topology and sources, only the window/decay moved: parameter-only.
    parameter_only = {
        "normalized_expression": "group_rank(ts_rank(close,126),subindustry)",
        "settings_json": json.dumps({"decay": 20}),
    }
    assert seed_bank.distance_band(seed, parameter_only) == "D1"

    # Same sources, one structural edit on the operator tree.
    one_edit = {"normalized_expression": "rank(ts_rank(close,60))", "settings_json": json.dumps({"decay": 6})}
    assert grammar.operator_edit_distance(
        seed.expression, one_edit["normalized_expression"],
    ) <= seed_bank.D2_MAX_OPERATOR_EDITS
    assert seed_bank.distance_band(seed, one_edit) == "D2"

    # Same topology, a different source: a transfer.
    transfer = {
        "normalized_expression": "group_rank(ts_rank(open,60),subindustry)",
        "settings_json": json.dumps({"decay": 6}),
    }
    assert seed_bank.distance_band(seed, transfer) == "D3"

    # Different topology and different sources: a new hypothesis.
    unrelated = {"normalized_expression": "winsorize(zscore(ts_delta(assets,126)),std=4)",
                 "settings_json": json.dumps({"decay": 6})}
    assert seed_bank.distance_band(seed, unrelated) == "D4"


def test_ladder_measures_pass_rate_from_proven_parents_only(db):
    parent = _settle(db, "group_rank(ts_rank(close,60),subindustry)", sharpe=1.9)
    _settle(db, "group_rank(ts_rank(close,126),subindustry)", is_pass=False, parent_ids=(parent,))
    _settle(db, "rank(ts_rank(close,60))", is_pass=False, parent_ids=(parent,))
    _settle(db, "rank(assets)", is_pass=False)  # no proven parent

    ladder = seed_bank.distance_outcomes(db, min_sample=1)
    by_band = {cell["band"]: cell for cell in ladder["bands"]}
    assert ladder["seeds"] == 1  # only the parent is proven; the children are not
    assert by_band["D1"]["simulations"] == 1 and by_band["D1"]["is_pass"] == 0
    assert by_band["D2"]["simulations"] == 1 and by_band["D2"]["is_pass_rate"] == 0.0
    assert by_band["D4"]["simulations"] == 0
    assert set(by_band) == set(seed_bank.BANDS)
    assert by_band["D1"]["description"]
    assert ladder["children_without_a_seed_parent"] >= 1


def test_ladder_can_be_restricted_to_one_generator_under_test(db):
    """Mixing every campaign that ever mutated a proven alpha flatters the ladder."""
    parent = _settle(db, "group_rank(ts_rank(close,60),subindustry)", sharpe=1.9)
    _settle(db, "group_rank(ts_rank(close,126),subindustry)", is_pass=False, parent_ids=(parent,),
            version="catalog-generator-v3")
    _settle(db, "group_rank(ts_rank(open,60),subindustry)", parent_ids=(parent,),
            version="agent-hypothesis-v8")
    mixed = seed_bank.distance_outcomes(db, min_sample=1)
    restricted = seed_bank.distance_outcomes(db, min_sample=1, child_versions=["catalog-generator-v3"])
    by_band = {cell["band"]: cell for cell in mixed["bands"]}
    assert by_band["D1"]["simulations"] == 1  # only the V3 child is parameter-only
    assert by_band["D3"]["simulations"] == 1  # the agent child transferred the shape
    assert {cell["band"]: cell["simulations"] for cell in restricted["bands"]}["D3"] == 0
    assert restricted["child_versions"] == ["catalog-generator-v3"]


def test_ladder_never_ranks_a_rung_below_the_minimum_sample(db):
    parent = _settle(db, "group_rank(ts_rank(close,60),subindustry)", sharpe=1.9)
    _settle(db, "group_rank(ts_rank(close,126),subindustry)", is_pass=False, parent_ids=(parent,))
    ladder = seed_bank.distance_outcomes(db, min_sample=5)
    by_band = {cell["band"]: cell for cell in ladder["bands"]}
    assert by_band["D1"]["simulations"] == 1 and by_band["D1"]["low_confidence"] is True


# ---------------------------------------------------------------------------
# Recipe prior, perturbation and source transfer
# ---------------------------------------------------------------------------


def test_recipe_prior_reports_what_the_platform_accepted(db):
    """Legacy seeds have no recipe record; their settings and window are the recipe."""
    _settle(db, "ts_rank(close,126)", settings={"decay": 4, "truncation": 0.08, "neutralization": "SUBINDUSTRY"})
    _settle(db, "ts_rank(open,126)", settings={"decay": 4, "truncation": 0.08, "neutralization": "SUBINDUSTRY"})
    _settle(db, "ts_rank(high,20)", settings={"decay": 10, "truncation": 0.1, "neutralization": "SECTOR"})
    prior = seed_bank.proven_recipe_prior(seed_bank.build_seed_bank(db))
    assert prior["truncation"]["0.08"] == 2
    assert prior["neutralization"]["SUBINDUSTRY"] == 2
    assert prior["lookback"]["126"] == 2


def test_recipe_perturbation_moves_exactly_one_dimension():
    import random

    recipe = {"lookback": 126, "decay": 6, "truncation": 0.08, "neutralization": "SUBINDUSTRY"}
    for seed in range(12):
        updated = seed_bank.perturb_recipe(recipe, random.Random(seed))
        changed = [key for key in recipe if updated.get(key) != recipe.get(key)]
        assert len(changed) <= 1, f"seed {seed} moved {changed}"
        assert set(updated) >= set(recipe)


def test_source_substitution_prefers_a_different_dataset_with_the_same_type_and_role():
    import random

    import generator

    catalog = generator.Catalog()
    substitution = seed_bank.substitute_field(["close"], catalog, random.Random(0))
    assert substitution is not None and substitution != "close"
    source = catalog.get("close")
    replacement = catalog.get(substitution)
    assert replacement is not None
    assert replacement.field_type == source.field_type
    assert replacement.dataset != source.dataset


def test_source_substitution_returns_nothing_when_nothing_is_compatible():
    import random

    class _Empty:
        fields = ()
        scope = {}

    assert seed_bank.substitute_field(["close"], _Empty(), random.Random(0)) is None
    assert seed_bank.substitute_field([], _Empty(), random.Random(0)) is None


def test_bank_summary_is_sanitized_and_counts_the_proven_regions(db):
    _settle(db, "group_rank(ts_rank(close,60),subindustry)", motif_id="ranked_level",
            settings={"decay": 4, "truncation": 0.08})
    summary = seed_bank.bank_summary(seed_bank.build_seed_bank(db))
    assert summary["seeds"] == 1
    assert summary["by_motif"] == {"ranked_level": 1}
    assert summary["by_outer_operator"] == {"group_rank": 1}
    assert summary["recipe_prior"]
    text = json.dumps(summary, sort_keys=True)
    assert "group_rank(ts_rank(close,60),subindustry)" not in text


def test_seed_bank_cli_reports_the_bank_and_the_ladder(db, capsys):
    _settle(db, "rank(close)")
    assert seed_bank.main(["--db", str(db.path), "--ladder", "--min-sample", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["bank"]["seeds"] == 1
    assert {cell["band"] for cell in report["distance_ladder"]["bands"]} == set(seed_bank.BANDS)
