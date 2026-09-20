"""Offline regression coverage for Priority 1 knowledge and skill safety."""
from __future__ import annotations

import pytest

import research_db as rdb
import skill_manager


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _observation(db, group: str, *, value=None, privacy="PRIVATE"):
    return db.record_observation(
        subject_type="signal_structure",
        subject_key="skeleton-1",
        claim="cash flow improves fitness",
        value=value or {"is_pass": True, "fitness": 1.4},
        scope={"region": "USA", "universe": "TOP3000"},
        evidence_group=group,
        provenance={"source": "offline-test"},
        privacy_class=privacy,
    )


def test_observations_are_scoped_deduplicated_and_private_by_default(db):
    first = _observation(db, "campaign-a")
    duplicate = db.record_observation(
        subject_type="signal_structure", subject_key="skeleton-1", claim="cash flow improves fitness",
        value={"is_pass": True}, scope={"region": "USA"}, evidence_group="campaign-a",
        provenance={"source": "offline-test"}, source_event_id=42,
    )
    same_event = db.record_observation(
        subject_type="signal_structure", subject_key="skeleton-1", claim="cash flow improves fitness",
        value={"is_pass": False}, scope={"region": "CHN"}, evidence_group="campaign-b",
        provenance={"source": "different"}, source_event_id=42,
    )

    assert first != duplicate
    assert duplicate == same_event
    rows = db.observations(subject_key="skeleton-1")
    assert len(rows) == 2
    assert rows[0]["privacy_class"] == "PRIVATE"
    assert rows[0]["scope"]["region"] == "USA"


def test_learning_store_rejects_secret_material_even_when_marked_private(db):
    with pytest.raises(ValueError, match="credential-like"):
        _observation(db, "secret", value={"password": "not-stored"})
    with pytest.raises(ValueError, match="SECRET"):
        _observation(db, "secret", privacy="SECRET")


def test_rule_proposal_requires_independent_evidence_and_evaluation_before_promotion(db):
    first = _observation(db, "campaign-a")
    second = _observation(db, "campaign-b")
    contradiction = _observation(db, "campaign-c", value={"is_pass": False, "fitness": 0.4})
    rule_id = db.propose_rule(
        title="Prefer realized cash flow",
        body="Prefer realized cash-flow families after the baseline clears validation.",
        scope={"region": "USA", "universe": "TOP3000"},
        evidence=[(first, "support"), (second, "support"), (contradiction, "contradiction")],
        provenance={"source": "offline-test"},
        privacy_class="SANITIZED",
    )

    rule = db.get_rule(rule_id)
    assert rule["state"] == "proposed"
    assert (rule["support_count"], rule["contradiction_count"]) == (2, 1)
    evaluation = db.evaluate_rule(rule_id)
    assert evaluation["passed"] is True
    assert evaluation["independent_groups"] == 2

    active = db.transition_rule(rule_id, "active", expected_version=1)
    assert active["state"] == "active" and active["version"] == 2
    with pytest.raises(ValueError, match="version mismatch"):
        db.transition_rule(rule_id, "weakened", expected_version=1)


def test_rule_lifecycle_protects_user_owned_rules_and_supports_retirement(db):
    obs = _observation(db, "campaign-a")
    rule_id = db.propose_rule(
        title="Pinned rule", body="Keep this user decision.", evidence=[(obs, "support")],
        owner="user", pinned=True, privacy_class="SANITIZED",
    )
    with pytest.raises(PermissionError):
        db.transition_rule(rule_id, "retired")
    retired = db.transition_rule(rule_id, "retired", force=True, expected_version=1)
    assert retired["state"] == "retired"


def test_fts_recall_respects_scope_state_and_privacy(db):
    obs = _observation(db, "campaign-a")
    private_rule = db.propose_rule(
        title="Private cash-flow note", body="Cash-flow evidence is useful in USA TOP3000.",
        scope={"region": "USA"}, evidence=[(obs, "support")], privacy_class="PRIVATE",
    )
    public_rule = db.propose_rule(
        title="Public cash-flow rule", body="Cash-flow evidence is useful in USA TOP3000.",
        scope={"region": "USA"}, evidence=[(obs, "support")], privacy_class="SANITIZED",
    )

    recalled = db.recall_knowledge("cash-flow", scope={"region": "USA"}, max_privacy="SANITIZED")
    assert [row["id"] for row in recalled] == [public_rule]
    assert db.recall_knowledge("cash-flow", scope={"region": "CHN"}, max_privacy="PRIVATE") == []
    assert private_rule not in [row["id"] for row in recalled]


def test_skill_manager_uses_expected_sha_atomic_backup_and_rollback(tmp_path, db):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Router\n", encoding="utf-8")
    before = skill_manager.read_skill(skill)[1]
    rule = {"id": 7, "title": "General rule", "body": "Use evidence before promotion.",
            "scope": {"region": "USA"}, "privacy_class": "SANITIZED"}

    result = skill_manager.apply_rule(db, skill, rule, expected_sha=before, backup_dir=tmp_path / "history")
    assert result["before_sha"] == before
    assert skill_manager.read_skill(skill)[1] == result["after_sha"]
    assert (tmp_path / "history" / f"{before}.md").read_text(encoding="utf-8") == "# Router\n"
    assert db.knowledge_status()["mutations"] == 1

    with pytest.raises(ValueError, match="SHA mismatch"):
        skill_manager.apply_rule(db, skill, rule, expected_sha=before)

    current, current_sha = skill_manager.read_skill(skill)
    rolled_back = skill_manager.rollback(
        db, skill, result["backup_sha"], expected_sha=current_sha,
        backup_dir=tmp_path / "history",
    )
    assert rolled_back["after_sha"] == before
    assert skill_manager.read_skill(skill)[1] == before


def test_skill_manager_applies_sanitized_snippets_through_the_same_ledger(tmp_path, db):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Router\n", encoding="utf-8")
    before = skill_manager.read_skill(skill)[1]
    result = skill_manager.apply_snippet(
        db, skill, "### General lesson\n\nPrefer independent evidence.", expected_sha=before,
    )
    assert result["version"] == 1
    assert "Prefer independent evidence" in skill.read_text(encoding="utf-8")
    assert db.query("SELECT operation FROM skill_mutations")[0]["operation"] == "skill.apply_snippet"


def test_skill_manager_refuses_private_rules_and_secret_text(tmp_path, db):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Router\n", encoding="utf-8")
    with pytest.raises(ValueError, match="PUBLIC or SANITIZED"):
        skill_manager.apply_rule(db, skill, {"title": "x", "body": "y", "privacy_class": "PRIVATE"})
    with pytest.raises(ValueError, match="private identifiers"):
        skill_manager.apply_rule(db, skill, {
            "title": "x", "body": "Never use password = value.", "privacy_class": "SANITIZED"
        })


def test_materialize_event_observations_keeps_one_event_one_observation(db):
    outcome = db.queue_candidate("rank(close)", source="offline")
    claimed = db.claim_simulation("worker")
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": 1.5, "fitness": 1.2, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "PASS"}], brain_alpha_id="PRIVATE-ID",
    )
    first = db.materialize_event_observations()
    second = db.materialize_event_observations()
    assert first["created"] == 1 and second["created"] == 0
    rows = db.observations(claim="simulation_outcome")
    assert len(rows) == 1
    assert rows[0]["privacy_class"] == "PRIVATE"


def test_status_reports_fts_and_schema_capabilities(db):
    status = db.knowledge_status()
    assert status["fts5"] is True
    assert db.get_meta("schema_version") == str(rdb.SCHEMA_VERSION)
