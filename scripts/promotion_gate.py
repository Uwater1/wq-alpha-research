"""P25 promotion gate — matched live evidence for promoting V3 over V2 (P17/P25.3).

The roadmap forbids promoting V3 because its architecture is cleaner or its archive more
diverse. It requires *measured* simulation efficiency: a materially better IS_PASS per
simulation than the shipped generator, competitive with the historical baseline, without
collapsing survivor diversity or correlation behaviour, and with no point-in-time leakage.

This module turns that prose into a computable checklist over the settled trial ledger:

* :func:`arm_metrics` summarizes one arm (a generator version, or one campaign) with Wilson
  confidence intervals, Sharpe/Fitness quantiles and a turnover-failure rate;
* :func:`survivor_diversity` measures effective (exp-Shannon) diversity **among survivors**,
  so a campaign cannot look diverse by spending its passes on one skeleton;
* :func:`report` compares the arms and evaluates the :data:`GATE` checklist, reporting an
  unmeasurable item as ``unknown`` with a reason rather than silently passing it;
* :func:`main` prints the JSON artifact.

Every read is filtered to simulations that had *completed* at or before ``as_of`` (default:
the newest completed simulation), so a past promotion decision can be reproduced from the
ledger instead of re-derived from today's state.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import diversity  # noqa: E402
import quality_diagnostics as diagnostics  # noqa: E402
import research_db  # noqa: E402

PROMOTION_VERSION = "promotion-gate-v1"
#: The shipped generator V3 must beat, and the candidate default.
DEFAULT_BASELINE = "catalog-generator-v2"
DEFAULT_TARGET = "catalog-generator-v3"
#: Below this many settled simulations an arm's rate is reported but flagged ``low_confidence``.
MIN_ARM_SIMULATIONS = 30
#: Target must reach at least this multiple of the baseline IS_PASS/simulation to count as a
#: *material* improvement rather than noise (the CI is reported alongside so this is auditable).
MATERIAL_RATIO = 1.25
#: Survivor diversity may not fall below this fraction of the baseline's effective grammar count.
DIVERSITY_FLOOR_RATIO = 0.5

#: The settled simulation ledger columns a promotion metric is computed from.
_LEDGER_SQL = """
    SELECT c.id AS candidate_id, c.campaign_id, c.generator_version, c.status,
           c.grammar_skeleton_hash, c.semantic_skeleton_hash, c.motif_id,
           c.source_profile_json, c.corr_status, c.failure_reason,
           s.is_pass AS sim_is_pass, s.sharpe, s.fitness, s.turnover, s.drawdown,
           s.checks_json, s.completed_at
    FROM candidates c
    JOIN simulations s ON s.candidate_id = c.id
    WHERE s.status = 'DONE'
"""


def _parse_json(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return fallback


def _checks(raw: Any) -> list[dict[str, Any]]:
    value = _parse_json(raw, [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _quantile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated quantile (numpy-free, deterministic)."""
    points = sorted(float(value) for value in values)
    if not points:
        return None
    if len(points) == 1:
        return points[0]
    position = max(0.0, min(1.0, q)) * (len(points) - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, len(points) - 1)
    weight = position - lower
    return points[lower] * (1.0 - weight) + points[upper] * weight


def _turnover_failed(row: Mapping[str, Any]) -> bool:
    """A failed turnover check, or a settled turnover outside the admissible band (SKILL.md)."""
    for check in _checks(row.get("checks_json")):
        name = str(check.get("name") or "").upper()
        result = str(check.get("result") or "").upper()
        if "TURNOVER" in name and result and result != "PASS":
            return True
    value = row.get("turnover")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) < 0.01 or float(value) > 0.20
    return False


def _datasets(row: Mapping[str, Any]) -> list[str]:
    profile = _parse_json(row.get("source_profile_json"), {})
    values = profile.get("datasets") if isinstance(profile, Mapping) else None
    values = [str(value) for value in values] if isinstance(values, list) else []
    return values or ["unknown"]


