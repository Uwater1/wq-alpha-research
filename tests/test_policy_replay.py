"""Offline tests for advisory-rule calibration and the P5 policy replay.

Two halves of the same question: *which* local screening rules does history show BRAIN
actually refuses, and *would acting on that evidence have produced a better funnel?* The
tests here hold the answer to the point-in-time contract, because a benchmark that can see
the future is worse than no benchmark at all.

Nothing in this suite touches the network or credentials.
"""
from __future__ import annotations

import itertools
import json

import pytest

import canonical
import compatibility
import finding_calibration as calibration
import policy_replay
import research_db as rdb
import validate

GROUP_ARGUMENT_FINDING = compatibility.CODE_GROUP_ARGUMENT


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


# ---------------------------------------------------------------------------
# Corpus helpers
# ---------------------------------------------------------------------------


#: A sequential campaign: each candidate arrives as its own generation wave. Without a
#: distinct source per call the whole test would collapse into one wave, because every
#: candidate is queued inside the same clock second.
_RUNS = itertools.count(1)


def _settle(db, expression, *, accepted=True, family="momentum", settings=None, sharpe=None,
            source=None, is_fail=False):
    """Queue one expression and settle its simulation: DONE = BRAIN accepted the request.

    ``is_fail`` is the third outcome that matters: BRAIN accepted and evaluated the request,
    the IS gate refused the alpha. It costs a slot and yields nothing — and it is *not* evidence
    that any local screening rule was right about the request.
    """
    outcome = db.queue_candidate(expression, settings or {"decay": 6}, signal_family=family,
                                 source=source or f"run-{next(_RUNS)}")
    assert outcome.action in ("queued", "requeued"), outcome.action
    db.claim_simulation("seed", candidate_id=outcome.candidate_id)
    if accepted:
        db.record_simulation_result(
            candidate_id=outcome.candidate_id, status="DONE",
            metrics={"sharpe": 0.4 if is_fail else (sharpe if sharpe is not None else 1.4),
                     "fitness": 0.3 if is_fail else 1.1, "turnover": 0.4 if is_fail else 0.06},
            checks=[{"name": "LOW_SHARPE", "result": "FAIL" if is_fail else "PASS"}],
            brain_alpha_id=f"LOCAL{outcome.candidate_id}",
        )
    else:
        db.record_simulation_result(
            candidate_id=outcome.candidate_id, status="ERROR",
            error="an error message whose text must never be reported verbatim",
        )
    return outcome.candidate_id


#: Three distinct expressions that trip the same type rule but have no shared canonical key.
GROUP_ARGUMENT_EXPRESSIONS = (
    "group_rank(close, close)",
    "group_rank(open, open)",
    "group_rank(high, high)",
)


# ---------------------------------------------------------------------------
# Calibration: measure, never guess
# ---------------------------------------------------------------------------


def test_calibration_measures_a_rule_against_real_platform_outcomes(db):
    for index, expression in enumerate(GROUP_ARGUMENT_EXPRESSIONS):
        _settle(db, expression, accepted=index == 0)  # 2 refused, 1 accepted
    report = calibration.refresh(db, min_samples=1, strict_threshold=0.75)

    finding = next(item for item in report["findings"] if item["code"] == GROUP_ARGUMENT_FINDING)
    assert (finding["samples"], finding["rejections"], finding["accepted"]) == (3, 2, 1)
    assert finding["reject_rate"] == pytest.approx(2 / 3)
    assert report["recommended_policy"] == {}  # 2/3 is below a 0.75 strict threshold
    assert report["observations"] == {"total": 3, "accepted": 1, "rejected": 2}


def test_calibration_promotes_only_rules_history_refuses(db):
    for expression in GROUP_ARGUMENT_EXPRESSIONS:
        _settle(db, expression, accepted=False)
    report = calibration.refresh(db, min_samples=1, strict_threshold=0.8)

    assert report["recommended_policy"] == {GROUP_ARGUMENT_FINDING: validate.SEVERITY_ERROR}
    # One accepted run with the same code drops 3/3 to 3/4: the rule must stop being promoted.
    _settle(db, "group_rank(volume, volume)", accepted=True)
    report = calibration.refresh(db, min_samples=1, strict_threshold=0.8)
    assert report["recommended_policy"] == {}


