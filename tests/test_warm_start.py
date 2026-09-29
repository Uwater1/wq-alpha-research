"""P19.2/P22.1 regression: warm-started exploitation from a proven seed.

The control experiment is only a control if three things hold: the campaign clock decides which
seed is allowed, every rung lands where it says it does (or says so), and an exploitation child
keeps the parent's economic core — the same sources with one thing changed.
"""
from __future__ import annotations

import json

import pytest

import canonical
import diversity
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


def _settle(db, expression, *, settings=None, sharpe=2.0, fitness=1.5, turnover=0.1,
            is_pass=True, parent_ids=(), motif_id="ratio", version="catalog-generator-v2",
            checks=None):
    outcome = db.queue_candidate(
        expression, settings or {"decay": 8, "truncation": 0.08},
        signal_family="pv1", generator_version=version, motif_id=motif_id,
        parent_ids=list(parent_ids), generation_mode="explore",
    )
    claimed = db.claim_simulation("t", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": sharpe, "fitness": fitness, "turnover": turnover},
        checks=checks or [{"name": "IS", "result": "PASS" if is_pass else "FAIL"}],
        brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return outcome.candidate_id


# ---------------------------------------------------------------------------
# Band schedule
# ---------------------------------------------------------------------------


def test_warm_start_schedule_is_exact_interleaved_and_deterministic():
    schedule = policy.warm_start_schedule(40, seed=3)
    assert len(schedule) == 40
    counts = {band: schedule.count(band) for band in seed_bank.BANDS}
    # D0 is not budgeted by default: an exact duplicate can never reach BRAIN, so it would
    # shrink the arm rather than spend a slot.
    assert counts["D0"] == 0
    assert counts["D2"] > counts["D4"] > 0 and counts["D1"] > counts["D4"]
    assert sum(counts.values()) == 40
    assert schedule == policy.warm_start_schedule(40, seed=3)
    assert policy.warm_start_schedule(0) == []
    # Interleaved: the first rungs are not all one band.
    assert len(set(schedule[:5])) >= 3


def test_warm_start_schedule_honours_an_explicit_weighting():
    schedule = policy.warm_start_schedule(10, {"D1": 1.0}, seed=0)
    assert schedule == ["D1"] * 10
    assert policy.warm_start_schedule(4, {"D0": 1.0}) == ["D0"] * 4
    # An all-zero weighting cannot divide by zero: it falls back to one rung.
    assert policy.warm_start_schedule(3, {"D2": 0.0}) == ["D1"] * 3


# ---------------------------------------------------------------------------
# The ladder rungs
# ---------------------------------------------------------------------------


def _seed_row(db, expression="add(group_rank(ts_rank(ebit,126),industry),group_rank(ts_rank(assets,126),subindustry))",
              **kwargs):
    _settle(db, expression, **kwargs)
    record = seed_bank.build_seed_bank(db)[0]
    return record, db.get_candidate(record.candidate_id)


def test_d0_is_an_exact_control(db, catalog):
    record, row = _seed_row(db)
    proposal = generator.CandidateGenerator(db, catalog).warm_start(
        row, band="D0", campaign_id="ws", seed=0,
    )
    assert proposal is not None
    assert proposal.parameters["requested_band"] == "D0"
    assert proposal.parameters["realized_band"] == "D0"
    assert proposal.parameters["band_mismatch"] is False
    assert proposal.parent_ids == (record.candidate_id,)
    assert proposal.generation_mode == "exploit"


def test_d1_moves_one_recipe_dimension_and_keeps_the_expression(db, catalog):
    record, row = _seed_row(db)
    proposal = generator.CandidateGenerator(db, catalog).warm_start(
        row, band="D1", campaign_id="ws", seed=1,
    )
    assert proposal is not None
    assert proposal.expression == record.expression, "a parameter-only child keeps the tree"
    changed = [
        key for key in seed_bank.SETTINGS_RECIPE_DIMENSIONS
        if proposal.settings.get(key) != row and proposal.settings.get(key) != json.loads(row["settings_json"]).get(key)
    ]
    assert len(changed) == 1, f"exactly one setting dimension may move, moved {changed}"
    assert proposal.parameters["realized_band"] == "D1"


def test_d3_substitutes_exactly_one_source_with_a_compatible_field(db, catalog):
    record, row = _seed_row(db)
    source = db.get_candidate(record.candidate_id)
    proposal = generator.CandidateGenerator(db, catalog).warm_start(
        row, band="D3", campaign_id="ws", seed=2,
    )
    assert proposal is not None
    before = set(diversity.derive_source_profile(str(source["normalized_expression"]), catalog)["field_ids"])
    after = set(proposal.source_profile["field_ids"])
    assert len(after - before) == 1, "a transfer replaces one source, not several"
    assert len(before - after) == 1
    replacement = next(iter(after - before))
    original = next(iter(before - after))
    assert catalog.get(replacement).field_type == catalog.get(original).field_type
    assert set(proposal.source_profile["datasets"]) != set(
        diversity.derive_source_profile(str(source["normalized_expression"]), catalog)["datasets"]
    )
    # The rung is verified, not assumed.
    assert proposal.parameters["realized_band"] in {"D3", "D4"}
    assert proposal.parameters["band_mismatch"] is (proposal.parameters["realized_band"] != "D3")


def test_d2_one_structural_edit_keeps_the_sources(db, catalog):
    record, row = _seed_row(db)
    proposal = generator.CandidateGenerator(db, catalog).warm_start(
        row, band="D2", campaign_id="ws", seed=3,
    )
    assert proposal is not None
    assert proposal.expression != record.expression
    assert set(proposal.source_profile["field_ids"]) == set(record.fields)
    assert proposal.parameters["realized_band"] in {"D2", "D4"}
    # Whatever the edit was, the ladder label must agree with the measurement.
    assert proposal.parameters["band_mismatch"] is (proposal.parameters["realized_band"] != "D2")


def test_d4_rebuilds_the_same_sources_through_a_different_motif(db, catalog):
    record, row = _seed_row(db)
    proposal = generator.CandidateGenerator(db, catalog).warm_start(
        row, band="D4", campaign_id="ws", seed=4,
    )
    assert proposal is not None
    assert set(proposal.source_profile["field_ids"]) == set(record.fields)
    assert proposal.expression != record.expression
    assert proposal.parameters["realized_band"] == "D4"


def test_an_unknown_band_is_refused_rather_than_guessed(db, catalog):
    _, row = _seed_row(db)
    with pytest.raises(ValueError, match="unknown distance band"):
        generator.CandidateGenerator(db, catalog).warm_start(row, band="D9", campaign_id="ws")


# ---------------------------------------------------------------------------
# The campaign
# ---------------------------------------------------------------------------


def test_warm_start_campaign_materializes_its_budget_from_a_point_in_time_bank(db, catalog):
    seeded = _settle(db, "group_rank(ts_rank(ebit,126),industry)", sharpe=2.2)

    # Before the seed settled there was no proven region to exploit at all.
    cold, cold_proposals = generator.CandidateGenerator(db, catalog).warm_start_campaign(
        campaign_id="ws-arms", count=24, seed=5, as_of="2000-01-01T00:00:00",
    )
    assert cold["seeds"] == 0 and cold_proposals == []

    report, proposals = generator.CandidateGenerator(db, catalog).warm_start_campaign(
        campaign_id="ws-arms", count=24, seed=5,
    )
    assert report["seeds"] == 1
    assert report["planned_budget"] == 24
    assert report["materialized"] == len(proposals) <= 24
    # A single proven seed has a *finite* controlled neighborhood: the campaign reports the
    # slots it could not fill instead of silently shrinking the arm (equal-budget comparisons
    # depend on knowing the realized size).
    assert report["slots_unfilled"] == 24 - len(proposals)
    assert report["duplicates_dropped"] > 0
    assert report["band_mismatch"] <= len(proposals)
    assert set(report["requested_bands"]) <= set(seed_bank.BANDS)
    assert sum(report["realized_bands"].values()) == len(proposals)
    assert report["seeds_used"] == 1
    assert {proposal.parent_ids[0] for proposal in proposals} == {seeded}
    assert all(proposal.generation_mode == "exploit" for proposal in proposals)
    assert all(proposal.motif_id.startswith("warm_start:") for proposal in proposals)
    assert all(proposal.strategy == "exploit" for proposal in proposals)
    # Distinct *requests*: the same expression under different settings is different work for
    # BRAIN, so identity is the canonical key — exactly what the cache dedupes on.
    keys = {canonical.canonical_key(p.expression, p.settings) for p in proposals}
    assert len(keys) == len(proposals)


def test_warm_start_campaign_spreads_over_seeds_and_never_repeats_a_proposal(db, catalog):
    for expression in (
        "group_rank(ts_rank(ebit,126),industry)",
        "group_rank(ts_rank(close,60),subindustry)",
        "winsorize(zscore(ts_delta(assets,126)),std=4)",
    ):
        _settle(db, expression)

    report, proposals = generator.CandidateGenerator(db, catalog).warm_start_campaign(
        campaign_id="ws-spread", count=18, seed=6,
    )
    assert report["materialized"] == 18
    assert report["seeds_used"] == 3
    # Rounds robin across the three proven datasets instead of taking the best Sharpe first.
    assert len({p.source_profile["datasets"][0] for p in proposals}) >= 2
    keys = {json.dumps([p.expression, sorted(p.settings.items())], sort_keys=True) for p in proposals}
    assert len(keys) == 18
    assert report["duplicates_dropped"] <= report["attempts"]


def test_warm_start_is_deterministic_and_queueable_with_seed_lineage(db, catalog):
    seeded = _settle(db, "group_rank(ts_rank(ebit,126),industry)")
    first = generator.CandidateGenerator(db, catalog, seed=9).warm_start_campaign(
        campaign_id="ws-repro", count=12, seed=9,
    )
    second = generator.CandidateGenerator(db, catalog, seed=9).warm_start_campaign(
        campaign_id="ws-repro", count=12, seed=9,
    )
    assert [p.expression for p in first[1]] == [p.expression for p in second[1]]
    assert [p.settings for p in first[1]] == [p.settings for p in second[1]]

    outcomes = generator.CandidateGenerator(db, catalog, seed=9).queue("ws-repro", first[1])
    queued = [outcome for outcome in outcomes if outcome["action"] == "queued"]
    assert queued, "a warm-started campaign must produce queueable work"
    row = db.get_candidate(queued[0]["candidate_id"])
    assert row["generator_version"] == generator.GENERATOR_VERSION_V3
    assert json.loads(row["parent_ids_json"]) == [seeded]
    trial = db.trials("ws-repro")[-1]
    provenance = json.loads(trial["provenance_json"])
    assert provenance["generation_mode"] == "exploit"
    assert provenance["source_profile"]["field_ids"]


def test_warm_start_campaign_without_any_seed_materializes_nothing(db, catalog):
    report, proposals = generator.CandidateGenerator(db, catalog).warm_start_campaign(
        campaign_id="ws-empty", count=10, seed=0,
    )
    assert report["seeds"] == 0 and proposals == []
    assert report["materialized"] == 0


def test_warm_start_cli_reports_the_realized_ladder(db, catalog, capsys):
    _settle(db, "group_rank(ts_rank(ebit,126),industry)")
    assert generator.main([
        "generate", "--campaign", "ws-cli", "--count", "6", "--warm-start", "--dry-plan",
        "--db", str(db.path),
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["materialized"] == 6
    assert sum(report["realized_bands"].values()) == 6
    assert "band_mismatch" in report
