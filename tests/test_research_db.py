"""Tests for the research state store and canonicalization (Priority 4 regression coverage).

Nothing here needs credentials or the network: `batch_simulate` is exercised with a
mocked `run_simulation`, and every store test uses a throwaway research.db.
"""
from __future__ import annotations

import csv
import json
import sys

import pytest

import batch_simulate as bs
import canonical as canon
import research_db as rdb


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


# ---------------------------------------------------------------------------
# canonicalization
# ---------------------------------------------------------------------------


def test_normalize_expression_collapses_formatting_only():
    assert canon.normalize_expression("  rank( TS_MEAN( close , 20.0 ) )  ") == "rank(ts_mean(close,20))"
    assert canon.normalize_expression("ts_mean(close, 20) / ts_std_dev(close, 20)") == \
        canon.normalize_expression("ts_mean(close,20)/ts_std_dev(close,20)")
    assert canon.normalize_expression("trade_when(rank(x)>0.5, ts_mean(close,20), -1)") == \
        "trade_when(rank(x)>0.5,ts_mean(close,20),-1)"


def test_normalize_expression_leaves_field_case_and_unknown_calls_alone():
    # Only identifiers that are real operators are case-folded.
    assert canon.normalize_expression("MyCustomOp(close)") == "MyCustomOp(close)"
    assert canon.normalize_expression("group_rank(Close, subindustry)") == "group_rank(Close,subindustry)"


@pytest.mark.parametrize("value", ["", "   ", None, 5])
def test_normalize_expression_rejects_empty_input(value):
    with pytest.raises(ValueError):
        canon.normalize_expression(value)


def test_normalize_expression_sorts_only_proven_commutative_arguments():
    assert canon.normalize_expression("add(close, open)") == canon.normalize_expression("add(open, close)")
    assert canon.normalize_expression("max(ts_delta(close,5), -ts_delta(close,5))") == \
        canon.normalize_expression("max(-ts_delta(close, 5), ts_delta(close, 5))")
    # subtraction is not commutative, and a keyword argument makes sorting unsafe.
    assert canon.normalize_expression("subtract(close, open)") != canon.normalize_expression("subtract(open, close)")
    assert canon.normalize_expression("add(close, open, filter=true)") != \
        canon.normalize_expression("add(open, close, filter=true)")


def test_normalize_numbers_has_one_spelling_per_value():
    assert canon.normalize_expression("ts_delay(close, 5.0)") == canon.normalize_expression("ts_delay(close,5)")
    assert canon.normalize_expression("ts_delay(close, 1e1)") == canon.normalize_expression("ts_delay(close,10)")
    assert canon.normalize_expression("winsorize(close, std=4.0)") == "winsorize(close,std=4)"


def test_canonical_key_is_order_independent_and_default_aware():
    reference = canon.canonical_key("rank(TS_MEAN(close, 20))", {"region": "usa"})
    assert reference == canon.canonical_key(
        " rank(ts_mean(close, 20.0)) ",
        {"universe": "TOP3000", "region": "USA", "decay": 6, "visualization": True},
    )
    assert reference != canon.canonical_key("rank(ts_mean(close, 20))", {"region": "CHN"})
    assert reference != canon.canonical_key("rank(ts_mean(close, 60))", {"region": "USA"})


def test_canonical_key_separates_expressions_and_settings_hashes():
    key = canon.canonical_key("rank(close)", {"decay": 4})
    assert canon.canonical_key("rank(close)", {"decay": 5}) != key  # settings_only change
    assert canon.canonical_key("rank(open)", {"decay": 4}) != key  # expression_only change
    assert canon.expression_hash("rank( close )") == canon.expression_hash("rank(close)")
    assert canon.settings_hash({"region": " usa "}) == canon.settings_hash({"region": "USA"})


def test_normalize_settings_defaults_and_garbage():
    settings = canon.normalize_settings({"region": " usa ", "decay": "10", "truncation": "0.1"})
    assert settings["region"] == "USA"
    assert settings["decay"] == 10
    assert settings["universe"] == "TOP3000"  # default
    assert "visualization" not in settings
    with pytest.raises(ValueError):
        canon.normalize_settings({"delay": "1.5"})


def test_unknown_settings_still_change_the_key():
    assert canon.canonical_key("rank(close)", {"plot": True}) != canon.canonical_key("rank(close)", {"plot": False})
    assert canon.canonical_key("rank(close)", {"plot": True}) != canon.canonical_key("rank(close)")