def test_calibration_never_counts_a_candidate_the_local_gate_refused(db):
    # A rule that blocks its own inputs would erase its own evidence.
    rejected = db.queue_candidate("not_a_real_field + close", {"decay": 6})
    assert rejected.action == "rejected_invalid"
    for expression in GROUP_ARGUMENT_EXPRESSIONS:
        _settle(db, expression, accepted=False)

    report = calibration.refresh(db, min_samples=1, strict_threshold=0.6)
    rows, _excluded = calibration.observations(db)

    # A locally refused candidate never reached BRAIN, so it adds no evidence either way.
    assert rejected.candidate_id not in {row.candidate_id for row in rows}
    assert report["observations"]["total"] == 3


def test_calibration_can_be_computed_as_of_a_decision_clock(db):
    first = _settle(db, GROUP_ARGUMENT_EXPRESSIONS[0], accepted=False)
    second = _settle(db, GROUP_ARGUMENT_EXPRESSIONS[1], accepted=False)
    rows, _excluded = calibration.observations(db)
    clocks = {row.candidate_id: row.settled_clock for row in rows}
    assert clocks[first] is not None and clocks[second] is not None
    assert clocks[first] < clocks[second]

    early = calibration.calibrate_from(rows, as_of_clock=clocks[first], min_samples=1)
    assert early["observations"]["total"] == 0  # nothing had settled *before* that event
    assert early["recommended_policy"] == {}

    later = calibration.calibrate_from(rows, as_of_clock=clocks[second] + 1, min_samples=1,
                                       strict_threshold=0.6)
    assert later["observations"]["total"] == 2
    assert later["as_of_clock"] == clocks[second] + 1


def test_unexplained_rejections_are_the_candidate_list_for_new_rules(db):
    _settle(db, "rank(ts_delta(close, 5))", accepted=False)
    rows, _excluded = calibration.observations(db)

    report = calibration.rejections_without_findings(rows)

    assert report["count"] == 1
    assert report["error_classes"]  # sanitized classes only


def _measured_recommendation(db, **kwargs):
    """The policy the calibration proposes for the group-argument rule."""
    report = calibration.refresh(db, min_samples=1, strict_threshold=0.6, **kwargs)
    return calibration.recommended_policy(report)


def test_approval_is_enforced_and_reversible_once_the_benchmark_agrees(db):
    for expression in GROUP_ARGUMENT_EXPRESSIONS:
        _settle(db, expression, accepted=False)
    for index in range(3):
        _settle(db, f"rank(ts_delta(close, {5 + index}))", accepted=True)
    proposed = _measured_recommendation(db)

    assert db.load_severity_policy() == {}  # measuring alone changes nothing
    assert validate.validate("group_rank(low, low)").ok is True  # advisory still, by default

    result = calibration.approve(db, proposed, budget=6)

    assert result["enforced"] == proposed
    assert result["refused"] == {}
    evidence = result["evidence"][GROUP_ARGUMENT_FINDING]
    assert evidence["verdict"] == "improved"
    assert evidence["leakage"]["finding_gate"] == "passed"
    assert evidence["refused_decisions"]  # the baseline really did buy the refused work
    assert evidence["gate"]["simulations_used"] < evidence["baselines"]["fifo"]["simulations_used"]
    assert evidence["gate"]["is_pass_from_simulated"] == evidence["baselines"]["fifo"]["is_pass_from_simulated"]

    assert db.queue_candidate("group_rank(low, low)", {"decay": 6}).action == "rejected_invalid"

    calibration.clear(db)
    # A different expression, because the rejected one is already settled history.
    assert db.queue_candidate("group_rank(vwap, vwap)", {"decay": 6}).action == "queued"


def test_approval_is_refused_when_the_rule_would_decline_everything(db):
    """A rule that blocks the whole corpus proves nothing, however accurate it looks."""
    for expression in GROUP_ARGUMENT_EXPRESSIONS:
        _settle(db, expression, accepted=False)
    proposed = _measured_recommendation(db)

    result = calibration.approve(db, proposed, budget=3)

    assert result["enforced"] == {}
    assert result["refused"][GROUP_ARGUMENT_FINDING]["verdict"] == "declines_everything"
    assert db.load_severity_policy() == {}
    assert db.queue_candidate("group_rank(low, low)", {"decay": 6}).action == "queued"


