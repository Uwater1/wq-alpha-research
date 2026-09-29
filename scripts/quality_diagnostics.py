"""Failure attribution over the permanent trial ledger (P18).

The live V3 campaigns recorded **0 IS passes in 138 simulations** against a historical
baseline around 23%. The roadmap is explicit that this must be localised before any policy
weight is tuned, and that a bucket must never be ranked from one or two trials.

This module turns the ledger into that evidence:

* **P18.1 outcome cube** — attempts / simulations / IS passes / pass rate with Wilson
  intervals, Sharpe, Fitness and turnover quantiles, and failure-reason shares, bucketed by
  generation mode, motif, source dataset/category, semantic-role signature, grammar and
  semantic skeleton, recipe dimensions, concrete mutation operation and parent lineage
  quality.
* **P18.2 matched cohort comparison** — nested matched strata (scope → source →
  expression shape → recipe) that separate the *field/source* gap, the *expression/motif*
  gap, the *recipe/settings* gap and the residual *search-policy* gap between two generator
  versions.
* **P18.3 failure taxonomy** — how much of the gap is low Sharpe, low Fitness, turnover,
  correlation, invalid/unsupported expressions or a specific weak family/motif/recipe cell.

Everything is **point-in-time safe**: a single ``as_of`` clock is applied to when a trial
settled, and :func:`leakage_check` re-verifies afterwards that no later outcome entered the
report. The artifact is deliberately credential-free and expression-free: it reports derived
identities, catalog field/dataset names and counts, never an expression or an alpha id.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical
import diversity
import expression_grammar as grammar
import research_db

REPO_ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTICS_VERSION = "quality-diagnostics-v1"
DEFAULT_ARTIFACT = REPO_ROOT / "quality_diagnostics.json"

#: A trial only exists once it has been attempted; these are the statuses that mean the
#: candidate reached BRAIN and the platform answered.
SIMULATED_STATUSES = frozenset(
    {"SIMULATED", "IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE", "REJECTED"}
)
PASS_STATUSES = frozenset({"IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"})
CORR_STATUSES = frozenset({"CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"})

#: Coarse failure regions (P18.3). BRAIN check name -> region. A check name that is not
#: listed here lands in ``other_check``: unknown is reported as unknown, never guessed into
#: a region where it would change a conclusion.
FAILURE_REGION_BY_CHECK = {
    "LOW_SHARPE": "low_sharpe",
    "LOW_FITNESS": "low_fitness",
    "HIGH_TURNOVER": "turnover",
    "LOW_TURNOVER": "turnover",
    "SELF_CORRELATION": "correlation",
    "PROD_CORRELATION": "correlation",
    "CONCENTRATED_WEIGHT": "concentration",
    "LOW_SUB_UNIVERSE_SHARPE": "sub_universe",
    "UNITS": "invalid_or_unsupported",
    "MATCHES_COMPETITION": "other_check",
    "MATCHES_PYRAMID": "other_check",
    "IS_LADDER_SHARPE": "low_sharpe",
    "PENDING": "unsettled",
}
#: Preference order when one simulation violates several checks: the *first* region a BRAIN
#: reviewer would name. Failures are not double counted.
FAILURE_REGION_PRIORITY = (
    "invalid_or_unsupported", "low_sharpe", "low_fitness", "turnover", "concentration",
    "sub_universe", "correlation", "other_check", "unsettled", "unsimulated", "pass",
)
DEFAULT_MIN_SAMPLE = 5
#: Nothing identifying a private expression or an alpha may ever reach the artifact.
FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {"expression", "normalized_expression", "brain_alpha_id", "simulation_id", "alpha_id",
     "canonical_key", "expression_hash", "settings_hash", "skeleton_hash"}
)


def wilson_interval(passes: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial rate; ``(0.0, 1.0)`` when there is no sample.

    A raw ``passes / total`` from a 3-trial bucket reads like knowledge. The interval is what
    makes ``1/1`` and ``30/100`` visibly different, and it is why a bucket is never ranked
    from one or two trials (P18.1).
    """
    total = int(total)
    passes = max(0, min(int(passes), total))
    if total <= 0:
        return (0.0, 1.0)
    phat = passes / total
    denominator = 1.0 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _quantiles(values: Sequence[float], points: Sequence[float] = (0.1, 0.5, 0.9)) -> dict[str, float | None]:
    clean = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not clean:
        return {f"p{int(point * 100)}": None for point in points} | {"max": None}
    result: dict[str, float | None] = {}
    for point in points:
        index = min(len(clean) - 1, max(0, int(round(point * (len(clean) - 1)))))
        result[f"p{int(point * 100)}"] = round(clean[index], 4)
    result["max"] = round(clean[-1], 4)
    return result


