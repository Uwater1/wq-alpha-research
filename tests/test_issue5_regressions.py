"""Issue #5 acceptance regressions: concurrency, privacy, lifecycle, surrogate, observability."""
from __future__ import annotations

import json
import multiprocessing as mp
import time

import pytest

import brain_api
import research_db as rdb
import sim_scheduler
import skill_manager
import surrogate


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def _obs(db, group, *, scope=None, value=None, state="active"):
    return db.record_observation(
        subject_type="signal_structure", subject_key="skeleton-1",
        claim="cash flow improves fitness", value=value or {"is_pass": True},
        scope=scope or {"region": "USA", "universe": "TOP3000"},
        evidence_group=group, provenance={"source": "issue5"},
        privacy_class="PRIVATE", lifecycle_state=state,
    )


def _evaluated_rule(db, *, scope=None, privacy="SANITIZED"):
    a = _obs(db, "campaign-a", scope=scope)
    b = _obs(db, "campaign-b", scope=scope)
    rule_id = db.propose_rule(
        title="Issue5 rule", body="Durable guidance after review.",
        scope=scope or {"region": "USA", "universe": "TOP3000"},
        evidence=[(a, "support"), (b, "support")],
        provenance={"source": "issue5"}, privacy_class=privacy,
    )
    db.evaluate_rule(rule_id)
    return db.transition_rule(rule_id, "active", expected_version=1)["id"]


def _worker_apply(db_path, skill_path, rule_id, version, sha, queue):
    try:
        with rdb.ResearchDB.open(db_path) as store:
            skill_manager.apply_evaluated_rule(
                store, skill_path, rule_id, expected_rule_version=version, expected_sha=sha)
        queue.put("ok")
    except Exception as exc:  # noqa: BLE001 - result transport
        queue.put(f"error: {type(exc).__name__}: {exc}")


