"""Generator V3 integration tests: planning, materialization, provenance, archive integration,
and the V2 regressions that must keep passing.
"""
from __future__ import annotations

import json
from dataclasses import replace

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


def test_rebuild_drops_stale_niche_version_cells(db):
    """P5/P15: an old-version archive cell must not survive a rebuild into occupancy/parents."""
    _settle(db, "rank(close)", "pv1", 1.2)
    archive.rebuild(db)
    # Simulate a long-lived DB carrying a cell written under an older niche definition.
    db.query(
        "INSERT INTO archive_cells(cell_key, dimensions_json, elite_candidate_id, elite_score, member_count, updated_at)"
        " VALUES(?,?,?,?,?,?)",
        ("stale-cell", json.dumps({"niche_version": "archive-niche-v3",
                                   "grammar_skeleton_hash": "STALE"}), None, 99.0, 1, "2020-01-01T00:00:00"),
    )
    assert db.query("SELECT COUNT(*) AS n FROM archive_cells")[0]["n"] == 2

    report = archive.rebuild(db)
    assert report["cells"] == 1
    assert {row["cell_key"] for row in db.query("SELECT cell_key FROM archive_cells")} != {"stale-cell"}
    assert "STALE" not in archive.structure_occupancy(db)
    assert archive.parents(db, count=5, seed=0)


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


def test_newly_settled_candidate_reaches_the_next_plan_without_manual_rebuild(db):
    """P5/P15: the planner refreshes derived archive state before reading it."""
    cold = generator.CandidateGenerator(db, seed=4).plan(
        campaign_id="v3-lifecycle", budget=12, seed=4, mode="mutate",
    )
    assert all(slot.generation_mode == "explore" for slot in cold.slots)  # nothing to mutate

    _settle(db, "group_rank(ts_rank(close,60),subindustry)", "pv1", 1.7)
    # No explicit archive.rebuild() call: the next plan must pick the elite up on its own.
    warm = generator.CandidateGenerator(db, seed=4).plan(
        campaign_id="v3-lifecycle", budget=12, seed=4, mode="mutate",
    )
    mutated = [slot for slot in warm.slots if slot.generation_mode == "mutate"]
    assert mutated and all(slot.parent_ids for slot in mutated)


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


def test_crossover_distance_is_a_selection_objective_not_only_a_filter():
    """P6.2: the structurally most distant eligible pair wins deterministically."""
    rows = [
        {"elite_candidate_id": 1, "cell_key": "a", "signal_family": "alpha",
         "normalized_expression": "ts_rank(close,60)", "motif_id": "momentum"},
        {"elite_candidate_id": 2, "cell_key": "b", "signal_family": "beta",
         "normalized_expression": "ts_rank(ts_mean(close,20),60)", "motif_id": "momentum"},
        {"elite_candidate_id": 3, "cell_key": "c", "signal_family": "gamma",
         "normalized_expression": "group_rank(winsorize(zscore(ts_delta(assets,126)),std=4),subindustry)",
         "motif_id": "reversal"},
    ]
    import random as _random

    # Pairwise distances are strict: d(1,3) > d(2,3) > d(1,2), so the most distant eligible
    # pair (1,3) is a unique argmax. Every seed must pick it: a filter-then-shuffle
    # implementation would vary across seeds even with a unique best pair.
    distances = {
        frozenset((left["elite_candidate_id"], right["elite_candidate_id"])):
            policy._pair_distance(left, right)
        for index, left in enumerate(rows)
        for right in rows[index + 1:]
    }
    assert distances[frozenset((1, 3))] > distances[frozenset((2, 3))]
    assert distances[frozenset((2, 3))] > distances[frozenset((1, 2))]
    for seed in range(8):
        pair = policy._pick_crossover_pair(rows, {}, _random.Random(seed))
        assert set(pair) == {1, 3}, f"seed {seed} chose {pair} instead of the most distant pair"


