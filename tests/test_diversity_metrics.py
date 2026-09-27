"""Diversity measurement tests (Generator V3 P0).

Covers child-derived source profiles, family relabelling, entropy effective counts, and the
archive niche identity that must now preserve AST topology.
"""
from __future__ import annotations

import pytest

import archive
import canonical
import diversity
import expression_grammar as grammar
import research_db


@pytest.fixture()
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def test_single_dataset_profile_uses_the_dataset_as_family():
    profile = diversity.derive_source_profile("group_rank(ts_rank(close,60),subindustry)")
    assert profile["datasets"] == ["pv1"]
    assert profile["categories"] == ["pv"]
    assert profile["cross_dataset"] is False
    assert profile["primary_family"] == "pv1"
    assert profile["field_ids"] == ["close"]


def test_multi_dataset_profile_is_a_stable_composite():
    profile = diversity.derive_source_profile(
        "subtract(rank(est_eps),rank(assets))"
    )
    assert profile["cross_dataset"] is True
    assert profile["datasets"] == sorted(profile["datasets"])
    assert len(profile["datasets"]) >= 2
    assert profile["primary_family"] == "multi:" + "+".join(profile["datasets"])


def test_group_literals_are_not_economic_sources():
    profile = diversity.derive_source_profile("group_rank(ts_rank(close,60),subindustry)")
    assert "subindustry" not in profile["field_ids"]
    assert profile["datasets"] == ["pv1"]


def test_cross_dataset_child_never_keeps_the_parent_family():
    profile = diversity.derive_source_profile("subtract(rank(est_eps),rank(assets))")
    family = diversity.family_for_profile(profile, parent_family="analyst4")
    assert family == profile["primary_family"]
    assert family.startswith("multi:")


def test_same_source_child_may_keep_a_still_correct_parent_label():
    profile = diversity.derive_source_profile("rank(open)")  # pv1 / category pv
    assert diversity.family_for_profile(profile, parent_family="pv") == "pv"
    assert diversity.family_for_profile(profile, parent_family="fundamental6") == "pv1"


def test_unknown_field_keeps_the_parent_label():
    profile = diversity.derive_source_profile("rank(not_a_real_field_xyz)")
    assert profile["primary_family"] == "unknown"
    assert diversity.family_for_profile(profile, parent_family="pv") == "pv"


def test_effective_count_reflects_entropy_not_category_count():
    assert diversity.effective_count({"a": 95, "b": 5}) < 2.0
    assert diversity.effective_count({"a": 25, "b": 25, "c": 25, "d": 25}) == pytest.approx(4.0, abs=1e-3)
    assert diversity.effective_count({"only": 40}) == 1.0
    assert diversity.effective_count({}) == 0.0


def test_novelty_screen_keeps_new_work_and_skips_only_requested_duplicates():
    expression = "group_rank(ts_rank(close,60),subindustry)"
    metadata = diversity.load_field_metadata()
    key = canonical.canonical_key(expression, {"decay": 6})
    context = diversity.NoveltyContext(
        canonical_keys=frozenset({key}),
        skeleton_hashes=frozenset({canonical.skeleton_hash(expression)}),
        grammar_hashes=frozenset({grammar.grammar_skeleton_hash(expression, metadata)}),
        semantic_hashes=frozenset({grammar.semantic_skeleton_hash(expression, metadata)}),
        datasets=frozenset({"pv1"}),
        motifs=frozenset({"change"}),
        candidate_count=1,
    )
    report = diversity.screen_novelty(expression, canonical_key=key, settings={"decay": 6}, context=context)
    assert report.decision == diversity.DOWNWEIGHT
    assert report.exact_novel is False

    strict = diversity.screen_novelty(
        expression, canonical_key=key, settings={"decay": 6}, context=context, request_novelty=True,
    )
    assert strict.decision == diversity.SKIP_REDUNDANT

    fresh = diversity.screen_novelty("group_rank(ts_rank(open,20),industry)", context=context)
    assert fresh.decision == diversity.KEEP
    assert fresh.exact_novel is True


