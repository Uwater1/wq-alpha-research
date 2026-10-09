"""P20/P21 regression: quality conditions the search without collapsing it.

Two things are being protected here. Quality evidence must be able to move budget toward what
works. And it must never be able to delete a mode or a niche from the search, or to let a single
lucky cell rewrite the whole campaign — a bounded prior is the point.
"""
from __future__ import annotations

import collections
import json
import math

import pytest

import archive
import generation_policy as policy
from generation_policy import _operator_evidence as generation_policy_operator_evidence
import generator
import quality_prior as qp
import research_db
import seed_bank


@pytest.fixture()
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


@pytest.fixture(scope="module")
def catalog():
    return generator.Catalog()


def _settle(db, expression, *, motif_id="ratio", version="catalog-generator-v2",
            is_pass=True, sharpe=2.0, generation_mode="explore", settings=None):
    outcome = db.queue_candidate(
        expression, settings or {"decay": 8, "truncation": 0.08}, signal_family="pv1",
        generator_version=version, motif_id=motif_id, generation_mode=generation_mode,
        campaign_id="ledger",
    )
    claimed = db.claim_simulation("t", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": sharpe, "fitness": 1.5 if is_pass else 0.2, "turnover": 0.1},
        checks=[{"name": "IS", "result": "PASS" if is_pass else "FAIL"}],
        brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return outcome.candidate_id


# ---------------------------------------------------------------------------
# P22.3 / P20.2: bounded mode weights
# ---------------------------------------------------------------------------


def test_without_evidence_the_mode_weights_are_unchanged():
    base = dict(policy.STRATEGY_WEIGHTS)
    assert policy.quality_conditioned_weights(base, {}) == pytest.approx(
        {name: base[name] / sum(base.values()) for name in policy.GENERATION_MODES}
    )
    # A mode with too few simulations is not judged.
    thin = policy.quality_conditioned_weights(base, {"crossover": (2, 2)})
    assert thin["crossover"] == pytest.approx(thin["explore"] * base["crossover"] / base["explore"])


def test_a_paying_mode_gains_budget_and_a_weak_one_is_shrunk_not_deleted():
    base = {"explore": 0.4, "exploit": 0.25, "mutate": 0.25, "crossover": 0.10}
    stats = {"mutate": (40, 20), "crossover": (40, 1), "explore": (40, 8)}
    weights = policy.quality_conditioned_weights(base, stats)
    assert weights["mutate"] > base["mutate"]
    assert weights["crossover"] < base["crossover"]
    assert weights["crossover"] >= policy.DEFAULT_MODE_FLOOR * 0.9
    assert sum(weights.values()) == pytest.approx(1.0)
    assert max(weights.values()) <= policy.DEFAULT_MODE_MAX_SHARE + 1e-6


def test_mode_weight_movement_is_bounded_by_the_ratio():
    base = {"explore": 0.25, "exploit": 0.25, "mutate": 0.25, "crossover": 0.25}
    extreme = policy.quality_conditioned_weights(base, {"mutate": (200, 200), "explore": (200, 0)})
    plain = {name: 0.25 for name in policy.GENERATION_MODES}
    for name in policy.GENERATION_MODES:
        assert extreme[name] <= plain[name] * policy.MODE_WEIGHT_RATIO
        assert extreme[name] >= plain[name] / policy.MODE_WEIGHT_RATIO - 1e-6


def test_crossover_is_reduced_when_it_has_no_marginal_value():
    """P22.3: crossover keeps only its floor until it demonstrates positive marginal value."""
    base = dict(policy.STRATEGY_WEIGHTS)
    flat = policy.quality_conditioned_weights(base)
    weak = policy.quality_conditioned_weights(base, {
        "explore": (30, 6), "exploit": (30, 7), "mutate": (30, 8), "crossover": (30, 0),
    })
    assert weak["crossover"] < flat["crossover"]
    assert weak["mutate"] >= flat["mutate"]


def test_mode_outcomes_read_the_persisted_counters(db):
    _settle(db, "group_rank(ts_rank(close,60),subindustry)", generation_mode="explore")
    _settle(db, "rank(ts_delta(open,60))", generation_mode="mutate", is_pass=False)
    db.refresh_generation_stats()
    outcomes = policy.mode_outcomes(db)
    assert outcomes["explore"][0] >= 1 and outcomes["explore"][1] >= 1
    assert outcomes["mutate"][0] >= 1 and outcomes["mutate"][1] == 0


# ---------------------------------------------------------------------------
# P21.1: conditional motif allocation
# ---------------------------------------------------------------------------


class _StubPrior:
    """A prior whose answer depends only on the (motif, dataset) pair, for exact assertions.

    A pair absent from ``means`` is reported as *unobserved* (zero simulations), which is how
    the allocator recognises a motif it has never tested and reserves the exploration floor
    for it.
    """

    def __init__(self, means):
        self.means = means

    def score(self, context, *, novelty=1.0, cost=1.0, uncertainty=None):
        observed = self.means.get((context.motif_id, context.dataset))
        simulations = 20 if observed is not None else 0
        mean = 0.05 if observed is None else observed
        look = qp.Look(mean=mean, lower=max(0.0, mean - 0.05), upper=mean,
                       simulations=simulations, level="motif+dataset",
                       specific=qp.Evidence(simulations=simulations),
                       specific_level="motif+dataset", global_prior=0.05)
        return qp.quality_conditioned_score(look, novelty=novelty, cost=cost), look


def test_conditional_allocation_spends_where_the_evidence_is():
    prior = _StubPrior({("ratio", "analyst4"): 0.40, ("change", "analyst4"): 0.01})
    allocation, report = policy.allocate_motifs_conditioned(
        ["ratio", "change"], ["analyst4"], 40, prior, max_share=0.75,
    )
    assert sum(allocation.values()) == 40
    assert allocation["ratio"] > allocation["change"]
    assert allocation["change"] > 0, "a weak motif keeps an exploration share, it is not deleted"
    assert report["contexts"]["ratio"]["dataset"] == "analyst4"


def test_the_default_share_cap_bounds_an_evidence_rich_motif():
    """The cap is never below an even split, so two motifs split 50/50 however lopsided the
    evidence is, and neither is dropped: evidence moves budget only inside the bound."""
    prior = _StubPrior({("ratio", "analyst4"): 0.40, ("change", "analyst4"): 0.01})
    allocation, _ = policy.allocate_motifs_conditioned(["ratio", "change"], ["analyst4"], 40, prior)
    cap = max(math.ceil(40 * policy.DEFAULT_MOTIF_MAX_SHARE), math.ceil(40 / 2))
    assert sum(allocation.values()) == 40
    assert allocation["ratio"] == cap
    assert allocation["change"] > 0


def test_conditional_allocation_prefers_the_dataset_a_motif_actually_pays_in():
    prior = _StubPrior({("ratio", "analyst4"): 0.40, ("ratio", "pv1"): 0.01})
    allocation, report = policy.allocate_motifs_conditioned(["ratio"], ["analyst4", "pv1"], 10, prior)
    assert allocation == {"ratio": 10}
    assert report["contexts"]["ratio"]["dataset"] == "analyst4"


def test_conditional_allocation_floors_untested_motifs_and_caps_any_one_motif():
    prior = _StubPrior({("ratio", "analyst4"): 0.40})
    allocation, report = policy.allocate_motifs_conditioned(
        ["ratio", "never_tried"], ["analyst4"], 20, prior, exploration_floor=0.25, max_share=0.6,
    )
    assert sum(allocation.values()) == 20
    assert allocation["never_tried"] >= 5  # the exploration reserve, not zero
    # The cap is 12 = 60% of 20, so the motif with room absorbs the three slots that do not fit
    # under ratio's cap; nothing may exceed the cap.
    assert allocation["ratio"] == 12
    assert allocation["never_tried"] == 8
    assert report["untested"] == ["never_tried"]


def test_conditional_allocation_returns_nothing_without_a_prior_or_a_budget():
    assert policy.allocate_motifs_conditioned(["ratio"], ["analyst4"], 10, None) == ({}, {})
    assert policy.allocate_motifs_conditioned([], ["analyst4"], 10, _StubPrior({})) == ({}, {})


# ---------------------------------------------------------------------------
# P20.1: the archive keeps local competition on quality
# ---------------------------------------------------------------------------


def test_a_niche_elite_is_the_best_evidence_not_the_best_novelty(db):
    # `rank(close)` and `rank(open)` share a niche (same topology, same source family): the
    # elite must be chosen by evidence inside that niche, not by whichever came first.
    weak = _settle(db, "rank(close)", is_pass=False, sharpe=0.1)
    strong = _settle(db, "rank(open)", is_pass=True, sharpe=2.0)
    archive.rebuild(db)
    cells = db.query("SELECT elite_candidate_id, dimensions_json FROM archive_cells")
    assert len(cells) == 1
    assert cells[0]["elite_candidate_id"] == strong
    # Within the niche the passed candidate wins even though both share the same structure.
    elite = archive.quality_elites(db, per_niche=2)
    assert [row["candidate_id"] for row in elite][:2] == [strong, weak]
    assert elite[0]["quality"] > elite[1]["quality"]


def test_quality_elite_score_ranks_stage_before_metrics():
    passed = {"status": "IS_PASS", "sharpe": 1.3, "fitness": 1.1, "turnover": 0.1}
    huge_but_refused = {"status": "SIMULATED", "sharpe": 99.0, "fitness": 90.0, "turnover": 0.1}
    assert archive.quality_elite_score(passed) > archive.quality_elite_score(huge_but_refused)


def test_turnover_acceptability_is_a_bonus_bounded_on_both_sides():
    good = archive.quality_elite_score({"status": "SIMULATED", "turnover": archive.TURNOVER_TARGET})
    too_low = archive.quality_elite_score({"status": "SIMULATED", "turnover": 0.0})
    too_high = archive.quality_elite_score({"status": "SIMULATED", "turnover": 0.6})
    assert good > too_low > too_high


def test_under_tested_niches_are_reported_not_invented(db):
    _settle(db, "rank(close)")
    _settle(db, "group_rank(ts_rank(ebit,126),industry)")
    archive.rebuild(db)
    sparse = archive.under_tested_niches(db, limit=5, max_members=1)
    assert sparse and all(row["members"] == 1 for row in sparse)
    assert all(row["sparse"] for row in sparse)


# ---------------------------------------------------------------------------
# P22.2: lineage modes descend from parents that reached a gate
# ---------------------------------------------------------------------------


def test_the_lineage_pool_holds_only_gate_reaching_elites(db):
    failed = _settle(db, "rank(close)", is_pass=False, sharpe=0.1)
    passed = _settle(db, "group_rank(ts_rank(ebit,126),industry)", is_pass=True, sharpe=2.0)
    archive.rebuild(db)
    pool = archive.exploitation_parents(db, count=8)
    assert [row["elite_candidate_id"] for row in pool] == [passed]
    assert failed not in {row["elite_candidate_id"] for row in pool}


def test_a_young_campaign_has_no_lineage_pool_and_keeps_its_diversity_pool(db):
    _settle(db, "rank(close)", is_pass=False, sharpe=0.1)
    archive.rebuild(db)
    assert archive.exploitation_parents(db, count=8) == []
    assert archive.parents(db, count=8), "the diversity pool is unaffected"


def test_a_mutation_parent_comes_from_the_proven_pool_when_one_exists(db, catalog):
    passed = _settle(db, "group_rank(ts_rank(ebit,126),industry)", is_pass=True, sharpe=2.0)
    _settle(db, "rank(close)", is_pass=False, sharpe=0.1)
    archive.rebuild(db)
    plan = generator.CandidateGenerator(db, catalog, seed=12).plan(
        campaign_id="lineage", budget=12, seed=12, mode="mutate",
    )
    lineages = [slot for slot in plan.slots if slot.generation_mode == "mutate" and slot.parent_ids]
    assert lineages
    assert {slot.parent_ids[0] for slot in lineages} == {passed}


def test_the_lineage_pool_spreads_over_families(db):
    for index in range(4):
        _settle(db, f"group_rank(ts_rank(ebit,{120 + index}),industry)", is_pass=True,
                motif_id=f"ratio{index}")
    _settle(db, "rank(close)", is_pass=True)
    archive.rebuild(db)
    pool = archive.exploitation_parents(db, count=4)
    families = collections.Counter(str(row["signal_family"]) for row in pool)
    assert max(families.values()) <= math.ceil(4 / 3), "one family must not own the lineage pool"


# ---------------------------------------------------------------------------
# The conditioned plan itself
# ---------------------------------------------------------------------------


def test_conditioned_plan_records_the_evidence_behind_its_allocation(db, catalog):
    for index in range(4):
        _settle(db, f"group_rank(ts_rank(ebit,{20 + index}),industry)", motif_id="ratio")
    for index in range(4):
        _settle(db, f"rank(ts_delta(open,{30 + index}))", motif_id="change", is_pass=False)

    gen = generator.CandidateGenerator(db, catalog, seed=4)
    prior = gen.quality_prior()
    plan = gen.plan(campaign_id="cond", budget=20, seed=4, mode="mixed", prior=prior)
    assert plan.prior_version == qp.QUALITY_PRIOR_VERSION
    assert plan.quality_allocation["contexts"]
    assert plan.mode_weights and sum(plan.mode_weights.values()) == pytest.approx(1.0)
    assert sum(plan.motif_allocation.values()) == 20
    assert plan.as_dict()["quality_allocation"]["version"] == qp.QUALITY_PRIOR_VERSION

    unconditioned = gen.plan(campaign_id="cond", budget=20, seed=4, mode="mixed")
    assert unconditioned.prior_version == ""
    assert unconditioned.quality_allocation == {}


def test_conditioned_generation_still_materializes_its_budget_and_stays_reproducible(db, catalog):
    _settle(db, "group_rank(ts_rank(ebit,126),industry)", motif_id="ratio")
    first = generator.CandidateGenerator(db, catalog, seed=6).generate(
        campaign_id="cond-gen", count=24, seed=6, strategy="mixed",
    )
    second = generator.CandidateGenerator(db, catalog, seed=6).generate(
        campaign_id="cond-gen", count=24, seed=6, strategy="mixed",
    )
    assert first[0].prior_version == qp.QUALITY_PRIOR_VERSION
    assert len(first[1]) == 24
    assert [p.expression for p in first[1]] == [p.expression for p in second[1]]
    assert {proposal.generation_mode for proposal in first[1]} <= {"explore", "exploit", "mutate", "crossover"}


def test_conditioning_can_be_switched_off_for_a_control_arm(db, catalog):
    _settle(db, "group_rank(ts_rank(ebit,126),industry)")
    plan, proposals = generator.CandidateGenerator(db, catalog, seed=7).generate(
        campaign_id="control", count=12, seed=7, strategy="mixed", quality_conditioned=False,
    )
    assert plan.prior_version == ""
    assert len(proposals) == 12


# ---------------------------------------------------------------------------
# P21.1/P20.2: the emitted shape is conditioned, with a bounded paid share
# ---------------------------------------------------------------------------


def _settle_operator(db, operator, *, is_pass, index):
    """Settle one candidate whose emitted root operator is ``operator``."""
    if operator == "add":
        expression = (f"add(ts_rank(ebit,{60 + index}),ts_rank(close,{60 + index}))")
    else:
        expression = f"group_rank(ts_rank(ebit,{60 + index}),subindustry)"
    _settle(db, expression, motif_id=operator, is_pass=is_pass)


def test_operator_evidence_reads_only_cells_with_minimum_evidence():
    assert generation_policy_operator_evidence(None) == {}
    assert generation_policy_operator_evidence(qp.QualityPrior()) == {}
    assert generation_policy_operator_evidence(_StubPrior({})) == {}, (
        "a prior without the operator level yields no preference at all"
    )
    prior = qp.QualityPrior.from_rows([
        {"decision": "KEEP", "simulated": True, "is_pass": True, "outer_operator": "add"}
        for _ in range(6)
    ] + [
        {"decision": "KEEP", "simulated": True, "is_pass": False, "outer_operator": "divide"}
        for _ in range(6)
    ] + [
        {"decision": "KEEP", "simulated": True, "is_pass": True, "outer_operator": "hump"}
        for _ in range(2)
    ], min_evidence=3)
    evidence = generation_policy_operator_evidence(prior)
    assert evidence == {"add": 1.0, "divide": 0.0}, "a thin cell is not a verdict"


def test_the_paid_shape_is_preferred_inside_a_bounded_share_of_explore_seats(db, catalog):
    for index in range(4):
        _settle_operator(db, "add", is_pass=True, index=index)
        _settle_operator(db, "group_rank", is_pass=False, index=index)

    def shape_counts(quality_conditioned):
        _, proposals = generator.CandidateGenerator(db, catalog, seed=31).generate(
            campaign_id=f"paid-{quality_conditioned}", count=40, seed=31, strategy="mixed",
            quality_conditioned=quality_conditioned,
        )
        return collections.Counter(p.expression.split("(", 1)[0] for p in proposals), proposals

    conditioned, conditioned_proposals = shape_counts(True)
    control, _ = shape_counts(False)
    assert conditioned["add"] > control["add"], (
        "the only shape the ledger has paid for must be reachable in preference to novelty"
    )
    assert len(conditioned) >= 4, "and the search must not collapse onto it"
    assert len(conditioned_proposals) == 40

    gen = generator.CandidateGenerator(db, catalog, seed=31)
    _, proposals = gen.generate(campaign_id="paid-seats", count=40, seed=31, strategy="mixed")
    report = gen.plan(campaign_id="paid-seats", budget=40, seed=31, mode="mixed",
                      prior=gen.quality_prior()).operator_preference
    assert "add" in report["evidence"]
    assert report["evidence"]["add"] > report["earning_bar"] > 0.0
    assert report["seats"] <= math.ceil(report["explore_seats"] * policy.OPERATOR_PREFERENCE_SHARE)
    assert report["used"] <= report["seats"]

    # P16A regression: the reported number of paid seats is the total claimed across
    # the campaign, not just the last slot (the counter used to reset inside the loop).
    planned, materialized = gen.generate(
        campaign_id="paid-seats", count=40, seed=31, strategy="mixed",
        prior=gen.quality_prior(), screen=False,
    )
    assert len(planned.slots) == len(materialized) == 40
    explore_roots = [
        p.expression.split("(", 1)[0]
        for slot, p in zip(planned.slots, materialized)
        if slot.generation_mode == "explore"
    ]
    expected_claims = sum(
        report["evidence"].get(root, 0.0) >= report["earning_bar"]
        for root in explore_roots[:report["seats"]]
    )
    assert report["used"] == expected_claims


def test_a_conditioned_plan_still_materializes_its_whole_budget(db, catalog):
    for index in range(4):
        _settle_operator(db, "add", is_pass=True, index=index)
    _, proposals = generator.CandidateGenerator(db, catalog, seed=33).generate(
        campaign_id="paid-budget", count=30, seed=33, strategy="mixed",
    )
    assert len(proposals) == 30
    assert all(not p.parameters.get("planned_grammar_hash")
               or p.parameters.get("planned_structure_matched") for p in proposals)


# ---------------------------------------------------------------------------
# P21.2: warm-started selection uses the prior with an exploration floor
# ---------------------------------------------------------------------------


def test_warm_start_plan_scores_every_rung_and_keeps_a_floor(db, catalog):
    _settle(db, "group_rank(ts_rank(ebit,126),industry)")
    record = seed_bank.build_seed_bank(db)[0]
    gen = generator.CandidateGenerator(db, catalog, seed=2)
    prior = gen.quality_prior()
    plan = gen.warm_start_plan([record], 8, prior, exploration_floor=0.25)
    bands = {row["band"] for row in plan}
    assert "D0" not in bands, "an exact duplicate can never spend a simulation slot"
    assert bands == {"D1", "D2", "D3", "D4"}
    assert len(plan) == 8
    scores = [row["score"] for row in plan]
    assert scores == sorted(scores, reverse=True)
    assert all(row["look"]["level"] for row in plan)


def test_warm_start_plan_spreads_slots_across_seeds(db, catalog):
    for expression in (
        "group_rank(ts_rank(ebit,126),industry)",
        "group_rank(ts_rank(close,60),subindustry)",
        "winsorize(zscore(ts_delta(assets,126)),std=4)",
    ):
        _settle(db, expression)
    bank = seed_bank.build_seed_bank(db)
    gen = generator.CandidateGenerator(db, catalog, seed=2)
    plan = gen.warm_start_plan(bank, 12, gen.quality_prior(), exploration_floor=0.2)
    assert len({row["candidate_id"] for row in plan}) == 3
    assert len(plan) == 12


def test_warm_start_campaign_with_a_prior_reports_the_prior_and_the_selection(db, catalog):
    _settle(db, "group_rank(ts_rank(ebit,126),industry)")
    gen = generator.CandidateGenerator(db, catalog, seed=3)
    report, proposals = gen.warm_start_campaign(
        campaign_id="ws-prior", count=10, seed=3, prior=gen.quality_prior(),
    )
    assert report["prior_version"] == qp.QUALITY_PRIOR_VERSION
    assert report["selection"] and report["selection_count"] >= 1
    assert report["realized_bands"]
    assert report["slots_unfilled"] == 10 - len(proposals)
    assert proposals
    assert json.dumps(report, default=str)


def test_conditioned_exploitation_does_not_depend_on_the_catalog_shape(db, catalog):
    """The prior must be derivable from any ledger; an empty one cannot break the arm."""
    gen = generator.CandidateGenerator(db, catalog, seed=1)
    prior = gen.quality_prior()
    assert prior.global_evidence.simulations == 0
    report, proposals = gen.warm_start_campaign(campaign_id="ws-empty", count=5, seed=1, prior=prior)
    assert report["seeds"] == 0 and proposals == []
    assert prior.lookup(qp.Context(motif_id="ratio")).backed_off is True


# ---------------------------------------------------------------------------
# P22.1: penalize edits whose children destroy parent quality
# ---------------------------------------------------------------------------


def _settle_child(db, expression, *, parent_ids, operation, sharpe,
                  version="catalog-generator-v3"):
    outcome = db.queue_candidate(
        expression, {"decay": 8}, signal_family="pv1", generator_version=version,
        parent_ids=tuple(parent_ids), mutation_type=operation,
        mutation_parameters={"operation": operation, "realized_operation": operation},
    )
    claimed = db.claim_simulation("t", candidate_id=outcome.candidate_id)
    passed = sharpe >= 1.25
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": sharpe, "fitness": 1.0, "turnover": 0.1},
        checks=[{"name": "IS", "result": "PASS" if passed else "FAIL"}],
        brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return outcome.candidate_id


def test_operation_retention_measures_children_that_drop_below_their_parent(db):
    parent = _settle(db, "rank(close)", version="catalog-generator-v3", is_pass=True, sharpe=2.0)
    # Two add_component children dismantle the parent; one group_change child improves it.
    _settle_child(db, "rank(open)", parent_ids=[parent], operation="add_component", sharpe=0.3)
    _settle_child(db, "rank(high)", parent_ids=[parent], operation="add_component", sharpe=0.4)
    _settle_child(db, "group_rank(ts_rank(ebit,126),industry)", parent_ids=[parent],
                  operation="group_change", sharpe=2.5)
    report = seed_bank.operation_quality_retention(db, min_sample=1)
    assert report["add_component"]["destruction"] == pytest.approx(1.0)
    assert report["group_change"]["retention"] == pytest.approx(1.0)


def test_destruction_penalty_discounts_passes_without_going_negative():
    stats = {"add_component": (20, 10), "group_change": (20, 10)}
    destruction = {
        "add_component": {"destruction": 0.9, "low_confidence": False},
        "group_change": {"destruction": 0.0, "low_confidence": False},
    }
    adjusted = policy.penalized_operation_stats(stats, destruction, penalty=0.5)
    assert adjusted["add_component"][0] == 20
    assert adjusted["add_component"][1] < adjusted["group_change"][1]
    assert adjusted["add_component"][1] >= 0
    # An extra-strong penalty still cannot drive the count below zero.
    clamped = policy.penalized_operation_stats(stats, {
        "add_component": {"destruction": 1.0, "low_confidence": False},
    }, penalty=10.0)
    assert clamped["add_component"] == (20, 0)
    # A thin or unmeasured operation is not judged.
    assert policy.penalized_operation_stats(stats, {
        "add_component": {"destruction": 1.0, "low_confidence": True},
    })["add_component"] == (20, 10)
    assert policy.penalized_operation_stats(stats, {}) == {name: tuple(value) for name, value in stats.items()}


def test_the_allocator_spends_fewer_slots_on_a_destructive_operation():
    # The allocator's share cap floors at an even split, so the effect is only visible with
    # more operations than a 50/50 pair (which is pinned to an equal split by construction).
    operations = list(policy.V3_MUTATION_OPERATIONS)
    stats = {name: (40, 20) for name in operations}
    base = policy.allocate_mutation_operations(operations, 30, stats, seed=1)
    destruction = {
        name: {"destruction": 1.0 if name == "add_component" else 0.0,
               "low_confidence": False}
        for name in operations
    }
    adjusted = policy.allocate_mutation_operations(
        operations, 30, stats, seed=1, destruction=destruction,
    )
    assert sum(adjusted.values()) == 30
    assert adjusted["add_component"] < adjusted["group_change"]
    assert adjusted["add_component"] <= base["add_component"]


def test_the_plan_reports_the_retention_it_used(db, catalog):
    parent = _settle(db, "rank(close)", version="catalog-generator-v3", is_pass=True, sharpe=2.0)
    _settle_child(db, "rank(open)", parent_ids=[parent], operation="add_component", sharpe=0.2)
    plan = generator.CandidateGenerator(db, catalog, seed=5).plan(
        campaign_id="p22", budget=8, seed=5, mode="mutate",
    )
    assert plan.as_dict()["mutation_quality"] == plan.mutation_quality
    assert "add_component" in plan.mutation_quality


def _set_failure_reason(db, candidate_id, reason):
    """Write the submission-gate verdict a real SUBMISSION_READY parent carries."""
    import sqlite3

    with sqlite3.connect(str(db.path)) as conn:
        conn.execute("UPDATE candidates SET failure_reason=? WHERE id=?", (reason, candidate_id))


def test_a_submission_gate_failure_does_not_hijack_the_budgeted_edit(db, catalog):
    # Every SUBMISSION_READY parent stores a *submission* gate verdict (correlation), not an IS
    # metric failure. The old `if operation and not diagnosed` test treated that as a repair
    # case and silently replaced the allocated edit with the legacy `field_swap`, so a mutate
    # campaign kept spending capacity on the one edit the allocator had refused.
    parent = _settle(db, "add(group_rank(ts_rank(ebit,126),industry),"
                            "group_rank(ts_rank(sales,126),industry))",
                     version="catalog-generator-v3", generation_mode="mutate")
    _set_failure_reason(db, parent,
                        "gates failed: self-correlation 0.87 >= 0.7 and sharpe 2.30 is below "
                        "1.10x the correlated alpha's 2.10")
    gen = generator.CandidateGenerator(db, catalog, seed=3)
    row = db.get_candidate(parent)
    assert gen.diagnose(row) == ["SELF_CORRELATION"]  # the diagnoser itself is unchanged
    child = gen._mutate_child(row, campaign_id="p22-4", operation="group_change")
    assert child is not None
    realized = child.parameters.get("realized_operation") or child.parameters.get("operation")
    assert realized == "group_change"
    assert realized != "field_swap"
    assert child.parameters.get("operation_alternative_for") is None


def test_a_failed_pinned_edit_retries_a_budgeted_alternative(db, catalog):
    # A group-less parent cannot realize `group_change`. The slot must retry the campaign's own
    # budgeted edits before any unbudgeted fallback, and say which edit it substituted for.
    parent = _settle(db, "add(rank(close),rank(ebit))",
                     version="catalog-generator-v3", generation_mode="mutate")
    gen = generator.CandidateGenerator(db, catalog, seed=7)
    row = db.get_candidate(parent)
    assert gen.structural_mutation(row, operation="group_change", campaign_id="probe") is None
    child = gen._mutate_child(row, campaign_id="p22-4b", operation="group_change",
                              alternatives=("dataset_swap", "subtree_replace"))
    assert child is not None
    assert child.parameters.get("operation_alternative_for") == "group_change"
    realized = child.parameters.get("realized_operation") or child.parameters.get("operation")
    assert realized in policy.V3_MUTATION_OPERATIONS
    assert realized != "field_swap"


def test_a_repairable_metric_failure_still_gets_its_repair_first(db, catalog):
    # The repairable-failure precedence is preserved: a HIGH_TURNOVER parent is repaired rather
    # than structurally edited, so the P22.1 repair path is not disabled by the fix above.
    parent = _settle(db, "rank(close)", version="catalog-generator-v3", generation_mode="mutate",
                     sharpe=0.4, is_pass=False)
    _set_failure_reason(db, parent, "HIGH_TURNOVER")
    gen = generator.CandidateGenerator(db, catalog, seed=5)
    row = db.get_candidate(parent)
    assert "HIGH_TURNOVER" in gen.diagnose(row)
    child = gen._mutate_child(row, campaign_id="p22-4c", operation="group_change")
    assert child is not None
    assert child.mutation_type == "turnover_repair"


def test_seed_context_includes_the_proven_recipe_and_source(db, catalog):
    _settle(db, "group_rank(ts_rank(ebit,126),industry)",
            settings={"decay": 8, "truncation": 0.08, "neutralization": "SUBINDUSTRY"})
    record = seed_bank.build_seed_bank(db)[0]
    context = generator.CandidateGenerator(db, catalog)._warm_start_context(record)
    assert context.motif_id == "ratio"
    assert context.parent_quality_bucket == "proven_parent"
    assert context.recipe_bucket.startswith("t0.08")
    assert context.dataset and context.dataset != "unknown"