def test_concurrent_writers_exactly_one_commits(tmp_path):
    db_path = tmp_path / "research.db"
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Router\n", encoding="utf-8")
    with rdb.ResearchDB.open(db_path) as store:
        rule_id = _evaluated_rule(store)
        version = int(store.get_rule(rule_id)["version"])
    sha = skill_manager.read_skill(skill)[1]
    queue = mp.Queue()
    procs = [mp.Process(target=_worker_apply, args=(str(db_path), str(skill), rule_id, version, sha, queue))
             for _ in range(2)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(30)
    results = sorted(queue.get() for _ in procs)
    assert sum(1 for r in results if r == "ok") == 1
    assert sum(1 for r in results if r.startswith("error")) == 1


def test_apply_rule_refuses_proposed_unevaluated_stale(tmp_path, db):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Router\n", encoding="utf-8")
    sha = skill_manager.read_skill(skill)[1]
    a = _obs(db, "campaign-a")
    proposed = db.propose_rule(title="Draft", body="Not yet reviewed.",
                               scope={"region": "USA"}, evidence=[(a, "support")])
    with pytest.raises(ValueError, match="only evaluated active/pinned"):
        skill_manager.apply_evaluated_rule(db, skill, proposed, expected_sha=sha)
    rule_id = _evaluated_rule(db)
    with pytest.raises(ValueError, match="version mismatch"):
        skill_manager.apply_evaluated_rule(
            db, skill, rule_id, expected_rule_version=999, expected_sha=sha)


def test_snippet_path_is_user_only(tmp_path, db):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Router\n", encoding="utf-8")
    sha = skill_manager.read_skill(skill)[1]
    with pytest.raises(PermissionError):
        skill_manager.apply_snippet(db, skill, "### x\n\nAutonomous prose.", expected_sha=sha,
                                    actor="agent")
    out = skill_manager.apply_snippet(db, skill, "### x\n\nManual prose.", expected_sha=sha,
                                      actor="user")
    assert out["version"] >= 1


def test_raw_private_material_cannot_reach_tracked_skill(tmp_path, db):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Router\n", encoding="utf-8")
    sha = skill_manager.read_skill(skill)[1]
    with pytest.raises(ValueError, match="PUBLIC or SANITIZED"):
        skill_manager.apply_rule(db, skill, {"title": "x", "body": "y", "privacy_class": "PRIVATE"},
                                 actor="user", expected_sha=sha)
    with pytest.raises(ValueError, match="private identifiers"):
        skill_manager.apply_snippet(db, skill, "see /alphas/ABCDEF123456 for details",
                                    expected_sha=sha, actor="user")


def test_rollback_captures_left_state_and_supports_redo(tmp_path, db):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Router\n", encoding="utf-8")
    before = skill_manager.read_skill(skill)[1]
    rule_id = _evaluated_rule(db)
    version = int(db.get_rule(rule_id)["version"])
    applied = skill_manager.apply_evaluated_rule(
        db, skill, rule_id, expected_rule_version=version,
        expected_sha=before, backup_dir=tmp_path / "history")
    mid_sha = applied["after_sha"]
    rolled = skill_manager.rollback(db, skill, before, expected_sha=mid_sha,
                                    backup_dir=tmp_path / "history")
    assert rolled["after_sha"] == before
    assert (tmp_path / "history" / f"{mid_sha}.md").exists()
    redo = skill_manager.rollback(db, skill, mid_sha, backup_dir=tmp_path / "history")
    assert redo["after_sha"] == mid_sha


def test_file_db_failure_leaves_recoverable_state(tmp_path, db, monkeypatch):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Router\n", encoding="utf-8")
    before = skill_manager.read_skill(skill)[1]
    before_text = skill.read_text(encoding="utf-8")
    rule_id = _evaluated_rule(db)
    version = int(db.get_rule(rule_id)["version"])

    def _boom(**kwargs):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(db, "record_skill_mutation_atomic", _boom)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        skill_manager.apply_evaluated_rule(
            db, skill, rule_id, expected_rule_version=version,
            expected_sha=before, backup_dir=tmp_path / "history")
    assert skill.read_text(encoding="utf-8") == before_text
    assert db.query("SELECT COUNT(*) AS n FROM skill_mutations")[0]["n"] == 0


def test_pinned_transition_sets_protection_flag(db):
    a = _obs(db, "campaign-a")
    b = _obs(db, "campaign-b")
    rule_id = db.propose_rule(title="Pin me", body="Durable user decision.",
                              scope={"region": "USA"}, evidence=[(a, "support"), (b, "support")])
    db.evaluate_rule(rule_id)
    pinned = db.transition_rule(rule_id, "pinned", expected_version=1)
    assert pinned["state"] == "pinned" and pinned["pinned"] == 1
    with pytest.raises(PermissionError):
        db.transition_rule(rule_id, "active")
    back = db.transition_rule(rule_id, "active", force=True)
    assert back["pinned"] == 0


def test_retracted_evidence_excluded_from_evaluation(db):
    a = _obs(db, "campaign-a")
    b = _obs(db, "campaign-b")
    rule_id = db.propose_rule(title="Fragile", body="Two supports.",
                              scope={"region": "USA", "universe": "TOP3000"},
                              evidence=[(a, "support"), (b, "support")])
    assert db.evaluate_rule(rule_id)["passed"] is True
    db._conn.execute("UPDATE knowledge_observations SET lifecycle_state='retracted' WHERE id=?", (b,))
    db._conn.commit()
    evaluation = db.evaluate_rule(rule_id)
    assert evaluation["support"] == 1
    assert evaluation["ignored_non_active_evidence"] == 1
    assert evaluation["passed"] is False


def test_cross_scope_evidence_rejected_and_audited(db):
    usa = _obs(db, "campaign-a", scope={"region": "USA", "universe": "TOP3000"})
    chn = _obs(db, "campaign-b", scope={"region": "CHN", "universe": "TOP3000"})
    rule_id = db.propose_rule(title="USA rule", body="USA only.",
                              scope={"region": "USA", "universe": "TOP3000"},
                              evidence=[(usa, "support")])
    with pytest.raises(ValueError, match="incompatible"):
        db.attach_rule_evidence(rule_id, chn, "support")
    assert db.query(
        "SELECT * FROM events WHERE event='rule_evidence_rejected'")[0]["entity"] == "knowledge"


def test_surrogate_bias_column_ordering():
    import numpy as np
    names = ["aaa_first", "bias", "zzz_last"]
    x = np.array([[1.0, 1.0, 2.0], [1.0, 1.0, 4.0]])
    y = np.array([3.0, 5.0])
    weights = surrogate._ridge_fit(x, y, penalty=100.0, feature_names=names)
    # Heavy penalty shrinks slopes but the unpenalized bias must still carry signal.
    assert abs(weights[1]) > abs(weights[0])


def test_surrogate_oos_and_insufficient(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "small.db") as small:
        for i in range(3):
            outcome = small.queue_candidate(f"rank(close) + {i}", {"decay": 6})
            row = small.claim_simulation("seed", candidate_id=outcome.candidate_id)
            small.record_simulation_result(
                candidate_id=row["id"], status="DONE",
                metrics={"sharpe": 1.6, "fitness": 1.3, "turnover": 0.05},
                checks=[{"name": "LOW_SHARPE", "result": "PASS"}], brain_alpha_id=f"S{i}")
        surrogate.fit(small, min_samples=2)
        assert surrogate.evaluate(small)["reason"] == "insufficient_oos_data"


def _settle(db, expression, passed):
    outcome = db.queue_candidate(expression, {"decay": 6})
    row = db.claim_simulation("seed", candidate_id=outcome.candidate_id)
    db.record_simulation_result(
        candidate_id=row["id"], status="DONE",
        metrics={"sharpe": 1.6 if passed else 0.3, "fitness": 1.3 if passed else 0.2,
                 "turnover": 0.05 if passed else 0.35},
        checks=[{"name": "LOW_SHARPE", "result": "PASS" if passed else "FAIL"}],
        brain_alpha_id=f"LOCAL{row['id']}")


def test_surrogate_oos_report_has_split_boundaries(db):
    for i in range(8):
        _settle(db, f"rank(open) + {i}", i % 2 == 0)
    surrogate.fit(db, min_samples=5)
    report = surrogate.evaluate(db)
    assert report["available"] is True and report["oos"] is True
    assert report["train_samples"] + report["test_samples"] == 8
    assert report["split"]["train_max_id"] < report["split"]["test_min_id"]


def test_brain_client_keeps_retry_attempt_history():
    class Resp:
        def __init__(self, status, headers=None):
            self.status_code = status
            self.headers = headers or {}
            self.text = "{}"
            self.content = b"{}"

        def json(self):
            return {}

    calls = {"n": 0}

    class Session:
        def request(self, method, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return Resp(429, {"Retry-After": "0"})
            return Resp(200)

        def close(self):
            pass

    client = brain_api.BrainClient(session=Session(), retries=1)
    client.get("https://api.worldquantbrain.com/health")
    assert len(client.attempt_history) == 2
    assert client.attempt_history[0]["http_status"] == 429
    assert client.attempt_history[-1]["http_status"] == 200
    assert client.last_request["http_status"] == 200


def test_throughput_and_cache_metric_semantics(db):
    stats = db.stats()
    assert stats.simulations_per_active_alpha is None
    first = db.queue_candidate("rank(close)", source="offline")
    assert first.action == "queued"
    replay = db.queue_candidate("rank(close)", source="offline")
    assert replay.action == "in_flight"  # leased/queued, not a second validation
    validations = db.query("SELECT * FROM events WHERE event='validation_passed'")
    assert len(validations) == 1
    assert db.stats().validated_per_hour >= 0
    payload = db.stats().as_dict()
    assert 0.0 <= payload["cache_hit_rate"] <= 1.0


def test_contradiction_review_weakens_active_rule(db):
    a = _obs(db, "campaign-a")
    b = _obs(db, "campaign-b")
    rule_id = db.propose_rule(title="Stale", body="Was supported.",
                              scope={"region": "USA", "universe": "TOP3000"},
                              evidence=[(a, "support"), (b, "support")])
    db.evaluate_rule(rule_id)
    db.transition_rule(rule_id, "active", expected_version=1)
    c1 = _obs(db, "campaign-c", value={"is_pass": False})
    c2 = _obs(db, "campaign-d", value={"is_pass": False})
    db.attach_rule_evidence(rule_id, c1, "contradiction")
    db.attach_rule_evidence(rule_id, c2, "contradiction")
    flagged = db.review_contradicted_rules()
    assert any(item["rule_id"] == rule_id and item["to"] == "weakened" for item in flagged)
    assert db.get_rule(rule_id)["state"] == "weakened"


def test_production_like_review_worker_from_events(db):
    outcome = db.queue_candidate("rank(close)", source="offline")
    claimed = db.claim_simulation("worker")
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": 1.5, "fitness": 1.2, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "PASS"}], brain_alpha_id="X1")
    second = db.queue_candidate("rank(open)", source="offline")
    claimed2 = db.claim_simulation("worker", candidate_id=second.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed2["id"], status="DONE",
        metrics={"sharpe": 1.5, "fitness": 1.2, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "PASS"}], brain_alpha_id="X2")
    report = db.review_knowledge(min_support=1, min_groups=1)
    assert report["materialized_simulation"]["created"] >= 1
    assert report["aggregates"]["aggregates"] >= 1
    # Autonomous generation proposes only; nothing becomes active on its own.
    assert all(db.get_rule(item["rule_id"])["state"] == "proposed"
               for item in report["proposals"])
    assert not db.recall_knowledge("rank", max_privacy="PRIVATE")


def test_executable_rule_requires_real_passing_replay(db):
    for i in range(8):
        _settle(db, f"rank(close) + {i}", i % 2 == 0)
    first = _obs(db, "campaign-a")
    second = _obs(db, "campaign-b")
    rule_id = db.propose_rule(
        title="Filter high-turnover candidates",
        body="Executable policy: keep the historically stronger low-turnover slice.",
        scope={"region": "USA", "universe": "TOP3000"},
        evidence=[(first, "support"), (second, "support")],
        provenance={
            "source": "issue5",
            "effect": {
                "kind": "candidate_filter",
                "conditions": [{"field": "turnover", "op": "lte", "value": 0.10}],
                "acceptance": {
                    "min_selected": 2,
                    "min_pass_rate_delta": 0.20,
                    "max_wasted_rate_delta": 0.0,
                    "max_mean_turnover_delta": 0.0,
                },
            },
        },
    )
    evaluation = db.evaluate_rule(rule_id)
    assert evaluation["rule_mode"] == "executable"
    assert evaluation["evidence_passed"] is True
    assert evaluation["replay"]["status"] == "passed"
    assert evaluation["replay"]["result"] is True
    assert evaluation["passed"] is True

    unsupported = db.propose_rule(
        title="Unsupported executable rule",
        body="Must not promote without an executable replay evaluator.",
        scope={"region": "USA", "universe": "TOP3000"},
        evidence=[(first, "support"), (second, "support")],
        provenance={"effect": {"kind": "priority_adjustment", "amount": 1.0}},
    )
    blocked = db.evaluate_rule(unsupported)
    assert blocked["evidence_passed"] is True
    assert blocked["replay"]["status"] == "unsupported_effect"
    assert blocked["passed"] is False
    with pytest.raises(ValueError, match="evaluation gate"):
        db.transition_rule(unsupported, "active", expected_version=1)


def test_submission_observation_resolves_submission_to_real_candidate(db):
    _settle(db, "rank(open) + 100", True)  # candidate id 1; never submitted
    target = db.queue_candidate("rank(close) + 200", {"decay": 6})
    claimed = db.claim_simulation("seed", candidate_id=target.candidate_id)
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": 1.8, "fitness": 1.4, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "PASS"}], brain_alpha_id="TARGET2",
    )
    submission_id = db.enqueue_submission(target.candidate_id)
    submission = db.claim_submission("submitter")
    assert submission["id"] == submission_id
    assert submission_id != target.candidate_id  # catches the old id-domain join bug
    db.finish_submission(submission_id, "ACTIVE", brain_alpha_id="TARGET2")

    report = db.materialize_submission_observations()
    assert report["created"] >= 1
    observations = db.observations(claim="submission_outcome")
    active = [row for row in observations if row["value"].get("status") == "ACTIVE"]
    assert active
    assert active[-1]["candidate_id"] == target.candidate_id
    assert active[-1]["submission_id"] == submission_id
    assert active[-1]["subject_key"] != f"candidate:{submission_id}"