def test_two_parent_crossover_novelty_reflects_the_closest_parent(db):
    """P7/P15: a crossover child close to parent B must be judged against B, not only A."""
    parent_a = db.queue_candidate(
        "group_rank(ts_rank(close,60),subindustry)", {"decay": 6}, signal_family="pv1",
    ).candidate_id
    parent_b = db.queue_candidate(
        "ts_delta(assets,126)", {"decay": 6}, signal_family="fundamental6",
    ).candidate_id
    service = generator.CandidateGenerator(db, seed=4)
    context = diversity.novelty_context(db, service.catalog)
    child = "ts_delta(assets,252)"  # near-clone of parent B, far from parent A

    only_a = diversity.screen_novelty(
        child, catalog=service.catalog, context=context,
        parent="group_rank(ts_rank(close,60),subindustry)",
    )
    both = diversity.screen_novelty(
        child, catalog=service.catalog, context=context,
        parent="group_rank(ts_rank(close,60),subindustry)", parents=["ts_delta(assets,126)"],
    )
    assert both.parent_distance == 0.0, "the child is a clone of the second parent"
    assert both.parent_distance < only_a.parent_distance
    assert both.score < only_a.score, "the close parent must reduce the novelty score"
    assert len(both.parent_distances) == 2 and min(both.parent_distances) == both.parent_distance

    # The generator screen persists both parent distances for a two-parent child (P7).
    proposal = generator.Proposal(
        expression=child, settings={"decay": 6}, family="fundamental6",
        mutation_type="crossover", parameters={"operation": "crossover"},
        parent_ids=(parent_a, parent_b), strategy="crossover", generation_mode="crossover",
    )
    screened = service.screen_proposals([proposal], campaign_id="v3-two-parent", seed=4)[0]
    assert len(screened.parent_distances) == 2
    assert screened.parent_distances[0] > screened.parent_distances[1] == 0.0
    assert service.queue("v3-two-parent", [screened])[0]["action"] == "queued"
    provenance = json.loads(db.trials("v3-two-parent")[-1]["provenance_json"])
    assert provenance["novelty_decision"] == screened.novelty_decision
    assert len(provenance["parent_distances"]) == 2


def test_crossover_distance_uses_real_dataset_and_category_metadata():
    """P6/P15: without catalog metadata same-topology pairs collapse; with it the
    cross-dataset pair is strictly farther and wins selection."""
    import random as _random

    metadata = diversity.load_field_metadata()
    alpha = {"elite_candidate_id": 1, "cell_key": "a", "signal_family": "alpha",
             "normalized_expression": "ts_rank(close,60)"}      # pv1 / pv
    beta = {"elite_candidate_id": 2, "cell_key": "b", "signal_family": "beta",
            "normalized_expression": "ts_rank(returns,60)"}    # pv1 / pv (same dataset)
    gamma = {"elite_candidate_id": 3, "cell_key": "c", "signal_family": "gamma",
             "normalized_expression": "ts_rank(assets,60)"}    # fundamental6 / fundamental

    # Blind: the same topology and the same masked field type make every pair identical.
    assert policy._pair_distance(alpha, gamma) == policy._pair_distance(alpha, beta) == 0.0
    assert policy._pair_distance(alpha, gamma, metadata=metadata) > policy._pair_distance(
        alpha, beta, metadata=metadata)

    for seed in range(8):
        blind = policy._pick_crossover_pair([alpha, beta, gamma], {}, _random.Random(seed))
        aware = policy._pick_crossover_pair([alpha, beta, gamma], {}, _random.Random(seed), metadata=metadata)
        assert set(blind) == {1, 2}, f"seed {seed}: metadata-free selection cannot see datasets"
        assert 3 in aware, f"seed {seed} chose {aware}: the cross-dataset pair must win"


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


def test_skipped_mutation_preserves_generation_parent_and_operation(db):
    """P7/P10: a novelty-skipped mutation keeps its full lineage in the ledger."""
    parent_id = db.queue_candidate(
        "rank(ts_delta(close,20))", {"decay": 6}, signal_family="pv1",
    ).candidate_id
    parent = db.get_candidate(parent_id)
    service = generator.CandidateGenerator(db, seed=8)
    proposal = service.structural_mutation(parent, operation="dataset_swap", campaign_id="v3-skip-mut")
    assert proposal is not None
    service.queue("v3-skip-mut", [proposal])  # the child now exists
    screened = generator.CandidateGenerator(db, seed=8).screen_proposals(
        [proposal], campaign_id="v3-skip-mut",
    )
    assert screened[0].novelty_decision == diversity.SKIP_REDUNDANT
    outcomes = generator.CandidateGenerator(db, seed=8).queue("v3-skip-mut", screened)
    assert outcomes[0]["action"] == "skipped_redundant"
    assert outcomes[0]["parent_ids"] == [parent_id]
    trial = [row for row in db.trials("v3-skip-mut") if row["decision"] == diversity.SKIP_REDUNDANT][-1]
    assert json.loads(trial["parent_ids_json"]) == [parent_id]
    assert trial["generation"] == 1
    assert trial["mutation_type"] == "dataset_swap"
    assert json.loads(trial["mutation_parameters_json"])["operation"] == "dataset_swap"