def test_archive_niche_preserves_topology_and_source_identity():
    same_a = archive.niche({"normalized_expression": "ts_mean(close,20)"})
    same_b = archive.niche({"normalized_expression": "ts_mean(open,126)"})
    different = archive.niche({"normalized_expression": "ts_std_dev(close,20)"})
    # Same topology, different exact fields and numeric parameters -> one niche.
    assert same_a["grammar_skeleton_hash"] == same_b["grammar_skeleton_hash"] == grammar.grammar_skeleton_hash("ts_mean(close,20)")
    assert same_a == same_b
    # Different topology -> a different niche even though the operator count is similar.
    assert different["grammar_skeleton_hash"] != same_a["grammar_skeleton_hash"]


def test_archive_niche_carries_v3_dimensions():
    dims = archive.niche({
        "normalized_expression": "rank(close)",
        "signal_family": "pv",
        "turnover": 0.08,
        "mutation_type": "motif_generation",
        "mutation_parameters_json": '{"motif_id": "ranked_level", "generation_mode": "explore"}',
    })
    assert dims["niche_version"] == archive.NICHE_VERSION
    assert dims["primary_family"] == "pv1"
    assert dims["dataset_set"] == "pv1"
    assert dims["category_set"] == "pv"
    assert dims["motif_id"] == "ranked_level"
    assert dims["generation_mode"] == "explore"
    assert dims["turnover_bucket"] == "medium"
    assert dims["cross_dataset"] is False
    assert "operator_set" in dims  # descriptive metadata only


def test_campaign_diversity_report_separates_every_dimension(db):
    import json

    def add(expression, *, motif, mode, family, status="IS_PASS", grammar_mode="motif_generation"):
        outcome = db.queue_candidate(
            expression, {"decay": 6}, signal_family=family, campaign_id="div-campaign",
            motif_id=motif, generation_mode=mode,
            grammar_skeleton_hash=grammar.grammar_skeleton_hash(expression),
            semantic_skeleton_hash=grammar.semantic_skeleton_hash(expression),
            source_profile=diversity.derive_source_profile(expression),
        )
        if status is None:
            return outcome.candidate_id
        db.query("UPDATE candidates SET status=? WHERE id=?", (status, outcome.candidate_id))
        return outcome.candidate_id

    # Two candidates share a grammar skeleton (same topology, different fields); one is distinct.
    add("group_rank(ts_rank(close,60),subindustry)", motif="group_relative", mode="explore", family="pv1")
    add("group_rank(ts_rank(open,60),subindustry)", motif="group_relative", mode="explore", family="pv1")
    add("rank(ts_delta(assets,126))", motif="momentum", mode="exploit", family="fundamental2")

    report = diversity.campaign_diversity_report(db, "div-campaign")

    assert report["trial_count"] == 3
    assert report["unique_exact_candidates"] == 3
    assert report["unique_grammar_skeletons"] == 2  # the two group_rank variants collapse
    assert report["unique_fields"] == 3
    assert report["unique_motifs"] == 2
    assert report["effective_motif_count"] < 2.0  # 2 of 3 use one motif
    assert report["effective_grammar_count"] < 2.0
    assert report["is_pass_by_motif"]["group_relative"] == 2
    assert report["generation_mode"] == {"explore": 2, "exploit": 1}
    assert report["duplicate_rate"] == 0.0
    assert json.dumps(report)  # JSON-serializable


def test_parent_child_distance_sees_parents_from_an_earlier_campaign(db):
    """Lineage is cross-campaign: a parent may be a previous campaign's elite."""
    import json

    import generator

    first = generator.CandidateGenerator(db, seed=3)
    _, proposals = first.generate(campaign_id="lineage-parents", count=12, seed=3, strategy="explore")
    outcomes = first.queue("lineage-parents", proposals)
    parent_id = outcomes[0]["candidate_id"]
    parent = db.get_candidate(parent_id)

    child_expression = generator.CandidateGenerator(db, seed=1).mutate(
        parent, count=1, campaign_id="lineage-child",
    )[0].expression
    db.queue_candidate(
        child_expression, {"decay": 6}, signal_family=str(parent["signal_family"]),
        campaign_id="lineage-child", parent_id=parent_id, parent_ids=(parent_id,),
        motif_id="mutation:window", generation_mode="mutate",
        grammar_skeleton_hash=grammar.grammar_skeleton_hash(child_expression),
        semantic_skeleton_hash=grammar.semantic_skeleton_hash(child_expression),
        source_profile=diversity.derive_source_profile(child_expression),
    )

    report = diversity.campaign_diversity_report(db, "lineage-child")
    assert report["parent_child_pairs"] == 1
    assert report["median_parent_child_grammar_distance"] is not None
    assert json.dumps(report)