def test_approval_is_refused_when_the_rule_would_refuse_passing_work(db):
    """The asymmetry that a rejection rate cannot see: refusing a pass costs a pass."""
    # These trip POSITIONAL_OPTIONAL_ARGUMENT and every one of them PASSED on BRAIN.
    for index in range(3):
        _settle(db, f"hump(rank(ts_mean(close, {10 + index})), 0.005)", accepted=True)
    for index in range(3):
        _settle(db, f"rank(ts_delta(close, {5 + index}))", accepted=True)
    proposed = {validate.CODE_POSITIONAL_OPTIONAL_ARGUMENT: validate.SEVERITY_ERROR}

    evidence = policy_replay.evaluate_severity_policy(db, proposed, budget=6)
    result = calibration.approve(db, proposed, budget=6)

    assert evidence["verdict"] == "regression"
    assert any("lose passes" in reason for reason in evidence["reasons"])
    assert result["enforced"] == {}
    assert result["refused"][validate.CODE_POSITIONAL_OPTIONAL_ARGUMENT]["verdict"] == "regression"


def test_a_rule_with_no_baseline_exposure_has_no_evidence_either_way(db):
    for index in range(3):
        _settle(db, f"rank(ts_delta(close, {5 + index}))", accepted=True)

    evidence = policy_replay.evaluate_severity_policy(
        db, {GROUP_ARGUMENT_FINDING: validate.SEVERITY_ERROR}, budget=3
    )

    assert evidence["verdict"] == "insufficient_evidence"
    assert evidence["flagged_candidates"] == 0
    assert evidence["enforceable"] is False


def test_the_funnel_view_can_back_a_code_the_rejection_rate_calls_inconclusive(db):
    """The two views answer different questions, and they disagree in both directions."""
    # Refused by BRAIN and, on this corpus, the only work that ever fails: the rate is 1.0.
    for expression in GROUP_ARGUMENT_EXPRESSIONS:
        _settle(db, expression, accepted=False)
    # Accepted by BRAIN (so the refusal rate is 0.0 and the rate view calls it inconclusive) but
    # an IS failure every time: flagging it is still free capacity, because nothing that carried
    # it ever passed.
    for index in range(3):
        _settle(db, f"hump(rank(ts_mean(close, {30 + index})), 0.005)", accepted=True,
                is_fail=True, source=f"hump-{index}")
    for index in range(3):
        _settle(db, f"rank(ts_delta(close, {5 + index}))", accepted=True)
    report = calibration.refresh(db, min_samples=1, strict_threshold=0.6, funnel=True)

    by_code = report["funnel"]["by_code"]
    assert by_code[GROUP_ARGUMENT_FINDING]["verdict"] == "improved"
    assert by_code[validate.CODE_POSITIONAL_OPTIONAL_ARGUMENT]["verdict"] == "improved"
    assert report["funnel"]["supported"] == sorted(
        [GROUP_ARGUMENT_FINDING, validate.CODE_POSITIONAL_OPTIONAL_ARGUMENT]
    )

    # The rate-based shortlist and the funnel shortlist are independent, and a code the rate
    # view never recommended can still be proposed by name and approved on its own evidence.
    # Approval covers the whole corpus by default: a scarce budget would only reach the first
    # generation wave and report no evidence for anything tripped after it.
    proposed = {validate.CODE_POSITIONAL_OPTIONAL_ARGUMENT: validate.SEVERITY_ERROR}
    assert calibration.approve(db, proposed)["enforced"] == proposed
    # …and with a budget too small to reach them, there is no exposure to learn from at all.
    assert calibration.approve(db, proposed, budget=3)["refused"][
        validate.CODE_POSITIONAL_OPTIONAL_ARGUMENT]["verdict"] == "insufficient_evidence"
    calibration.clear(db)


def test_an_empty_policy_enforces_nothing_and_an_override_is_reported(db):
    for expression in GROUP_ARGUMENT_EXPRESSIONS:
        _settle(db, expression, accepted=False)
    proposed = _measured_recommendation(db)

    assert calibration.approve(db, {})["enforced"] == {}

    forced = calibration.approve(db, proposed, budget=3, force=True)
    assert forced["enforced"] == proposed
    assert forced["evidence"]["verdict"] == "overridden"
    assert db.load_severity_policy() == proposed

    skipped = calibration.approve(db, proposed, require_replay=False)
    assert skipped["evidence"]["verdict"] == "skipped"
    assert skipped["enforced"] == proposed


