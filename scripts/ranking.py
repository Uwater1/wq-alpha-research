"""Candidate ranking for the simulation scheduler.

priority = expected_quality + novelty + information_gain
             + family_diversity - duplicate_penalty - failure_risk
             + queue_priority

Every component is stored on the candidate row, not just the total, so the same
numbers can later train a surrogate model (Priority 2).

The heuristics are the ones `SKILL.md` already encodes as rules of thumb (fundamental
signals and group normalization are the strongest starting points, deep nesting and
parameter clones waste BRAIN capacity, a family that keeps failing is a bad bet). They
only reorder the queue: nothing here rejects a candidate or proves anything about its
quality, so a heuristic miss costs a slot, never a lost idea.

Usage:
    context = build_context(db)
    scores = {row["id"]: score_candidate(row, context) for row in db.list_queued()}
    best = max(scores, key=lambda cid: scores[cid].priority)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

import canonical
import surrogate

# Weights kept in one place so the surrogate work can tune them without touching the logic.
WEIGHTS: dict[str, float] = {
    "expected_quality": 1.0,
    "novelty": 1.0,
    "information_gain": 1.0,
    "family_diversity": 0.5,
    "duplicate_penalty": 1.0,
    "failure_risk": 1.0,
    "portfolio_diversification": 0.5,
}

#: Minimum observations before an empirical family pass rate outweighs the prior.
FAMILY_EVIDENCE_MIN = 5
#: Skeleton count at which a candidate counts as a fully-duplicated parameter variant.
DUPLICATE_SATURATION = 5

PASSING_STATUSES = frozenset({"IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"})
FAILING_STATUSES = frozenset({"REJECTED"})
OPEN_STATUSES = frozenset({"GENERATED", "VALIDATED", "QUEUED", "SIMULATING", "SIMULATED", "RETRY"})


@dataclass
class RankingContext:
    """Aggregates the scorer needs; built once per scheduler pass."""

    family_outcomes: dict[str, tuple[int, int]] = field(default_factory=dict)  # family -> (settled, passed)
    expression_attempts: dict[str, int] = field(default_factory=dict)          # expression_hash -> rows seen
    skeleton_counts: dict[str, int] = field(default_factory=dict)              # skeleton_hash -> rows seen
    family_queue_counts: dict[str, int] = field(default_factory=dict)          # family -> queued now
    family_active_counts: dict[str, int] = field(default_factory=dict)         # family -> ACTIVE alphas
    field_counts: dict[str, int] = field(default_factory=dict)                 # field -> rows using it
    queued_total: int = 0
    total_candidates: int = 0
    surrogate_model: dict[str, Any] | None = None


@dataclass
class Score:
    """One candidate's ranking components plus the resulting priority."""

    priority: float
    expected_quality: float
    novelty: float
    information_gain: float
    family_diversity: float
    duplicate_penalty: float
    failure_risk: float
    reasons: dict[str, Any] = field(default_factory=dict)

    def components(self) -> dict[str, float]:
        return {
            "expected_quality": self.expected_quality,
            "novelty": self.novelty,
            "information_gain": self.information_gain,
            "family_diversity": self.family_diversity,
            "duplicate_penalty": self.duplicate_penalty,
            "failure_risk": self.failure_risk,
        }