def test_skipped_crossover_preserves_both_parents(db):
    """P7/P10: a novelty-skipped crossover keeps both parents and its operation."""
    parent_a = db.queue_candidate(
        "group_rank(ts_rank(close,60),subindustry)", {"decay": 6}, signal_family="pv1",
    ).candidate_id
    parent_b = db.queue_candidate(
        "group_rank(ts_rank(assets,60),industry)", {"decay": 6}, signal_family="fundamental6",
    ).candidate_id
    slot = policy.PlanSlot(
        slot=0, generation_mode="crossover", family="crossover", motif_id="crossover",
        recipe_index=0, reason="explicit crossover request", parent_ids=(parent_a, parent_b),
    )
    service = generator.CandidateGenerator(db, seed=2)
    proposal = service.materialize(slot, campaign_id="v3-skip-x")
    assert proposal is not None and len(proposal.parent_ids) == 2
    service.queue("v3-skip-x", [proposal])  # the child now exists
    screened = generator.CandidateGenerator(db, seed=2).screen_proposals(
        [proposal], campaign_id="v3-skip-x",
    )
    assert screened[0].novelty_decision == diversity.SKIP_REDUNDANT
    outcomes = generator.CandidateGenerator(db, seed=2).queue("v3-skip-x", screened)
    assert outcomes[0]["action"] == "skipped_redundant"
    assert sorted(outcomes[0]["parent_ids"]) == sorted([parent_a, parent_b])
    trial = [row for row in db.trials("v3-skip-x") if row["decision"] == diversity.SKIP_REDUNDANT][-1]
    assert sorted(json.loads(trial["parent_ids_json"])) == sorted([parent_a, parent_b])
    assert trial["mutation_type"] == "crossover"
    parameters = json.loads(trial["mutation_parameters_json"])
    assert parameters["operation"] == "crossover"
    assert parameters["crossover_form"]


def test_generation_stats_preserve_cross_family_mutation_lineage(db):
    """P9.2: source family is the parent's, target family the child-derived one."""
    parent_id = db.queue_candidate(
        "rank(ts_delta(close,20))", {"decay": 6}, signal_family="pv1",
    ).candidate_id
    service = generator.CandidateGenerator(db, seed=4)
    proposal = service.structural_mutation(
        db.get_candidate(parent_id), operation="dataset_swap", campaign_id="v3-gstats",
    )
    assert proposal is not None and proposal.family != "pv1"
    outcomes = service.queue("v3-gstats", [proposal])
    assert outcomes[0]["action"] == "queued"
    settled = db.claim_simulation("gstats", candidate_id=outcomes[0]["candidate_id"])
    db.record_simulation_result(
        candidate_id=settled["id"], status="DONE",
        metrics={"sharpe": 1.4, "fitness": 1.1, "turnover": 0.08},
        checks=[{"name": "IS", "result": "PASS"}], brain_alpha_id="A-gstats",
    )
    db.refresh_generation_stats(generator_version=generator.GENERATOR_VERSION_V3)
    rows = [row for row in db.generation_stats(
        generator_version=generator.GENERATOR_VERSION_V3, table="generation_operator_stats",
    ) if row["mutation_operation"] == "dataset_swap"]
    assert rows
    row = rows[0]
    assert row["source_family"] == "pv1"
    assert row["target_family"] == proposal.family
    assert row["source_family"] != row["target_family"], "a cross-family mutation must not collapse both sides"


