"""Unit tests for the WQ alpha tooling helpers.

These cover the failure modes found while auditing the repo:
    - CSV settings cells that are blank or garbage used to kill a whole batch;
    - a null `is`/`regular` field crashed the skill-evolution reporting;
    - PnL correlation was aligned by list length instead of by date;
    - submission treated a SELF_CORRELATION PASS as a live alpha.
"""
from __future__ import annotations

import csv
import json
import math
import sys

import pytest

import batch_simulate as bs
import evolve_skill as es
import scrape_submittable as ss
import submit_from_csv as sc


class _Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload=None, headers=None, status_code=200, content=b"{}", text=None):
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status_code
        self.content = content
        self.text = json.dumps(payload) if text is None and payload is not None else (text or "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


# ---------------------------------------------------------------------------
# batch_simulate: settings coercion and polling
# ---------------------------------------------------------------------------


def test_build_settings_blank_cells_fall_back_to_defaults():
    settings = bs.build_settings({"code": "rank(close)", "decay": "", "truncation": "  ", "delay": None})
    assert settings["decay"] == 6
    assert settings["truncation"] == 0.1
    assert settings["delay"] == 1


def test_build_settings_normalizes_blank_region_and_case():
    settings = bs.build_settings({"code": "x", "region": "", "universe": "", "neutralization": "subindustry"})
    assert settings["region"] == "USA"
    assert settings["universe"] == "TOP3000"
    assert settings["neutralization"] == "SUBINDUSTRY"


@pytest.mark.parametrize("row", [{"decay": "abc"}, {"truncation": "x"}, {"delay": "1.5"}])
def test_build_settings_rejects_garbage_cells(row):
    with pytest.raises(ValueError):
        bs.build_settings({"code": "rank(close)", **row})


def test_run_simulation_reports_bad_settings_without_calling_the_api(monkeypatch):
    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("api_post should not be reached for invalid settings")

    monkeypatch.setattr(bs, "api_post", explode)

    result = bs.run_simulation(None, {"code": "rank(close)", "decay": "nope"})

    assert result["error"].startswith("bad_settings:")
    assert result["code"] == "rank(close)"


def test_run_simulation_returns_metrics(monkeypatch):
    monkeypatch.setattr(
        bs,
        "api_post",
        lambda *a, **k: _Resp(headers={"Location": "https://api.worldquantbrain.com/simulations/abc/"}),
    )
    alpha = {
        "id": "A1",
        "is": {
            "sharpe": 1.5,
            "fitness": 1.2,
            "turnover": 0.0834,
            "checks": [
                {"name": "CONCENTRATED_WEIGHT", "result": "PASS"},
                {"name": "LOW_SUB_UNIVERSE_SHARPE", "value": 1.1, "result": "PASS"},
                {"name": "LOW_SHARPE", "result": "PASS"},
            ],
        },
    }
    monkeypatch.setattr(bs, "api_get", lambda *a, **k: _Resp({"alpha": "A1"} if "simulations" in a[1] else alpha))

    result = bs.run_simulation(object(), {"code": "rank(close)", "decay": 0})

    assert result["sharpe"] == 1.5
    assert result["turnover"] == 8.34
    assert result["passed"] == 3
    assert result["weight"] == "PASS"
    assert result["subsharpe"] == 1.1
    assert "alpha/A1" in result["link"]


def test_run_simulation_times_out_instead_of_polling_forever(monkeypatch):
    monkeypatch.setattr(bs, "api_post", lambda *a, **k: _Resp(headers={"Location": "https://api/simulations/1"}))
    # A body with no alpha, no status and no progress used to spin forever.
    monkeypatch.setattr(bs, "api_get", lambda *a, **k: _Resp({}))

    result = bs.run_simulation(object(), {"code": "rank(close)"}, max_wait=0.0)

    assert result["error"].startswith("simulation_timeout")
    assert result["code"] == "rank(close)"


def test_sim_signature_tracks_expression_and_settings():
    row = {"code": "rank(close)", "neutralization": "SUBINDUSTRY", "decay": "10"}
    other = {**row, "decay": "11"}

    assert bs.sim_signature(row) == bs.sim_signature(dict(row))
    assert bs.sim_signature(row) != bs.sim_signature(other)


def test_skip_done_does_not_resimulate_finished_rows(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bs, "DATA_DIR", tmp_path)
    inp = tmp_path / "in.csv"
    inp.write_text('code,neutralization,decay,truncation,delay,universe,region\n"rank(open - close)",SUBINDUSTRY,10,0.1,1,TOP3000,USA\n')
    with (tmp_path / "results_20260101_000000.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=bs.RESULT_COLUMNS)
        writer.writeheader()
        writer.writerow(
            {
                "code": "rank(open - close)", "neutralization": "SUBINDUSTRY", "decay": "10",
                "truncation": "0.1", "delay": "1", "universe": "TOP3000", "region": "USA",
            }
        )

    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("a finished expression must not be re-simulated")

    monkeypatch.setattr(bs, "run_simulation", explode)
    monkeypatch.setattr(bs, "get_session", lambda: object())
    monkeypatch.setattr(sys, "argv", ["batch_simulate.py", str(inp), "--skip-done"])

    assert bs.main() == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_rate_limited_rows_are_retried(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "DATA_DIR", tmp_path)
    inp = tmp_path / "in.csv"
    inp.write_text('code\n"rank(close)"\n')
    monkeypatch.setattr(bs, "get_session", lambda: object())

    calls = {"n": 0}

    def flaky(session, sim, max_wait=0):
        calls["n"] += 1
        if calls["n"] == 1:
            return {**sim, "error": "rate_limited"}
        return {
            **sim, "passed": 7, "sharpe": 1.5, "fitness": 1.2, "turnover": 4.7,
            "link": "https://platform.worldquantbrain.com/alpha/X", "weight": "PASS", "subsharpe": 1.1,
        }

    monkeypatch.setattr(bs, "run_simulation", flaky)
    monkeypatch.setattr(
        sys, "argv", ["batch_simulate.py", str(inp), "--retries", "1", "--retry-delay", "0"]
    )

    assert bs.main() == 0
    assert calls["n"] == 2  # rate-limited once, then retried
    rows = list(csv.DictReader(sorted(tmp_path.glob("results_*.csv"))[-1].open()))
    assert len(rows) == 1
    assert rows[0]["sharpe"] == "1.5"


# ---------------------------------------------------------------------------
# evolve_skill: None-safe access, formatting, sanitizing
# ---------------------------------------------------------------------------


def test_alpha_code_tolerates_null_dict_and_string():
    assert es.alpha_code({"regular": None}) == ""
    assert es.alpha_code({"regular": {"code": "rank(close)"}}) == "rank(close)"
    assert es.alpha_code({"regular": "rank(close)"}) == "rank(close)"
    assert es.alpha_code({"expression": "rank(open)"}) == "rank(open)"


def test_fmt_num_handles_missing_and_non_finite_values():
    assert es.fmt_num(None) == "n/a"
    assert es.fmt_num(float("nan")) == "n/a"
    assert es.fmt_num("1.5") == "n/a"
    assert es.fmt_num(True) == "n/a"
    assert es.fmt_num(1.234) == "1.23"
    assert es.fmt_num(0.1234, ".3f") == "0.123"


def test_metric_reads_null_is_block():
    assert es._metric({"is": None}, "sharpe") is None
    assert es._metric({"is": {"sharpe": 1.2}}, "sharpe") == 1.2


def test_redact_alpha_id_is_stable_and_hides_the_raw_id():
    redacted = es.redact_alpha_id("mLmO6mY9")
    assert redacted == es.redact_alpha_id("mLmO6mY9")
    assert redacted != es.redact_alpha_id("ABCDEFGH")
    assert "mLmO6mY9" not in redacted


def test_expression_shape_keeps_operators_only():
    assert es.expression_shape("group_rank(ts_rank(est_eps / close, 126), subindustry)") == "group_rank+ts_rank"
    assert es.expression_shape("close") == "raw-field"


def _sample_alphas():
    return [
        {
            "id": "mLmO6mY9",
            "status": "ACTIVE",
            "regular": {"code": "group_rank(ts_rank(operating_income / equity, 126), subindustry)"},
            "is": {"sharpe": 1.58, "fitness": 1.02, "turnover": 0.0634},
        },
        {"id": "ZZZ999", "status": "ACTIVE", "regular": None, "is": None},
    ]


def test_build_bulk_summary_survives_null_fields_and_sanitizes():
    report = es.build_bulk_summary(_sample_alphas(), {}, sanitize=True)

    assert "mLmO6mY9" not in report
    assert "operating_income" not in report
    assert "alpha-" in report
    assert "group_rank+ts_rank" in report
    assert "n/a" in report  # the ACTIVE alpha with no metrics


def test_classify_alpha_ignores_whitespace():
    spaced = "group_rank(ts_rank(operating_income / equity, 126), subindustry)"
    assert es.classify_alpha(spaced) == "profitability"
    assert es.classify_alpha("rank(high + low)") == "technical"
    assert es.classify_alpha("rank(est_eps / close)") == "analyst"
    assert es.classify_alpha(None) == "other"


def test_build_bulk_summary_raw_mode_keeps_ids():
    report = es.build_bulk_summary(_sample_alphas(), {}, sanitize=False)
    assert "mLmO6mY9" in report
    assert "operating_income" in report


def test_build_incremental_report_handles_partial_entries():
    report = es.build_incremental_report(
        [
            {
                "alpha_id": "AAA",
                "status": "UNSUBMITTED",
                "family": "analyst",
                "sharpe": None,
                "fitness": None,
                "turnover": None,
                "drawdown": None,
                "top_corr": [{"alpha_id": "BBB", "corr": 0.72}],
                "lesson": "highly correlated",
            },
            {"alpha_id": "CCC", "event": "status_or_metric_changed", "old_status": "UNSUBMITTED", "new_status": "ACTIVE"},
        ],
        sanitize=True,
    )
    assert "AAA" not in report
    assert "CCC" not in report
    assert "n/a" in report
    assert "highly correlated" in report


# ---------------------------------------------------------------------------
# correlation: align on dates, guard degenerate series
# ---------------------------------------------------------------------------


def test_aligned_daily_returns_pairs_on_common_dates():
    new_dates = ["d1", "d2", "d3", "d4"]
    new_pnl = [0.0, 1.0, 3.0, 6.0]
    old_dates = ["d2", "d3", "d4", "d5"]
    old_pnl = [10.0, 12.0, 15.0, 19.0]

    new_ret, old_ret = es.aligned_daily_returns(new_dates, new_pnl, old_dates, old_pnl)

    assert new_ret == [2.0, 3.0]
    assert old_ret == [2.0, 3.0]


def test_aligned_daily_returns_falls_back_to_length_without_dates():
    new_ret, old_ret = es.aligned_daily_returns([], [1.0, 2.0, 4.0], [], [0.0, 1.0, 2.0])
    assert new_ret == [1.0, 2.0]
    assert old_ret == [1.0, 1.0]


def test_aligned_daily_returns_rejects_mismatched_or_disjoint_series():
    assert es.aligned_daily_returns([], [1.0, 2.0], [], [1.0, 2.0, 3.0]) == ([], [])
    assert es.aligned_daily_returns(["d1", "d2"], [1.0, 2.0], ["d9", "d10"], [1.0, 2.0]) == ([], [])


def test_safe_corrcoef_guards_degenerate_inputs():
    assert es._safe_corrcoef([1.0, 2.0, 3.0], [4.0, 5.0, 6.0]) == pytest.approx(1.0)
    assert es._safe_corrcoef([1.0, 1.0, 1.0], [4.0, 5.0, 6.0]) is None  # constant series
    assert es._safe_corrcoef([1.0, 2.0], [1.0, 2.0, 3.0]) is None
    assert es._safe_corrcoef([1.0, math.nan, 3.0], [4.0, 5.0, 6.0]) is None


def test_correlation_with_existing_uses_active_alphas_only():
    days = [f"2026-01-{i:02d}" for i in range(1, 61)]
    moves = [1.0 if i % 2 else -1.0 for i in range(60)]
    cum = [sum(moves[: i + 1]) for i in range(60)]
    db = {
        "alphas": {
            "OLD": {"status": "ACTIVE", "pnl": cum, "pnl_dates": days, "sharpe": 1.4},
            "DEAD": {"status": "UNSUBMITTED", "pnl": cum, "pnl_dates": days},
        }
    }
    doubled = [2.0 * value for value in cum]

    results = es.correlation_with_existing(days, doubled, db)

    assert [r["alpha_id"] for r in results] == ["OLD"]
    assert results[0]["corr"] == pytest.approx(1.0)


def test_fetch_pnl_series_warns_instead_of_silently_returning_nothing(monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise RuntimeError("GET ... failed after 4 retries")

    monkeypatch.setattr(es, "get_with_retry", boom)

    assert es.fetch_pnl_series(object(), "A1") == ([], [])
    assert "WARNING" in capsys.readouterr().err


def test_lesson_blames_the_missing_pnl_not_the_book():
    fp = {"fitness": 1.5, "turnover": 0.1, "sharpe": 1.8, "expression": "rank(close)"}
    assert "PnL series unavailable" in es.generate_lesson(fp, [], pnl_available=False)
    assert "no ACTIVE alpha available" in es.generate_lesson(fp, [])


def test_fresh_alpha_pnl_retries_until_brain_serves_it(monkeypatch):
    calls = {"n": 0}

    def flaky(_session, _alpha_id):
        calls["n"] += 1
        return ([], []) if calls["n"] < 3 else (["2020-01-01"], [1.0])

    monkeypatch.setattr(es, "fetch_pnl_series", flaky)
    monkeypatch.setattr(es.time, "sleep", lambda _s: None)

    assert es.fetch_pnl_for_new_alpha(object(), "A1") == (["2020-01-01"], [1.0])
    assert calls["n"] == 3


def test_fetch_pnl_series_parses_list_shaped_schema(monkeypatch):
    payload = {
        "schema": {"properties": [{"name": "date"}, {"name": "pnl"}]},
        "records": [["2020-01-02", 1.5], ["2020-01-01", 0.5]],
    }
    monkeypatch.setattr(es, "get_with_retry", lambda *a, **k: _Resp(payload))

    dates, values = es.fetch_pnl_series(object(), "A1")

    assert dates == ["2020-01-01", "2020-01-02"]  # sorted oldest first
    assert values == [0.5, 1.5]


def test_fetch_pnl_series_parses_dict_shaped_schema(monkeypatch):
    payload = {
        "schema": {"properties": {"date": {"index": 0}, "pnl": {"index": 1}}},
        "records": [["2020-01-01", 0.5]],
    }
    monkeypatch.setattr(es, "get_with_retry", lambda *a, **k: _Resp(payload))

    assert es.fetch_pnl_series(object(), "A1") == (["2020-01-01"], [0.5])


def test_correlation_with_existing_skips_too_short_history():
    db = {"alphas": {"OLD": {"status": "ACTIVE", "pnl": [0.0, 1.0, 2.0], "pnl_dates": ["d1", "d2", "d3"]}}}
    assert es.correlation_with_existing(["d1", "d2", "d3"], [0.0, 1.0, 2.0], db) == []


# ---------------------------------------------------------------------------
# submission: a correlation PASS is not proof of a live alpha
# ---------------------------------------------------------------------------


def test_submit_one_reports_submitted_only_when_active(monkeypatch):
    monkeypatch.setattr(sc, "api_post", lambda *a, **k: _Resp())
    monkeypatch.setattr(sc, "poll_self_correlation", lambda *a, **k: True)
    monkeypatch.setattr(sc, "confirm_active", lambda *a, **k: "ACTIVE")

    assert sc.submit_one(object(), "A1")["outcome"] == "submitted"


def test_submit_one_flags_pass_without_active_status(monkeypatch):
    monkeypatch.setattr(sc, "api_post", lambda *a, **k: _Resp())
    monkeypatch.setattr(sc, "poll_self_correlation", lambda *a, **k: True)
    monkeypatch.setattr(sc, "confirm_active", lambda *a, **k: "UNSUBMITTED")

    record = sc.submit_one(object(), "A1")

    assert record["outcome"] == "not_active"
    assert record["status"] == "UNSUBMITTED"


def test_submit_one_maps_correlation_failure(monkeypatch):
    monkeypatch.setattr(sc, "api_post", lambda *a, **k: _Resp())
    monkeypatch.setattr(sc, "poll_self_correlation", lambda *a, **k: False)
    assert sc.submit_one(object(), "A1")["outcome"] == "correlation_fail"


def test_submit_one_treats_404_as_already_submitted(monkeypatch):
    def raise_404(*_args, **_kwargs):
        raise RuntimeError("POST https://api/alphas/A1/submit -> HTTP 404: not found")

    monkeypatch.setattr(sc, "api_post", raise_404)

    assert sc.submit_one(object(), "A1")["outcome"] == "skipped"


def test_submit_one_keeps_waiting_on_403(monkeypatch):
    """403 = a previous submit request is still being evaluated, not a failure."""

    def raise_403(*_args, **_kwargs):
        raise RuntimeError("POST https://api/alphas/A1/submit -> HTTP 403: ")

    monkeypatch.setattr(sc, "api_post", raise_403)
    monkeypatch.setattr(sc, "poll_self_correlation", lambda *a, **k: True)
    monkeypatch.setattr(sc, "confirm_active", lambda *a, **k: "ACTIVE")

    assert sc.submit_one(object(), "A1")["outcome"] == "submitted"


def test_submit_one_reports_unresolved_not_error_when_pending(monkeypatch):
    monkeypatch.setattr(sc, "api_post", lambda *a, **k: _Resp())
    monkeypatch.setattr(sc, "poll_self_correlation", lambda *a, **k: None)
    monkeypatch.setattr(sc, "confirm_active", lambda *a, **k: "UNSUBMITTED")

    record = sc.submit_one(object(), "A1")

    assert record["outcome"] == "unresolved"
    assert "pending" in record["detail"]


def test_poll_self_correlation_survives_a_failed_poll_request(monkeypatch):
    """A transient GET failure must not abandon a submission that may become ACTIVE."""
    calls = {"n": 0}

    def flaky_get(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("GET ... -> HTTP 429: too many requests")
        return _Resp({"is": {"checks": [{"name": "SELF_CORRELATION", "result": "PASS"}]}})

    monkeypatch.setattr(sc, "api_get", flaky_get)
    monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

    assert sc.poll_self_correlation(object(), "A1") is True
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# scraping: null `is` blocks must not crash the check reader
# ---------------------------------------------------------------------------


def test_fetch_checks_tolerates_null_is_block(monkeypatch):
    monkeypatch.setattr(ss, "api_get", lambda *a, **k: _Resp({"is": None}, content=b"{}"))
    assert ss.fetch_checks(object(), "A1", attempts=1) == []


def test_fetch_checks_returns_checks(monkeypatch):
    checks = [{"name": "LOW_SHARPE", "result": "PASS"}]
    monkeypatch.setattr(ss, "api_get", lambda *a, **k: _Resp({"is": {"checks": checks}}, content=b"{}"))
    assert ss.fetch_checks(object(), "A1", attempts=1) == checks
