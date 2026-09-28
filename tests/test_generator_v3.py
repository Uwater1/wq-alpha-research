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
    nodes = {field.name: grammar.field_node_from(field) for field in catalog.fields}
    seats: dict[str, int] = {}
    for slot in explore:
        hashes = policy._structure_hashes(slot.motif_id, [nodes[name] for name in slot.fields])
        assert hashes is not None, f"planned motif {slot.motif_id} must be materializable"
        grammar_key = hashes[0]
        assert grammar_key not in history, (
            "a history-saturated structure may not be re-planned while unseen structures remain"
        )
        seats[grammar_key] = seats.get(grammar_key, 0) + 1
    # Unseen structures first: with 18+ reachable structures and 12 explore slots, no
    # structure may be seated twice.
    assert max(seats.values()) == 1, f"repeated planned structures: {seats}"


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