def test_generation_stats_learn_concrete_operations_apart_from_repair_class(db):
    """P9.2: the concrete edit is aggregated separately from the broad repair class."""
    parent_id = db.queue_candidate("ts_rank(close,60)", {"decay": 6}, signal_family="pv1").candidate_id
    parent = dict(db.get_candidate(parent_id), failure_reason="HIGH_TURNOVER")
    proposals = generator.CandidateGenerator(db, seed=9).mutate(parent, count=3)
    assert {p.mutation_type for p in proposals} == {"turnover_repair"}
    assert len({p.parameters["operation"] for p in proposals}) >= 2  # hump_smoothing + window_change
    service = generator.CandidateGenerator(db, seed=9)
    labelled = [replace(p, generation_mode="mutate", strategy="mutate") for p in proposals]
    outcomes = service.queue("v3-op-class", labelled)
    assert all(outcome["action"] == "queued" for outcome in outcomes)
    db.refresh_generation_stats(generator_version=generator.GENERATOR_VERSION_V3)
    rows = [row for row in db.generation_stats(
        generator_version=generator.GENERATOR_VERSION_V3, table="generation_operator_stats",
    ) if row["mutation_type"] == "turnover_repair"]
    operations = {row["mutation_operation"] for row in rows}
    assert {"hump_smoothing", "window_change"} <= operations, (
        "one repair class with two concrete edits must aggregate into separate operations"
    )
    learned = db.mutation_operation_outcomes(generator_version=generator.GENERATOR_VERSION_V3)
    assert {"hump_smoothing", "window_change"} <= set(learned)


def test_planned_mutation_operations_are_budgeted_and_realized(db):
    """P9.2: the allocation lands on mutate slots and materialization honors the pin."""
    _settle(db, "group_rank(ts_rank(close,60),subindustry)", "pv1", 1.7)
    _settle(db, "group_rank(ts_rank(assets,60),industry)", "fundamental6", 1.5)
    archive.rebuild(db)
    service = generator.CandidateGenerator(db, seed=2)
    plan = service.plan(campaign_id="v3-ops-plan", budget=24, seed=2, mode="mixed")
    planned = [slot for slot in plan.slots if slot.generation_mode == "mutate"]
    assert planned
    assert all(slot.mutation_operation in generator.V3_MUTATION_OPERATIONS for slot in planned)
    assert plan.mutation_allocation and sum(plan.mutation_allocation.values()) >= len(planned)

    pinned = replace(planned[0], mutation_operation="group_change")
    proposal = service.materialize(pinned, campaign_id="v3-ops-plan")
    assert proposal is not None
    assert proposal.parameters["operation"] == "group_change"


def test_skipped_rediscovery_does_not_create_simulated_or_pass_evidence(db):
    """P9.2/P15: a skipped duplicate is a generator attempt, never fresh pass evidence."""
    service = generator.CandidateGenerator(db, seed=13)
    _, first = service.generate(campaign_id="v3-skip-evidence", count=6, seed=13, strategy="explore")
    outcomes = service.queue("v3-skip-evidence", first)
    assert all(outcome["action"] == "queued" for outcome in outcomes)
    settled = db.claim_simulation("skip-evidence", candidate_id=outcomes[0]["candidate_id"])
    db.record_simulation_result(
        candidate_id=settled["id"], status="DONE",
        metrics={"sharpe": 1.6, "fitness": 1.2, "turnover": 0.08},
        checks=[{"name": "IS", "result": "PASS"}], brain_alpha_id="A-skip-evidence",
    )
    assert db.get_candidate(outcomes[0]["candidate_id"])["status"] in {"IS_PASS", "SUBMISSION_READY"}
    target = first[0]
    db.refresh_generation_stats(generator_version=generator.GENERATOR_VERSION_V3)

    def bucket_totals() -> dict[str, int]:
        rows = [
            row for row in db.generation_stats(generator_version=generator.GENERATOR_VERSION_V3)
            if row["motif_id"] == target.motif_id and row["generation_mode"] == target.generation_mode
        ]
        return {
            key: sum(int(row[key]) for row in rows)
            for key in ("attempts", "simulated", "is_pass", "corr_pass", "active")
        }

    before = bucket_totals()
    assert before["is_pass"] == 1  # the one real pass of this motif/mode

    # Rediscover and refuse the exact same proposal three times: three recorded trials, no
    # new simulation, and no inherited pass evidence from the existing candidate.
    for _ in range(3):
        screened = generator.CandidateGenerator(db, seed=13).screen_proposals(
            [target], campaign_id="v3-skip-evidence", seed=13,
        )
        assert screened[0].novelty_decision == diversity.SKIP_REDUNDANT
        skipped = generator.CandidateGenerator(db, seed=13).queue("v3-skip-evidence", screened)
        assert skipped[0]["action"] == "skipped_redundant"

    db.refresh_generation_stats(generator_version=generator.GENERATOR_VERSION_V3)
    after = bucket_totals()
    assert after["attempts"] == before["attempts"] + 3
    for key in ("simulated", "is_pass", "corr_pass", "active"):
        assert after[key] == before[key], f"skipped rediscovery inflated {key}"


