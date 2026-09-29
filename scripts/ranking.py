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
    # Submission-stage only: this term is added in `submission_priority`, never in the
    # simulation priority computed by `score_candidate` (P8 contract, see below).
    "portfolio_diversification": 0.5,
    # Generator V3 terms (P8). Small, bounded weights: novelty already dominates, and these
    # must never let a diversity bonus outrank expected quality.
    "grammar_novelty": 0.35,
    "semantic_novelty": 0.30,
    "archive_sparsity": 0.20,
    "exact_novelty": 0.0,
}

#: How much measured cell evidence may move ``expected_quality`` (P24.1). Deliberately a
#: minority share: this ranking only *reorders* the queue, and a cheap conditional prior must
#: never outvote the structural and family terms on its own.
CONTEXTUAL_QUALITY_WEIGHT = 0.35

#: Correlated novelty terms are weighted and then *capped*, never summed at full weight.
NOVELTY_WEIGHTS: dict[str, float] = {"exact": 1.0, "grammar": 0.6, "semantic": 0.6}
#: The maximum combined novelty a single candidate may contribute.
NOVELTY_CAP = 1.0

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
    grammar_counts: dict[str, int] = field(default_factory=dict)               # grammar_skeleton_hash -> rows seen
    semantic_counts: dict[str, int] = field(default_factory=dict)              # semantic_skeleton_hash -> rows seen
    motif_counts: dict[str, int] = field(default_factory=dict)                 # motif_id -> rows seen
    family_queue_counts: dict[str, int] = field(default_factory=dict)          # family -> queued now
    family_active_counts: dict[str, int] = field(default_factory=dict)         # family -> ACTIVE alphas
    field_counts: dict[str, int] = field(default_factory=dict)                 # field -> rows using it
    #: Real archive niche occupancy (P8): grammar skeleton hash -> archive member count.
    archive_occupancy: dict[str, int] = field(default_factory=dict)
    queued_total: int = 0
    total_candidates: int = 0
    surrogate_model: dict[str, Any] | None = None
    #: Point-in-time conditional quality prior over the ledger (P21/P24.1). ``None`` keeps the
    #: previous hand-written heuristic; a prior that answers only from the global level is
    #: treated as "no cell evidence" and changes nothing.
    conditional_prior: Any = None


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
    # -- Generator V3 components (P8); default to 0 so older callers keep working --
    exact_novelty: float = 0.0
    grammar_novelty: float = 0.0
    semantic_novelty: float = 0.0
    #: Real archive-cell occupancy of the candidate's grammar niche (P8), not a frequency proxy.
    archive_sparsity: float = 0.0
    #: Submission-stage only (P8 contract): computed for every candidate but applied to
    #: priority only by :func:`submission_priority`.
    portfolio_diversification: float = 0.0
    combined_novelty: float = 0.0
    reasons: dict[str, Any] = field(default_factory=dict)

    def components(self) -> dict[str, float]:
        return {
            "expected_quality": self.expected_quality,
            "novelty": self.novelty,
            "information_gain": self.information_gain,
            "family_diversity": self.family_diversity,
            "duplicate_penalty": self.duplicate_penalty,
            "failure_risk": self.failure_risk,
            "exact_novelty": self.exact_novelty,
            "grammar_novelty": self.grammar_novelty,
            "semantic_novelty": self.semantic_novelty,
            "archive_sparsity": self.archive_sparsity,
            "portfolio_diversification": self.portfolio_diversification,
            "combined_novelty": self.combined_novelty,
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
    for column, target in (("grammar_skeleton_hash", context.grammar_counts),
                           ("semantic_skeleton_hash", context.semantic_counts),
                           ("motif_id", context.motif_counts)):
        for row in db.query(
            f"SELECT {column} AS key, COUNT(*) AS n FROM candidates WHERE {column} IS NOT NULL GROUP BY {column}"
        ):
            target[str(row["key"])] = int(row["n"])
    for row in db.query("SELECT COALESCE(signal_family, '') AS family, COUNT(*) AS n FROM candidates WHERE status='QUEUED' GROUP BY family"):
        context.family_queue_counts[str(row["family"])] = int(row["n"])
    for row in db.query("SELECT COALESCE(signal_family, '') AS family, COUNT(*) AS n FROM candidates WHERE status='ACTIVE' GROUP BY family"):
        context.family_active_counts[str(row["family"])] = int(row["n"])

    for row in db.query("SELECT normalized_expression FROM candidates"):
        for data_field in canonical.fields_of(str(row["normalized_expression"])):
            context.field_counts[data_field] = context.field_counts.get(data_field, 0) + 1
    try:
        import diversity

        context.archive_occupancy = diversity.archive_occupancy(db)
    except Exception:  # pragma: no cover - advisory only
        context.archive_occupancy = {}

    context.queued_total = int(db.query("SELECT COUNT(*) AS n FROM candidates WHERE status='QUEUED'")[0]["n"])
    context.total_candidates = int(db.query("SELECT COUNT(*) AS n FROM candidates")[0]["n"])
    context.surrogate_model = surrogate.load(db)
    try:
        # The cheapest useful model of "where do alphas pass": the ledger's own conditional
        # outcome counts. Advisory only, and built once per scheduler pass (P24.1).
        import quality_prior

        context.conditional_prior = quality_prior.QualityPrior.build(db)
    except Exception:  # pragma: no cover - ranking must survive an unbuildable prior
        context.conditional_prior = None
    return context


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _derived_grammar_hash(row: Mapping[str, Any]) -> str:
    """Grammar skeleton hash for rows written before the V3 columns existed."""
    expression = str(row.get("normalized_expression") or "")
    if not expression:
        return ""
    try:
        import diversity
        import expression_grammar

        return expression_grammar.grammar_skeleton_hash(expression, diversity.load_field_metadata())
    except Exception:  # pragma: no cover - advisory only
        return ""


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


def _contextual_quality(
    row: Mapping[str, Any],
    context: RankingContext,
    reasons: dict[str, Any],
) -> float | None:
    """Measured pass rate of this candidate's own cell, or ``None`` when there is none (P21/P24.1).

    The hierarchical prior already decides how specific an answer is allowed to be, so this
    only refuses the *global* level: "alphas pass at 19% overall" is not a reason to prefer one
    queued candidate over another, and using it here would silently re-scale the whole queue.
    Every level that answered is recorded, so a promotion from a thin cell to a broader one is
    auditable rather than invisible.
    """
    prior = context.conditional_prior
    if prior is None or not getattr(prior, "tables", None):
        return None
    try:
        import quality_prior

        look = prior.lookup(quality_prior.context_from_row(row))
    except Exception:  # pragma: no cover - advisory only
        return None
    if look.backed_off or look.level == "global":
        return None
    reasons["conditional_level"] = look.level
    reasons["conditional_simulations"] = look.simulations
    reasons["conditional_mean"] = round(look.mean, 4)
    reasons["conditional_specific_simulations"] = look.specific.simulations
    return max(0.0, min(1.0, float(look.mean)))


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
    contextual = _contextual_quality(row, context, reasons)
    if contextual is not None:
        expected_quality = (
            (1.0 - CONTEXTUAL_QUALITY_WEIGHT) * expected_quality
            + CONTEXTUAL_QUALITY_WEIGHT * contextual
        )
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
    grammar_hash = str(row.get("grammar_skeleton_hash") or "")
    semantic_hash = str(row.get("semantic_skeleton_hash") or "")
    grammar_seen = max(context.grammar_counts.get(grammar_hash, 1) - 1, 0) if grammar_hash else 0
    semantic_seen = max(context.semantic_counts.get(semantic_hash, 1) - 1, 0) if semantic_hash else 0
    grammar_novelty = 1.0 / (1.0 + grammar_seen)
    semantic_novelty = 1.0 / (1.0 + semantic_seen)
    # Archive sparsity (P8): real niche occupancy from archive cells, deliberately separate
    # from grammar frequency — a topology tried twice but never settled is still sparse, and
    # a topology with one row but ten archived members is crowded.
    occupancy_key = grammar_hash or _derived_grammar_hash(row)
    archive_sparsity = 1.0 / (1.0 + context.archive_occupancy.get(occupancy_key, 0))
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

    active_in_family = context.family_active_counts.get(family, 0)
    portfolio_diversification = 1.0 / (1.0 + active_in_family)

    # Correlated novelty terms are weighted, averaged, then capped: three views of "is this
    # new?" must not add up to three times the evidence.
    novelty_total = (
        NOVELTY_WEIGHTS["exact"] * novelty
        + NOVELTY_WEIGHTS["grammar"] * grammar_novelty
        + NOVELTY_WEIGHTS["semantic"] * semantic_novelty
    ) / sum(NOVELTY_WEIGHTS.values())
    combined_novelty = _clamp(min(NOVELTY_CAP, novelty_total))
    reasons.update({
        "grammar_seen": grammar_seen, "semantic_seen": semantic_seen,
        "grammar_novelty": round(grammar_novelty, 4), "semantic_novelty": round(semantic_novelty, 4),
        "archive_sparsity": round(archive_sparsity, 4),
        "portfolio_diversification": round(portfolio_diversification, 4),
        "combined_novelty": round(combined_novelty, 4),
    })

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
    priority += WEIGHTS["novelty"] * combined_novelty
    priority += WEIGHTS["information_gain"] * information_gain
    priority += WEIGHTS["family_diversity"] * family_diversity
    priority -= WEIGHTS["duplicate_penalty"] * duplicate_penalty
    priority -= WEIGHTS["failure_risk"] * failure_risk
    priority += WEIGHTS["grammar_novelty"] * grammar_novelty
    priority += WEIGHTS["semantic_novelty"] * semantic_novelty
    priority += WEIGHTS["archive_sparsity"] * archive_sparsity

    return Score(
        priority=round(priority, 6),
        expected_quality=round(expected_quality, 6),
        novelty=round(novelty, 6),
        information_gain=round(information_gain, 6),
        family_diversity=round(family_diversity, 6),
        duplicate_penalty=round(duplicate_penalty, 6),
        failure_risk=round(failure_risk, 6),
        exact_novelty=round(novelty, 6),
        grammar_novelty=round(grammar_novelty, 6),
        semantic_novelty=round(semantic_novelty, 6),
        archive_sparsity=round(archive_sparsity, 6),
        portfolio_diversification=round(portfolio_diversification, 6),
        combined_novelty=round(combined_novelty, 6),
        reasons=reasons,
    )


def submission_priority(row: Mapping[str, Any], context: RankingContext) -> Score:
    """Order the submission queue: quality + novelty + portfolio diversification.

    A family that already owns ACTIVE alphas scores lower, so the queue spreads across
    economic ideas instead of stacking near-clones of the same signal. This is the *only*
    stage where ``portfolio_diversification`` materially affects priority (P8 contract):
    simulation scheduling uses :func:`score_candidate`, whose priority excludes the term.
    """
    base = score_candidate(row, context)
    family = str(row.get("signal_family") or "")
    active_in_family = context.family_active_counts.get(family, 0)
    diversification = 1.0 / (1.0 + active_in_family)
    return replace(
        base,
        priority=round(base.priority + WEIGHTS["portfolio_diversification"] * diversification, 6),
        portfolio_diversification=round(diversification, 6),
        reasons={**base.reasons, "active_in_family": active_in_family,
                 "portfolio_diversification": round(diversification, 6)},
    )


def rank(candidates: list[Mapping[str, Any]], context: RankingContext) -> list[tuple[Mapping[str, Any], Score]]:
    """All candidates with their scores, best first (ties broken by id for determinism)."""
    scored = [(row, score_candidate(row, context)) for row in candidates]
    scored.sort(key=lambda item: (-item[1].priority, int(item[0]["id"])))
    return scored
