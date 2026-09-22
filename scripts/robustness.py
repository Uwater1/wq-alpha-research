"""Advisory search-aware robustness diagnostics for P3.

These diagnostics never reject candidates. They describe the trial population that produced
a result, so a winner from 1,000 adaptive variants is not presented like a manual idea.

Two correctness rules drive the statistics:

* ``active_pnl.values_json`` stores **cumulative** PnL. Every Sharpe-like number here is
  computed on first-differenced daily returns; cumulative levels are only ever used as an
  equity curve for drawdown.
* the population of interest is the permanent trial ledger, not the surviving candidates:
  validation rejects, duplicate/cache decisions, simulation failures, IS failures,
  correlation failures and submission rejects are all counted.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import research_db

REPORT_VERSION = "robustness-v2"
PASSING = {"IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"}
IS_PASS_STATUSES = {"IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING"}
#: Mutually exclusive buckets every research decision lands in; nothing is dropped.
TRIAL_OUTCOMES: tuple[str, ...] = (
    "validation_reject",
    "pending",
    "simulation_fail",
    "is_fail",
    "correlation_fail",
    "submission_reject",
    "is_pass",
    "active",
)
TRADING_DAYS = 252
ROLLING_WINDOW = 63


def _finite(values: Sequence[Any]) -> np.ndarray:
    return np.asarray([float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(float(value))], dtype=float)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def daily_returns(values: Sequence[Any]) -> np.ndarray:
    """First differences of a **cumulative** PnL path, i.e. the daily PnL series.

    Sharpe/Fitness on a cumulative level series is meaningless (roughly the ratio of a
    growing level to its own autocorrelation), so every statistic below starts here.
    """
    levels = _finite(values)
    if levels.size < 2:
        return np.asarray([], dtype=float)
    return np.diff(levels)


def _sharpe(returns: np.ndarray) -> float | None:
    if returns.size < 2:
        return None
    std = float(np.std(returns, ddof=1))
    if std <= 0.0:
        return None
    return float(np.mean(returns) / std * math.sqrt(TRADING_DAYS))


def _max_drawdown(levels: np.ndarray) -> float:
    """Largest peak-to-trough decline of the PnL path (levels, not returns)."""
    if levels.size < 2:
        return 0.0
    running_peak = np.maximum.accumulate(levels)
    return float(np.max(running_peak - levels))


def subperiod_stats(returns: np.ndarray, *, chunks: int = 4) -> list[dict[str, Any]]:
    """Subperiod statistics computed on daily returns, not on cumulative levels."""
    if returns.size < 2:
        return []
    parts = np.array_split(returns, max(1, min(chunks, returns.size // 10)))
    result: list[dict[str, Any]] = []
    for index, chunk in enumerate(parts):
        if chunk.size < 2:
            continue
        sharpe = _sharpe(chunk)
        result.append({
            "period": index,
            "observations": int(chunk.size),
            "mean_return": round(float(np.mean(chunk)), 8),
            "std_return": round(float(np.std(chunk, ddof=1)), 8),
            "sharpe": None if sharpe is None else round(sharpe, 6),
        })
    return result


def yearly_stats(dates: Sequence[Any], returns: np.ndarray) -> list[dict[str, Any]]:
    """Year-by-year return and Sharpe; empty when the cache carries no usable dates."""
    if not dates or returns.size == 0:
        return []
    # returns[i] is the change between dates[i] and dates[i+1].
    aligned = list(dates)[1 : returns.size + 1]
    if len(aligned) != returns.size:
        return []
    buckets: dict[str, list[float]] = defaultdict(list)
    for date, value in zip(aligned, returns):
        text = str(date)
        year = text[:4]
        if not year.isdigit():
            continue
        buckets[year].append(float(value))
    result: list[dict[str, Any]] = []
    for year in sorted(buckets):
        values = np.asarray(buckets[year], dtype=float)
        if values.size < 2:
            continue
        result.append({
            "year": year,
            "observations": int(values.size),
            "total_return": round(float(np.sum(values)), 8),
            "sharpe": round(_sharpe(values), 6) if _sharpe(values) is not None else None,
        })
    return result


def rolling_stats(returns: np.ndarray, *, window: int = ROLLING_WINDOW) -> dict[str, Any]:
    """Rolling-window Sharpe, so an isolated good stretch is visible as such."""
    usable = min(window, returns.size)
    if returns.size < 2 or usable < 2:
        return {"status": "unavailable"}
    windows: list[float] = []
    for start in range(0, returns.size - usable + 1):
        value = _sharpe(returns[start : start + usable])
        if value is not None:
            windows.append(value)
    if not windows:
        return {"status": "unavailable"}
    array = np.asarray(windows, dtype=float)
    return {
        "status": "available",
        "window": int(usable),
        "count": len(windows),
        "mean_sharpe": round(float(np.mean(array)), 6),
        "min_sharpe": round(float(np.min(array)), 6),
        "max_sharpe": round(float(np.max(array)), 6),
        "positive_share": round(float(np.mean(array > 0)), 6),
    }


def dispersion(values: Sequence[Any]) -> dict[str, Any]:
    """Spread of a metric across related trials (turnover, correlation, ...)."""
    finite = _finite(values)
    if finite.size == 0:
        return {"status": "unavailable", "count": 0}
    return {
        "status": "available" if finite.size > 1 else "single_observation",
        "count": int(finite.size),
        "median": round(float(np.median(finite)), 6),
        "p10": round(float(np.percentile(finite, 10)), 6),
        "p90": round(float(np.percentile(finite, 90)), 6),
        "min": round(float(np.min(finite)), 6),
        "max": round(float(np.max(finite)), 6),
        "std": round(float(np.std(finite, ddof=1)), 6) if finite.size > 1 else 0.0,
    }


def multiple_testing(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    sharpes = _finite([row.get("sharpe") for row in rows])
    families = {str(row.get("signal_family") or "unknown") for row in rows}
    structures = {str(row.get("skeleton_hash") or row.get("id")) for row in rows}
    n = len(rows)
    independent = max(1, len(structures)) if rows else 0
    if len(sharpes) < 2:
        return {"status": "insufficient_trials", "trial_count": n, "independence_count": independent}
    best = float(np.max(sharpes))
    mean = float(np.mean(sharpes))
    std = float(np.std(sharpes, ddof=1)) or 1e-12
    expected_max = mean + std * math.sqrt(2.0 * math.log(max(2, independent)))
    # Candidate-level PSR is a screening diagnostic, not a replacement for return-level PSR.
    psr = _normal_cdf((best / std) * math.sqrt(max(1, independent - 1)))
    return {
        "status": "advisory",
        "trial_count": n,
        "independence_count": independent,
        "effective_number_of_trials": independent,
        "distinct_families": len(families),
        "best_sharpe": round(best, 6),
        "mean_sharpe": round(mean, 6),
        "expected_max_sharpe": round(expected_max, 6),
        "deflated_sharpe_proxy": round(best - expected_max, 6),
        "probabilistic_sharpe_proxy": round(psr, 6),
        "winner_uplift_vs_mean": round(best - mean, 6),
    }


def family_diagnostics(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("signal_family") or "unknown")].append(row)
    result = []
    for family, members in sorted(groups.items()):
        values = _finite([row.get("sharpe") for row in members])
        passes = sum(str(row.get("status")) in PASSING or bool(row.get("is_pass")) for row in members)
        result.append({
            "family": family,
            "trials": len(members),
            "independent_structures": len({str(row.get("skeleton_hash") or row.get("id")) for row in members}),
            "passes": passes,
            "pass_rate": round(passes / len(members), 6) if members else 0.0,
            "best_sharpe": round(float(np.max(values)), 6) if len(values) else None,
            "median_sharpe": round(float(np.median(values)), 6) if len(values) else None,
        })
    return result


def classify_trial(trial: Mapping[str, Any]) -> str:
    """Map one ledger row onto a countable outcome bucket."""
    result = str(trial.get("validation_result") or "")
    if result == "rejected_invalid":
        return "validation_reject"
    if not trial.get("candidate_id"):
        return "pending"
    status = str(trial.get("candidate_status") or "")
    submission = str(trial.get("submission_status") or "")
    reasons = " ".join(str(trial.get(key) or "") for key in ("gate_reason", "failure_reason")).upper()
    if status == "ACTIVE":
        return "active"
    if status in IS_PASS_STATUSES:
        return "is_pass"
    if status == "REJECTED":
        if submission == "SELF_CORR_FAIL" or "CORR" in reasons:
            return "correlation_fail"
        if submission in ("PLATFORM_REJECTED", "EXHAUSTED"):
            return "submission_reject"
        if trial.get("brain_alpha_id") or trial.get("sharpe") is not None:
            return "is_fail"
        return "simulation_fail"
    return "pending"


def trial_accounting(trials: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count every generated attempt, including losing/invalid/duplicate ones.

    ``outcomes`` is a partition: every ledger row lands in exactly one bucket, so
    ``sum(outcomes.values()) == trial_count`` and losing variants can never be dropped.
    ``duplicate_decisions`` is an orthogonal counter: a repeated decision on an already
    known canonical candidate can end in any of those buckets.
    """
    buckets = {outcome: 0 for outcome in TRIAL_OUTCOMES}
    for trial in trials:
        buckets[classify_trial(trial)] += 1
    duplicates = sum(1 for trial in trials if int(trial.get("is_duplicate") or 0) == 1)
    transient = sum(1 for trial in trials if str(trial.get("validation_result") or "") in ("cache_hit", "in_flight"))
    return {
        "status": "advisory",
        "trial_count": len(trials),
        "outcomes": buckets,
        "duplicate_decisions": duplicates,
        "cache_or_in_flight_responses": transient,
        "accounted_trials": sum(buckets.values()),
    }