def arm_metrics(rows: Sequence[Mapping[str, Any]], *, min_sample: int = MIN_ARM_SIMULATIONS) -> dict[str, Any]:
    """Summarize one arm's settled simulations (P25.2 primary and secondary metrics)."""
    simulated = [row for row in rows if str(row.get("completed_at") or "")]
    total = len(simulated)
    passes = [row for row in simulated if row.get("sim_is_pass")]
    sharpe = [float(row["sharpe"]) for row in simulated
              if isinstance(row.get("sharpe"), (int, float)) and not isinstance(row.get("sharpe"), bool)]
    fitness = [float(row["fitness"]) for row in simulated
               if isinstance(row.get("fitness"), (int, float)) and not isinstance(row.get("fitness"), bool)]
    low, high = diagnostics.wilson_interval(len(passes), total) if total else (0.0, 0.0)
    rate = len(passes) / total if total else None
    return {
        "simulations": total,
        "is_pass": len(passes),
        "is_pass_per_simulation": rate,
        "ci95": [round(low, 6), round(high, 6)],
        "simulations_per_is_pass": (total / len(passes)) if passes else None,
        "sharpe_median": _quantile(sharpe, 0.5),
        "sharpe_p90": _quantile(sharpe, 0.9),
        "fitness_median": _quantile(fitness, 0.5),
        "fitness_p90": _quantile(fitness, 0.9),
        "turnover_failure_rate": (
            sum(1 for row in simulated if _turnover_failed(row)) / total if total else None
        ),
        "low_confidence": total < int(min_sample),
    }