def build_context(db: Any) -> RankingContext:
    """Aggregate observed outcomes and structural counts from the store."""
    context = RankingContext()

    for row in db.query(
        """
        SELECT COALESCE(signal_family, '') AS family,
               SUM(CASE WHEN status IN ('IS_PASS','CORR_PASS','SUBMISSION_READY','SUBMITTING','ACTIVE') THEN 1 ELSE 0 END) AS passed,
               SUM(CASE WHEN status IN ('IS_PASS','CORR_PASS','SUBMISSION_READY','SUBMITTING','ACTIVE','REJECTED') THEN 1 ELSE 0 END) AS settled
        FROM candidates GROUP BY family
        """
    ):
        context.family_outcomes[str(row["family"])] = (int(row["settled"] or 0), int(row["passed"] or 0))

    for row in db.query("SELECT expression_hash, COUNT(*) AS n FROM candidates GROUP BY expression_hash"):
        context.expression_attempts[str(row["expression_hash"])] = int(row["n"])
    for row in db.query("SELECT skeleton_hash, COUNT(*) AS n FROM candidates WHERE skeleton_hash IS NOT NULL GROUP BY skeleton_hash"):
        context.skeleton_counts[str(row["skeleton_hash"])] = int(row["n"])
    for row in db.query("SELECT COALESCE(signal_family, '') AS family, COUNT(*) AS n FROM candidates WHERE status='QUEUED' GROUP BY family"):
        context.family_queue_counts[str(row["family"])] = int(row["n"])
    for row in db.query("SELECT COALESCE(signal_family, '') AS family, COUNT(*) AS n FROM candidates WHERE status='ACTIVE' GROUP BY family"):
        context.family_active_counts[str(row["family"])] = int(row["n"])

    for row in db.query("SELECT normalized_expression FROM candidates"):
        for data_field in canonical.fields_of(str(row["normalized_expression"])):
            context.field_counts[data_field] = context.field_counts.get(data_field, 0) + 1

    context.queued_total = int(db.query("SELECT COUNT(*) AS n FROM candidates WHERE status='QUEUED'")[0]["n"])
    context.total_candidates = int(db.query("SELECT COUNT(*) AS n FROM candidates")[0]["n"])
    context.surrogate_model = surrogate.load(db)
    return context


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def structural_prior(row: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    """Prior quality score from expression shape alone (SKILL.md rules of thumb)."""
    expression = str(row.get("normalized_expression") or "")
    operators = set(canonical.operators_of(expression))
    fields = canonical.fields_of(expression)
    depth = canonical.expression_depth(expression)

    score = 0.4
    reasons: dict[str, Any] = {"operators": sorted(operators), "fields": list(fields), "depth": depth}
    if any(op.startswith("group_") for op in operators):
        score += 0.10  # group normalization is the documented default baseline
        reasons["group_normalized"] = True
    if operators & {"ts_rank", "group_rank", "rank"}:
        score += 0.05
    if fields and all(field in {"close", "open", "high", "low", "volume", "returns", "vwap"} for field in fields):
        score -= 0.10  # pure price/volume technicals fail more often without decay or blends
        reasons["pure_technical"] = True
    if depth <= 4:
        score += 0.05
    elif depth > 7:
        score -= 0.15
        reasons["deep_nesting"] = True
    return _clamp(score), reasons


def score_candidate(row: Mapping[str, Any], context: RankingContext) -> Score:
    """Rank one candidate; higher priority means 'simulate this sooner'."""
    expression_hash = str(row.get("expression_hash") or "")
    skeleton_hash = str(row.get("skeleton_hash") or "")
    family = str(row.get("signal_family") or "")
    attempts = max(int(row.get("attempt_count") or 0), 0)

    prior, reasons = structural_prior(row)
    settled, passed = context.family_outcomes.get(family, (0, 0))
    if family and settled >= FAMILY_EVIDENCE_MIN:
        observed = passed / settled
        expected_quality = 0.5 * prior + 0.5 * observed
        reasons["family_pass_rate"] = round(observed, 3)
    else:
        expected_quality = prior
    if context.surrogate_model:
        prediction = surrogate.predict(context.surrogate_model, row).get("is_pass")
        if prediction is not None:
            expected_quality = 0.7 * expected_quality + 0.3 * max(0.0, min(1.0, prediction))
            reasons["surrogate_is_pass"] = round(prediction, 4)
    reasons["prior_quality"] = round(prior, 3)

    # The counts include this very candidate, so subtract it: we want prior attempts.
    seen = max(context.expression_attempts.get(expression_hash, 1) - 1, 0)
    novelty = 1.0 / (1.0 + seen)
    reasons["expression_seen"] = seen

    skeleton_seen = max(context.skeleton_counts.get(skeleton_hash, 1) - 1, 0)
    if seen == 0 and skeleton_seen == 0:
        information_gain, tier = 1.0, "new_structure"
    elif skeleton_seen == 0:
        information_gain, tier = 0.7, "new_structure_variant"
    elif seen == 0:
        information_gain, tier = 0.4, "skeleton_revisit"
    else:
        information_gain, tier = 0.15, "already_explored"
    reasons["information_tier"] = tier

    if family in context.family_active_counts:
        share = context.family_queue_counts.get(family, 0) / max(context.queued_total, 1)
        family_diversity = _clamp(1.0 - share)
    else:
        family_diversity = _clamp(1.0 - context.family_queue_counts.get(family, 0) / max(context.queued_total, 1) + 0.2)
        reasons["family_not_active"] = True

    duplicate_penalty = _clamp(skeleton_seen / DUPLICATE_SATURATION) if skeleton_seen else 0.0

    failure_rate = 0.0
    if settled:
        failure_rate = 1.0 - (passed / settled)
    attempt_risk = 1.0 - math.exp(-0.7 * attempts)  # 0 attempts -> 0, each retry counts
    risk = 0.5 * failure_rate + 0.35 * attempt_risk
    if reasons.get("deep_nesting"):
        risk += 0.15
    failure_risk = _clamp(risk)
    reasons["attempts"] = attempts

    priority = float(row.get("priority") or 0.0)
    priority += WEIGHTS["expected_quality"] * expected_quality
    priority += WEIGHTS["novelty"] * novelty
    priority += WEIGHTS["information_gain"] * information_gain
    priority += WEIGHTS["family_diversity"] * family_diversity
    priority -= WEIGHTS["duplicate_penalty"] * duplicate_penalty
    priority -= WEIGHTS["failure_risk"] * failure_risk

    return Score(
        priority=round(priority, 6),
        expected_quality=round(expected_quality, 6),
        novelty=round(novelty, 6),
        information_gain=round(information_gain, 6),
        family_diversity=round(family_diversity, 6),
        duplicate_penalty=round(duplicate_penalty, 6),
        failure_risk=round(failure_risk, 6),
        reasons=reasons,
    )


def submission_priority(row: Mapping[str, Any], context: RankingContext) -> Score:
    """Order the submission queue: quality + novelty + portfolio diversification.

    A family that already owns ACTIVE alphas scores lower, so the queue spreads across
    economic ideas instead of stacking near-clones of the same signal.
    """
    base = score_candidate(row, context)
    family = str(row.get("signal_family") or "")
    active_in_family = context.family_active_counts.get(family, 0)
    diversification = 1.0 / (1.0 + active_in_family)
    return replace(
        base,
        priority=round(base.priority + WEIGHTS["portfolio_diversification"] * diversification, 6),
        reasons={**base.reasons, "active_in_family": active_in_family,
                 "portfolio_diversification": round(diversification, 6)},
    )


def rank(candidates: list[Mapping[str, Any]], context: RankingContext) -> list[tuple[Mapping[str, Any], Score]]:
    """All candidates with their scores, best first (ties broken by id for determinism)."""
    scored = [(row, score_candidate(row, context)) for row in candidates]
    scored.sort(key=lambda item: (-item[1].priority, int(item[0]["id"])))
    return scored
