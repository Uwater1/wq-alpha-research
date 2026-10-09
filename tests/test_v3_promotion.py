"""P17/P25 regression: V3 is the default generator and the promotion gate is honest.

Two contracts are protected here. The CLI's ``generate`` command must default to the
quality-conditioned V3 generator while keeping the V2 template path reachable through
``--legacy-v2`` (that is what "promote" means operationally). And the promotion gate must
report an unmeasurable item as ``unknown`` rather than reading as a pass, so a promotion
cannot be claimed off a metric the ledger never produced.
"""
from __future__ import annotations

import pytest

import generator
import promotion_gate
import research_db


@pytest.fixture()
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _settle(db, expression, *, version, is_pass, campaign="ledger", grammar=None,
            semantic=None, motif="ratio", dataset="analyst4", turnover=0.1,
            turnover_check=None):
    checks = [{"name": "IS", "result": "PASS" if is_pass else "FAIL"}]
    if turnover_check is not None:
        checks.append({"name": "HIGH_TURNOVER", "result": turnover_check})
    outcome = db.queue_candidate(
        expression, {"decay": 8, "truncation": 0.08}, signal_family="pv1",
        generator_version=version, motif_id=motif, campaign_id=campaign,
        grammar_skeleton_hash=grammar, semantic_skeleton_hash=semantic,
        source_profile={"datasets": [dataset]},
    )
    claimed = db.claim_simulation("t", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": 1.8 if is_pass else 0.2, "fitness": 1.4, "turnover": turnover},
        checks=checks, brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return outcome.candidate_id


# ---------------------------------------------------------------------------
# P17: the CLI default is V3, and V2 stays reachable
# ---------------------------------------------------------------------------


def test_generate_cli_defaults_to_the_v3_generator(tmp_path, capsys):
    path = tmp_path / "research.db"
    assert generator.main(
        ["generate", "--campaign", "cli-default", "--count", "6", "--seed", "1", "--db", str(path)]
    ) == 0
    capsys.readouterr()
    with research_db.ResearchDB.open(path) as store:
        rows = store.query(
            "SELECT generator_version FROM candidates WHERE campaign_id='cli-default'"
        )
    assert rows, "the default campaign must queue candidates"
    assert {row["generator_version"] for row in rows} == {generator.GENERATOR_VERSION_V3}


def test_a_v2_only_flag_implicitly_routes_to_the_legacy_generator(tmp_path, capsys):
    # ``--truncation``/``--all-fields``/``--dataset``/``--template`` only exist on the V2 path,
    # so a coverage invocation must keep working without an explicit ``--legacy-v2``.
    path = tmp_path / "research.db"
    assert generator.main(
        ["generate", "--campaign", "cli-coverage", "--count", "4", "--truncation", "0.05",
         "--seed", "1", "--db", str(path)]
    ) == 0
    capsys.readouterr()
    with research_db.ResearchDB.open(path) as store:
        rows = store.query(
            "SELECT generator_version FROM candidates WHERE campaign_id='cli-coverage'"
        )
    assert rows
    assert {row["generator_version"] for row in rows} == {generator.LEGACY_GENERATOR_VERSION}


def test_legacy_v2_flag_still_produces_v2_rows(tmp_path, capsys):
    path = tmp_path / "research.db"
    assert generator.main(
        ["generate", "--campaign", "cli-legacy", "--legacy-v2", "--count", "6",
         "--seed", "1", "--db", str(path)]
    ) == 0
    capsys.readouterr()
    with research_db.ResearchDB.open(path) as store:
        rows = store.query(
            "SELECT generator_version FROM candidates WHERE campaign_id='cli-legacy'"
        )
    assert rows
    assert {row["generator_version"] for row in rows} == {generator.LEGACY_GENERATOR_VERSION}


# ---------------------------------------------------------------------------
# P25: arm metrics
# ---------------------------------------------------------------------------


def test_arm_metrics_reports_the_rate_and_its_interval(db):
    for index in range(4):
        _settle(db, f"rank(close) + {index}", version="catalog-generator-v3", is_pass=index < 1,
                campaign="a", turnover_check="PASS")
    rows = [dict(row) for row in db.query(promotion_gate._LEDGER_SQL)]
    metrics = promotion_gate.arm_metrics(rows)
    assert metrics["simulations"] == 4
    assert metrics["is_pass"] == 1
    assert metrics["is_pass_per_simulation"] == pytest.approx(0.25)
    assert metrics["simulations_per_is_pass"] == pytest.approx(4.0)
    assert metrics["ci95"][0] < 0.25 < metrics["ci95"][1]
    assert metrics["low_confidence"] is True  # 4 < the default minimum sample


def test_turnover_failure_counted_from_the_check_and_from_the_metric(db):
    assert promotion_gate._turnover_failed({"checks_json": "[]", "turnover": 0.30}) is True
    assert promotion_gate._turnover_failed(
        {"checks_json": '[{"name": "HIGH_TURNOVER", "result": "FAIL"}]', "turnover": 0.1}
    ) is True
    assert promotion_gate._turnover_failed(
        {"checks_json": '[{"name": "IS", "result": "FAIL"}]', "turnover": 0.1}
    ) is False


def test_survivor_diversity_counts_only_survivors(db):
    for index in range(3):
        _settle(db, f"rank(close) + {index}", version="catalog-generator-v3", is_pass=True,
                grammar=f"g{index}", semantic=f"s{index}", motif=f"m{index}", dataset=f"d{index}")
    _settle(db, "rank(close)", version="catalog-generator-v3", is_pass=False, grammar="gx")
    rows = [dict(row) for row in db.query(promotion_gate._LEDGER_SQL)]
    diversity = promotion_gate.survivor_diversity(rows)
    assert diversity["survivors"] == 3
    assert diversity["effective_grammar"] == pytest.approx(3.0)
    assert diversity["effective_dataset"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# P25.3: the gate refuses to pass what it cannot measure
# ---------------------------------------------------------------------------


def test_efficiency_passes_only_above_the_material_ratio(db):
    for index in range(4):
        _settle(db, f"rank(close) + {index}", version="catalog-generator-v2",
                is_pass=index == 0, campaign="v2")
    for index in range(4):
        _settle(db, f"rank(open) + {index}", version="catalog-generator-v3",
                is_pass=index < 3, campaign="v3")
    payload = promotion_gate.report(db, min_sample=1)
    checklist = payload["gate"]["checklist"]
    assert checklist["materially_better_efficiency"]["status"] == "pass"
    assert checklist["materially_better_efficiency"]["ratio"] == pytest.approx(3.0)
    assert checklist["survivor_diversity_preserved"]["status"] == "pass"
    # Correlation/robustness and config reproducibility cannot be read from this ledger alone.
    assert checklist["correlation_and_robustness"]["status"] == "unknown"
    assert checklist["reproducible_from_config"]["status"] == "unknown"
    assert payload["gate"]["promoted"] is False
    assert set(payload["gate"]["blocking"]) == {
        "correlation_and_robustness", "reproducible_from_config",
        "no_point_in_time_leakage", "matched_live_comparison",
    }
    assert checklist["no_point_in_time_leakage"]["status"] == "unknown"
    assert checklist["matched_live_comparison"]["status"] == "unknown"
    assert payload["comparison_design"] == "pooled_historical_by_generator_version_unmatched"


def test_a_thin_arm_cannot_claim_a_promotion(db):
    _settle(db, "rank(close)", version="catalog-generator-v2", is_pass=False, campaign="v2")
    _settle(db, "rank(open)", version="catalog-generator-v3", is_pass=True, campaign="v3")
    payload = promotion_gate.report(db, min_sample=30)
    item = payload["gate"]["checklist"]["materially_better_efficiency"]
    assert item["status"] == "unknown"
    assert "insufficient" in item["reason"]


def test_a_losing_arm_fails_the_efficiency_gate(db):
    for index in range(4):
        _settle(db, f"rank(close) + {index}", version="catalog-generator-v2",
                is_pass=index < 3, campaign="v2")
    for index in range(4):
        _settle(db, f"rank(open) + {index}", version="catalog-generator-v3",
                is_pass=index == 0, campaign="v3")
    payload = promotion_gate.report(db, min_sample=1)
    assert payload["gate"]["checklist"]["materially_better_efficiency"]["status"] == "fail"


def test_report_is_point_in_time_bounded(db):
    for index in range(3):
        _settle(db, f"rank(close) + {index}", version="catalog-generator-v3", is_pass=True,
                campaign="v3")
    clock = db.query("SELECT MAX(completed_at) AS last FROM simulations")[0]["last"]
    payload = promotion_gate.report(db, as_of=clock, min_sample=1)
    assert payload["target_metrics"]["simulations"] == 3
    cold = promotion_gate.report(db, as_of="2000-01-01T00:00:00", min_sample=1)
    assert cold["target_metrics"]["simulations"] == 0


def test_legacy_v2_dry_plan_never_queues_candidates_or_research_trials(tmp_path, capsys):
    path = tmp_path / "dry-v2.db"
    assert generator.main([
        "generate", "--campaign", "dry-v2", "--legacy-v2",
        "--dry-plan", "--count", "6", "--seed", "3", "--db", str(path),
    ]) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["generator_version"] == generator.LEGACY_GENERATOR_VERSION
    assert payload["queued"] == 0
    assert payload["planned_budget"] > 0
    assert "expression" not in str(payload)
    with research_db.ResearchDB.open(path) as store:
        assert store.query("SELECT COUNT(*) AS n FROM candidates")[0]["n"] == 0
        assert store.query("SELECT COUNT(*) AS n FROM research_trials")[0]["n"] == 0


def test_v2_only_dry_plan_flags_route_implicitly_without_queue_writes(tmp_path, capsys):
    path = tmp_path / "dry-v2-implied.db"
    assert generator.main([
        "generate", "--campaign", "dry-v2-implied", "--template", generator.SIGNAL_TEMPLATES[0].id,
        "--dry-plan", "--count", "4", "--seed", "1", "--db", str(path),
    ]) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["generator_version"] == generator.LEGACY_GENERATOR_VERSION
    with research_db.ResearchDB.open(path) as store:
        assert store.query("SELECT COUNT(*) AS n FROM candidates")[0]["n"] == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_seed_as_of_does_not_silently_time_travel_for_non_warm_start(tmp_path, legacy):
    argv = ["generate", "--campaign", "bad-clock", "--dry-plan",
            "--seed-as-of", "2026-01-01T00:00:00", "--db", str(tmp_path / "bad.db")]
    if legacy:
        argv.append("--legacy-v2")
    with pytest.raises(SystemExit) as exc:
        generator.main(argv)
    assert exc.value.code == 2


def test_v2_legacy_skeleton_diversity_is_rebuilt_from_stored_expressions(db):
    _settle(db, "rank(close)", version="catalog-generator-v2",
            is_pass=True, campaign="v2-one")
    _settle(db, "ts_rank(close,22)", version="catalog-generator-v2",
            is_pass=True, campaign="v2-two")
    rows = [dict(row) for row in db.query(promotion_gate._LEDGER_SQL)]
    assert all(not row["grammar_skeleton_hash"] for row in rows)
    result = promotion_gate.survivor_diversity(rows)
    assert result["survivors"] == 2
    assert result["reconstructed_grammar"] == 2
    assert result["reconstructed_semantic"] == 2
    assert result["missing_grammar"] == result["missing_semantic"] == 0
    assert result["effective_grammar"] == pytest.approx(2.0)


def test_unknown_structure_is_not_counted_as_a_diverse_skeleton():
    report = promotion_gate.survivor_diversity([
        {"sim_is_pass": True, "normalized_expression": ""},
    ])
    assert report["effective_grammar"] is None
    assert report["effective_semantic"] is None
    assert report["missing_grammar"] == 1