def test_skeleton_groups_parameter_grids_without_merging_them():
    assert canon.skeleton_hash("ts_mean(close, 20)") == canon.skeleton_hash("ts_mean(close,60)")
    assert canon.canonical_key("ts_mean(close, 20)") != canon.canonical_key("ts_mean(close, 60)")
    assert canon.fields_of("group_rank(ts_delta(close, 5), subindustry)") == ("close", "subindustry")


# ---------------------------------------------------------------------------
# State machine + persistence
# ---------------------------------------------------------------------------


def test_transition_path_allows_multi_step_but_blocks_immutable_states():
    assert rdb.transition_path("SIMULATING", "IS_PASS") == ["SIMULATED", "IS_PASS"]
    assert rdb.transition_path("QUEUED", "SIMULATED") == ["SIMULATED"]
    assert rdb.transition_path("ACTIVE", "QUEUED") is None
    assert rdb.transition_path("REJECTED", "SUBMISSION_READY") is None


def test_is_gate_uses_brain_checks_then_thresholds():
    passed, reason = rdb.is_gate({"sharpe": 0.4}, [{"name": "LOW_SHARPE", "result": "FAIL"}])
    assert not passed and "LOW_SHARPE" in reason
    assert rdb.is_gate({"sharpe": 9.0}, [{"name": "LOW_SHARPE", "result": "PASS"}])[0]
    assert rdb.is_gate({"sharpe": 1.5, "fitness": 1.2, "turnover": 0.05})[0]
    passed, reason = rdb.is_gate({"sharpe": 1.5, "fitness": 1.2, "turnover": 0.35})
    assert not passed and "turnover" in reason


def test_illegal_status_move_raises_and_force_overrides(db):
    outcome = db.queue_candidate("rank(close)")
    db.set_status(outcome.candidate_id, "SIMULATING")
    with pytest.raises(ValueError):
        db.set_status(outcome.candidate_id, "ACTIVE")
    db.set_status(outcome.candidate_id, "ACTIVE", force=True)
    assert db.get_candidate(outcome.candidate_id)["status"] == "ACTIVE"


def test_restart_keeps_state_and_never_resimulates_a_finished_request(db, tmp_path):
    outcome = db.queue_candidate("rank(close)", {"decay": 4})
    claimed = db.claim_simulation("worker-1")
    assert claimed["id"] == outcome.candidate_id
    db.record_simulation_result(
        candidate_id=claimed["id"], status="DONE",
        metrics={"sharpe": 1.6, "fitness": 1.3, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "PASS"}], brain_alpha_id="A1",
    )
    db.close()

    with rdb.ResearchDB.open(tmp_path / "research.db") as reopened:
        replay = reopened.queue_candidate("RANK( close )", {"decay": "4"})
        assert replay.action == "cache_hit"
        assert replay.status == "SUBMISSION_READY"  # passing work goes to the submission queue
        assert replay.cached["brain_alpha_id"] == "A1"
        assert replay.cached["sharpe"] == 1.6
        assert reopened.claim_simulation("worker-2") is None  # nothing left to simulate


def test_failed_simulation_is_retryable_without_losing_the_cache_slot(db):
    outcome = db.queue_candidate("rank(close)")
    db.claim_simulation("worker-1")
    db.record_simulation_result(candidate_id=outcome.candidate_id, status="ERROR", error="rate_limited",
                                retry_delay_seconds=0)
    assert db.get_candidate(outcome.candidate_id)["status"] == "RETRY"
    assert db.queue_candidate("rank(close)").action == "requeued"
    assert db.claim_simulation("worker-1") is not None


def test_duplicate_queueing_is_deduped_while_in_flight(db):
    first = db.queue_candidate("rank(close)", source="agent-a")
    second = db.queue_candidate(" rank( close ) ", source="agent-b")
    assert first.action == "queued" and second.action == "in_flight"
    assert second.candidate_id == first.candidate_id
    assert db.counts("candidates") == {"QUEUED": 1}


def test_rejected_and_active_candidates_are_not_requeued(db):
    outcome = db.queue_candidate("rank(close)")
    db.claim_simulation("worker-1")
    db.record_simulation_result(
        candidate_id=outcome.candidate_id, status="DONE",
        metrics={"sharpe": 0.2, "fitness": 0.1, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "FAIL"}],
    )
    assert db.get_candidate(outcome.candidate_id)["status"] == "REJECTED"
    replay = db.queue_candidate("rank(close)")
    assert replay.needs_simulation is False  # the failing result is replayed, never re-simulated
    assert replay.action == "cache_hit"
    assert db.claim_simulation("worker-2") is None