def test_pinned_inapplicable_mutation_operation_records_an_honest_fallback(db):
    """P4.4/P9.2/P15: a pinned edit that cannot apply is reported, and the edit that actually
    ran is the one statistics learn from."""
    parent_id = db.queue_candidate("ts_rank(close,60)", {"decay": 6}, signal_family="pv1").candidate_id
    slot = policy.PlanSlot(
        slot=0, generation_mode="mutate", family="pv1", motif_id="mutation", recipe_index=0,
        reason="pinned", parent_ids=(parent_id,), mutation_operation="group_change",
    )
    service = generator.CandidateGenerator(db, seed=3)
    proposal = service.materialize(slot, campaign_id="v3-op-fallback")
    assert proposal is not None
    realized = proposal.parameters["operation"]
    assert realized != "group_change", "group_change cannot apply to a parent with no group field"
    assert proposal.parameters["planned_operation"] == "group_change"
    assert proposal.parameters["realized_operation"] == realized
    assert proposal.parameters["operation_fallback"] is True

    plan = policy.Plan(campaign_id="v3-op-fallback", mode="mutate", budget=1, seed=0,
                       slots=(slot,), weights={})
    distribution = generator._v3_distribution(plan, [proposal])
    assert distribution["mutation_operation_fallback_count"] == 1
    assert distribution["mutation_operation_fallback"] == {f"group_change->{realized}": 1}

    outcomes = service.queue("v3-op-fallback", [proposal])
    assert outcomes[0]["action"] == "queued"
    db.refresh_generation_stats(generator_version=generator.GENERATOR_VERSION_V3)
    rows = db.generation_stats(generator_version=generator.GENERATOR_VERSION_V3,
                               table="generation_operator_stats")
    assert any(row["mutation_operation"] == realized and int(row["attempts"]) >= 1 for row in rows)
    assert not [row for row in rows if row["mutation_operation"] == "group_change"], (
        "the inapplicable pinned edit must not be credited with an attempt"
    )


def test_applicable_pinned_mutation_operation_is_recorded_without_a_fallback(db):
    """P4.4: when the pinned edit can apply, planned and realized agree."""
    parent_id = db.queue_candidate(
        "group_rank(ts_rank(close,60),subindustry)", {"decay": 6}, signal_family="pv1",
    ).candidate_id
    slot = policy.PlanSlot(
        slot=0, generation_mode="mutate", family="pv1", motif_id="mutation", recipe_index=0,
        reason="pinned", parent_ids=(parent_id,), mutation_operation="group_change",
    )
    proposal = generator.CandidateGenerator(db, seed=3).materialize(slot, campaign_id="v3-op-direct")
    assert proposal is not None
    assert proposal.parameters["operation"] == "group_change"
    assert proposal.parameters["realized_operation"] == "group_change"
    assert proposal.parameters["planned_operation"] == "group_change"
    assert "operation_fallback" not in proposal.parameters


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


# ---------------------------------------------------------------------------
# P6 tolerance for real, already-evolved parents
# ---------------------------------------------------------------------------


def test_crossover_budget_scales_with_its_parents(db):
    """Two mutated elites must still be combinable, without an unbounded child."""
    deep_a = (
        "trade_when(ts_std_dev(returns,20)>ts_mean(ts_std_dev(returns,20),120),"
        "rank(ts_rank(est_ptp/close,126)),-1)"
    )
    deep_b = "group_rank(ts_zscore(implied_volatility_mean_10 - ts_std_dev(returns,60),120),sector)"
    parent_a = db.queue_candidate(deep_a, {"decay": 6}, signal_family="analyst4").candidate_id
    parent_b = db.queue_candidate(deep_b, {"decay": 6}, signal_family="option8").candidate_id

    slot = policy.PlanSlot(
        slot=0, generation_mode="crossover", family="crossover", motif_id="crossover",
        recipe_index=0, reason="explicit crossover request", parent_ids=(parent_a, parent_b),
    )
    proposal = generator.CandidateGenerator(db, seed=5).materialize(slot, campaign_id="v3-deep")
    assert proposal is not None, "an evolved parent pair must not be silently refused"
    assert proposal.generation_mode == "crossover"
    assert sorted(proposal.parent_ids) == sorted((parent_a, parent_b))
    assert proposal.mutation_type == "crossover"

    node = grammar.parse_expression(proposal.expression)
    assert not grammar.check_complexity(node, generator.CROSSOVER_LIMITS)
    # The cap is absolute: the child never exceeds the fixed ceiling, whatever the parents.
    assert grammar.node_depth(node) <= generator.CROSSOVER_LIMITS.max_depth
    assert grammar.node_count(node) <= generator.CROSSOVER_LIMITS.max_nodes