def test_knowledge_aggregates_never_mix_scopes(db):
    common = dict(
        subject_type="signal_structure", subject_key="same-skeleton",
        claim="simulation_outcome", value={"is_pass": True},
        provenance={"source": "scope-test"}, privacy_class="PRIVATE",
    )
    db.record_observation(scope={"region": "USA", "universe": "TOP3000"},
                          evidence_group="usa-a", **common)
    db.record_observation(scope={"region": "CHN", "universe": "TOP3000"},
                          evidence_group="chn-a", **common)
    report = db.refresh_knowledge_aggregates()
    assert report["aggregates"] == 2
    rows = db.query(
        "SELECT aggregate_key, scope_json, sample_count FROM knowledge_aggregates "
        "WHERE subject_key='same-skeleton' ORDER BY aggregate_key"
    )
    assert len(rows) == 2
    scopes = {json.loads(row["scope_json"])["region"] for row in rows}
    assert scopes == {"USA", "CHN"}
    assert all(int(row["sample_count"]) == 1 for row in rows)


def test_autonomous_skill_application_requires_both_cas_inputs(tmp_path, db):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Router\n", encoding="utf-8")
    sha = skill_manager.read_skill(skill)[1]
    rule_id = _evaluated_rule(db)
    version = int(db.get_rule(rule_id)["version"])
    with pytest.raises(ValueError, match="expected_rule_version"):
        skill_manager.apply_evaluated_rule(db, skill, rule_id, expected_sha=sha)
    with pytest.raises(ValueError, match="expected_sha"):
        skill_manager.apply_evaluated_rule(
            db, skill, rule_id, expected_rule_version=version
        )