def test_claim_simulation_is_priority_first_and_single_winner(db):
    db.queue_candidate("rank(close)", priority=1.0)
    db.queue_candidate("rank(open)", priority=9.0)
    claimed = db.claim_simulation("worker-1")
    assert db.get_candidate(claimed["id"])["expression"] == "rank(open)"
    assert db.claim_simulation("worker-2") is not None
    assert db.claim_simulation("worker-3") is None


def test_expired_lease_is_recovered_as_retry(db):
    outcome = db.queue_candidate("rank(close)")
    db.claim_simulation("worker-1", lease_seconds=-1)
    recovered = db.recover_expired_leases()
    assert recovered["simulations"] == 1
    candidate = db.get_candidate(outcome.candidate_id)
    assert candidate["status"] == "RETRY"
    assert candidate["failure_reason"] == "lease_expired"
    assert db.queue_candidate("rank(close)").action == "requeued"


def test_near_duplicates_are_flagged_not_merged(db):
    first = db.queue_candidate("ts_mean(close, 20)")
    second = db.queue_candidate("ts_mean(close, 60)")
    assert first.canonical_key != second.canonical_key
    assert db.get_candidate(second.candidate_id)["near_duplicate_of"] == first.candidate_id


# ---------------------------------------------------------------------------
# Submission primitives
# ---------------------------------------------------------------------------


def test_submission_queue_is_leased_once(db):
    outcome = db.queue_candidate("rank(close)")
    db.claim_simulation("worker-1")
    db.record_simulation_result(
        candidate_id=outcome.candidate_id, status="DONE",
        metrics={"sharpe": 1.6, "fitness": 1.3, "turnover": 0.05},
        checks=[{"name": "LOW_SHARPE", "result": "PASS"}], brain_alpha_id="A1",
    )
    submission_id = db.enqueue_submission(outcome.candidate_id, priority=2.0)
    claimed = db.claim_submission("worker-1")
    assert claimed["id"] == submission_id
    assert db.claim_submission("worker-2") is None  # the lease prevents double submission

    db.finish_submission(submission_id, "ACTIVE", brain_alpha_id="A1")
    assert db.get_candidate(outcome.candidate_id)["status"] == "ACTIVE"
    assert db.active_alpha_ids() == ["A1"]


def test_expired_submission_lease_never_posted_returns_to_ready(db):
    """A crashed claim that never reached POST is safe to retry immediately."""
    outcome = db.queue_candidate("rank(close)")
    db.claim_simulation("worker-1")
    db.record_simulation_result(candidate_id=outcome.candidate_id, status="DONE",
                                checks=[{"name": "LOW_SHARPE", "result": "PASS"}], brain_alpha_id="A1")
    db.enqueue_submission(outcome.candidate_id)
    db.claim_submission("worker-1", lease_seconds=-1)
    recovered = db.recover_expired_leases()
    assert recovered["submissions"] == 1
    assert db.counts("submissions") == {"READY": 1}


def test_expired_submission_lease_after_post_becomes_reconcilable(db):
    """Once the POST left the process, BRAIN must be consulted before any retry."""
    outcome = db.queue_candidate("rank(close)")
    db.claim_simulation("worker-1")
    db.record_simulation_result(candidate_id=outcome.candidate_id, status="DONE",
                               checks=[{"name": "LOW_SHARPE", "result": "PASS"}], brain_alpha_id="A1")
    db.enqueue_submission(outcome.candidate_id)
    claimed = db.claim_submission("worker-1", lease_seconds=-1)
    db.mark_submission_posted(claimed["id"])

    recovered = db.recover_expired_leases()

    assert recovered["submissions"] == 1
    assert db.counts("submissions") == {"CHECK_PENDING": 1}
    row = db.query("SELECT * FROM submissions")[0]
    assert "reconcile" in row["message"]


def _check_pending_submission(db, brain_alpha_id="A1") -> int:
    """A submission whose POST outcome is unknown, i.e. reconcilable."""
    outcome = db.queue_candidate("rank(close)")
    db.claim_simulation("worker-1")
    db.record_simulation_result(candidate_id=outcome.candidate_id, status="DONE",
                                checks=[{"name": "LOW_SHARPE", "result": "PASS"}],
                                brain_alpha_id=brain_alpha_id)
    submission_id = db.enqueue_submission(outcome.candidate_id)
    db.claim_submission("worker-1", lease_seconds=-1)
    db.mark_submission_posted(submission_id)
    db.recover_expired_leases()
    return submission_id


