"""Generator V3 integration tests: planning, materialization, provenance, archive integration,
and the V2 regressions that must keep passing.
"""
from __future__ import annotations

import json

import pytest

import archive
import canonical
import diversity
import expression_grammar as grammar
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


def _settle(db, expression, family, sharpe=1.5):
    outcome = db.queue_candidate(expression, {"decay": 6}, signal_family=family)
    claimed = db.claim_simulation("v3", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": sharpe, "fitness": 1.1, "turnover": 0.09},
        checks=[{"name": "IS", "result": "PASS"}], brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return outcome.candidate_id


# ---------------------------------------------------------------------------
# V2 regression: the default generator is unchanged
# ---------------------------------------------------------------------------


def test_v2_proposals_are_unchanged_and_still_reproducible(db):
    first = generator.CandidateGenerator(db, seed=7)
    proposals_a = first.proposals(count=5, family="all")
    proposals_b = generator.CandidateGenerator(db, seed=7).proposals(count=5, family="all")
    assert proposals_a == proposals_b
    assert all(proposal.generation_mode == "" for proposal in proposals_a)
    assert all(proposal.family == proposal.parameters["dataset"] for proposal in proposals_a)

    outcomes = first.queue("v2-campaign", proposals_a)
    row = db.get_candidate(outcomes[0]["candidate_id"])
    assert row["generator_version"] == generator.LEGACY_GENERATOR_VERSION == "catalog-generator-v2"


def test_v2_mutation_child_family_is_derived_from_the_child(db):
    """A cross-dataset repair must not keep the parent's stale family label."""
    parent_id = db.queue_candidate("rank(close)", {"decay": 6}, signal_family="pv1").candidate_id
    parent = db.get_candidate(parent_id)
    proposals = generator.CandidateGenerator(db, seed=2).mutate(parent, count=1)
    assert proposals
    child = proposals[0]
    profile = diversity.derive_source_profile(child.expression, catalog=generator.Catalog())
    assert child.family != "pv1" or "pv1" in profile["datasets"]


# ---------------------------------------------------------------------------
# V3 planning + materialization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy, count", [("mixed", 25), ("explore", 12), ("exploit", 8)])
def test_materialized_budget_matches_the_plan(db, strategy, count):
    plan, proposals = generator.CandidateGenerator(db, seed=7).generate(
        campaign_id=f"v3-{strategy}", count=count, seed=7, strategy=strategy,
    )
    assert plan.planned_budget == count
    assert len(proposals) == count
    allowed = {"explore", "exploit", "mutate", "crossover"}
    assert {proposal.generation_mode for proposal in proposals} <= (
        allowed if strategy == "mixed" else {policy.resolve_strategy(strategy)}
    )


def test_cold_archive_substitutes_lineage_modes_instead_of_faking_them(db):
    """An empty archive cannot mutate or cross anything: the slot must say so, not pretend."""
    plan, proposals = generator.CandidateGenerator(db, seed=5).generate(
        campaign_id="v3-cold", count=30, seed=5, strategy="mixed",
    )
    assert len(proposals) == plan.planned_budget == 30
    assert {proposal.generation_mode for proposal in proposals} <= {"explore", "exploit"}
    assert all(not proposal.parent_ids for proposal in proposals)
    substituted = [slot for slot in plan.slots if "planned crossover" in slot.reason or "planned mutate" in slot.reason]
    assert substituted, "the substituted slots must explain what they replaced"
    modes = {proposal.generation_mode for proposal in proposals}
    assert {"crossover", "mutate"}.isdisjoint(modes)


def test_every_proposal_has_complete_provenance(db):
    _, proposals = generator.CandidateGenerator(db, seed=1).generate(
        campaign_id="v3-provenance", count=20, seed=1, strategy="mixed",
    )
    for proposal in proposals:
        assert proposal.motif_id
        assert proposal.generation_mode
        assert proposal.strategy
        assert proposal.recipe and set(proposal.recipe) >= {
            "lookback", "smoothing_window", "decay", "neutralization", "group_level",
            "truncation", "normalization", "winsorization", "rank_or_zscore", "sign",
        }
        assert proposal.source_profile["field_ids"]
        assert proposal.grammar_skeleton_hash and proposal.semantic_skeleton_hash


def test_same_snapshot_and_seed_reproduce_identical_proposals(db):
    first = generator.CandidateGenerator(db, seed=5).generate(
        campaign_id="v3-repro", count=15, seed=5, strategy="mixed")[1]
    second = generator.CandidateGenerator(db, seed=5).generate(
        campaign_id="v3-repro", count=15, seed=5, strategy="mixed")[1]
    assert first == second


def test_one_field_generates_multiple_valid_recipes(db):
    _, proposals = generator.CandidateGenerator(db, seed=3).generate(
        campaign_id="v3-recipes", count=24, seed=3, strategy="explore",
    )
    by_field: dict[str, set[tuple]] = {}
    for proposal in proposals:
        for field in proposal.source_profile["field_ids"]:
            key = tuple(sorted(proposal.recipe.items()))
            by_field.setdefault(field, set()).add(key)
    assert any(len(variants) > 1 for variants in by_field.values())


def test_multi_field_motifs_are_generated_before_any_repair(db, catalog):
    _, proposals = generator.CandidateGenerator(db, seed=4).generate(
        campaign_id="v3-multifield", count=60, seed=4, strategy="mixed",
    )
    two_source = [proposal for proposal in proposals if len(proposal.source_profile["field_ids"]) >= 2]
    assert two_source, "multi-field motifs must exist in base generation"
    assert all(
        len(grammar.motif_by_id(proposal.motif_id).input_roles) == 2
        for proposal in two_source
        if proposal.motif_id in grammar.MOTIF_BY_ID
    )
    # Cross-dataset composites specifically must span two datasets.
    assert all(
        len(proposal.source_profile["datasets"]) >= 2
        for proposal in two_source if proposal.motif_id == "cross_dataset_composite"
    )


def test_forced_motif_materializes_every_slot(db):
    plan, proposals = generator.CandidateGenerator(db, seed=6).generate(
        campaign_id="v3-motif", count=10, seed=6, strategy="mixed", motif="normalized_difference",
    )
    assert len(proposals) == plan.planned_budget == 10
    assert all(proposal.motif_id == "normalized_difference" for proposal in proposals)
    for proposal in proposals:
        assert grammar.check_complexity(grammar.parse_expression(proposal.expression), generator.V3_LIMITS) == []


def test_queue_records_v3_provenance_and_child_family(db):
    _, proposals = generator.CandidateGenerator(db, seed=8).generate(
        campaign_id="v3-queue", count=12, seed=8, strategy="mixed",
    )
    outcomes = generator.CandidateGenerator(db, seed=8).queue("v3-queue", proposals)
    assert all(outcome["action"] == "queued" for outcome in outcomes)
    row = db.get_candidate(outcomes[0]["candidate_id"])
    assert row["generator_version"] == generator.GENERATOR_VERSION_V3 == generator.GENERATOR_VERSION
    parameters = json.loads(row["mutation_parameters_json"])
    assert parameters["motif_id"]
    assert parameters["generation_mode"]
    assert parameters["recipe"]["lookback"]
    # Provenance lives on the permanent trial ledger, not on the candidate row.
    trial = db.trials("v3-queue")[-1]
    provenance = json.loads(trial["provenance_json"])
    assert provenance["generation_mode"]
    assert provenance["policy_version"] == policy.GENERATION_POLICY_VERSION
    assert provenance["source_profile"]["field_ids"]


def test_archive_parent_selection_affects_generation(db, catalog):
    _settle(db, "group_rank(ts_rank(close,60),subindustry)", "pv1", 1.7)
    _settle(db, "group_rank(ts_rank(free_cash_flow_reported_value,60),industry)", "fundamental6", 1.5)
    _settle(db, "winsorize(zscore(ts_delta(assets,126)),std=4)", "fundamental2", 1.3)
    archive.rebuild(db)

    _, proposals = generator.CandidateGenerator(db, seed=2).generate(
        campaign_id="v3-archive", count=30, seed=2, strategy="mixed",
    )
    mutated = [proposal for proposal in proposals if proposal.generation_mode == "mutate"]
    crossed = [proposal for proposal in proposals if proposal.generation_mode == "crossover"]
    assert any(proposal.parent_ids for proposal in mutated)
    assert any(len(proposal.parent_ids) == 2 for proposal in crossed)

    outcomes = generator.CandidateGenerator(db, seed=2).queue("v3-archive", proposals)
    child = next(
        db.get_candidate(outcome["candidate_id"])
        for proposal, outcome in zip(proposals, outcomes)
        if proposal.generation_mode == "crossover" and len(proposal.parent_ids) == 2
        and proposal.mutation_type == "crossover" and outcome["action"] == "queued"
    )
    assert len(json.loads(child["parent_ids_json"])) == 2
    assert child["mutation_type"] == "crossover"


def test_dry_plan_distribution_covers_all_dimensions(db):
    # A warm archive is what makes mutate/crossover real; only then may the plan claim them.
    _settle(db, "group_rank(ts_rank(close,60),subindustry)", "pv1", 1.7)
    _settle(db, "group_rank(ts_rank(free_cash_flow_reported_value,60),industry)", "fundamental6", 1.5)
    _settle(db, "winsorize(zscore(ts_delta(assets,126)),std=4)", "fundamental2", 1.3)
    archive.rebuild(db)

    plan, proposals = generator.CandidateGenerator(db, seed=9).generate(
        campaign_id="v3-dry", count=40, seed=9, strategy="mixed",
    )
    distribution = generator._v3_distribution(plan, proposals)
    assert distribution["planned_budget"] == 40
    assert distribution["materialized"] == 40
    assert set(distribution["generation_mode"]) == {"explore", "exploit", "mutate", "crossover"}
    assert sum(distribution["motif"].values()) == 40
    assert distribution["grammar_skeleton"] and distribution["semantic_skeleton"]
    # Semantic diversity must be at least as large as grammar diversity is not guaranteed,
    # but both must be measurably finer than "one hash".
    assert len(distribution["grammar_skeleton"]) >= 5
    assert len(plan.distribution("dataset")) >= 1


def test_canonical_keys_still_deduplicate_v3_proposals(db):
    plan, proposals = generator.CandidateGenerator(db, seed=11).generate(
        campaign_id="v3-dedup", count=10, seed=11, strategy="explore",
    )
    keys = {canonical.canonical_key(proposal.expression, proposal.settings) for proposal in proposals}
    assert len(keys) == len(proposals)  # explore mode should not emit exact duplicates
    outcomes = generator.CandidateGenerator(db, seed=11).queue("v3-dedup", proposals)
    trial_ids = {outcome["candidate_id"] for outcome in outcomes}
    assert len(trial_ids) == len(proposals)


# ---------------------------------------------------------------------------
# P6/P7/P10: crossover distance, novelty skip, persisted provenance
# ---------------------------------------------------------------------------


def test_near_identical_parents_are_not_chosen_for_crossover():
    rows = [
        {"elite_candidate_id": 1, "cell_key": "a", "signal_family": "alpha",
         "normalized_expression": "group_rank(ts_rank(close,60),subindustry)"},
        {"elite_candidate_id": 2, "cell_key": "b", "signal_family": "beta",
         "normalized_expression": "group_rank(ts_rank(open,60),subindustry)"},  # same topology
        {"elite_candidate_id": 3, "cell_key": "c", "signal_family": "gamma",
         "normalized_expression": "rank(ts_delta(assets,126))"},
    ]
    import random as _random

    for seed in range(6):
        pair = policy._pick_crossover_pair(rows, {}, _random.Random(seed))
        assert len(pair) == 2
        assert pair != (1, 2), "near-identical parents must not be paired while a distant one exists"

    # With only near-identical parents available, a pair is still returned so a planned
    # crossover slot is not silently dropped.
    fallback = policy._pick_crossover_pair(rows[:2], {}, _random.Random(0))
    assert len(fallback) == 2


def test_novelty_screen_skips_duplicates_and_records_the_decision(db):
    generator_service = generator.CandidateGenerator(db, seed=13)
    _, first = generator_service.generate(campaign_id="v3-novel", count=12, seed=13, strategy="explore")
    first_outcomes = generator_service.queue("v3-novel", first)
    assert all(outcome["action"] == "queued" for outcome in first_outcomes)

    # The first batch was novel against an empty ledger. Re-screening those exact proposals now
    # that the ledger contains them must refuse every one of them.
    assert all(proposal.novelty_decision in {diversity.KEEP, diversity.DOWNWEIGHT} for proposal in first)
    rescreened = generator.CandidateGenerator(db, seed=13).screen_proposals(
        first, campaign_id="v3-novel", seed=13,
    )
    assert all(proposal.novelty_decision == diversity.SKIP_REDUNDANT for proposal in rescreened)

    before = db.counts("simulations")
    outcomes = generator.CandidateGenerator(db, seed=13).queue("v3-novel", rescreened)
    assert all(outcome["action"] == "skipped_redundant" for outcome in outcomes)
    assert db.counts("simulations") == before  # a skip spends no capacity

    trials = [row for row in db.trials("v3-novel") if row["decision"] == diversity.SKIP_REDUNDANT]
    assert trials
    assert all(row["skip_reason"] for row in trials)

    # A later round of the same campaign is planned against the updated ledger, so it still
    # produces fresh structure rather than a re-run of round one.
    _, second = generator.CandidateGenerator(db, seed=13).generate(
        campaign_id="v3-novel", count=6, seed=13, strategy="explore",
    )
    assert any(proposal.novelty_decision != diversity.SKIP_REDUNDANT for proposal in second)


def test_v3_structure_columns_are_queryable(db):
    _, proposals = generator.CandidateGenerator(db, seed=21).generate(
        campaign_id="v3-columns", count=8, seed=21, strategy="mixed",
    )
    outcomes = generator.CandidateGenerator(db, seed=21).queue("v3-columns", proposals)
    row = db.get_candidate(outcomes[0]["candidate_id"])
    for column in ("generator_strategy", "generation_mode", "motif_id", "grammar_skeleton_hash",
                   "semantic_skeleton_hash", "source_profile_json"):
        assert row[column], column
    assert row["recipe_index"] is not None
    trial = db.trials("v3-columns")[-1]
    assert trial["grammar_skeleton_hash"] and trial["motif_id"]
    assert json.loads(trial["source_profile_json"])["field_ids"]


def test_generation_stats_refresh_aggregates_the_ledger(db):
    _, proposals = generator.CandidateGenerator(db, seed=17).generate(
        campaign_id="v3-stats", count=10, seed=17, strategy="mixed",
    )
    outcomes = generator.CandidateGenerator(db, seed=17).queue("v3-stats", proposals)
    settled = db.claim_simulation("stats", candidate_id=outcomes[0]["candidate_id"])
    db.record_simulation_result(
        candidate_id=settled["id"], status="DONE",
        metrics={"sharpe": 1.5, "fitness": 1.2, "turnover": 0.08},
        checks=[{"name": "IS", "result": "PASS"}], brain_alpha_id="A-stats",
    )
    report = db.refresh_generation_stats(generator_version=generator.GENERATOR_VERSION_V3)
    assert report["attempts"] >= 10
    stats = db.generation_stats(generator_version=generator.GENERATOR_VERSION_V3)
    assert stats and all(row["generator_version"] == generator.GENERATOR_VERSION_V3 for row in stats)
    outcomes_by_motif = db.motif_outcomes(generator_version=generator.GENERATOR_VERSION_V3)
    assert sum(passes for _attempts, passes in outcomes_by_motif.values()) >= 1
