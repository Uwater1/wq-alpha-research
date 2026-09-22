"""Issue #7 acceptance regressions.

Covers the audit items: lineage/provenance for rejected generated children, correct
descendant generations, exact family-budget conservation, family monopoly limits,
diversity-aware parent selection, the permanent trial ledger, daily-return robustness
metrics, coverage-aware generation, local type enforcement, evidence-derived coverage
stages, and scope-separated coverage history.
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

import archive
import canonical
import compatibility
import field_intelligence
import generator
import research_db as rdb
import robustness


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _settle(db, expression, family, *, sharpe, fitness, turnover, status="DONE"):
    outcome = db.queue_candidate(expression, {"decay": 6}, signal_family=family)
    claimed = db.claim_simulation("issue7", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status=status,
        metrics={"sharpe": sharpe, "fitness": fitness, "turnover": turnover},
        checks=[{"name": "IS", "result": "PASS"}], brain_alpha_id=f"A{outcome.candidate_id}",
    )
    return outcome.candidate_id


# ---------------------------------------------------------------------------
# P1 — lineage, generations, mutation dispatch
# ---------------------------------------------------------------------------


def test_invalid_generated_child_preserves_lineage(db):
    root = db.queue_candidate("rank(close)", {"decay": 6}, signal_family="pv").candidate_id
    proposal = generator.Proposal(
        # Swapping in a field that does not exist fails local validation.
        expression="group_rank(ts_rank(not_a_real_field_xyz, 60), subindustry)",
        settings={"decay": 6},
        family="pv",
        mutation_type="field_swap",
        parameters={"replaced_field": "close", "replacement_field": "not_a_real_field_xyz"},
        parent_ids=(root,),
        reason="deliberate invalid child",
    )
    outcomes = generator.CandidateGenerator(db, seed=0).queue("campaign-lineage", [proposal])
    assert outcomes[0]["action"] == "rejected_invalid"

    child = db.get_candidate(outcomes[0]["candidate_id"])
    assert child["status"] == "REJECTED"
    assert child["failure_reason"].startswith("validation:")
    assert child["campaign_id"] == "campaign-lineage"
    assert child["parent_id"] == root
    assert json.loads(child["parent_ids_json"]) == [root]
    assert child["generation"] == 1
    assert child["mutation_type"] == "field_swap"
    assert json.loads(child["mutation_parameters_json"])["replacement_field"] == "not_a_real_field_xyz"
    assert child["generator_version"] == generator.GENERATOR_VERSION
    assert child["signal_family"] == "pv"
    assert child["generation_reason"] == "deliberate invalid child"

    trial = db.trials("campaign-lineage")[-1]
    assert trial["validation_result"] == "rejected_invalid"
    assert json.loads(trial["parent_ids_json"]) == [root]
    assert trial["field_catalog_version"] == generator.Catalog().version
    assert trial["operator_catalog_version"] == generator.Catalog().operator_version
    assert json.loads(trial["scope_json"])["region"] == "USA"
    # A rejected trial carries the same snapshot provenance a queued one would.
    snapshot = json.loads(trial["provenance_json"])["snapshot"]
    assert snapshot["field_catalog_version"] == generator.Catalog().version
    assert snapshot["field_count"] == 4367
    assert snapshot["scope"] == {"region": "USA", "universe": "TOP3000", "delay": 1}
    assert snapshot["operator_reference"] == "wq_operators.json"


def test_generation_increments_across_descendants(db):
    root = db.queue_candidate("rank(close)", {"decay": 6}, signal_family="pv").candidate_id
    assert db.get_candidate(root)["generation"] == 0
    generator_service = generator.CandidateGenerator(db, seed=3)

    child_proposals = generator_service.mutate(db.get_candidate(root), count=1)
    assert child_proposals[0].generation == 1
    child_id = generator_service.queue("campaign-gen", child_proposals)[0]["candidate_id"]
    assert db.get_candidate(child_id)["generation"] == 1

    grandchild_proposals = generator_service.mutate(db.get_candidate(child_id), count=1)
    assert grandchild_proposals[0].generation == 2
    grandchild_id = generator_service.queue("campaign-gen", grandchild_proposals)[0]["candidate_id"]
    assert db.get_candidate(grandchild_id)["generation"] == 2
    assert json.loads(db.get_candidate(grandchild_id)["parent_ids_json"]) == [child_id]


@pytest.mark.parametrize(
    "failure_reason, expected",
    [
        ("HIGH_TURNOVER", "turnover_repair"),
        ("LOW_TURNOVER", "low_turnover_repair"),
        ("LOW_SHARPE", "sharpe_repair"),
        ("LOW_FITNESS", "fitness_repair"),
        ("CONCENTRATED_WEIGHT", "concentration_repair"),
        ("LOW_SUB_UNIVERSE_SHARPE", "sub_universe_repair"),
        ("SELF_CORRELATION", "correlation_repair"),
        ("CORR_FAIL", "correlation_repair"),
    ],
)
def test_failure_directed_repair_emits_structured_mutation(db, failure_reason, expected):
    candidate_id = db.queue_candidate("group_rank(ts_rank(close, 60), subindustry)", {"decay": 6}).candidate_id
    db.query("UPDATE candidates SET failure_reason=? WHERE id=?", (failure_reason, candidate_id))
    proposals = generator.CandidateGenerator(db, seed=2).mutate(db.get_candidate(candidate_id), count=2)

    assert proposals
    assert proposals[0].mutation_type == expected
    assert proposals[0].parameters
    assert proposals[0].reason
    assert all(proposal.parameters for proposal in proposals)


# ---------------------------------------------------------------------------
# P2 — allocation and parent selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family_count, budget", [(3, 1), (10, 3), (3, 6), (3, 100), (1, 5)])
def test_allocator_uses_exact_budget(db, family_count, budget):
    families = [f"fam{index}" for index in range(family_count)]
    result = archive.allocate_families(db, families, budget=budget, seed=5, allocation_key=f"exact-{budget}")

    assert sum(row["budget"] for row in result) == budget
    assert all(row["budget"] >= 0 for row in result)


def test_allocator_never_exceeds_budget(db):
    families = [f"fam{index}" for index in range(10)]
    result = archive.allocate_families(db, families, budget=3, seed=5, allocation_key="subset")

    assert sum(row["budget"] for row in result) == 3
    assert sum(1 for row in result if row["budget"] > 0) == 3
    assert sum(1 for row in result if row["exploration"]) == 3


def test_allocator_limits_family_monopoly(db):
    _settle(db, "rank(close)", "proven", sharpe=1.6, fitness=1.3, turnover=0.1)
    result = archive.allocate_families(
        db, ["proven", "under1", "under2"], budget=20, seed=7, allocation_key="monopoly",
        max_family_share=0.5,
    )

    assert sum(row["budget"] for row in result) == 20
    assert max(row["budget"] for row in result) <= 10
    assert all(row["share"] <= 0.5 for row in result)


def test_parent_selection_preserves_diversity(db):
    # Three strong niches in one family, one weak niche in another family.
    for index, field in enumerate(("close", "open", "high")):
        _settle(db, f"rank({field}) + {index}", "dominant", sharpe=2.0 - 0.01 * index, fitness=1.8, turnover=0.1)
    _settle(db, "group_rank(ts_mean(free_cash_flow_reported_value, 60), industry)", "underdog",
            sharpe=0.2, fitness=0.1, turnover=0.05)
    archive.rebuild(db)

    selected = archive.parents(db, count=2, seed=11)
    assert len(selected) == 2
    assert {row["signal_family"] for row in selected} == {"dominant", "underdog"}
    # Diversity is seeded, not random noise over the same global ranking.
    assert [row["cell_key"] for row in selected] == [row["cell_key"] for row in archive.parents(db, count=2, seed=11)]


# ---------------------------------------------------------------------------
# P3 — trial ledger and daily-return robustness
# ---------------------------------------------------------------------------


def test_duplicate_candidate_creates_distinct_trial_record(db):
    first = db.queue_candidate("rank(close)", {"decay": 6}, campaign_id="dup")
    second = db.queue_candidate("rank(close)", {"decay": 6}, campaign_id="dup")

    assert first.candidate_id == second.candidate_id  # canonical identity is stable
    trials = db.trials("dup")
    assert len(trials) == 2
    assert [trial["creation_order"] for trial in trials] == [1, 2]
    assert len({trial["candidate_id"] for trial in trials}) == 1
    assert trials[1]["is_duplicate"] == 1
    # One research decision per call, but no duplicated BRAIN capacity.
    assert sum(db.counts("simulations").values()) == 1


def test_cache_hit_is_a_trial_but_not_a_new_simulation(db):
    candidate_id = _settle(db, "rank(close)", "pv", sharpe=1.4, fitness=1.2, turnover=0.1)
    repeat = db.queue_candidate("rank(close)", {"decay": 6}, campaign_id="replay")

    assert repeat.action == "cache_hit"
    assert repeat.candidate_id == candidate_id
    assert sum(db.counts("simulations").values()) == 1
    assert [trial["validation_result"] for trial in db.trials("replay")] == ["cache_hit"]


def test_trial_ledger_backfills_pre_v7_history_once(db):
    db.queue_candidate("rank(close)", {"decay": 6}, campaign_id="historical")
    # Simulate a research.db upgraded from schema v6: candidates exist, no ledger rows.
    db.query("DELETE FROM research_trials")
    db.query("DELETE FROM meta WHERE key=?", (rdb.META_TRIALS_BACKFILLED,))
    path = db.path
    db.close()

    with rdb.ResearchDB.open(path) as reopened:
        trials = reopened.trials("historical")
        assert len(trials) == 1
        assert json.loads(trials[0]["provenance_json"])["backfilled"] is True
        assert trials[0]["scope_json"] is not None
    with rdb.ResearchDB.open(path) as reopened:
        assert reopened.trial_count() == 1  # idempotent, never re-runs


def test_invalid_trial_is_counted_in_campaign(db):
    parent = db.queue_candidate("rank(close)", {"decay": 6}, campaign_id="camp").candidate_id
    invalid = generator.Proposal(
        expression="rank(not_a_real_field_xyz)", settings={"decay": 6}, family="pv",
        mutation_type="field_swap", parameters={"replacement_field": "not_a_real_field_xyz"},
        parent_ids=(parent,), reason="invalid child",
    )
    generator.CandidateGenerator(db, seed=0).queue("camp", [invalid])

    report = robustness.campaign_report(db, "camp", persist=False)
    assert report["provenance"]["trial_count"] == 2
    assert report["provenance"]["candidate_count"] == 2
    assert report["trials"]["outcomes"]["validation_reject"] == 1
    assert report["trials"]["accounted_trials"] == report["trials"]["trial_count"]


def test_robustness_uses_daily_returns_not_cumulative_levels(db):
    candidate_id = _settle(db, "rank(close)", "pv", sharpe=1.4, fitness=1.2, turnover=0.1)
    candidate = db.get_candidate(candidate_id)
    rng = np.random.default_rng(4)
    daily = rng.normal(0.0005, 0.002, size=300)
    levels = np.cumsum(daily)
    db.cache_active_pnl(candidate["brain_alpha_id"], [f"2024-01-{day % 28 + 1:02d}" for day in range(301)], list(levels))

    stability = robustness.campaign_report(db, None, persist=False)["stability"]
    entry = stability["candidates"][0]

    assert entry["metric_basis"] == "daily_returns"
    assert entry["daily_return_count"] == 299
    # First differencing actually happened: the daily mean is the diff mean, not the level mean.
    assert entry["daily_return_mean"] == pytest.approx(float(np.mean(np.diff(levels))), abs=1e-8)
    assert entry["daily_return_mean"] != pytest.approx(float(np.mean(levels)), abs=1e-6)
    assert entry["subperiods"] and entry["rolling"]["status"] == "available"
    assert entry["max_drawdown"] >= 0.0


def test_flat_cumulative_path_does_not_produce_a_fake_sharpe(db):
    candidate_id = _settle(db, "rank(open)", "pv", sharpe=1.4, fitness=1.2, turnover=0.1)
    candidate = db.get_candidate(candidate_id)
    levels = [3.5] * 200  # a constant cumulative level: zero daily returns
    db.cache_active_pnl(candidate["brain_alpha_id"], [f"2024-02-{day % 28 + 1:02d}" for day in range(200)], levels)

    entry = robustness.campaign_report(db, None, persist=False)["stability"]["candidates"][0]
    assert entry["flat_return_path"] is True
    assert entry["daily_sharpe"] is None  # no dispersion -> no meaningful Sharpe
    assert entry["daily_return_std"] == 0.0


def test_level_shift_does_not_inflate_sharpe(db):
    """A single level shift is one large daily return, not a persistent signal."""
    candidate_id = _settle(db, "rank(high)", "pv", sharpe=1.4, fitness=1.2, turnover=0.1)
    candidate = db.get_candidate(candidate_id)
    # One jump up and one back down in cumulative PnL: two non-zero daily returns.
    levels = [0.0] * 40 + [50.0] * 40 + [0.0] * 40
    db.cache_active_pnl(candidate["brain_alpha_id"], [f"2024-03-{day % 28 + 1:02d}" for day in range(120)], levels)

    entry = robustness.campaign_report(db, None, persist=False)["stability"]["candidates"][0]
    level_proxy = float(np.mean(levels) / (np.std(levels, ddof=1) or 1e-12) * np.sqrt(252))
    assert entry["daily_sharpe"] is not None
    # The up-shift and down-shift cancel: the honest daily Sharpe is zero, while the
    # cumulative-level proxy is ~10. Treating levels as returns hid exactly this.
    assert entry["daily_sharpe"] == pytest.approx(0.0, abs=1e-9)
    assert level_proxy > 10.0


def test_stability_reports_missing_data_explicitly(db):
    _settle(db, "rank(volume)", "pv", sharpe=1.4, fitness=1.2, turnover=0.1)
    stability = robustness.campaign_report(db, None, persist=False)["stability"]
    assert stability["status"] == "unavailable"
    assert stability["missing"] == [{"candidate_id": 1, "status": "no_cached_pnl"}]


# ---------------------------------------------------------------------------
# P4 — coverage ordering, type enforcement, scope/version history
# ---------------------------------------------------------------------------


def test_generator_prefers_under_tested_fields(db):
    for index in range(6):
        db.queue_candidate(f"rank(close) + {index}", {"decay": 6})
    catalog = generator.Catalog()
    fields = [field for field in catalog.fields if field.name in {"close", "open", "high", "low", "volume"}]

    for seed in range(8):
        ordered = generator.CandidateGenerator(db, seed=seed).coverage_ordered(fields)
        assert ordered[-1].name == "close", ordered
        assert ordered[0].name != "close"

    # The same ordering drives real generation: the first proposed field is never the
    # repeatedly tested one, whatever the seed shuffles inside a coverage bucket.
    for seed in range(4):
        proposals = generator.CandidateGenerator(db, seed=seed).proposals(count=1, family="all")
        assert proposals[0].parameters["field"] != "close"


def test_known_type_mismatch_is_advisory_and_strict_mode_is_opt_in(db):
    import validate

    impossible = ("group_rank(close, close)", "max(composite_sentiment_score_2, close)", "vec_avg(close)")
    for expression in impossible:
        report = validate.validate(expression, {"region": "USA"})
        # BRAIN decides: locally this only lowers priority.
        assert report.ok and report.warnings, expression
        strict = validate.validate(expression, {"region": "USA"}, type_policy=validate.TYPE_POLICY_STRICT)
        assert not strict.ok, expression

    assert db.queue_candidate("vec_avg(close)", {"region": "USA"}).action == "queued"

    field_intelligence.refresh(db)
    rows = {row["operator_name"]: row for row in db.query("SELECT * FROM operator_compatibility")}
    for name in ("group_rank", "vec_avg", "ts_mean", "group_backfill"):
        spec = compatibility.constraint_for(name)
        assert rows[name]["requires_group"] == spec.requires_group
        assert rows[name]["requires_vector"] == spec.requires_vector
        assert rows[name]["min_args"] == spec.min_args


def test_field_coverage_does_not_count_validation_reject_as_simulated(db):
    outcome = db.queue_candidate("ts_mean(close)", {"region": "USA"})  # wrong arity: never simulated
    assert outcome.action == "rejected_invalid"
    field_intelligence.refresh(db)

    rows = db.query("SELECT * FROM field_coverage WHERE field_id='close'")
    assert len(rows) == 1
    row = rows[0]
    assert row["attempts"] == 1
    assert row["rejected"] == 1
    assert row["validated"] == 0  # never passed validation
    assert row["simulated"] == 0  # no simulation row exists


def test_field_coverage_is_scope_separated(db):
    db.queue_candidate("rank(close)", {"region": "USA", "universe": "TOP3000", "delay": 1})
    db.queue_candidate("rank(close)", {"region": "CHN", "universe": "TOP2000U", "delay": 1})
    result = field_intelligence.refresh(db)

    rows = db.query("SELECT * FROM field_coverage WHERE field_id='close'")
    assert len(rows) == 2
    assert len({row["scope_hash"] for row in rows}) == 2
    china = next(row for row in rows if "CHN" in row["scope_json"])
    usa = next(row for row in rows if "USA" in row["scope_json"])
    assert china["attempts"] == 1 and china["cataloged"] == 0
    assert usa["attempts"] == 1 and usa["cataloged"] == 1
    assert result["scopes"] == 2


def test_coverage_attempts_do_not_leak_across_scopes(db):
    for index in range(3):
        db.queue_candidate(f"rank(close) + {index}", {"region": "CHN", "universe": "TOP2000U"})
    catalog = generator.Catalog()
    fields = [field for field in catalog.fields if field.name == "close"]
    assert field_intelligence.coverage_attempts(db, fields, scope=catalog.scope)["close"] == 0
    assert field_intelligence.coverage_attempts(db, fields, scope={"region": "CHN", "universe": "TOP2000U"})["close"] == 3


def test_legacy_field_coverage_is_migrated_into_scope(tmp_path):
    path = tmp_path / "research.db"
    with rdb.ResearchDB.open(path):
        pass
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        DROP TABLE field_coverage;
        CREATE TABLE field_coverage (
            field_id TEXT NOT NULL, dataset TEXT NOT NULL, category TEXT, field_type TEXT,
            catalog_version TEXT NOT NULL, scope_json TEXT NOT NULL,
            cataloged INTEGER NOT NULL DEFAULT 1, validated INTEGER NOT NULL DEFAULT 0,
            simulated INTEGER NOT NULL DEFAULT 0, is_pass INTEGER NOT NULL DEFAULT 0,
            corr_pass INTEGER NOT NULL DEFAULT 0, submitted INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, median_sharpe REAL, median_fitness REAL,
            median_turnover REAL, failure_reasons_json TEXT NOT NULL, last_tested_at TEXT,
            PRIMARY KEY(field_id, catalog_version)
        );
        INSERT INTO field_coverage(field_id, dataset, catalog_version, scope_json, attempts, failure_reasons_json)
        VALUES('close', 'pv1', 'legacy', '{"delay": 1, "region": "USA", "universe": "TOP3000"}', 4, '[]');
        """
    )
    connection.commit()
    connection.close()

    with rdb.ResearchDB.open(path) as store:
        rows = store.query("SELECT * FROM field_coverage")
        assert len(rows) == 1
        assert rows[0]["scope_hash"] == canonical.scope_hash()
        assert rows[0]["attempts"] == 4
        assert store.get_meta("schema_version") == str(rdb.SCHEMA_VERSION)
