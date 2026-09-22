"""Advisory search-aware robustness diagnostics for P3.

These diagnostics never reject candidates. They describe the trial population that produced
a result, so a winner from 1,000 adaptive variants is not presented like a manual idea.
Metrics are intentionally lightweight and dependency-compatible with the existing numpy
stack; missing PnL or insufficient trials is reported explicitly rather than imputed.
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

REPORT_VERSION = "robustness-v1"
PASSING = {"IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"}


def _finite(values: Sequence[Any]) -> np.ndarray:
    return np.asarray([float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(float(value))], dtype=float)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


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


def _pnl_stability(db: research_db.ResearchDB, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        alpha = row.get("brain_alpha_id")
        if not alpha:
            continue
        cached = db.query("SELECT dates_json, values_json FROM active_pnl WHERE brain_alpha_id=?", (alpha,))
        if not cached:
            continue
        try:
            dates = json.loads(cached[0]["dates_json"])
            values = _finite(json.loads(cached[0]["values_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if len(values) < 20:
            continue
        chunks = np.array_split(values, min(4, len(values) // 20))
        subperiods = []
        for index, chunk in enumerate(chunks):
            if len(chunk) < 2:
                continue
            subperiods.append({"period": index, "mean": round(float(np.mean(chunk)), 8),
                               "std": round(float(np.std(chunk, ddof=1)), 8),
                               "sharpe_proxy": round(float(np.mean(chunk) / (np.std(chunk, ddof=1) or 1e-12) * math.sqrt(252)), 6)})
        candidates.append({"candidate_id": row["id"], "observations": len(values), "date_count": len(dates), "subperiods": subperiods})
    return {"status": "available" if candidates else "unavailable", "candidates": candidates}


def campaign_report(db: research_db.ResearchDB, campaign_id: str | None = None, *, persist: bool = True) -> dict[str, Any]:
    where = "WHERE campaign_id=?" if campaign_id else ""
    rows = db.query(f"SELECT * FROM candidates {where} ORDER BY created_at, id", (campaign_id,) if campaign_id else ())
    trials = multiple_testing(rows)
    report = {
        "report_version": REPORT_VERSION,
        "campaign_id": campaign_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": {
            "trial_count": len(rows),
            "independence_groups": len({str(row.get("skeleton_hash") or row.get("id")) for row in rows}),
            "generations": sorted({int(row.get("generation") or 0) for row in rows}),
            "mutation_types": sorted({str(row.get("mutation_type") or "manual") for row in rows}),
            "families": sorted({str(row.get("signal_family") or "unknown") for row in rows}),
        },
        "multiple_testing": trials,
        "families": family_diagnostics(rows),
        "stability": _pnl_stability(db, rows),
    }
    if persist:
        now = report["created_at"]
        with db._tx() as conn:
            conn.execute(
                "INSERT INTO robustness_reports(campaign_id,candidate_id,report_type,metrics_json,trial_count,independence_count,created_at) VALUES(?,?,?,?,?,?,?)",
                (campaign_id, None, REPORT_VERSION, json.dumps(report, sort_keys=True), len(rows), int(trials.get("independence_count", 0)), now),
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