def test_approving_an_unknown_severity_is_refused(db):
    with pytest.raises(ValueError, match="unknown severity"):
        calibration.approve(db, {GROUP_ARGUMENT_FINDING: "reject"})
    with pytest.raises(ValueError, match="unknown severity"):
        validate.validate("rank(close)", severity_policy={"ANY": "loud"})


# ---------------------------------------------------------------------------
# Replay: the point-in-time contract
# ---------------------------------------------------------------------------


def test_replay_charges_a_slot_for_work_that_had_not_settled(db):
    """Regression: second-granularity timestamps must not turn every choice into a cache hit."""
    for index in range(4):
        _settle(db, f"rank(ts_delta(close, {5 + index}))", accepted=True)

    environment = policy_replay.ReplayEnvironment.from_db(db, budget=4)
    report = policy_replay.replay(environment, policy_replay.build_policy("fifo", environment=environment))

    assert report["leakage_check"]["status"] == "passed"
    assert report["metrics"]["decisions"] == 4
    # The bug this guards: every candidate created and settled inside the same second used to
    # look settled at the moment it was created, so nothing ever spent a slot.
    assert report["metrics"]["simulations_used"] == 4
    assert report["metrics"]["cache_hit_decisions"] == 0


def test_replay_orders_the_corpus_by_event_clock_not_timestamp(db):
    first = _settle(db, "rank(close)", accepted=True)
    second = _settle(db, "rank(open)", accepted=True)

    environment = policy_replay.ReplayEnvironment.from_db(db)
    assert [item.candidate_id for item in environment.items] == [first, second]
    assert environment.items[0].created_clock < environment.items[1].created_clock
    assert environment.items[0].settled_clock < environment.items[1].settled_clock


def test_settlement_is_visible_only_strictly_before_the_decision_clock(db):
    first = _settle(db, "rank(close)", accepted=True)
    _settle(db, "rank(open)", accepted=True)
    environment = policy_replay.ReplayEnvironment.from_db(db)
    settled_clock = environment.item(first).settled_clock

    assert environment.outcome_if_settled(first, settled_clock) is None  # the decision instant
    assert environment.outcome_if_settled(first, settled_clock - 1) is None
    assert environment.outcome_if_settled(first, settled_clock + 1) is not None


class _Waiter(policy_replay.Policy):
    """Declines the first decision, then takes the oldest available candidate."""

    name = "waiter"

    def order(self, available, context):
        if context.step == 0:
            return []
        return [card.candidate_id for card in sorted(available, key=lambda card: card.creation_order)]


def test_waiting_can_reuse_already_settled_results_for_free(db):
    for index in range(4):
        _settle(db, f"rank(ts_mean(close, {10 + index}))", accepted=True)

    environment = policy_replay.ReplayEnvironment.from_db(db, budget=4)
    report = policy_replay.replay(environment, _Waiter(environment=environment))
    metrics = report["metrics"]
    by_id = {entry["candidate_id"]: entry for entry in report["decisions"]}

    # The first decision declines; the next round opens once the second candidate has arrived,
    # by which point the first one had already settled — so taking it costs nothing at all.
    first = environment.items[0].candidate_id
    assert by_id[first]["cache_hit"] is True and by_id[first]["slot_cost"] == 0
    assert metrics["cache_hit_decisions"] == 1
    assert metrics["simulations_used"] == 3  # the three candidates nobody had run yet
    assert metrics["is_pass_from_simulated"] == 3

    # Charging for cached work is the pessimistic bound. Note a declined decision opportunity
    # is not refunded: pure waiting can only ever recover the work already settled.
    no_cache = policy_replay.ReplayEnvironment.from_db(db, budget=4, allow_cache_hits=False)
    charged = policy_replay.replay(no_cache, _Waiter(environment=no_cache))
    assert charged["metrics"]["simulations_used"] == 4