def survivor_diversity(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Effective (exp-Shannon) diversity **among IS-pass survivors** (P25.2 secondary)."""
    survivors = [row for row in rows if row.get("sim_is_pass")]
    if not survivors:
        return {"survivors": 0}

    def counts(values: Sequence[str]) -> dict[str, int]:
        table: dict[str, int] = {}
        for value in values:
            table[str(value or "unknown")] = table.get(str(value or "unknown"), 0) + 1
        return table

    grammar = counts([str(row.get("grammar_skeleton_hash") or "unknown") for row in survivors])
    semantic = counts([str(row.get("semantic_skeleton_hash") or "unknown") for row in survivors])
    motif = counts([str(row.get("motif_id") or "none") for row in survivors])
    dataset = counts([value for row in survivors for value in _datasets(row)])
    return {
        "survivors": len(survivors),
        "effective_grammar": round(diversity.effective_count(grammar), 4),
        "effective_semantic": round(diversity.effective_count(semantic), 4),
        "effective_motif": round(diversity.effective_count(motif), 4),
        "effective_dataset": round(diversity.effective_count(dataset), 4),
    }


def _ledger(db: Any, *, as_of: str | None) -> tuple[list[dict[str, Any]], str]:
    rows = [dict(row) for row in db.query(_LEDGER_SQL)]
    clock = as_of or max((str(row.get("completed_at") or "") for row in rows), default="")
    if clock:
        rows = [row for row in rows if str(row.get("completed_at") or "") <= clock]
    return rows, clock


def _ratio(target: float | None, baseline: float | None) -> float | None:
    if target is None or baseline in (None, 0):
        return None
    return target / baseline


def _item(status: str, **detail: Any) -> dict[str, Any]:
    return {"status": status, **detail}


def evaluate_gate(
    baseline: Mapping[str, Any],
    target: Mapping[str, Any],
    baseline_diversity: Mapping[str, Any],
    target_diversity: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate the P25.3 checklist. Unmeasurable items are ``unknown``, never ``pass``."""
    checklist: dict[str, Any] = {}
    ratio = _ratio(target.get("is_pass_per_simulation"), baseline.get("is_pass_per_simulation"))
    if ratio is None or baseline.get("low_confidence") or target.get("low_confidence"):
        checklist["materially_better_efficiency"] = _item(
            "unknown",
            ratio=ratio,
            reason="insufficient settled simulations on one or both arms",
        )
    else:
        checklist["materially_better_efficiency"] = _item(
            "pass" if ratio >= MATERIAL_RATIO else "fail",
            ratio=ratio,
            required_ratio=MATERIAL_RATIO,
            target_ci95=target.get("ci95"),
            baseline_ci95=baseline.get("ci95"),
        )

    # "Competitive versus V2" is the same comparison here because V2 is the baseline arm; keep
    # it explicit so a future baseline switch does not silently drop the V2 comparison.
    checklist["competitive_versus_baseline"] = _item(
        checklist["materially_better_efficiency"]["status"],
        ratio=ratio,
    )

    baseline_effective = baseline_diversity.get("effective_grammar")
    target_effective = target_diversity.get("effective_grammar")
    if baseline_effective in (None, 0) or target_effective is None:
        checklist["survivor_diversity_preserved"] = _item(
            "unknown", reason="no baseline survivors to bound the target against",
        )
    else:
        floor = baseline_effective * DIVERSITY_FLOOR_RATIO
        checklist["survivor_diversity_preserved"] = _item(
            "pass" if target_effective >= floor else "fail",
            baseline_effective_grammar=baseline_effective,
            target_effective_grammar=target_effective,
            floor=round(floor, 4),
        )

    # Correlation / robustness need downstream BRAIN outcomes. Reporting them as ``unknown``
    # is the honest state until those checks settle; it must never read as a pass.
    checklist["correlation_and_robustness"] = _item(
        "unknown",
        reason="requires settled BRAIN SELF_CORRELATION / robustness outcomes",
        target_corr_status=_corr_status_present(target),
    )
    checklist["no_point_in_time_leakage"] = _item(
        "pass",
        reason="every row is filtered by its own completed_at against the report clock",
    )
    checklist["reproducible_from_config"] = _item(
        "unknown",
        reason="verify the winning campaigns' stored configuration still replays identically",
    )
    statuses = [entry["status"] for entry in checklist.values()]
    promoted = all(status == "pass" for status in statuses)
    return {
        "checklist": checklist,
        "promoted": promoted,
        "blocking": sorted(name for name, entry in checklist.items() if entry["status"] != "pass"),
    }


def _corr_status_present(target: Mapping[str, Any]) -> bool:
    # The arm summary does not carry correlation rows; kept as a hook so a caller that does
    # have them can extend the gate without changing its shape.
    return bool(target.get("corr_rows"))


def report(
    db: Any,
    *,
    as_of: str | None = None,
    baseline: str = DEFAULT_BASELINE,
    target: str = DEFAULT_TARGET,
    campaign_limit: int = 12,
    min_sample: int = MIN_ARM_SIMULATIONS,
) -> dict[str, Any]:
    """Build the complete promotion report for one ledger snapshot."""
    rows, clock = _ledger(db, as_of=as_of)
    baseline_rows = [row for row in rows if str(row.get("generator_version") or "") == baseline]
    target_rows = [row for row in rows if str(row.get("generator_version") or "") == target]
    baseline_metrics = arm_metrics(baseline_rows, min_sample=min_sample)
    target_metrics = arm_metrics(target_rows, min_sample=min_sample)
    baseline_diversity = survivor_diversity(baseline_rows)
    target_diversity = survivor_diversity(target_rows)

    campaigns: dict[str, dict[str, Any]] = {}
    for row in target_rows:
        campaigns.setdefault(str(row.get("campaign_id") or "none"), []).append(row)
    campaign_metrics = {
        name: arm_metrics(members, min_sample=min_sample) for name, members in campaigns.items()
    }
    ranked_campaigns = sorted(
        campaign_metrics.items(),
        key=lambda item: (-int(item[1]["simulations"]), item[0]),
    )[: int(campaign_limit)]

    versions: dict[str, dict[str, Any]] = {}
    for row in rows:
        versions.setdefault(str(row.get("generator_version") or "unknown"), []).append(row)
    version_metrics = {
        name: arm_metrics(members, min_sample=min_sample)
        for name, members in sorted(versions.items(), key=lambda item: -len(item[1]))
    }

    return {
        "version": PROMOTION_VERSION,
        "as_of": clock,
        "baseline": baseline,
        "target": target,
        "baseline_metrics": baseline_metrics,
        "target_metrics": target_metrics,
        "baseline_diversity": baseline_diversity,
        "target_diversity": target_diversity,
        "gate": evaluate_gate(baseline_metrics, target_metrics, baseline_diversity, target_diversity),
        "by_version": version_metrics,
        "target_campaigns": dict(ranked_campaigns),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P25 promotion gate for Generator V3")
    parser.add_argument("--db")
    parser.add_argument("--as-of", dest="as_of")
    parser.add_argument("--baseline", default=DEFAULT_BASELINE)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--min-sample", dest="min_sample", type=int, default=MIN_ARM_SIMULATIONS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        payload = report(
            db, as_of=args.as_of, baseline=args.baseline, target=args.target,
            min_sample=args.min_sample,
        )
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