def test_crossover_child_family_matches_its_own_source_profile(db):
    """P0.1/P6.3: a cross-family pair must not leave the first parent's label on the child."""
    parent_a = db.queue_candidate(
        "group_rank(ts_rank(close,60),subindustry)", {"decay": 6}, signal_family="pv1",
    ).candidate_id
    parent_b = db.queue_candidate(
        "group_rank(ts_rank(assets,60),industry)", {"decay": 6}, signal_family="fundamental6",
    ).candidate_id
    slot = policy.PlanSlot(
        slot=0, generation_mode="crossover", family="crossover", motif_id="crossover",
        recipe_index=0, reason="explicit crossover request", parent_ids=(parent_a, parent_b),
    )
    service = generator.CandidateGenerator(db, seed=1)
    proposal = service.materialize(slot, campaign_id="v3-x-fam")
    assert proposal is not None
    profile = diversity.derive_source_profile(proposal.expression, catalog=generator.Catalog())
    assert profile["cross_dataset"], "the fixture pair must genuinely span two datasets"
    assert proposal.family == profile["primary_family"]
    assert proposal.family == "multi:" + "+".join(profile["datasets"])
    assert proposal.family != "pv1", "the first parent's family must not leak onto the child"
    assert proposal.source_profile == profile

    outcomes = service.queue("v3-x-fam", [proposal])
    assert outcomes[0]["action"] == "queued"
    row = db.get_candidate(outcomes[0]["candidate_id"])
    assert row["signal_family"] == proposal.family
    trial = db.trials("v3-x-fam")[-1]
    assert trial["signal_family"] == proposal.family


# ---------------------------------------------------------------------------
# P4.4: the concrete structural mutation vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation, parent_expression", [
    ("dataset_swap", "rank(ts_delta(close,20))"),
    ("motif_change", "ts_rank(close,60)"),
    ("normalization_change", "rank(ts_delta(close,20))"),
    ("group_change", "group_rank(ts_rank(close,60),subindustry)"),
    ("subtree_replace", "group_rank(ts_rank(ts_delta(close,20),60),subindustry)"),
    ("add_component", "ts_rank(close,60)"),
])
def test_every_v3_mutation_operation_is_generatable(db, operation, parent_expression):
    """Each promised operation is a real, validated edit that records its own name."""
    parent_id = db.queue_candidate(
        parent_expression, {"decay": 6}, signal_family="pv1",
    ).candidate_id
    parent = db.get_candidate(parent_id)
    service = generator.CandidateGenerator(db, seed=3)
    proposal = service.structural_mutation(parent, operation=operation, campaign_id="v3-ops")
    assert proposal is not None, f"{operation} must be generatable"
    assert proposal.parameters["operation"] == operation
    assert proposal.mutation_type == operation
    assert proposal.parent_ids == (parent_id,)
    assert proposal.expression != parent_expression
    # AST/type/complexity validation happens before the child exists.
    metadata = service.metadata()
    child_node = grammar.parse_expression(proposal.expression, metadata)
    parent_node = grammar.parse_expression(parent_expression, metadata)
    grammar.validate_tree(child_node, generator._mutation_limits(parent_node))
    assert proposal.family == diversity.derive_source_profile(
        proposal.expression, catalog=generator.Catalog())["primary_family"]


