"""P18 regression: the diagnostics must localise a failure without inventing confidence.

These tests pin the three properties the roadmap's exit gate depends on: a bucket is never
ranked below the minimum sample, the matched-cohort attribution blames the dimension that
actually holds the gap, and no future outcome can enter a report.
"""
from __future__ import annotations

import json

import pytest

import quality_diagnostics as diag
import research_db


@pytest.fixture()
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _row(**overrides):
    row = {
        "trial_id": 1, "candidate_id": 1, "campaign_id": "c", "generator_version": "v3",
        "scope": {"region": "USA", "universe": "TOP3000", "delay": 1},
        "settled_at": "2026-09-01T00:00:00", "created_at": "2026-09-01T00:00:00",
        "decision": "KEEP", "generation_mode": "explore", "strategy": "explore",
        "motif_id": "ratio", "mutation_operation": "none", "mutation_type": "motif_generation",
        "recipe": {"lookback": 126, "decay": 6, "truncation": 0.1, "neutralization": "SUBINDUSTRY"},
        "recipe_bucket": "t0.1|d6|lb126|SUBINDUSTRY", "truncation": 0.1, "decay": 6,
        "lookback": 126, "neutralization": "SUBINDUSTRY", "fields": ["close"],
        "datasets": ["pv1"], "categories": ["price"], "primary_dataset": "pv1",
        "primary_category": "price", "semantic_roles": ("generic",),
        "semantic_role_signature": "generic", "cross_dataset": False, "field_count": 1,
        "grammar_skeleton_hash": "g1", "semantic_skeleton_hash": "s1", "parent_ids": [],
        "shape_family": "group_rank|1src", "signal_family": "pv1", "status": "REJECTED",
        "simulated": True, "sharpe": 0.0, "fitness": 0.0, "turnover": 0.1, "is_pass": False,
        "corr_pass": False, "error": "", "failing_checks": ("LOW_SHARPE",),
        "failure_region": "low_sharpe", "parent_quality_bucket": "no_parent",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Rate estimation
# ---------------------------------------------------------------------------


def test_wilson_interval_makes_a_one_trial_bucket_visibly_uninformative():
    assert diag.wilson_interval(0, 0) == (0.0, 1.0)
    low, high = diag.wilson_interval(1, 1)
    assert low > 0.0 and high == 1.0 and (high - low) > 0.5
    low, high = diag.wilson_interval(9, 112)
    assert 0.04 < low < 9 / 112 < high < 0.16  # the historical V2 baseline is not "0%..23%"


def test_summarize_reports_quantiles_shares_and_a_low_confidence_flag():
    rows = [
        _row(trial_id=1, sharpe=2.5, fitness=1.9, is_pass=True, status="IS_PASS",
             failing_checks=(), failure_region="pass"),
        _row(trial_id=2, sharpe=-0.4, fitness=-0.1, failing_checks=("LOW_SHARPE", "LOW_FITNESS"),
             failure_region="low_sharpe"),
        _row(trial_id=3, sharpe=0.2, fitness=0.1, failing_checks=("HIGH_TURNOVER",),
             failure_region="turnover"),
    ]
    summary = diag.summarize(rows, min_sample=5)
    assert summary["attempts"] == 3 and summary["simulations"] == 3
    assert summary["is_pass"] == 1
    assert summary["sharpe"]["p50"] == 0.2 and summary["sharpe"]["max"] == 2.5
    assert summary["failure_region"]["low_sharpe"] == 1
    assert summary["failure_region"]["turnover"] == 1
    assert summary["failure_region"]["pass"] == 1
    assert summary["low_confidence"] is True
    assert diag.summarize(rows, min_sample=3)["low_confidence"] is False


def test_a_failed_simulation_is_attributed_to_one_region_only():
    checks = [
        {"name": "LOW_SHARPE", "result": "FAIL"},
        {"name": "LOW_FITNESS", "result": "FAIL"},
        {"name": "HIGH_TURNOVER", "result": "FAIL"},
        {"name": "SELF_CORRELATION", "result": "PENDING"},
    ]
    assert diag.failure_region(simulated=True, error=None, checks=checks) == "low_sharpe"
    assert diag.failure_region(simulated=False, error=None, checks=[]) == "unsimulated"
    assert diag.failure_region(simulated=True, error="SyntaxError", checks=[]) == "invalid_or_unsupported"
    assert diag.failure_region(simulated=True, error=None, checks=[]) == "pass"
    assert diag.failure_region(
        simulated=True, error=None, checks=[{"name": "BRAND_NEW_CHECK", "result": "FAIL"}],
    ) == "other_check"


def test_unknown_check_names_are_reported_not_guessed():
    rows = [_row(failing_checks=("BRAND_NEW_CHECK",), failure_region="other_check")]
    taxonomy = diag.failure_taxonomy(rows)
    assert taxonomy["overall"]["failing_check"] == {"BRAND_NEW_CHECK": 1}
    assert "other_check" in taxonomy["regions"]


# ---------------------------------------------------------------------------
# Outcome cube
# ---------------------------------------------------------------------------


def test_outcome_cube_never_ranks_a_bucket_from_one_or_two_trials():
    rows = [
        # A lucky 1/1 bucket must not be allowed to look like the best hypothesis.
        _row(trial_id=1, motif_id="lucky", is_pass=True, status="IS_PASS", failing_checks=(),
             failure_region="pass"),
        _row(trial_id=2, motif_id="real", sharpe=1.4, is_pass=True, status="IS_PASS",
             failing_checks=(), failure_region="pass"),
        _row(trial_id=3, motif_id="real", sharpe=1.3, is_pass=True, status="IS_PASS",
             failing_checks=(), failure_region="pass"),
        _row(trial_id=4, motif_id="real", sharpe=0.2),
        _row(trial_id=5, motif_id="real", sharpe=-0.1),
        _row(trial_id=6, motif_id="real", sharpe=0.3),
        _row(trial_id=7, motif_id="junk", sharpe=-0.3),
        _row(trial_id=8, motif_id="junk", sharpe=-0.2),
        _row(trial_id=9, motif_id="junk", sharpe=0.1),
    ]
    cube = diag.outcome_cube(rows, ("motif_id",), min_sample=3)
    by_bucket = {cell["bucket"]: cell for cell in cube["motif_id"]}
    assert by_bucket["lucky"]["low_confidence"] is True
    assert by_bucket["real"]["low_confidence"] is False
    assert by_bucket["real"]["simulations"] == 5
    assert by_bucket["junk"]["is_pass_rate"] == 0.0
    # The thin bucket is excluded from the ranking rather than winning it.
    separation = diag.separation_analysis(rows, ("motif_id",), min_sample=3)
    assert separation["dimensions"][0]["dimension"] == "motif_id"
    assert separation["dimensions"][0]["well_sampled_buckets"] == 2
    assert separation["dimensions"][0]["best_bucket"] == "real"
    assert separation["dimensions"][0]["worst_bucket"] == "junk"
    assert separation["dimensions"][0]["rate_range"] == pytest.approx(0.4)


def test_simulation_free_trials_are_attempts_not_negative_outcomes():
    """A SKIP_REDUNDANT proposal was never simulated: it is not a performance observation."""
    rows = [
        _row(trial_id=1, simulated=False, decision="SKIP_REDUNDANT", status="", sharpe=None,
             fitness=None, turnover=None, failing_checks=(), failure_region="unsimulated"),
        _row(trial_id=2, sharpe=1.5, is_pass=True, status="IS_PASS", failing_checks=()),
    ]
    summary = diag.summarize(rows)
    assert summary["attempts"] == 2 and summary["simulations"] == 1
    assert summary["is_pass_rate"] == 1.0


# ---------------------------------------------------------------------------
# Matched cohorts: the attribution must blame the dimension that holds the gap
# ---------------------------------------------------------------------------


def _cohort_rows(*, version, dataset, shape, recipe_bucket, passes, total):
    rows = []
    for index in range(total):
        rows.append(_row(
            trial_id=int(f"{len(rows) + 1}{index}"), generator_version=version,
            primary_dataset=dataset, shape_family=shape, recipe_bucket=recipe_bucket,
            truncation=0.08 if recipe_bucket.startswith("t0.08") else 0.1,
            is_pass=index < passes, status="IS_PASS" if index < passes else "REJECTED",
            failing_checks=() if index < passes else ("LOW_SHARPE",),
            failure_region="pass" if index < passes else "low_sharpe",
        ))
    return rows


def test_matched_cohorts_blames_the_source_when_only_the_source_differs():
    # Same shape, same recipe, same scope: the baseline only reaches better datasets.
    rows = (
        _cohort_rows(version="v2", dataset="analyst4", shape="add|2src", recipe_bucket="t0.08|d8|lb126|SUBINDUSTRY", passes=6, total=10)
        + _cohort_rows(version="v3", dataset="pv1", shape="add|2src", recipe_bucket="t0.08|d8|lb126|SUBINDUSTRY", passes=0, total=10)
    )
    cohorts = diag.matched_cohorts(rows, baseline="v2", target="v3")
    assert cohorts["levels"]["scope"]["gap"] == pytest.approx(-0.6)
    # Disjoint sources: there is no like-for-like comparison left at this level.
    assert cohorts["levels"]["scope+source"]["gap"] is None
    assert cohorts["levels"]["scope+source"]["unmatched"] is True
    attribution = cohorts["gap_attribution"]
    assert attribution["field_or_source_gap"] == pytest.approx(-0.6)
    assert attribution["expression_or_motif_gap"] == pytest.approx(0.0)
    assert attribution["recipe_or_settings_gap"] == pytest.approx(0.0)
    assert attribution["residual_search_policy_gap"] == pytest.approx(0.0)


def test_matched_cohorts_blames_the_recipe_when_sources_and_shapes_are_held_fixed():
    rows = (
        _cohort_rows(version="v2", dataset="analyst4", shape="add|2src", recipe_bucket="t0.08|d8|lb126|SUBINDUSTRY", passes=5, total=10)
        + _cohort_rows(version="v3", dataset="analyst4", shape="add|2src", recipe_bucket="t0.15|d20|lb252|SECTOR", passes=0, total=10)
    )
    cohorts = diag.matched_cohorts(rows, baseline="v2", target="v3")
    # Sources and shapes are identical, so those levels close nothing; the recipe buckets are
    # disjoint, so the whole remaining -0.5 is attributed to the recipe/settings dimension.
    assert cohorts["gap_attribution"]["field_or_source_gap"] == pytest.approx(0.0)
    assert cohorts["gap_attribution"]["expression_or_motif_gap"] == pytest.approx(0.0)
    assert cohorts["gap_attribution"]["recipe_or_settings_gap"] == pytest.approx(-0.5)
    assert cohorts["gap_attribution"]["residual_search_policy_gap"] == pytest.approx(0.0)


def test_matched_cohorts_compare_only_strata_present_in_both_versions():
    rows = (
        _cohort_rows(version="v2", dataset="analyst4", shape="add|2src", recipe_bucket="t0.08|d8|lb126|SUBINDUSTRY", passes=6, total=10)
        # A V3 stratum with no V2 counterpart must not be able to move the source-level gap.
        + _cohort_rows(version="v3", dataset="pv1", shape="group_rank|1src", recipe_bucket="t0.1|d6|lb20|SECTOR", passes=10, total=10)
    )
    cohorts = diag.matched_cohorts(rows, baseline="v2", target="v3")
    # The scope level is shared, so the unmatched-but-in-scope comparison still runs there.
    assert cohorts["levels"]["scope"]["target"]["simulations"] == 10
    # At the source level only "analyst4" exists, so the V3-only stratum cannot move anything.
    assert cohorts["levels"]["scope+source"]["shared_strata"] == 0
    assert cohorts["levels"]["scope+source"]["target"]["simulations"] == 0
    assert cohorts["levels"]["scope+source"]["gap"] is None
    assert cohorts["levels"]["scope+source"]["target_coverage"] == 0.0


# ---------------------------------------------------------------------------
# Point-in-time safety and artifact hygiene
# ---------------------------------------------------------------------------


def _settle(db, expression, *, version, sharpe, is_pass, checks, campaign="c1", settings=None):
    outcome = db.queue_candidate(
        expression, settings or {"decay": 6}, signal_family="pv1",
        generator_version=version, generation_mode="explore", motif_id="ratio",
        campaign_id=campaign,
    )
    claimed = db.claim_simulation("t", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": sharpe, "fitness": sharpe / 2, "turnover": 0.1},
        checks=checks, brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return outcome.candidate_id


def test_report_is_point_in_time_safe_and_the_clock_is_enforced(db):
    _settle(db, "group_rank(ts_rank(close,60),subindustry)", version="catalog-generator-v2",
            sharpe=1.9, is_pass=True, checks=[{"name": "LOW_SHARPE", "result": "PASS"}])
    _settle(db, "rank(ts_delta(assets,126))", version="catalog-generator-v3",
            sharpe=-0.2, is_pass=False, checks=[{"name": "LOW_SHARPE", "result": "FAIL"}])

    report = diag.build_report(db)
    assert report["by_version"]["catalog-generator-v2"]["is_pass"] == 1
    assert report["by_version"]["catalog-generator-v3"]["is_pass"] == 0
    # Every included trial settled at or before the report clock.
    assert report["included_trial_ids"]
    diag.leakage_check(report, diag.load_evidence(db))

    # A clock before any settlement must produce an empty, honest report.
    early = diag.build_report(db, as_of="2000-01-01T00:00:00")
    assert early["included_trial_ids"] == []


def test_leakage_check_rejects_a_report_that_used_a_later_outcome():
    rows = [_row(trial_id=7, settled_at="2026-09-10T00:00:00")]
    with pytest.raises(ValueError, match="leakage"):
        diag.leakage_check({"as_of": "2026-09-01T00:00:00", "included_trial_ids": [7]}, rows)
    with pytest.raises(ValueError, match="unverifiable"):
        diag.leakage_check({"included_trial_ids": [7]}, rows)


def test_artifact_carries_no_expression_and_no_alpha_id(db):
    _settle(db, "group_rank(ts_rank(close,60),subindustry)", version="catalog-generator-v2",
            sharpe=1.9, is_pass=True, checks=[])
    report = diag.build_report(db)
    text = json.dumps(diag._sanitize(report), sort_keys=True)
    assert "group_rank(ts_rank(close,60),subindustry)" not in text
    assert "brain_alpha_id" not in text and "normalized_expression" not in text
    assert "A1" not in text.replace('"A1"', "")
    # Catalog identifiers (datasets/fields/motifs) are public reference data and are kept.
    assert "catalog-generator-v2" in text


def test_diagnostics_cli_writes_the_artifact(db, tmp_path, capsys):
    _settle(db, "rank(close)", version="catalog-generator-v2", sharpe=1.4, is_pass=True, checks=[])
    _settle(db, "rank(open)", version="catalog-generator-v3", sharpe=0.1, is_pass=False,
            checks=[{"name": "LOW_SHARPE", "result": "FAIL"}])
    out = tmp_path / "quality_diagnostics.json"
    assert diag.main(["--db", str(db.path), "--out", str(out)]) == 0
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["diagnostics_version"] == diag.DIAGNOSTICS_VERSION
    assert artifact["outcome_cube"]["generator_version"]
    assert artifact["matched_cohorts"]["gap_attribution"]["residual_search_policy_gap"] is not None
    assert "matched-cohort gap attribution" in capsys.readouterr().out