def _json_map(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        parsed = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return list(parsed) if isinstance(parsed, (list, tuple)) else []


def _checks_of(raw: Any) -> list[dict[str, Any]]:
    """Normalise ``checks_json``. Two shapes exist in the ledger: a list of check dicts and a
    legacy mapping of ``name -> bool``. Both are read; neither is trusted to be complete."""
    if isinstance(raw, Mapping):
        return [{"name": str(name), "result": "PASS" if value else "FAIL"} for name, value in raw.items()]
    try:
        parsed = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    if isinstance(parsed, Mapping):
        return [{"name": str(name), "result": "PASS" if value else "FAIL"} for name, value in parsed.items()]
    return [dict(item) for item in parsed if isinstance(item, Mapping)]


def failing_checks(checks: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Names of checks the platform marked ``FAIL`` (a ``PASS``/``PENDING`` check is not one)."""
    return tuple(
        str(check.get("name")) for check in checks
        if str(check.get("result") or "").upper() == "FAIL"
    )


def failure_region(*, simulated: bool, error: str | None, checks: Sequence[Mapping[str, Any]]) -> str:
    """The single coarse region a failed simulation is attributed to (P18.3)."""
    if not simulated:
        return "unsimulated"
    failed = failing_checks(checks)
    if not failed:
        if error:
            return "invalid_or_unsupported"
        return "pass"
    regions = {FAILURE_REGION_BY_CHECK.get(name, "other_check") for name in failed}
    for region in FAILURE_REGION_PRIORITY:
        if region in regions:
            return region
    return "other_check"


def _recipe_bucket(recipe: Mapping[str, Any]) -> str:
    """Coarse recipe cell: the dimensions that changed between the V2 and V3 samples.

    Truncation and decay are the two settings that visibly separated the historical winners
    (``0.08`` truncation, small decay) from the V3 sample, so they lead the bucket; lookback
    and neutralization follow because they were sampled from different grids.
    """
    if not recipe:
        return "unknown"
    truncation = recipe.get("truncation")
    decay = recipe.get("decay")
    lookback = recipe.get("lookback")
    neutralization = str(recipe.get("neutralization") or "unknown")
    truncation_key = "unknown" if truncation is None else f"{float(truncation):g}"
    return f"t{truncation_key}|d{decay if decay is not None else '?'}|lb{lookback if lookback is not None else '?'}|{neutralization}"


def _parent_quality_bucket(rows_by_id: Mapping[int, Mapping[str, Any]], parent_ids: Sequence[int]) -> str:
    """Where a child came from, in outcome terms: no parent, unknown, low/mid/high, proven."""
    if not parent_ids:
        return "no_parent"
    sharpe_values = [
        float(rows_by_id[int(pid)]["sharpe"]) for pid in parent_ids
        if int(pid) in rows_by_id and rows_by_id[int(pid)].get("sharpe") is not None
    ]
    if any(int(pid) in rows_by_id and rows_by_id[int(pid)].get("is_pass") for pid in parent_ids):
        return "proven_parent"
    if not sharpe_values:
        return "unknown_parent"
    best = max(sharpe_values)
    if best >= 1.25:
        return "high_parent"
    if best >= 0.5:
        return "mid_parent"
    return "low_parent"


def outer_operator(expression: str, metadata: Mapping[str, Any] | None = None) -> str:
    """The expression's actual outermost call, not an alphabetical operator list.

    ``canonical.operators_of`` returns a *sorted* set, so its first element says nothing
    about which operator combines the components. The outermost call is exactly the
    difference between a multi-component composite (``add(...)``) and a single signal wrapped
    in a normalizer (``group_rank(...)``), which is what separated the two search
    distributions, so it is parsed rather than guessed.
    """
    text = str(expression or "")
    if not text:
        return "unknown"
    try:
        node = grammar.parse_expression(text, metadata)
    except grammar.GrammarError:
        node = None
    if isinstance(node, grammar.CallNode):
        return str(node.operator).lower()
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)
    return match.group(1).lower() if match else "unknown"


def _shape_family(row: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> str:
    """Version-comparable expression shape: outermost operator plus source arity.

    V2 rows carry no motif id, so a motif-keyed comparison cannot include them at all. The
    outer operator with the number of distinct source fields is derivable for every row in
    the ledger and is what actually differed between the two search distributions.
    """
    expression = str(row.get("expression") or "")
    fields = canonical.fields_of(expression) if expression else []
    return f"{outer_operator(expression, metadata)}|{len(set(fields))}src"


def evidence_row(
    trial: Mapping[str, Any],
    candidate: Mapping[str, Any] | None,
    simulation: Mapping[str, Any] | None,
    *,
    catalog_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One ledger row as the diagnostic sees it. No expression or alpha id is retained."""
    candidate = candidate or {}
    simulation = simulation or {}
    expression = str(
        candidate.get("normalized_expression") or trial.get("normalized_expression") or ""
    )
    parameters = _json_map(trial.get("mutation_parameters_json"))
    recipe = _json_map(trial.get("recipe_json")) or _json_map(candidate.get("recipe_json")) or _json_map(parameters.get("recipe"))
    profile = _json_map(trial.get("source_profile_json")) or _json_map(candidate.get("source_profile_json"))
    if not profile.get("field_ids"):
        # Legacy V2 rows predate the stored source profile. Deriving it from the expression is
        # what makes a source-level V2/V3 comparison possible at all: without it every V2 row
        # reads as dataset ``unknown`` and the matched cohort level is empty by construction.
        profile = diversity.derive_source_profile(expression, catalog_metadata) or profile
    settings = _json_map(candidate.get("settings_json"))
    status = str(candidate.get("status") or "")
    simulated = bool(simulation) or status in SIMULATED_STATUSES
    checks = _checks_of(simulation.get("checks_json"))
    fields = [str(field) for field in (profile.get("field_ids") or canonical.fields_of(expression))]
    datasets = [str(name) for name in (profile.get("datasets") or [])]
    categories = [str(name) for name in (profile.get("categories") or [])]
    roles = sorted({role for field in fields for role in grammar.infer_field_roles(field)})
    return {
        "trial_id": int(trial.get("id") or 0),
        "candidate_id": int(trial.get("candidate_id") or 0),
        "campaign_id": str(trial.get("campaign_id") or ""),
        "generator_version": str(trial.get("generator_version") or "unknown"),
        "scope": canonical.scope_from_settings(settings) if settings else _json_map(trial.get("scope_json")),
        "settled_at": str(simulation.get("completed_at") or trial.get("created_at") or ""),
        "created_at": str(trial.get("created_at") or ""),
        "decision": str(trial.get("decision") or ""),
        "generation_mode": str(trial.get("generation_mode") or "legacy"),
        "strategy": str(trial.get("generator_strategy") or ""),
        "motif_id": str(trial.get("motif_id") or candidate.get("motif_id") or "none"),
        "mutation_operation": canonical.normalize_mutation_operation(parameters.get("operation")) or "none",
        "mutation_type": str(trial.get("mutation_type") or ""),
        "recipe": recipe,
        "recipe_bucket": _recipe_bucket(recipe),
        "truncation": recipe.get("truncation"),
        "decay": recipe.get("decay"),
        "lookback": recipe.get("lookback"),
        "neutralization": recipe.get("neutralization"),
        "fields": fields,
        "datasets": datasets,
        "categories": categories,
        "primary_dataset": (datasets or ["unknown"])[0],
        "primary_category": (categories or ["unknown"])[0],
        "semantic_roles": tuple(roles) or ("generic",),
        "semantic_role_signature": "+".join(sorted(set(roles)) or ["generic"]),
        "cross_dataset": bool(profile.get("cross_dataset")),
        "field_count": len(fields),
        "grammar_skeleton_hash": str(trial.get("grammar_skeleton_hash") or ""),
        "semantic_skeleton_hash": str(trial.get("semantic_skeleton_hash") or ""),
        "parent_ids": [int(pid) for pid in _json_list(trial.get("parent_ids_json"))],
        "outer_operator": outer_operator(expression, catalog_metadata),
        "shape_family": _shape_family({"expression": expression}, catalog_metadata),
        "signal_family": str(trial.get("signal_family") or candidate.get("signal_family") or ""),
        "status": status,
        "simulated": simulated,
        "sharpe": simulation.get("sharpe") if simulation.get("sharpe") is not None else candidate.get("sharpe"),
        "fitness": simulation.get("fitness") if simulation.get("fitness") is not None else candidate.get("fitness"),
        "turnover": simulation.get("turnover") if simulation.get("turnover") is not None else candidate.get("turnover"),
        "is_pass": bool(simulation.get("is_pass")) or status in PASS_STATUSES,
        "corr_pass": status in CORR_STATUSES,
        "error": str(simulation.get("error") or ""),
        "failing_checks": failing_checks(checks),
    }


#: Every dimension the P18.1 cube reports on.
CUBE_DIMENSIONS = (
    "generator_version", "campaign_id", "generation_mode", "strategy", "motif_id",
    "mutation_operation", "mutation_type", "primary_dataset", "primary_category",
    "semantic_role_signature", "outer_operator", "shape_family", "recipe_bucket", "truncation",
    "decay", "lookback", "neutralization", "grammar_skeleton_hash", "semantic_skeleton_hash",
    "parent_quality_bucket", "failure_region", "field_count", "cross_dataset",
)


def summarize(rows: Sequence[Mapping[str, Any]], *, min_sample: int = DEFAULT_MIN_SAMPLE) -> dict[str, Any]:
    """Attempts, simulations, IS/CORR passes, quality quantiles and failure shares for a set."""
    total = len(rows)
    simulated = [row for row in rows if row["simulated"]]
    passes = sum(1 for row in simulated if row["is_pass"])
    corr_passes = sum(1 for row in simulated if row["corr_pass"])
    low, high = wilson_interval(passes, len(simulated))
    regions = Counter(str(row.get("failure_region")) for row in simulated)
    checks = Counter(name for row in simulated for name in row["failing_checks"])
    return {
        "attempts": total,
        "simulations": len(simulated),
        "is_pass": passes,
        "corr_pass": corr_passes,
        "is_pass_rate": round(passes / len(simulated), 6) if simulated else None,
        "corr_pass_rate": round(corr_passes / len(simulated), 6) if simulated else None,
        "pass_rate_ci95": [round(low, 6), round(high, 6)],
        "sharpe": _quantiles([row["sharpe"] for row in simulated]),
        "fitness": _quantiles([row["fitness"] for row in simulated]),
        "turnover_median": _quantiles([row["turnover"] for row in simulated], (0.5,))["p50"],
        "failure_region": dict(regions.most_common()),
        "failing_check": dict(checks.most_common(12)),
        "low_confidence": len(simulated) < min_sample,
    }


def _bucket_value(row: Mapping[str, Any], dimension: str) -> str:
    value = row.get(dimension)
    if isinstance(value, (list, tuple)):
        return "|".join(str(item) for item in value) or "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None or value == "":
        return "unknown"
    return str(value)


def outcome_cube(
    rows: Sequence[Mapping[str, Any]],
    dimensions: Sequence[str] = CUBE_DIMENSIONS,
    *,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> dict[str, list[dict[str, Any]]]:
    """P18.1 outcome cube. Each cell carries its own sample size and confidence interval."""
    cube: dict[str, list[dict[str, Any]]] = {}
    for dimension in dimensions:
        buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            buckets[_bucket_value(row, dimension)].append(row)
        cells: list[dict[str, Any]] = []
        for value, members in buckets.items():
            cell = {"bucket": value, **summarize(members, min_sample=min_sample)}
            cell["dimension"] = dimension
            cells.append(cell)
        cells.sort(key=lambda cell: (cell["is_pass_rate"] is None, -(cell["is_pass_rate"] or 0.0),
                                     -cell["simulations"], cell["bucket"]))
        cube[dimension] = cells
    return cube


def failure_taxonomy(rows: Sequence[Mapping[str, Any]], *, min_sample: int = DEFAULT_MIN_SAMPLE) -> dict[str, Any]:
    """P18.3: how the outcomes decompose into coarse failure regions, by version."""
    by_version: dict[str, dict[str, Any]] = {}
    for version in sorted({str(row["generator_version"]) for row in rows}):
        members = [row for row in rows if str(row["generator_version"]) == version]
        by_version[version] = summarize(members, min_sample=min_sample)
    return {
        "overall": summarize(rows, min_sample=min_sample),
        "by_version": by_version,
        "regions": list(FAILURE_REGION_PRIORITY),
    }


def _matched_strata(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Nested strata keys, coarse to fine (P18.2).

    Each level adds exactly one candidate dimension, so the *change* in the version gap
    between two adjacent levels is the part of the gap that dimension explains.
    """
    def scope_of(row: Mapping[str, Any]) -> str:
        scope = row.get("scope") or {}
        if not isinstance(scope, Mapping):
            return "unknown"
        return "|".join(f"{key}={scope.get(key)}" for key in ("region", "universe", "delay"))

    levels: list[tuple[str, Any]] = [
        ("scope", lambda row: scope_of(row)),
        ("scope+source", lambda row: (scope_of(row), row["primary_dataset"])),
        ("+shape", lambda row: (scope_of(row), row["primary_dataset"], row["shape_family"])),
        ("+recipe", lambda row: (scope_of(row), row["primary_dataset"], row["shape_family"], row["recipe_bucket"])),
    ]
    result: list[dict[str, Any]] = []
    for name, key_fn in levels:
        result.append({"level": name, "key": key_fn})
    return result


def matched_cohorts(
    rows: Sequence[Mapping[str, Any]],
    *,
    baseline: str,
    target: str,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> dict[str, Any]:
    """P18.2: separate the source, expression and recipe gaps from the search-policy residual.

    At every level only strata present in **both** versions are compared, so the rates are
    like-for-like. ``gap_closed_by`` for a level is ``gap(coarser) - gap(this level)``: how
    much of the previous gap disappears once that level's dimension is held constant.
    """
    baseline_rows = [row for row in rows if str(row["generator_version"]) == baseline]
    target_rows = [row for row in rows if str(row["generator_version"]) == target]
    levels = _matched_strata(rows)
    measurements: dict[str, dict[str, Any]] = {}
    previous_gap: float | None = None
    previous_level: str | None = None
    attribution: dict[str, float | None] = {}
    for level in levels:
        key_fn = level["key"]
        name = str(level["level"])
        base_groups: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
        target_groups: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
        for row in baseline_rows:
            base_groups[key_fn(row)].append(row)
        for row in target_rows:
            target_groups[key_fn(row)].append(row)
        shared = sorted(set(base_groups) & set(target_groups), key=str)
        base_matched = [row for key in shared for row in base_groups[key]]
        target_matched = [row for key in shared for row in target_groups[key]]
        base_summary = summarize(base_matched, min_sample=min_sample)
        target_summary = summarize(target_matched, min_sample=min_sample)
        base_rate = base_summary["is_pass_rate"]
        target_rate = target_summary["is_pass_rate"]
        raw_gap = None if base_rate is None or target_rate is None else round(target_rate - base_rate, 6)
        # No shared stratum means the two versions searched *disjoint* regions at this
        # dimension, so there is nothing left to compare and the whole remaining gap belongs
        # to that dimension. The effective gap is therefore 0 there, and every finer level
        # inherits the same convention (there is still nothing to compare).
        unmatched = not shared or raw_gap is None
        effective = 0.0 if unmatched else raw_gap
        measurements[name] = {
            "level": name,
            "shared_strata": len(shared),
            "baseline": base_summary,
            "target": target_summary,
            "gap": raw_gap,
            "matched_gap": effective,
            "unmatched": bool(unmatched),
            "baseline_coverage": round(len(base_matched) / len(baseline_rows), 4) if baseline_rows else None,
            "target_coverage": round(len(target_matched) / len(target_rows), 4) if target_rows else None,
            "thin": bool(unmatched) or len(shared) < min_sample or base_summary["low_confidence"] or target_summary["low_confidence"],
        }
        if previous_gap is not None and previous_level is not None:
            attribution[f"{previous_level}->{name}"] = round(previous_gap - effective, 6)
        previous_gap, previous_level = effective, name
    return {
        "baseline_version": baseline,
        "target_version": target,
        "baseline_simulations": sum(1 for row in baseline_rows if row["simulated"]),
        "target_simulations": sum(1 for row in target_rows if row["simulated"]),
        # Sign convention: the gap is ``target - baseline``, so a negative number means the
        # target version is worse than the baseline. Attribution terms are negative when the
        # dimension explains a target shortfall.
        "sign": "target-minus-baseline",
        "levels": measurements,
        "gap_attribution": {
            "field_or_source_gap": attribution.get("scope->scope+source"),
            "expression_or_motif_gap": attribution.get("scope+source->+shape"),
            "recipe_or_settings_gap": attribution.get("+shape->+recipe"),
            # Whatever is left once sources, shapes and recipes are held fixed, i.e. the part
            # of the gap a change in search policy *inside* comparable regions could close.
            "residual_search_policy_gap": measurements["+recipe"]["matched_gap"],
        },
    }


def separation_analysis(
    rows: Sequence[Mapping[str, Any]],
    dimensions: Sequence[str] = CUBE_DIMENSIONS,
    *,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> dict[str, Any]:
    """Which candidate dimensions materially separate better from worse outcomes (exit gate).

    A dimension separates when its well-sampled buckets span a wide pass-rate range. Buckets
    below ``min_sample`` are excluded from the ranking rather than allowed to look extreme.
    """
    cube = outcome_cube(rows, dimensions, min_sample=min_sample)
    dimension_rows: list[dict[str, Any]] = []
    for dimension, cells in cube.items():
        eligible = [cell for cell in cells if cell["simulations"] >= min_sample]
        if len(eligible) < 2:
            continue
        rates = [cell["is_pass_rate"] for cell in eligible]
        best = max(eligible, key=lambda cell: (cell["is_pass_rate"], cell["simulations"]))
        worst = min(eligible, key=lambda cell: (cell["is_pass_rate"], -cell["simulations"]))
        dimension_rows.append({
            "dimension": dimension,
            "well_sampled_buckets": len(eligible),
            "rate_min": min(rates),
            "rate_max": max(rates),
            "rate_range": round(max(rates) - min(rates), 6),
            "best_bucket": best["bucket"],
            "best_rate": best["is_pass_rate"],
            "best_simulations": best["simulations"],
            "worst_bucket": worst["bucket"],
            "worst_rate": worst["is_pass_rate"],
            "worst_simulations": worst["simulations"],
        })
    dimension_rows.sort(key=lambda row: (-row["rate_range"], row["dimension"]))
    return {"dimensions": dimension_rows, "min_sample": min_sample}


def dominant_failure_regions(
    rows: Sequence[Mapping[str, Any]],
    dimensions: Sequence[str] = CUBE_DIMENSIONS,
    *,
    version: str,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """The cells that actually absorb a version's wasted simulations (P18 exit gate)."""
    members = [row for row in rows if str(row["generator_version"]) == version]
    cube = outcome_cube(members, dimensions, min_sample=min_sample)
    wasted: list[dict[str, Any]] = []
    for dimension, cells in cube.items():
        for cell in cells:
            if cell["simulations"] < min_sample or cell["is_pass"]:
                continue
            wasted.append({
                "dimension": dimension,
                "bucket": cell["bucket"],
                "simulations": cell["simulations"],
                "is_pass_rate": cell["is_pass_rate"],
                "sharpe_p90": cell["sharpe"]["p90"],
                "failure_region": next(iter(cell["failure_region"]), None),
            })
    wasted.sort(key=lambda cell: (-cell["simulations"], cell["dimension"], str(cell["bucket"])))
    return wasted[:limit]


def leakage_check(report: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> None:
    """Re-verify point-in-time safety *after* the report was built.

    Raises ``ValueError`` when any included row settled after the report clock, so a report
    that leaked a future outcome can never be used to tune a policy.
    """
    as_of = str(report.get("as_of") or "")
    if not as_of:
        raise ValueError("report has no as_of clock; point-in-time safety is unverifiable")
    included = report.get("included_trial_ids") or []
    if not included:
        return
    by_id = {int(row["trial_id"]): row for row in rows}
    late = [
        int(trial_id) for trial_id in included
        if str(by_id.get(int(trial_id), {}).get("settled_at") or "") > as_of
    ]
    if late:
        raise ValueError(f"{len(late)} trial(s) settled after the report clock; leakage")


def _sanitize(value: Any) -> Any:
    """Strip anything that could carry a private expression or alpha id out of the artifact."""
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize(item) for key, item in value.items()
            if str(key) not in FORBIDDEN_ARTIFACT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


def load_evidence(
    db: research_db.ResearchDB,
    *,
    as_of: str | None = None,
    generator_versions: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Read every trial that settled at or before ``as_of`` (P18 is a ledger-wide diagnostic)."""
    trials = db.query(
        "SELECT t.id AS trial_id, t.candidate_id, t.campaign_id, t.generator_version,"
        " t.generation_mode, t.generator_strategy, t.motif_id, t.mutation_type,"
        " t.mutation_parameters_json, t.recipe_json, t.grammar_skeleton_hash,"
        " t.semantic_skeleton_hash, t.source_profile_json, t.parent_ids_json, t.decision,"
        " t.validation_result, t.scope_json, t.signal_family, t.created_at"
        " FROM research_trials t WHERE t.candidate_id IS NOT NULL"
    )
    candidates = {int(row["id"]): row for row in db.query("SELECT * FROM candidates")}
    metadata = diversity.load_field_metadata()
    simulations: dict[int, dict[str, Any]] = {}
    for row in db.query("SELECT * FROM simulations ORDER BY id"):
        if row.get("candidate_id") is not None:
            simulations[int(row["candidate_id"])] = dict(row)  # later rows win
    rows: list[dict[str, Any]] = []
    for trial in trials:
        candidate_id = int(trial["candidate_id"])
        row = evidence_row(trial, candidates.get(candidate_id), simulations.get(candidate_id),
                           catalog_metadata=metadata)
        row["failure_region"] = failure_region(
            simulated=bool(row["simulated"]), error=row["error"], checks=_checks_of(
                (simulations.get(candidate_id) or {}).get("checks_json")
            ),
        )
        rows.append(row)
    parents = {int(row["candidate_id"]): row for row in rows}
    for row in rows:
        row["parent_quality_bucket"] = _parent_quality_bucket(parents, row["parent_ids"])
    if generator_versions:
        wanted = {str(version) for version in generator_versions}
        rows = [row for row in rows if str(row["generator_version"]) in wanted]
    if as_of:
        rows = [row for row in rows if str(row["settled_at"] or "") <= as_of]
    return rows


def build_report(
    db: research_db.ResearchDB,
    *,
    as_of: str | None = None,
    baseline_version: str = "catalog-generator-v2",
    target_version: str = "catalog-generator-v3",
    min_sample: int = DEFAULT_MIN_SAMPLE,
    dimensions: Sequence[str] = CUBE_DIMENSIONS,
) -> dict[str, Any]:
    """Build the complete P18 report (cube + matched cohorts + taxonomy) as one artifact."""
    rows = load_evidence(db)
    # The clock defaults to the newest settled trial, so "as of now" is explicit and a past
    # campaign can be replayed by passing its own clock.
    clock = as_of or max((str(row["settled_at"] or "") for row in rows), default="")
    included = [row for row in rows if str(row["settled_at"] or "") <= clock]
    report: dict[str, Any] = {
        "diagnostics_version": DIAGNOSTICS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": clock,
        "min_sample": int(min_sample),
        "baseline_version": baseline_version,
        "target_version": target_version,
        "included_trial_ids": [int(row["trial_id"]) for row in included],
        "versions": summarize(included),
        "by_version": {
            version: summarize([row for row in included if str(row["generator_version"]) == version], min_sample=min_sample)
            for version in sorted({str(row["generator_version"]) for row in included})
        },
        "outcome_cube": outcome_cube(included, dimensions, min_sample=min_sample),
        "failure_taxonomy": failure_taxonomy(included, min_sample=min_sample),
        "matched_cohorts": matched_cohorts(
            included, baseline=baseline_version, target=target_version, min_sample=min_sample,
        ),
        "separation": separation_analysis(included, dimensions, min_sample=min_sample),
        "dominant_failure_regions": dominant_failure_regions(
            included, dimensions, version=target_version, min_sample=min_sample,
        ),
    }
    leakage_check(report, rows)
    return report


def render(report: Mapping[str, Any]) -> str:
    """A short human-readable summary; the artifact keeps the machine-readable detail."""
    lines: list[str] = []
    lines.append(f"quality diagnostics {report['diagnostics_version']} as of {report['as_of']}")
    for version, summary in sorted(report["by_version"].items()):
        rate = summary["is_pass_rate"]
        rate_text = "n/a" if rate is None else f"{rate * 100:.1f}%"
        lines.append(
            f"  {version:28s} attempts={summary['attempts']:4d} sims={summary['simulations']:4d}"
            f" is_pass={summary['is_pass']:3d} rate={rate_text:>7s}"
            f" ci95={summary['pass_rate_ci95']} sharpe_med={summary['sharpe']['p50']}"
            f" sharpe_p90={summary['sharpe']['p90']}"
        )
    cohorts = report["matched_cohorts"]
    lines.append("  matched-cohort gap attribution:")
    for name, value in cohorts["gap_attribution"].items():
        lines.append(f"    {name:32s} {value}")
    lines.append("  separating dimensions (well-sampled buckets):")
    for row in report["separation"]["dimensions"][:6]:
        lines.append(
            f"    {row['dimension']:26s} range={row['rate_range']:.3f}"
            f" best={row['best_bucket']}({row['best_simulations']})"
            f" worst={row['worst_bucket']}({row['worst_simulations']})"
        )
    regions = report["dominant_failure_regions"]
    if regions:
        lines.append("  dominant failure regions:")
        for cell in regions[:8]:
            lines.append(
                f"    {cell['dimension']}={cell['bucket']} sims={cell['simulations']}"
                f" region={cell['failure_region']} sharpe_p90={cell['sharpe_p90']}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generator quality diagnostics over the trial ledger (P18)")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--as-of", dest="as_of", help="point-in-time clock (ISO); default: now")
    parser.add_argument("--baseline", default="catalog-generator-v2")
    parser.add_argument("--target", default="catalog-generator-v3")
    parser.add_argument("--min-sample", type=int, default=DEFAULT_MIN_SAMPLE)
    parser.add_argument("--out", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--dimension", action="append", dest="dimensions",
                        help="restrict the cube to these dimensions (repeatable)")
    parser.add_argument("--quiet", action="store_true", help="write the artifact without printing the summary")
    args = parser.parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        report = build_report(
            db, as_of=args.as_of, baseline_version=args.baseline, target_version=args.target,
            min_sample=args.min_sample,
            dimensions=tuple(args.dimensions) if args.dimensions else CUBE_DIMENSIONS,
        )
    if args.out:
        args.out.write_text(
            json.dumps(_sanitize(report), indent=2, sort_keys=True, default=str), encoding="utf-8",
        )
    if not args.quiet:
        print(render(report))
        if args.out:
            print(f"\nartifact: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