def test_structural_dataset_swap_moves_to_another_dataset(db):
    parent_id = db.queue_candidate("rank(ts_delta(close,20))", {"decay": 6}, signal_family="pv1").candidate_id
    proposal = generator.CandidateGenerator(db, seed=4).structural_mutation(
        db.get_candidate(parent_id), operation="dataset_swap", campaign_id="v3-swap",
    )
    assert proposal is not None
    assert proposal.parameters["previous_dataset"] == "pv1"
    assert proposal.parameters["replacement_dataset"] != "pv1"
    assert proposal.parameters["replacement_field"] in proposal.expression
    assert proposal.parameters["replaced_field"] not in proposal.expression


def test_structural_normalization_change_flips_the_normalizer(db):
    parent_id = db.queue_candidate("rank(ts_delta(close,20))", {"decay": 6}, signal_family="pv1").candidate_id
    proposal = generator.CandidateGenerator(db, seed=5).structural_mutation(
        db.get_candidate(parent_id), operation="normalization_change", campaign_id="v3-norm",
    )
    assert proposal is not None
    assert proposal.parameters["previous_normalizer"] == "rank"
    assert proposal.parameters["normalizer"] == "zscore"
    assert proposal.expression.startswith("zscore(")


def test_structural_group_change_moves_one_level(db):
    parent_id = db.queue_candidate(
        "group_rank(ts_rank(close,60),subindustry)", {"decay": 6}, signal_family="pv1",
    ).candidate_id
    proposal = generator.CandidateGenerator(db, seed=6).structural_mutation(
        db.get_candidate(parent_id), operation="group_change", campaign_id="v3-group",
    )
    assert proposal is not None
    assert proposal.parameters["previous_group"] == "subindustry"
    assert proposal.parameters["group"] == "industry"
    assert proposal.expression.endswith(",industry)")


def test_structural_add_component_combines_a_fresh_source(db):
    parent_id = db.queue_candidate("ts_rank(close,60)", {"decay": 6}, signal_family="pv1").candidate_id
    proposal = generator.CandidateGenerator(db, seed=7).structural_mutation(
        db.get_candidate(parent_id), operation="add_component", campaign_id="v3-add",
    )
    assert proposal is not None
    assert proposal.parameters["operation"] == "add_component"
    assert proposal.parameters["added_field"] not in ("close", "")
    assert proposal.expression.startswith("add(")


def test_v3_mutate_slots_generate_structural_operations(db):
    """The V3 mutate path must reach the structural vocabulary, not only repair/field swaps."""
    parent_id = db.queue_candidate(
        "group_rank(ts_rank(ts_delta(close,20),60),subindustry)", {"decay": 6}, signal_family="pv1",
    ).candidate_id
    operations: set[str] = set()
    for index in range(16):
        slot = policy.PlanSlot(
            slot=index, generation_mode="mutate", family="pv1", motif_id="mutation",
            recipe_index=index, reason="repair/perturb a diverse archive elite",
            parent_ids=(parent_id,),
        )
        proposal = generator.CandidateGenerator(db, seed=index).materialize(
            slot, campaign_id="v3-struct",
        )
        if proposal is not None:
            operations.add(str(proposal.parameters.get("operation") or proposal.mutation_type))
    assert operations & set(generator.V3_MUTATION_OPERATIONS), (
        f"expected at least one structural operation, got {sorted(operations)}"
    )


# ---------------------------------------------------------------------------
# P4.2/P4.3: cross-dataset reachability, novelty budgeting, structure transfer
# ---------------------------------------------------------------------------


def test_ordinary_planning_produces_a_cross_dataset_motif(db):
    """P4.2: a deterministic fixture must reach a genuine cross-dataset proposal."""
    plan, proposals = generator.CandidateGenerator(db, seed=13).generate(
        campaign_id="v3-xdata", count=24, seed=13, strategy="mixed",
    )
    assert plan.planned_budget == 24 == len(proposals)
    cross = [p for p in proposals if p.source_profile["cross_dataset"]]
    assert cross, "ordinary planning must be able to reach cross-dataset structures"
    assert all(len(p.source_profile["datasets"]) >= 2 for p in cross)
    composite = [p for p in proposals if p.motif_id == "cross_dataset_composite"]
    assert composite, "cross_dataset_composite must be reachable without forcing --motif"
    assert all(p.source_profile["cross_dataset"] for p in composite)