def test_claim_reconcile_throttles_repeat_queries_per_row(db):
    """One PENDING row is claimed once per TTL window, so overlapping runs cannot duplicate it."""
    submission_id = _check_pending_submission(db)

    assert db.claim_reconcile(submission_id, ttl_seconds=300, owner="run-a") is True
    assert db.claim_reconcile(submission_id, ttl_seconds=300, owner="run-b") is False
    # A deliberate zero-TTL sweep is still allowed.
    assert db.claim_reconcile(submission_id, ttl_seconds=0, owner="run-b") is True

    row = db.query("SELECT * FROM submissions WHERE id=?", (submission_id,))[0]
    assert row["status"] == "CHECK_PENDING"
    assert row["last_reconciled_at"] is not None


def test_claim_reconcile_refuses_rows_that_are_not_check_pending(db):
    outcome = db.queue_candidate("rank(close)")
    db.claim_simulation("worker-1")
    db.record_simulation_result(candidate_id=outcome.candidate_id, status="DONE",
                                checks=[{"name": "LOW_SHARPE", "result": "PASS"}], brain_alpha_id="A1")
    submission_id = db.enqueue_submission(outcome.candidate_id)

    assert db.claim_reconcile(submission_id, ttl_seconds=0) is False


def test_leaving_check_pending_clears_the_reconcile_stamp(db):
    """A fresh POST cycle must not inherit the previous pending streak's throttle."""
    submission_id = _check_pending_submission(db)
    assert db.claim_reconcile(submission_id, ttl_seconds=300) is True

    db.finish_submission(submission_id, "READY", message="reconciled: proved not submitted")
    row = db.query("SELECT * FROM submissions WHERE id=?", (submission_id,))[0]
    assert row["last_reconciled_at"] is None

    # A manual requeue of an open row opens a fresh reconcile window too.
    db.query("UPDATE submissions SET status='CHECK_PENDING'")
    assert db.claim_reconcile(submission_id, ttl_seconds=300) is True
    db.enqueue_submission(int(row["candidate_id"]))
    row = db.query("SELECT * FROM submissions WHERE id=?", (submission_id,))[0]
    assert row["status"] == "READY"
    assert row["last_reconciled_at"] is None