def _pnl_stability(db: research_db.ResearchDB, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Daily-return stability for every candidate that has a cached PnL series."""
    candidates: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for row in rows:
        alpha = row.get("brain_alpha_id")
        if not alpha:
            missing.append({"candidate_id": row["id"], "status": "no_brain_alpha_id"})
            continue
        cached = db.query("SELECT dates_json, values_json FROM active_pnl WHERE brain_alpha_id=?", (alpha,))
        if not cached:
            missing.append({"candidate_id": row["id"], "status": "no_cached_pnl"})
            continue
        try:
            dates = json.loads(cached[0]["dates_json"])
            levels = _finite(json.loads(cached[0]["values_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            missing.append({"candidate_id": row["id"], "status": "unreadable_pnl_cache"})
            continue
        returns = daily_returns(levels)
        if returns.size < 20:
            missing.append({"candidate_id": row["id"], "status": "insufficient_observations",
                            "observations": int(returns.size)})
            continue
        candidates.append({
            "candidate_id": row["id"],
            "status": "available",
            "observations": int(levels.size),
            "daily_return_count": int(returns.size),
            "date_count": len(dates),
            "daily_return_mean": round(float(np.mean(returns)), 8),
            "daily_return_std": round(float(np.std(returns, ddof=1)), 8),
            "daily_sharpe": round(_sharpe(returns), 6) if _sharpe(returns) is not None else None,
            "flat_return_path": bool(returns.size and not np.any(returns)),
            "max_drawdown": round(_max_drawdown(levels), 8),
            "subperiods": subperiod_stats(returns),
            "yearly": yearly_stats(dates, returns),
            "rolling": rolling_stats(returns),
            "metric_basis": "daily_returns",
        })
    if not candidates:
        return {"status": "unavailable", "candidates": [], "missing": missing, "metric_basis": "daily_returns"}
    return {"status": "available", "candidates": candidates, "missing": missing, "metric_basis": "daily_returns"}


def campaign_report(db: research_db.ResearchDB, campaign_id: str | None = None, *, persist: bool = True) -> dict[str, Any]:
    trials = db.trials(campaign_id)
    where = "WHERE campaign_id=?" if campaign_id else ""
    rows = db.query(f"SELECT * FROM candidates {where} ORDER BY created_at, id", (campaign_id,) if campaign_id else ())
    trials_metrics = multiple_testing(rows)
    report = {
        "report_version": REPORT_VERSION,
        "campaign_id": campaign_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": {
            "trial_count": len(trials),
            "candidate_count": len(rows),
            "independence_groups": len({str(row.get("skeleton_hash") or row.get("id")) for row in rows}),
            "generations": sorted({int(row.get("generation") or 0) for row in trials}),
            "mutation_types": sorted({str(row.get("mutation_type") or "manual") for row in trials}),
            "families": sorted({str(row.get("signal_family") or "unknown") for row in trials}),
            "field_catalog_versions": sorted({str(row.get("field_catalog_version")) for row in trials if row.get("field_catalog_version")}),
            "operator_catalog_versions": sorted({str(row.get("operator_catalog_version")) for row in trials if row.get("operator_catalog_version")}),
            "scopes": sorted({str(row.get("scope_json")) for row in trials if row.get("scope_json")}),
        },
        "trials": trial_accounting(trials),
        "multiple_testing": trials_metrics,
        "families": family_diagnostics(rows),
        "turnover_dispersion": dispersion([row.get("turnover") for row in rows]),
        "correlation_dispersion": dispersion([row.get("self_corr") for row in rows]),
        "stability": _pnl_stability(db, rows),
    }
    if persist:
        now = report["created_at"]
        with db._tx() as conn:
            conn.execute(
                "INSERT INTO robustness_reports(campaign_id,candidate_id,report_type,metrics_json,trial_count,independence_count,created_at) VALUES(?,?,?,?,?,?,?)",
                (campaign_id, None, REPORT_VERSION, json.dumps(report, sort_keys=True), int(report["provenance"]["trial_count"]),
                 int(trials_metrics.get("independence_count", 0)), now),
            )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Advisory search-aware robustness report")
    parser.add_argument("--db")
    parser.add_argument("--campaign")
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        print(json.dumps(campaign_report(db, args.campaign, persist=not args.no_persist), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