def test_work_arriving_together_forms_one_decision_round(db):
    batch = [_settle(db, f"rank(ts_mean(close, {10 + index}))", accepted=True, source="batch-1")
             for index in range(2)]
    late = _settle(db, "rank(ts_std_dev(close, 7))", accepted=True, source="batch-2")

    environment = policy_replay.ReplayEnvironment.from_db(db, budget=6)
    waves = environment.waves()

    # Same source, same second: one batch is one decision round, so the policy has a real
    # choice inside it. The later batch opens a second round.
    assert len(waves) == 2
    assert len({environment.item(candidate_id).wave_clock for candidate_id in batch}) == 1
    assert environment.item(late).wave_clock == waves[-1]

    report = policy_replay.replay(environment, policy_replay.build_policy("fifo", environment=environment))
    first_round = [entry for entry in report["decisions"] if entry["clock"] == waves[0]]
    assert sorted(entry["candidate_id"] for entry in first_round) == sorted(batch)


def test_a_round_cannot_learn_from_its_own_picks(db):
    """Outcomes that land mid-round stay invisible until the next wave."""
    first = db.queue_candidate("rank(ts_delta(close, 5))", {"decay": 6}, source="batch-1")
    second = db.queue_candidate("rank(ts_delta(open, 5))", {"decay": 6}, source="batch-1")
    for queued in (first, second):
        db.claim_simulation("seed", candidate_id=queued.candidate_id)
        db.record_simulation_result(
            candidate_id=queued.candidate_id, status="DONE",
            metrics={"sharpe": 1.4, "fitness": 1.1, "turnover": 0.06},
            checks=[{"name": "LOW_SHARPE", "result": "PASS"}],
            brain_alpha_id=f"LOCAL{queued.candidate_id}",
        )
    # Both results landed after the second candidate arrived, so both are still unknowable at
    # the round that has to decide about them.
    environment = policy_replay.ReplayEnvironment.from_db(db, budget=2)
    seen: list[tuple[int, int, bool]] = []

    class Recorder(policy_replay.Policy):
        name = "recorder"

        def order(self, available, context):
            for item in self.environment.items:
                seen.append((context.step, item.candidate_id,
                             context.recorded_outcome(item.candidate_id) is not None))
            return [card.candidate_id for card in sorted(available, key=lambda card: card.creation_order)]

    report = policy_replay.replay(environment, Recorder(environment=environment))

    assert report["leakage_check"]["status"] == "passed"
    assert report["metrics"]["cache_hit_decisions"] == 0
    assert report["metrics"]["simulations_used"] == 2
    assert all(visible is False for _step, _candidate, visible in seen)


def test_a_policy_cannot_read_an_outcome_that_had_not_settled(db):
    for index in range(3):
        _settle(db, f"rank(ts_mean(close, {10 + index}))", accepted=True)

    seen: list[tuple[int, bool]] = []

    class Peeker(policy_replay.Policy):
        name = "peeker"

        def order(self, available, context):
            settled_by_clock = {
                item.candidate_id: item.settled_clock for item in self.environment.items
            }
            for card in available:
                clock = settled_by_clock.get(card.candidate_id)
                # Ask for the outcome at the candidate's own settlement event: not yet knowable.
                if clock is not None:
                    seen.append((card.candidate_id, context.recorded_outcome(card.candidate_id) is not None))
            return [card.candidate_id for card in available]

    environment = policy_replay.ReplayEnvironment.from_db(db)
    policy_replay.replay(environment, Peeker(environment=environment))

    # The first decision point is the first candidate's creation: nothing had settled yet.
    first_card = environment.items[0].candidate_id
    assert (first_card, False) in seen


def test_no_expression_or_outcome_leaks_into_a_card(db):
    _settle(db, "rank(ts_delta(close, 5))", accepted=True)
    environment = policy_replay.ReplayEnvironment.from_db(db)

    card = environment.items[0].card
    visible = card.as_dict()
    assert set(visible) <= policy_replay.VISIBLE_CARD_FIELDS
    assert "expression" not in visible
    assert "rank(" not in repr(card)  # the expression itself is not even in the repr
    assert not {"sharpe", "fitness", "turnover", "self_corr", "is_pass"} & set(visible)
    assert card.expression_hash and "rank(" not in card.expression_hash


def test_a_look_ahead_finding_is_reported_as_leakage(db):
    """The self-check must fail loudly if a decision ever saw the future."""
    _settle(db, "rank(close)", accepted=True)
    _settle(db, "rank(open)", accepted=True)
    environment = policy_replay.ReplayEnvironment.from_db(db)
    forged = policy_replay.Decision(
        step=0, clock=environment.items[0].created_clock, as_of="", candidate_id=environment.items[1].candidate_id,
        slot_cost=0, cache_hit=True, considered=(environment.items[1].candidate_id,),
    )

    checks = policy_replay.verify_no_leakage([forged], environment)

    assert checks["status"] == "failed"
    assert any("created after" in problem for problem in checks["problems"])