def test_explore_slots_budget_unseen_structures_before_repeats(db):
    """P4.2: grammar/semantic history is budgeted at plan time, not after materialization."""
    for window in (20, 30, 40, 50, 60, 70, 80, 90):
        db.queue_candidate(f"ts_rank(close,{window})", {"decay": 6}, signal_family="pv1")
    catalog = generator.Catalog()
    history, _ = policy._structure_counts(db, catalog)
    assert history, "the fixture must leave a saturated grammar structure in history"
    plan = generator.CandidateGenerator(db, seed=6).plan(
        campaign_id="v3-budget", budget=12, seed=6, mode="explore",
    )
    explore = [slot for slot in plan.slots if slot.generation_mode == "explore"]
    assert explore
    seats: dict[str, int] = {}
    for slot in explore:
        # The planner must expose the exact structure it budgeted (P4.2), not a proxy.
        grammar_key = slot.planned_grammar_hash
        assert grammar_key, f"planned motif {slot.motif_id} must record its structure"
        assert grammar_key not in history, (
            "a history-saturated structure may not be re-planned while unseen structures remain"
        )
        seats[grammar_key] = seats.get(grammar_key, 0) + 1
    # Unseen structures first: with 18+ reachable structures and 12 explore slots, no
    # structure may be seated twice.
    assert max(seats.values()) == 1, f"repeated planned structures: {seats}"


def test_planned_structure_hashes_match_materialized_structures(db):
    """P4.2/P15: the planner's exact-recipe hashes equal the materialized proposal's."""
    for window in (20, 30, 40, 50, 60, 70, 80, 90):
        db.queue_candidate(f"ts_rank(close,{window})", {"decay": 6}, signal_family="pv1")
    catalog = generator.Catalog()
    history, _ = policy._structure_counts(db, catalog)
    plan = generator.CandidateGenerator(db, seed=6).plan(
        campaign_id="v3-exact", budget=12, seed=6, mode="explore",
    )
    service = generator.CandidateGenerator(db, seed=6)
    materialized = [service.materialize(slot, campaign_id="v3-exact", seed=6) for slot in plan.slots]
    assert all(proposal is not None for proposal in materialized), "every planned slot must materialize"
    seen: set[str] = set()
    for slot, proposal in zip(plan.slots, materialized):
        if slot.generation_mode != "explore":
            continue
        assert slot.planned_grammar_hash == proposal.grammar_skeleton_hash, slot.motif_id
        assert slot.planned_semantic_hash == proposal.semantic_skeleton_hash, slot.motif_id
        assert proposal.grammar_skeleton_hash not in history, (
            f"{slot.motif_id} re-used a saturated structure the planner said it avoided"
        )
        assert proposal.grammar_skeleton_hash not in seen, "repeated materialized structure"
        seen.add(proposal.grammar_skeleton_hash)
    assert seen


def test_exploit_transfers_proven_structure_to_a_new_source(db):
    """P4.3: a motif proven in one family may seed a compatible new dataset."""
    outcome = db.queue_candidate(
        "rank(ts_delta(close,20))", {"decay": 6}, signal_family="pv1",
        mutation_parameters={"motif_id": "momentum", "generation_mode": "exploit"},
    )
    claimed = db.claim_simulation("xfer", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": 1.5, "fitness": 1.1, "turnover": 0.08},
        checks=[{"name": "IS", "result": "PASS"}], brain_alpha_id="A-xfer",
    )
    plan = generator.CandidateGenerator(db, seed=3).plan(
        campaign_id="v3-xfer", budget=12, seed=3, mode="exploit", family="fundamental6",
    )
    assert plan.slots and all(slot.family == "fundamental6" for slot in plan.slots)
    motifs = {slot.motif_id for slot in plan.slots}
    assert "momentum" in motifs, "proven structure must transfer to the new dataset"
    assert any("transferred" in slot.reason for slot in plan.slots)


def test_add_component_is_the_stable_combine_operation_name(db):
    """The historical structural-combine name is normalized to ``add_component``."""
    parent_id = db.queue_candidate("ts_rank(close,60)", {"decay": 6}, signal_family="pv1").candidate_id
    parent = db.get_candidate(parent_id)
    proposals = generator.CandidateGenerator(db, seed=9).mutate(
        dict(parent, failure_reason="LOW_SHARPE"), count=10,
    )
    combined = [p for p in proposals if p.parameters.get("operation") in {"add_component", "combine_signals"}]
    assert combined, "the sharpe repair must offer a structural combine"
    assert all(p.parameters["operation"] == "add_component" for p in combined)