def test_surrogate_excludes_final_attempt_count_from_presim_features():
    features = surrogate._token_features({
        "expression": "rank(close)",
        "attempt_count": 99,
        "generation": 2,
    })
    assert "attempt_count" not in features
    assert features["generation"] == 2.0


def test_scheduler_persists_terminal_rate_limit_attempt(db):
    outcome = db.queue_candidate("rank(close) + 999", {"decay": 6})
    candidate = db.claim_simulation("scheduler-test", candidate_id=outcome.candidate_id)

    class Client:
        def __init__(self):
            self.attempt_history = []
            self.last_request = {}

        def submit(self, expression, settings, candidate_id=None):
            self.attempt_history.append({
                "operation": "POST /simulations", "attempt": 0, "http_status": 429,
                "error_category": "rate_limit", "latency_ms": 1.0,
                "rate_limit_seconds": 7.0, "final": True,
            })
            raise brain_api.RateLimitError("rate limited", 7.0)

        def drain_attempts(self):
            out = list(self.attempt_history)
            self.attempt_history = []
            return out

    scheduler = sim_scheduler.SimulationScheduler(
        db, Client(), adopt_orphans=False, halving=False, staged_search=False,
        sleep=lambda _: None,
    )
    assert scheduler.start(candidate) is False
    events = db.query(
        "SELECT * FROM events WHERE entity='transport' AND candidate_id=? ORDER BY id",
        (outcome.candidate_id,),
    )
    assert len(events) == 1
    assert events[0]["http_status"] == 429
    assert events[0]["error_category"] == "rate_limit"
    assert events[0]["operation"] == "simulation.submit"