def test_local_rejections_cost_no_capacity(db):
    rejected = db.queue_candidate("not_a_real_field + close", {"decay": 6})
    assert rejected.action == "rejected_invalid"
    for index in range(3):
        _settle(db, f"rank(ts_std_dev(close, {5 + index}))", accepted=True)

    environment = policy_replay.ReplayEnvironment.from_db(db, budget=10)
    report = policy_replay.replay(environment, policy_replay.build_policy("fifo", environment=environment))

    assert report["metrics"]["decisions"] == 4
    assert report["metrics"]["local_reject_decisions"] == 1
    assert report["metrics"]["simulations_used"] == 3


# ---------------------------------------------------------------------------
# Replay: scoring and comparison
# ---------------------------------------------------------------------------


def test_budget_is_never_exceeded_and_the_funnel_is_a_partition(db):
    for index in range(6):
        _settle(db, f"rank(ts_delta(close, {5 + index}))", accepted=index % 2 == 0)

    environment = policy_replay.ReplayEnvironment.from_db(db)
    report = policy_replay.replay(environment, policy_replay.build_policy("fifo", environment=environment),
                                  budget=3)
    metrics = report["metrics"]

    assert metrics["simulations_used"] <= 3
    assert metrics["is_pass_from_simulated"] + metrics["wasted_variants"] == metrics["simulations_used"]
    assert 0.0 <= metrics["top_k_success_recall"] <= 1.0
    assert metrics["oracle_objective_version"] == policy_replay.ORACLE_OBJECTIVE_VERSION


def test_comparison_is_deterministic_and_persisted(db):
    for index in range(5):
        _settle(db, f"rank(ts_delta(close, {5 + index}))", accepted=index % 2 == 0)

    first = policy_replay.compare(db, ["fifo", "ranking"], budget=3, seed=7)
    second = policy_replay.compare(db, ["fifo", "ranking"], budget=3, seed=7)

    assert first["comparison_id"] != second["comparison_id"]
    for name in ("fifo", "ranking"):
        assert first["policies"][name]["decisions"] == second["policies"][name]["decisions"]
        assert first["policies"][name]["metrics"] == second["policies"][name]["metrics"]
        assert first["policies"][name]["delta_vs_baseline"] == second["policies"][name]["delta_vs_baseline"]
    assert first["baseline"] == "fifo"
    assert first["policies"]["fifo"]["delta_vs_baseline"]["is_pass_from_simulated"]["delta"] == 0

    runs = db.policy_replay_runs(first["comparison_id"])
    assert {run["policy_name"] for run in runs} == {"fifo", "ranking"}
    assert sum(run["is_baseline"] for run in runs) == 1
    assert runs[0]["metrics"]["metrics"]["simulations_used"] >= 0


def test_every_registered_policy_replays_without_leaking(db):
    for index in range(6):
        _settle(db, f"rank(ts_delta(close, {5 + index}))", accepted=index % 2 == 0)
    _settle(db, "group_rank(close, close)", accepted=False)

    result = policy_replay.compare(db, sorted(policy_replay.POLICIES), budget=4, persist=False)

    for name, run in result["policies"].items():
        assert run["leakage_check"]["status"] == "passed", (name, run["leakage_check"])
        assert run["metrics"]["simulations_used"] <= 4
        assert run["policy"] == name


