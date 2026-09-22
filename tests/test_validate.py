"""Tests for static pre-screening.

The point of these checks is to save BRAIN capacity, so the tests assert both sides:
provably-broken candidates are refused, and plausible-but-suspicious ones are only
flagged — a validator that rejects real ideas costs more than it saves.
"""
from __future__ import annotations

import pytest

import research_db as rdb
import validate as v


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


# ---------------------------------------------------------------------------
# Structure, operators, arity
# ---------------------------------------------------------------------------


def test_a_valid_baseline_expression_passes_cleanly():
    report = v.validate("group_rank(ts_rank(operating_income/equity, 126), subindustry)", {"region": "USA"})

    assert report.ok and report.errors == [] and report.warnings == []
    assert report.features["categories"] == {"fundamental": 2, "pv": 1}
    assert report.features["operators"] == ["group_rank", "ts_rank"]
    assert report.features["windows"] == [126]
    assert report.features["groups"] == ["subindustry"]
    assert report.features["depth"] == 2


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("rank(close", "unbalanced"),
        ("rank(close,)", "empty argument"),
        ("ts_mean(close)", "at least 2 argument"),
        ("ts_mean(close, 20, 30)", "at most 2 argument"),
        ("foo_bar(close)", "unknown operator"),
        ("rank(does_not_exist)", "unknown field"),
    ],
)
def test_broken_expressions_are_errors(expression, expected):
    report = v.validate(expression, {"region": "USA"})

    assert not report.ok
    assert any(expected in error for error in report.errors), report.errors


def test_typo_suggestions_help_the_generator():
    report = v.validate("rank(operating_incom)", {"region": "USA"})

    assert not report.ok
    assert "operating_income" in report.errors[0]


def test_varargs_and_symbolic_operators_are_not_misjudged():
    for expression in (
        "max(close, open)",
        "min(close, open, volume)",
        "multiply(close, open, volume, vwap)",
        "add(close, open, filter=true)",
        "close > open",
        "if_else(close > open, 1, 0)",
    ):
        assert v.validate(expression, {"region": "USA"}).ok, expression


def test_keyword_values_are_not_treated_as_fields():
    report = v.validate('quantile(close, driver=gaussian, sigma=1.0) + bucket(rank(close), range="0,1,0.1")',
                        {"region": "USA"})

    assert report.ok, report.errors
    assert report.warnings == []


def test_a_nested_keyword_argument_is_still_positional():
    """`ts_rank(winsorize(x, std=4), 120)` is a 2-argument call, not a keyword one.

    Testing for a bare '=' anywhere misread the enclosing call as passing a keyword, which
    rejected a perfectly valid expression before it ever reached BRAIN.
    """
    report = v.validate("group_rank(ts_rank(winsorize(operating_income/equity, std=4), 120), sector)",
                        {"region": "USA"})

    assert report.ok, report.errors
    assert report.errors == []
    assert report.warnings == []


def test_an_unknown_top_level_keyword_is_still_warned_about():
    report = v.validate("ts_rank(close, 120, bogus_kw=3)", {"region": "USA"})

    assert report.ok
    assert any("bogus_kw" in warning for warning in report.warnings)


# ---------------------------------------------------------------------------
# Scope-aware field checks and soft warnings
# ---------------------------------------------------------------------------


def test_fields_outside_the_catalog_scope_are_warnings_not_errors():
    report = v.validate("rank(some_chn_field)", {"region": "CHN", "universe": "TOP2000U"})

    assert report.ok
    assert not report.scope_checked
    assert any("outside its scope" in warning for warning in report.warnings)


def test_bare_vector_fields_are_flagged_but_not_rejected():
    """Type findings are advisory: BRAIN is the final judge of what it accepts."""
    bare = v.validate("max(composite_sentiment_score_2, close)", {"region": "USA"})
    wrapped = v.validate("rank(vec_avg(composite_sentiment_score_2))", {"region": "USA"})

    assert bare.ok and any("vec_avg" in warning for warning in bare.warnings)
    assert wrapped.ok and wrapped.warnings == []


def test_group_arguments_must_be_group_fields():
    report = v.validate("group_rank(close, close)", {"region": "USA"})

    assert report.ok  # still simulated: the platform is the final judge
    assert any("GROUP field" in warning for warning in report.warnings)


def test_type_findings_can_be_promoted_to_errors_explicitly():
    strict = v.validate("group_rank(close, close)", {"region": "USA"}, type_policy=v.TYPE_POLICY_STRICT)
    bare_vector = v.validate("vec_avg(close)", {"region": "USA"}, type_policy=v.TYPE_POLICY_STRICT)

    assert not strict.ok and any("GROUP field" in error for error in strict.errors)
    assert not bare_vector.ok and any("VECTOR field" in error for error in bare_vector.errors)
    with pytest.raises(ValueError, match="unknown type_policy"):
        v.validate("rank(close)", {"region": "USA"}, type_policy="paranoid")


def test_warnings_lower_priority_without_blocking():
    clean = v.validate("rank(close)", {"region": "USA"})
    flagged = v.validate("group_rank(close, close)", {"region": "USA"})

    assert clean.penalty == 0.0
    assert flagged.penalty > 0.0


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "settings, fragment",
    [
        ({"truncation": 1.5}, "truncation"),
        ({"truncation": 0}, "truncation"),
        ({"decay": 900}, "decay"),
        ({"delay": 2}, "delay"),
        ({"neutralization": "SUBINDUSTRYY"}, "neutralization"),
        ({"nanHandling": "MAYBE"}, "nanHandling"),
        ({"language": "SQL"}, "language"),
        ({"delay": "not-a-number"}, "delay"),
    ],
)
def test_impossible_settings_are_errors(settings, fragment):
    assert any(fragment in error for error in v.validate_settings(settings))


def test_default_settings_are_valid():
    assert v.validate_settings({}) == []


# ---------------------------------------------------------------------------
# Queue integration
# ---------------------------------------------------------------------------


def test_queue_refuses_invalid_candidates_but_records_them(db):
    outcome = db.queue_candidate("rank(operating_incom)", {"region": "USA"}, source="agent")

    assert outcome.action == "rejected_invalid"
    assert outcome.needs_simulation is False
    assert outcome.issues
    candidate = db.get_candidate(outcome.candidate_id)
    assert candidate["status"] == "REJECTED"
    assert candidate["failure_reason"].startswith("validation:")
    assert db.list_queued() == []  # nothing for the scheduler to spend a slot on
    assert db.counts("simulations") == {}  # and no simulation row either


def test_queue_accepts_flagged_candidates_and_stores_features(db):
    outcome = db.queue_candidate("group_rank(close, close)", {"region": "USA"})

    assert outcome.action == "queued"
    candidate = db.get_candidate(outcome.candidate_id)
    assert candidate["structural_json"] is not None
    assert "GROUP field" in candidate["structural_json"]
    assert db.list_queued()[0]["id"] == candidate["id"]


def test_queue_validation_can_be_disabled_for_experiments(db):
    outcome = db.queue_candidate("rank(operating_incom)", {"region": "USA"}, validate=False)

    assert outcome.action == "queued"


def test_structural_features_survive_a_reopened_database(db, tmp_path):
    db.queue_candidate("group_rank(ts_rank(operating_income, 60), subindustry)", {"region": "USA"})
    db.close()

    with rdb.ResearchDB.open(tmp_path / "research.db") as reopened:
        row = reopened.list_queued()[0]
        assert '"fundamental"' in row["structural_json"]
        assert reopened.get_meta("schema_version") == str(rdb.SCHEMA_VERSION)