def test_submission_checks_are_cached_per_alpha(db):
    written = db.record_submission_checks("A1", [
        {"name": "SELF_CORRELATION", "result": "PENDING", "value": 0.4, "limit": 0.7},
        {"name": "LOW_SHARPE", "result": "PASS"},
    ], submission_id=7, candidate_id=3)
    assert written == 2

    checks = {c["name"]: c for c in db.latest_submission_checks("A1")}
    assert checks["SELF_CORRELATION"]["result"] == "PENDING"
    assert checks["SELF_CORRELATION"]["value"] == 0.4

    # A later fetch upserts the same alpha instead of stacking duplicate rows.
    db.record_submission_checks("A1", [{"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.91}],
                                submission_id=7)
    checks = {c["name"]: c for c in db.latest_submission_checks("A1")}
    assert checks["SELF_CORRELATION"]["result"] == "FAIL"
    assert checks["LOW_SHARPE"]["result"] == "PASS"  # unrelated checks survive the upsert
    assert db.latest_submission_checks("A2") == []


def test_coordination_gate_limits_concurrent_holders(db):
    assert db.acquire_gate("reconcile", "run-a", slots=1, lease_seconds=600) is True
    assert db.acquire_gate("reconcile", "run-b", slots=1, lease_seconds=600) is False
    assert db.acquire_gate("reconcile", "run-a", slots=1, lease_seconds=600) is True  # refresh is idempotent

    assert db.release_gate("reconcile", "run-a") is True
    assert db.acquire_gate("reconcile", "run-b", slots=1, lease_seconds=600) is True
    assert db.acquire_gate("reconcile", "run-c", slots=2, lease_seconds=600) is True
    assert db.release_gate("reconcile", "run-missing") is False


def test_coordination_gate_reclaims_an_expired_slot(db):
    assert db.acquire_gate("reconcile", "crashed", slots=1, lease_seconds=-1) is True
    assert db.acquire_gate("reconcile", "fresh", slots=1, lease_seconds=600) is True


def test_schema_carries_reconcile_throttle_and_check_cache(db):
    submission_columns = {row["name"] for row in db.query("PRAGMA table_info(submissions)")}
    assert "last_reconciled_at" in submission_columns
    tables = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"submission_checks", "coordination_gates"} <= tables


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_queue_and_status(tmp_path, capsys, monkeypatch):
    csv_path = tmp_path / "input.csv"
    csv_path.write_text(
        "code,region,decay\n"
        '"rank(close)",USA,6\n'
        '" rank( close ) ",USA,6\n'
        '"ts_mean(close, 20)",USA,6\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "cli.db"
    assert rdb.main(["--db", str(db_path), "queue", str(csv_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcomes"] == {"queued": 2, "in_flight": 1}

    assert rdb.main(["--db", str(db_path), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["candidates"] == {"QUEUED": 2}
    assert status["simulations"] == {"QUEUED": 2}


def test_cli_cache_miss_exits_nonzero(tmp_path, capsys):
    assert rdb.main(["--db", str(tmp_path / "cli.db"), "cache", "rank(close)"]) == 1
    capsys.readouterr()


# ---------------------------------------------------------------------------
# batch_simulate integration
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload=None, headers=None):
        self._payload = payload
        self.headers = headers or {}
        self.status_code = 200
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


def _write_input(tmp_path, rows: str):
    path = tmp_path / "in.csv"
    path.write_text(rows, encoding="utf-8")
    return path


def _seed_completed(db_path, expression: str, settings: dict, sharpe: float = 1.5):
    with rdb.ResearchDB.open(db_path) as store:
        outcome = store.queue_candidate(expression, settings)
        claimed = store.claim_simulation("seed")
        store.record_simulation_result(
            candidate_id=claimed["id"], status="DONE",
            metrics={"sharpe": sharpe, "fitness": 1.2, "turnover": 0.05},
            checks=[
                {"name": "LOW_SHARPE", "result": "PASS"},
                {"name": "CONCENTRATED_WEIGHT", "result": "PASS"},
                {"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "PASS", "value": 1.1},
            ],
            brain_alpha_id="CACHED1",
        )
        return outcome.candidate_id


def test_batch_simulate_serves_a_cached_request_without_touching_brain(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bs, "DATA_DIR", tmp_path)
    monkeypatch.setenv("WQ_RESEARCH_DB", str(tmp_path / "research.db"))
    _seed_completed(tmp_path / "research.db", "rank(open - close)", {"decay": 10, "universe": "TOP3000"})

    input_path = _write_input(tmp_path, 'code,decay,universe\n"rank(open - close)",10,TOP3000\n')

    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("a cached request must not be simulated again")

    monkeypatch.setattr(bs, "run_simulation", explode)
    monkeypatch.setattr(bs, "get_session", lambda: object())
    monkeypatch.setattr(sys, "argv", ["batch_simulate.py", str(input_path)])

    assert bs.main() == 0
    out = capsys.readouterr().out
    assert "Reusing 1 cached result(s)" in out

    rows = list(csv.DictReader(sorted(tmp_path.glob("results_*.csv"))[-1].open()))
    assert len(rows) == 1
    assert rows[0]["sharpe"] == "1.5"
    assert rows[0]["turnover"] == "5.0"
    assert "alpha/CACHED1" in rows[0]["link"]


def test_batch_simulate_dedupes_identical_rows_and_records_results(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "DATA_DIR", tmp_path)
    db_path = tmp_path / "research.db"
    monkeypatch.setenv("WQ_RESEARCH_DB", str(db_path))
    input_path = _write_input(
        tmp_path,
        "code,decay\n"
        '"rank(close)",6\n'
        '" rank( close ) ",6\n'
        '"ts_mean(close, 20)",6\n',
    )
    monkeypatch.setattr(bs, "get_session", lambda: object())

    calls = {"n": 0}

    def fake_run(session, sim, max_wait=0):
        calls["n"] += 1
        return {
            **sim, "passed": 2, "sharpe": 1.4, "fitness": 1.2, "turnover": 6.0, "turnover_fraction": 0.06,
            "link": "https://platform.worldquantbrain.com/alpha/A9", "alpha_id": "A9", "simulation_id": "sim1",
            "checks": [{"name": "LOW_SHARPE", "result": "PASS"}, {"name": "LOW_FITNESS", "result": "PASS"}],
        }

    monkeypatch.setattr(bs, "run_simulation", fake_run)
    monkeypatch.setattr(sys, "argv", ["batch_simulate.py", str(input_path)])

    assert bs.main() == 0
    assert calls["n"] == 2  # the formatting clone never reached BRAIN

    with rdb.ResearchDB.open(db_path) as store:
        assert store.counts("candidates") == {"SUBMISSION_READY": 2}
        assert store.counts("submissions") == {"READY": 2}
        assert store.cache_lookup("rank(close)", {"decay": 6})["brain_alpha_id"] == "A9"
    rows = list(csv.DictReader(sorted(tmp_path.glob("results_*.csv"))[-1].open()))
    assert len(rows) == 2
    assert {row["code"] for row in rows} == {"rank(close)", "ts_mean(close, 20)"}


def test_batch_simulate_records_failures_for_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "DATA_DIR", tmp_path)
    db_path = tmp_path / "research.db"
    monkeypatch.setenv("WQ_RESEARCH_DB", str(db_path))
    input_path = _write_input(tmp_path, 'code\n"rank(close)"\n')
    monkeypatch.setattr(bs, "get_session", lambda: object())
    monkeypatch.setattr(
        bs, "run_simulation",
        lambda session, sim, max_wait=0: {**sim, "error": "simulation_error: unsupported operator"},
    )
    monkeypatch.setattr(sys, "argv", ["batch_simulate.py", str(input_path), "--retries", "0"])

    assert bs.main() == 0
    with rdb.ResearchDB.open(db_path) as store:
        candidate = store.find_candidate("rank(close)")
        assert candidate["status"] == "RETRY"
        assert "unsupported operator" in candidate["failure_reason"]
        assert store.counts("simulations") == {"ERROR": 1}


def test_batch_simulate_skips_invalid_settings_before_calling_brain(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bs, "DATA_DIR", tmp_path)
    monkeypatch.setenv("WQ_RESEARCH_DB", str(tmp_path / "research.db"))
    input_path = _write_input(tmp_path, "code,decay\n\"rank(close)\",nope\n")
    monkeypatch.setattr(bs, "get_session", lambda: object())

    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("invalid settings must not reach run_simulation")

    monkeypatch.setattr(bs, "run_simulation", explode)
    monkeypatch.setattr(sys, "argv", ["batch_simulate.py", str(input_path)])

    assert bs.main() == 0
    assert "Nothing to do" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# schema migration: an existing research.db gains new columns in place
# ---------------------------------------------------------------------------


def test_open_upgrades_a_pre_v3_database_in_place(tmp_path):
    """A database predating the V3 columns must still open: ALTER before CREATE INDEX.

    The fixture is built by taking a current database and dropping the V3 columns back out,
    which is exactly what an older research.db looks like to this code path.
    """
    import sqlite3

    path = tmp_path / "legacy.db"
    with rdb.ResearchDB.open(path) as store:
        store.queue_candidate("rank(close)", {"decay": 6}, signal_family="pv1")

    dropped = (
        "generator_strategy", "generation_mode", "motif_id", "recipe_id", "recipe_index",
        "grammar_skeleton_hash", "semantic_skeleton_hash", "source_profile_json",
        "generator_policy_version", "grammar_version",
    )
    conn = sqlite3.connect(str(path))
    for index in ("idx_candidates_grammar", "idx_candidates_motif", "idx_research_trials_motif"):
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    for column in dropped:
        conn.execute(f"ALTER TABLE candidates DROP COLUMN {column}")
    conn.execute("ALTER TABLE research_trials DROP COLUMN motif_id")
    conn.commit()
    conn.close()

    with rdb.ResearchDB.open(path) as store:
        columns = {row["name"] for row in store.query("PRAGMA table_info(candidates)")}
        trial_columns = {row["name"] for row in store.query("PRAGMA table_info(research_trials)")}
        indexes = {row["name"] for row in store.query(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name IN ('candidates','research_trials')"
        )}
        assert {"generator_strategy", "generation_mode", "motif_id", "recipe_id", "recipe_index",
                "grammar_skeleton_hash", "semantic_skeleton_hash",
                "source_profile_json"} <= columns
        assert {"motif_id", "decision", "skip_reason"} <= trial_columns
        assert {"idx_candidates_grammar", "idx_candidates_motif", "idx_research_trials_motif"} <= indexes
        # The pre-existing row survives the upgrade untouched.
        row = store.get_candidate(1)
        assert row["expression"] == "rank(close)"
        assert row["grammar_skeleton_hash"] is None