def test_the_evidence_threshold_is_the_price_of_learning_a_rule(db):
    """Calibration + replay together: you pay to learn a rule, then you stop paying for it."""
    # Three candidates history refused, then alternating flagged / clean work. The flagged
    # candidates are only queued, never simulated: their finding is a pre-simulation fact.
    # Each source is its own generation wave, so the corpus spans decision rounds and the
    # rounds stay ordered the way a real campaign's did.
    refused = [
        _settle(db, expression, accepted=False, source=f"wave-{index}")
        for index, expression in enumerate(GROUP_ARGUMENT_EXPRESSIONS)
    ]
    later: list[int] = []
    for index, field in enumerate(("low", "volume", "vwap")):
        flagged_next = db.queue_candidate(f"group_rank({field}, {field})", {"decay": 6},
                                          source=f"wave-{index + 3}")
        assert flagged_next.action == "queued", flagged_next.action
        later.append(flagged_next.candidate_id)
        later.append(_settle(db, f"rank(ts_delta(close, {20 + index}))", accepted=True,
                             source=f"wave-{index + 6}"))

    environment = policy_replay.ReplayEnvironment.from_db(db, budget=12)
    flagged = {
        item.candidate_id for item in environment.items
        if GROUP_ARGUMENT_FINDING in item.card.finding_codes
    }
    assert flagged >= set(refused) and flagged & set(later), "flagged work exists on both sides"

    def run(name, **parameters):
        report = policy_replay.replay(
            environment, policy_replay.build_policy(name, environment=environment, parameters=parameters)
        )
        return report, {entry["candidate_id"] for entry in report["decisions"]}

    fifo_report, fifo_choices = run("fifo")
    # One settled failure is enough for a strict gate: the first refusal is the measurement,
    # and every flagged candidate after it is declined — including the ones kept back.
    strict_report, strict_choices = run("calibrated_skip", min_samples=1, strict_threshold=0.6)
    assert strict_choices & flagged == {refused[0]}  # the measuring run really happened
    assert not strict_choices & (flagged & set(later))
    # With a threshold history cannot meet, the same policy has nothing to act on and falls
    # back to ordinary behaviour: that difference *is* the cost of the evidence requirement.
    naive_report, naive_choices = run("calibrated_skip", min_samples=9)
    assert naive_choices == fifo_choices
    assert len(naive_choices & flagged) == len(flagged)

    assert strict_report["metrics"]["wasted_share"] < fifo_report["metrics"]["wasted_share"]
    assert (strict_report["metrics"]["is_pass_from_simulated"]
            >= fifo_report["metrics"]["is_pass_from_simulated"])
    assert naive_report["metrics"]["wasted_share"] == fifo_report["metrics"]["wasted_share"]


def test_calibration_needs_evidence_before_it_can_demote_anything(db):
    """Below min_samples the calibrated policy must behave like an ordinary ranking."""
    for index in range(4):
        _settle(db, f"rank(ts_delta(open, {5 + index}))", accepted=True)

    environment = policy_replay.ReplayEnvironment.from_db(db)
    calibration_report = environment.calibration()
    assert calibration_report["recommended_policy"] == {}
    assert all(item["recommendation"] == calibration.RECOMMEND_INSUFFICIENT
               for item in calibration_report["findings"])


def test_replay_rejects_an_unknown_policy(db):
    environment = policy_replay.ReplayEnvironment.from_db(db)
    with pytest.raises(ValueError, match="unknown policy"):
        policy_replay.build_policy("oracle", environment=environment)


def test_corpus_can_be_filtered_by_scope(db):
    _settle(db, "rank(close)", accepted=True, settings={"region": "USA", "universe": "TOP3000", "delay": 1})
    _settle(db, "rank(open)", accepted=True, settings={"region": "CHN", "universe": "TOP2000U", "delay": 1})

    usa = policy_replay.ReplayEnvironment.from_db(db, scope={"region": "USA", "universe": "TOP3000", "delay": 1})
    china = policy_replay.ReplayEnvironment.from_db(db, scope={"region": "CHN", "universe": "TOP2000U", "delay": 1})

    assert len(usa.items) == 1 and len(china.items) == 1
    assert canonical.scope_hash(usa.items[0].card.scope) == canonical.scope_hash(
        {"region": "USA", "universe": "TOP3000", "delay": 1}
    )


def test_cli_lists_and_compares_offline(db, tmp_path, capsys):
    _settle(db, "rank(ts_delta(close, 5))", accepted=True)
    _settle(db, "rank(ts_delta(open, 5))", accepted=False)
    path = str(tmp_path / "research.db")

    assert policy_replay.main(["--list"]) == 0
    registry = json.loads(capsys.readouterr().out)
    assert registry["fifo"]["baseline"] is True
    assert "calibrated_rank" in registry

    assert policy_replay.main(["--db", path, "--compare", "--budget", "2", "--no-persist"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["clock"] == "events.id"
    assert result["corpus_size"] == 2
    assert "fifo" in result["policies"]
