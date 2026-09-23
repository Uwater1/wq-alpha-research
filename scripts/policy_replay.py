"""Offline research-policy benchmark (TODO P5).

Replays candidate-selection policies over settled history and scores the funnel they would
have produced, so a new policy can show measurable evidence *before* it spends real BRAIN
capacity. There are no BRAIN calls anywhere in this module.

Point-in-time contract
----------------------

A policy only ever sees :class:`CandidateCard` — facts recorded **before** a simulation runs
(expression structure, fields, operators, settings, lineage, coverage priors, validator
finding codes). Outcomes live in :class:`CandidateOutcome` and are reachable only through
:meth:`DecisionContext.recorded_outcome`, which returns something only when the outcome had
already settled *strictly before* the decision's clock. That single rule is what makes the
benchmark honest:

* a candidate created after the decision is invisible;
* a candidate that settled at or after the decision exposes no result, so no policy can look
  ahead — neither for choosing nor for the calibration it may consult;
* a candidate BRAIN had already finished before the decision costs **no** simulation slot,
  because the real queue would serve it from cache.

**The clock is ``events.id``, not a timestamp.** Wall-clock stamps are second-granularity in
this database, so a candidate queued and simulated in the same second would look settled at
the moment it was created and every decision would score as a free cache hit. Event ids come
from one monotonic sequence, so "strictly earlier" is decidable. ``settled_at`` is carried
alongside for reporting only; nothing is ever ordered by it.

``replay`` re-checks all three conditions afterwards and reports ``leakage_check``.

Decision rounds follow the generation waves: a round opens when a batch of candidates has
finished arriving, the policy picks from everything visible at that moment (it may spend the
whole remaining budget there), and the round's information is frozen — an outcome that lands
mid-round is only visible from the next wave on. A round the policy declines is simply over;
since nothing new is knowable inside it, declining costs nothing but the chance to spend.

CLI:
    ./.venv/bin/python scripts/policy_replay.py --list
    ./.venv/bin/python scripts/policy_replay.py --compare --budget 20
    ./.venv/bin/python scripts/policy_replay.py --run calibrated_rank --budget 20 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical
import finding_calibration
import research_db
import staged_search
import validate

REPLAY_VERSION = "policy-replay-v1"
#: The objective a perfect-oracle ranking would use. Versioned like a reward definition.
ORACLE_OBJECTIVE_VERSION = "is-corr-active-sharpe-v1"
#: Keys a card is allowed to expose; used by the leakage self-check.
VISIBLE_CARD_FIELDS = frozenset({
    "candidate_id", "creation_order", "created_at", "family", "dataset", "generation",
    "mutation_type", "skeleton_hash", "priority", "expected_quality", "novelty_score",
    "failure_risk", "fields", "operators", "categories", "operator_counts", "depth",
    "finding_codes", "expression_hash", "scope",
})
#: Metrics where a *lower* value is better, so a comparison can score deltas correctly.
LOWER_IS_BETTER = frozenset({
    "simulations_used", "simulations_to_first_is_pass", "wasted_variants", "wasted_share",
    "duplicate_simulations", "correlation_failure_rate", "turnover_failure_rate",
})
IS_PASS_STATUSES = frozenset({"IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"})
CORR_PASS_STATUSES = frozenset({"CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"})
#: Default simulation budget as a share of the corpus. Deliberately scarce: the whole point of
#: a selector is that capacity is limited, and a budget that covers the corpus makes every
#: policy score the same. ``--budget`` overrides it.
FLAT_BUDGET_FRACTION = 0.1


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _settings(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _structural(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    features = parsed.get("features") if isinstance(parsed, Mapping) else None
    return features if isinstance(features, Mapping) else {}


# ---------------------------------------------------------------------------
# Visible and hidden halves of a replayed candidate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateCard:
    """Everything a policy may know before the simulation runs."""

    candidate_id: int
    creation_order: int
    created_at: str
    family: str
    dataset: str | None
    generation: int
    mutation_type: str | None
    skeleton_hash: str | None
    priority: float
    expected_quality: float | None
    novelty_score: float | None
    failure_risk: float | None
    fields: tuple[str, ...]
    operators: tuple[str, ...]
    categories: Mapping[str, int]
    operator_counts: Mapping[str, int]
    depth: int
    finding_codes: tuple[str, ...]
    expression_hash: str
    scope: Mapping[str, Any]
    #: Kept for policies that need the expression itself (e.g. the surrogate featurizer).
    #: Deliberately excluded from :meth:`as_dict` so it can never reach a report.
    expression: str = dataclass_field(default="", repr=False)
    settings: Mapping[str, Any] = dataclass_field(default_factory=dict, repr=False)

    def as_dict(self) -> dict[str, Any]:
        """Report-safe projection: no expression, no settings, no metrics."""
        return {key: getattr(self, key) for key in sorted(VISIBLE_CARD_FIELDS)}


@dataclass(frozen=True)
class CandidateOutcome:
    """The hidden half: only reachable once the outcome had already settled."""

    settled_at: str | None
    bucket: str
    sharpe: float | None
    fitness: float | None
    turnover: float | None
    self_corr: float | None
    failure_reason: str
    simulation_status: str | None
    submission_status: str | None

    @property
    def is_pass(self) -> bool:
        return self.bucket in {"is_pass", "corr_pass", "active"}

    @property
    def corr_pass(self) -> bool:
        return self.bucket in {"corr_pass", "active"}

    @property
    def active(self) -> bool:
        return self.bucket == "active"

    @property
    def brain_rejected(self) -> bool:
        return self.bucket == "simulation_fail"

    @property
    def locally_rejected(self) -> bool:
        return self.bucket == "validation_reject"

    @property
    def slot_cost(self) -> int:
        """A locally rejected candidate never reached BRAIN, so it costs no capacity."""
        return 0 if self.locally_rejected else 1

    @property
    def objective(self) -> float:
        """Perfect-oracle score used for top-k recall (``ORACLE_OBJECTIVE_VERSION``)."""
        base = 300.0 if self.active else 200.0 if self.corr_pass else 100.0 if self.is_pass else 0.0
        return base + (self.sharpe or 0.0) + 0.5 * (self.fitness or 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {"bucket": self.bucket, "settled_at": self.settled_at, "sharpe": self.sharpe,
                "fitness": self.fitness, "turnover": self.turnover, "self_corr": self.self_corr}


@dataclass(frozen=True)
class ReplayItem:
    card: CandidateCard
    outcome: CandidateOutcome
    #: Monotonic ``events.id`` of the event that created this candidate.
    created_clock: int = 0
    #: Monotonic ``events.id`` of the terminal event, or ``None`` when unknown. Never
    #: approximated from a timestamp or a row id: an invented settlement clock is exactly
    #: how a replay would come to believe it had seen the future.
    settled_clock: int | None = None
    #: Clock at which this candidate's whole generation wave became visible (the last creation
    #: event of that wave). ``None`` means the candidate is its own wave.
    wave_clock: int | None = None

    @property
    def candidate_id(self) -> int:
        return self.card.candidate_id


@dataclass(frozen=True)
class Decision:
    step: int
    clock: int
    as_of: str
    candidate_id: int
    slot_cost: int
    cache_hit: bool
    considered: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"step": self.step, "clock": self.clock, "as_of": self.as_of,
                "candidate_id": self.candidate_id, "slot_cost": self.slot_cost,
                "cache_hit": self.cache_hit}


# ---------------------------------------------------------------------------
# Decision context
# ---------------------------------------------------------------------------


@dataclass
class DecisionContext:
    """Everything a policy may use at one decision point; nothing beyond ``clock``."""

    clock: int
    as_of: str
    step: int
    remaining_budget: int
    history: tuple[tuple[CandidateCard, CandidateOutcome], ...]
    settled_before: Mapping[int, tuple[CandidateCard, CandidateOutcome]]

    def recorded_outcome(self, candidate_id: int) -> CandidateOutcome | None:
        """An outcome only when it had already settled strictly before ``clock``."""
        found = self.settled_before.get(int(candidate_id))
        return None if found is None else found[1]

    def known_cards(self) -> tuple[CandidateCard, ...]:
        """Cards that historically existed at ``as_of``: decided here or settled before."""
        return tuple(card for card, _outcome in self.history) + tuple(
            card for card, _outcome in self.settled_before.values()
        )

    def family_attempts(self) -> dict[str, int]:
        return self._counts(lambda card: card.family)

    def dataset_attempts(self) -> dict[str, int]:
        return self._counts(lambda card: card.dataset or "unknown")

    def field_attempts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for card, _outcome in list(self.history) + list(self.settled_before.values()):
            for name in card.fields:
                counts[name] += 1
        return dict(counts)

    def skeleton_stats(self) -> dict[str, tuple[int, int]]:
        """``skeleton -> (attempts, passes)`` among known decisions."""
        attempts: dict[str, int] = defaultdict(int)
        passes: dict[str, int] = defaultdict(int)
        for card, outcome in list(self.history) + list(self.settled_before.values()):
            key = card.skeleton_hash or f"candidate:{card.candidate_id}"
            attempts[key] += 1
            if outcome.is_pass:
                passes[key] += 1
        return {key: (attempts[key], passes[key]) for key in attempts}

    def _counts(self, key: Callable[[CandidateCard], str]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for card, _outcome in list(self.history) + list(self.settled_before.values()):
            counts[key(card)] += 1
        return dict(counts)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


class Policy:
    """Base class: order the candidates the environment offers at this decision point."""

    name = "policy"
    version = "1"
    description = ""
    is_baseline = False

    def __init__(self, *, seed: int = 0, parameters: Mapping[str, Any] | None = None,
                 environment: "ReplayEnvironment | None" = None) -> None:
        self.seed = int(seed)
        self.parameters = dict(parameters or {})
        self.environment = environment

    def order(self, available: Sequence[CandidateCard], context: DecisionContext) -> Sequence[int]:
        """Rank the offered candidates, best first.

        Returning nothing (or only candidates that were not offered) means *decline*: no slot
        is spent at this step and the decision moves to the next one. Decline is what makes
        "skip this structure until it proves itself" expressible, so a policy that declines
        forever legitimately ends with unused budget.
        """
        raise NotImplementedError

    def first(self, available: Sequence[CandidateCard], context: DecisionContext) -> int | None:
        ranked = list(self.order(available, context))
        offered = {card.candidate_id for card in available}
        return next((int(candidate_id) for candidate_id in ranked if int(candidate_id) in offered), None)


class FifoPolicy(Policy):
    name = "fifo"
    description = "Creation order: the incumbent behaviour a new policy must beat."
    is_baseline = True

    def order(self, available, context):
        return [card.candidate_id for card in sorted(available, key=lambda card: card.creation_order)]


class RankingPolicy(Policy):
    name = "ranking"
    description = "The stored pre-simulation ranking components (priority, then quality priors)."
    is_baseline = True

    def order(self, available, context):
        return [
            card.candidate_id for card in sorted(
                available,
                key=lambda card: (
                    -float(card.priority or 0.0),
                    -float(card.expected_quality or 0.0),
                    float(card.failure_risk or 0.0),
                    card.creation_order,
                ),
            )
        ]


class StagedSearchPolicy(Policy):
    name = "staged_search"
    description = "Per-structure budget: one baseline for an unproven structure, variants only once it pays off."
    is_baseline = True

    def order(self, available, context):
        stats = context.skeleton_stats()

        def score(card: CandidateCard) -> tuple[int, int, int, int]:
            key = card.skeleton_hash or f"candidate:{card.candidate_id}"
            attempts, passes = stats.get(key, (0, 0))
            exhausted = int(attempts >= self.parameters.get("max_variants", staged_search.DEFAULT_MAX_VARIANTS))
            saturated = int(
                attempts >= self.parameters.get("min_attempts_to_stop", staged_search.DEFAULT_MIN_ATTEMPTS_TO_STOP)
                and (passes / attempts if attempts else 0.0) < self.parameters.get("min_pass_ratio", staged_search.DEFAULT_MIN_PASS_RATIO)
            )
            # untried structure first, then a proven structure with budget left, then the rest
            tier = 0 if attempts == 0 else (1 if passes > 0 and not exhausted else 2)
            return (saturated, exhausted, tier, card.creation_order)

        return [card.candidate_id for card in sorted(available, key=score)]


class SurrogatePolicy(Policy):
    name = "surrogate"
    description = "The advisory surrogate model's predicted IS-pass probability (falls back to FIFO when untrained)."
    is_baseline = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._model = None
        db = getattr(self.environment, "db", None)
        if db is not None:
            import surrogate

            self._model = surrogate.load(db)

    def order(self, available, context):
        if self._model is None:
            return [card.candidate_id for card in sorted(available, key=lambda card: card.creation_order)]
        import surrogate

        scored = []
        for card in available:
            prediction = surrogate.predict(self._model, _surrogate_row(card))
            scored.append((-float(prediction.get("is_pass") or 0.0), card.creation_order, card.candidate_id))
        scored.sort()
        return [candidate_id for _score, _order, candidate_id in scored]


class CoveragePolicy(Policy):
    name = "coverage"
    description = "P4-informed: prefer the least-attempted datasets, then fields, then FIFO."

    def order(self, available, context):
        datasets = context.dataset_attempts()
        fields = context.field_attempts()

        def score(card: CandidateCard) -> tuple[int, int, int]:
            dataset_attempts = datasets.get(card.dataset or "unknown", 0)
            field_attempts = sum(fields.get(name, 0) for name in card.fields)
            return (dataset_attempts, field_attempts, card.creation_order)

        return [card.candidate_id for card in sorted(available, key=score)]


class CalibratedPolicy(Policy):
    """Skip or demote candidates whose findings history says BRAIN refuses.

    The calibration is recomputed from observations that had already settled at ``as_of``,
    so acting on it cannot use the future. ``parameters['mode']`` is ``rank`` (demote) or
    ``skip`` (treat as locally rejected and never spend a slot).
    """

    name = "calibrated_rank"
    description = "Deprioritize candidates whose advisory findings predict a BRAIN refusal."
    #: ``rank`` demotes flagged work, ``skip`` declines it outright.
    default_mode = "rank"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.mode = str(self.parameters.get("mode") or self.default_mode)
        self.min_samples = int(self.parameters.get("min_samples", finding_calibration.DEFAULT_MIN_SAMPLES))
        self.strict_threshold = float(self.parameters.get("strict_threshold", finding_calibration.DEFAULT_STRICT_REJECT_RATE))
        self._cache: dict[str, set[str]] = {}

    def risky_codes(self, context: DecisionContext) -> set[str]:
        if context.clock not in self._cache:
            observations = getattr(self.environment, "observations", None) or []
            # Strictly-earlier outcomes only: the calibration snapshot must be one that
            # could have been computed at this decision point.
            calibration = finding_calibration.calibrate_from(
                observations, as_of_clock=context.clock, min_samples=self.min_samples,
                strict_threshold=self.strict_threshold,
            )
            self._cache[context.clock] = set(finding_calibration.recommended_policy(calibration))
        return self._cache[context.clock]

    def order(self, available, context):
        risky = self.risky_codes(context)

        def flagged(card: CandidateCard) -> int:
            return int(bool(set(card.finding_codes) & risky))

        if self.mode == "skip":
            # Decline rather than fall back: running a candidate history says BRAIN refuses is
            # exactly the behaviour the benchmark exists to price.
            usable = [card for card in available if not flagged(card)]
        else:
            usable = list(available)
        return [card.candidate_id for card in sorted(usable, key=lambda card: (flagged(card), card.creation_order))]


class CalibratedSkipPolicy(CalibratedPolicy):
    name = "calibrated_skip"
    description = "Skip the candidates whose advisory findings predict a BRAIN refusal."
    default_mode = "skip"


class FindingGatePolicy(Policy):
    """Enforce a *proposed* severity policy: decline what it would reject locally.

    The codes are passed in rather than measured, which is what makes this the counterfactual
    the approval step needs: "had this rule been in force, what would the funnel have been?"
    With no codes it declines nothing.
    """

    name = "finding_gate"
    description = "Decline every candidate the proposed finding policy would reject locally."

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.codes = frozenset(str(code) for code in (self.parameters.get("codes") or ()))

    def order(self, available, context):
        usable = [card for card in available if not (set(card.finding_codes) & self.codes)]
        return [card.candidate_id for card in sorted(usable, key=lambda card: card.creation_order)]


POLICY_CLASSES: tuple[type[Policy], ...] = (
    FifoPolicy, RankingPolicy, StagedSearchPolicy, SurrogatePolicy, CoveragePolicy,
    CalibratedPolicy, CalibratedSkipPolicy, FindingGatePolicy,
)
POLICIES: dict[str, type[Policy]] = {policy.name: policy for policy in POLICY_CLASSES}
BASELINE_POLICIES: tuple[str, ...] = tuple(policy.name for policy in POLICY_CLASSES if policy.is_baseline)


def build_policy(name: str, *, environment: "ReplayEnvironment | None" = None, seed: int = 0,
                 parameters: Mapping[str, Any] | None = None) -> Policy:
    if name not in POLICIES:
        raise ValueError(f"unknown policy {name!r}; known: {sorted(POLICIES)}")
    return POLICIES[name](seed=seed, parameters=parameters, environment=environment)


def _surrogate_row(card: CandidateCard) -> dict[str, Any]:
    """Rebuild the pre-simulation row the surrogate featurizer expects (no outcomes)."""
    return {
        "normalized_expression": card.expression,
        "settings_json": json.dumps(dict(card.settings), sort_keys=True),
        "signal_family": card.family,
        "generation": card.generation,
        "mutation_type": card.mutation_type,
        "priority": card.priority,
        "expected_quality": card.expected_quality,
        "novelty_score": card.novelty_score,
        "failure_risk": card.failure_risk,
        "structural_json": json.dumps({"features": {
            "categories": dict(card.categories), "operator_counts": dict(card.operator_counts),
        }}, sort_keys=True),
    }


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


class ReplayEnvironment:
    """The corpus plus the point-in-time view of it."""

    def __init__(
        self,
        items: Sequence[ReplayItem],
        *,
        budget: int,
        allow_cache_hits: bool = True,
        observations: Sequence[finding_calibration.FindingObservation] | None = None,
        db: research_db.ResearchDB | None = None,
        window: Mapping[str, Any] | None = None,
        training_cutoff: str | None = None,
    ) -> None:
        self.items = sorted(items, key=lambda item: (item.created_clock, item.card.candidate_id))
        self.budget = int(budget)
        self.allow_cache_hits = bool(allow_cache_hits)
        self.observations = list(observations or [])
        self.db = db
        self.window = dict(window or {})
        self.training_cutoff = training_cutoff
        #: Rows whose creation event was missing and whose clock fell back to the row id.
        self.clock_fallbacks = 0
        self._by_id = {item.candidate_id: item for item in self.items}

    @classmethod
    def from_db(
        cls,
        db: research_db.ResearchDB,
        *,
        budget: int | None = None,
        campaign_id: str | None = None,
        window: Mapping[str, Any] | None = None,
        scope: Mapping[str, Any] | None = None,
        allow_cache_hits: bool = True,
        settled_only: bool = False,
        training_cutoff: str | None = None,
    ) -> "ReplayEnvironment":
        """Build the corpus from the research pool (no BRAIN calls, no credentials).

        Unsettled candidates are included by default, because the question a policy benchmark
        answers is "what should the next slot be spent on?" — a corpus of only finished work
        would leave nothing to decide. Pass ``settled_only=True`` to benchmark against resolved
        history alone.

        Terminal history is read from the event log, not only from the ``simulations`` row: a
        candidate BRAIN refused stays refused even after a retry resets that row to QUEUED, and
        a replay must not be able to quietly forget a rejection.
        """
        where: list[str] = []
        if settled_only:
            where.append(
                "(s.status IN ('DONE','ERROR') OR c.failure_reason LIKE 'validation:%'"
                " OR (SELECT MIN(e.id) FROM events e WHERE e.entity='simulation'"
                "     AND CAST(e.entity_id AS INTEGER)=c.id AND e.event='result') IS NOT NULL)"
            )
        params: list[Any] = []
        if campaign_id:
            where.append("c.campaign_id=?")
            params.append(campaign_id)
        rows = db.query(
            "SELECT c.*, s.status AS simulation_status, s.completed_at AS simulation_completed_at, "
            " (SELECT MIN(e.id) FROM events e WHERE e.entity='candidate' "
            "  AND CAST(e.entity_id AS INTEGER)=c.id) AS created_event_id, "
            " (SELECT MIN(e.id) FROM events e WHERE e.entity='simulation' "
            "  AND CAST(e.entity_id AS INTEGER)=c.id AND e.event='result') AS settled_event_id, "
            " (SELECT e.to_status FROM events e WHERE e.entity='simulation' "
            "  AND CAST(e.entity_id AS INTEGER)=c.id AND e.event='result' ORDER BY e.id LIMIT 1)"
            "   AS terminal_event_status, "
            " (SELECT MIN(e.id) FROM events e WHERE e.entity='candidate' "
            "  AND CAST(e.entity_id AS INTEGER)=c.id AND e.event='validation_failed') AS validation_event_id "
            "FROM candidates c LEFT JOIN simulations s ON s.canonical_key = c.canonical_key "
            + (f"WHERE {' AND '.join(where)} " if where else "") +
            "ORDER BY c.id",
            params,
        )
        window = dict(window or {})
        scope_key = canonical.scope_hash(scope) if scope else None
        draft: list[ReplayItem] = []
        #: A generation wave is work that arrived together: one source (a generation run, a CSV
        #: batch) within one clock second. The wave's clock is its *last* creation event — the
        #: moment the batch finished arriving, which is when a decision could first see all of it.
        wave_of: dict[tuple[str, str], int] = {}
        wave_keys: list[tuple[str, str]] = []
        clock_fallbacks = 0
        for row in rows:
            created_at = str(row["created_at"] or "")
            if window.get("from") and created_at < str(window["from"]):
                continue
            if window.get("to") and created_at > str(window["to"]):
                continue
            settings = _settings(row["settings_json"])
            if scope_key is not None and canonical.scope_hash(canonical.scope_from_settings(settings)) != scope_key:
                continue
            created_clock = row["created_event_id"]
            if created_clock is None:
                # Rows predating the event log (legacy/imported candidates) have no creation
                # event. Their row id is still monotonically increasing, so it only ever makes
                # them look *later* than they were — never earlier, so it cannot leak.
                created_clock = int(row["id"])
                clock_fallbacks += 1
            created_clock = int(created_clock)
            terminal = row["settled_event_id"]
            if terminal is None and str(row.get("failure_reason") or "").startswith("validation:"):
                terminal = row["validation_event_id"]
            key = (str(row.get("source") or ""), created_at[:19])
            wave_of[key] = max(wave_of.get(key, created_clock), created_clock)
            wave_keys.append(key)
            draft.append(_item_from_row(
                row, settings, created_clock=created_clock,
                settled_clock=None if terminal is None else int(terminal),
            ))
        items = [
            ReplayItem(card=item.card, outcome=item.outcome, created_clock=item.created_clock,
                       settled_clock=item.settled_clock, wave_clock=wave_of[key])
            for item, key in zip(draft, wave_keys)
        ]
        resolved_budget = budget if budget is not None else max(1, int(len(items) * FLAT_BUDGET_FRACTION))
        if training_cutoff is None and items:
            # Default training window: everything before the evaluation corpus starts.
            training_cutoff = min(item.card.created_at for item in items)
        environment = cls(
            items, budget=resolved_budget, allow_cache_hits=allow_cache_hits,
            observations=finding_calibration.observations(db)[0] if items else [],
            db=db, window=window, training_cutoff=training_cutoff,
        )
        environment.clock_fallbacks = clock_fallbacks
        return environment

    # -- point-in-time views ----------------------------------------------

    def waves(self) -> list[int]:
        """The decision rounds, in clock order: one per generation wave."""
        return sorted({
            item.wave_clock if item.wave_clock is not None else item.created_clock
            for item in self.items
        })

    def item(self, candidate_id: int) -> ReplayItem | None:
        return self._by_id.get(int(candidate_id))

    def outcome_if_settled(self, candidate_id: int, clock: int) -> CandidateOutcome | None:
        """The outcome only if it had already settled strictly before ``clock``."""
        item = self.item(candidate_id)
        if item is None or item.settled_clock is None or item.settled_clock >= clock:
            return None
        return item.outcome

    def calibration(self, clock: int | None = None, **kwargs: Any) -> dict[str, Any]:
        return finding_calibration.calibrate_from(self.observations, as_of_clock=clock, **kwargs)


def _item_from_row(
    row: Mapping[str, Any],
    settings: Mapping[str, Any],
    *,
    created_clock: int,
    settled_clock: int | None,
) -> ReplayItem:
    expression = str(row.get("normalized_expression") or row.get("expression") or "")
    features = _structural(row.get("structural_json"))
    # Screened with a neutral policy: a card carries the findings the rules produce, while
    # which of them are *enforced* is a decision-time question the policy answers itself.
    report = validate.validate(expression, settings, severity_policy={})
    settled_at = str(row.get("simulation_completed_at") or row.get("updated_at") or "") or None
    card = CandidateCard(
        candidate_id=int(row["id"]),
        creation_order=int(created_clock),
        created_at=str(row.get("created_at") or ""),
        family=str(row.get("signal_family") or "unknown"),
        dataset=(features.get("datasets") or [None])[0],
        generation=int(row.get("generation") or 0),
        mutation_type=row.get("mutation_type"),
        skeleton_hash=row.get("skeleton_hash"),
        priority=float(row.get("priority") or 0.0),
        expected_quality=_as_float(row.get("expected_quality")),
        novelty_score=_as_float(row.get("novelty_score")),
        failure_risk=_as_float(row.get("failure_risk")),
        fields=tuple(features.get("fields") or canonical.fields_of(expression)),
        operators=tuple(features.get("operators") or canonical.operators_of(expression)),
        categories=dict(features.get("categories") or {}),
        operator_counts=dict(features.get("operator_counts") or {}),
        depth=int(features.get("depth") or canonical.expression_depth(expression)),
        finding_codes=tuple(report.finding_codes),
        expression_hash=str(row.get("expression_hash") or ""),
        scope=canonical.scope_from_settings(settings),
        expression=expression,
        settings=settings,
    )
    reason = " ".join(str(row.get(key) or "") for key in ("gate_reason", "failure_reason"))
    outcome = CandidateOutcome(
        settled_at=settled_at,
        bucket=_bucket_for(row, reason),
        sharpe=_as_float(row.get("sharpe")),
        fitness=_as_float(row.get("fitness")),
        turnover=_as_float(row.get("turnover")),
        self_corr=_as_float(row.get("self_corr")),
        failure_reason=reason.strip(),
        simulation_status=row.get("simulation_status"),
        submission_status=row.get("submission_status"),
    )
    return ReplayItem(card=card, outcome=outcome, created_clock=int(created_clock),
                      settled_clock=settled_clock)


def _bucket_for(row: Mapping[str, Any], reason: str) -> str:
    if str(row.get("failure_reason") or "").startswith("validation:"):
        return "validation_reject"
    status = str(row.get("simulation_status") or "")
    terminal = str(row.get("terminal_event_status") or "")
    if status not in ("DONE", "ERROR") and terminal in ("DONE", "ERROR"):
        # The row was reset by a retry; the first terminal result is still what BRAIN said.
        status = terminal
    if status == "ERROR":
        return "simulation_fail"
    candidate_status = str(row.get("status") or "")
    upper = reason.upper()
    if candidate_status == "ACTIVE":
        return "active"
    if str(row.get("submission_status") or "") == "SELF_CORR_FAIL" or "CORR" in upper:
        return "correlation_fail"
    if candidate_status in IS_PASS_STATUSES:
        return "corr_pass" if candidate_status in CORR_PASS_STATUSES else "is_pass"
    if candidate_status == "REJECTED":
        return "is_fail" if status == "DONE" else "simulation_fail"
    return "no_outcome"


# ---------------------------------------------------------------------------
# Replay and metrics
# ---------------------------------------------------------------------------


def replay(
    environment: ReplayEnvironment,
    policy: Policy,
    *,
    budget: int | None = None,
    record_details: bool = True,
) -> dict[str, Any]:
    """Run one policy over the corpus and score the funnel it would have produced.

    One **decision round per generation wave**. Inside a round the policy keeps picking from
    the pool it can see, and it may spend the whole remaining budget that way, but the round's
    information is frozen: outcomes that land while the round is running are not visible until
    the next wave. That is the honest reading of a batch of fresh work plus a slot budget, and
    it is what makes the policies differ — presenting one new candidate per step would force
    every policy to reproduce creation order.

    A round ends when the policy declines, when it has nothing left to pick, or when the budget
    is spent. Declining forfeits only the rest of the round, never the round itself: the next
    wave brings a fresh opportunity.
    """
    limit = environment.budget if budget is None else int(budget)
    ordered = environment.items
    decided: set[int] = set()
    decisions: list[Decision] = []
    spent = 0
    step = -1
    for clock in environment.waves():
        if spent >= limit:
            break
        pool = [item for item in ordered if item.created_clock <= clock]
        if not pool or all(item.candidate_id in decided for item in pool):
            continue
        # The only outcomes reachable in this round are ones that had already settled strictly
        # earlier. Computed once per round, so the round cannot learn from its own picks.
        settled_before = {
            item.candidate_id: (item.card, item.outcome)
            for item in ordered
            if item.settled_clock is not None and item.settled_clock < clock
        }
        as_of = max(item.card.created_at for item in pool)
        while spent < limit:
            available = [item.card for item in pool if item.candidate_id not in decided]
            if not available:
                break
            step += 1
            context = DecisionContext(
                clock=clock, as_of=as_of, step=step, remaining_budget=limit - spent,
                history=tuple(
                    (environment.item(entry.candidate_id).card,
                     environment.item(entry.candidate_id).outcome)
                    for entry in decisions
                ),
                settled_before=settled_before,
            )
            chosen = policy.first(available, context)
            if chosen is None:
                break  # declined: nothing in this round is worth a slot
            item = environment.item(chosen)
            if item is None or chosen in decided:
                break
            cache_hit = bool(
                environment.allow_cache_hits
                and item.settled_clock is not None
                and item.settled_clock < clock
            )
            slot_cost = 0 if cache_hit else item.outcome.slot_cost
            decided.add(chosen)
            decisions.append(Decision(
                step=step, clock=clock, as_of=as_of, candidate_id=chosen, slot_cost=slot_cost,
                cache_hit=cache_hit, considered=tuple(card.candidate_id for card in available),
            ))
            spent += slot_cost
    checks = verify_no_leakage(decisions, environment)
    report = {
        "policy": policy.name,
        "policy_version": policy.version,
        "policy_parameters": dict(policy.parameters),
        "replay_version": REPLAY_VERSION,
        "budget": limit,
        "clock": "events.id",
        "waves": len(environment.waves()),
        "leakage_check": checks,
        "metrics": metrics(decisions, environment, budget=limit),
    }
    if record_details:
        report["decisions"] = [decision.as_dict() for decision in decisions]
    return report


def metrics(decisions: Sequence[Decision], environment: ReplayEnvironment, *, budget: int) -> dict[str, Any]:
    simulated = [decision for decision in decisions if decision.slot_cost > 0 and not decision.cache_hit]
    simulated_local_rejects = [decision for decision in decisions if decision.slot_cost == 0 and not decision.cache_hit]
    cache_hits = [decision for decision in decisions if decision.cache_hit]
    outcomes = {decision.candidate_id: environment.item(decision.candidate_id).outcome for decision in decisions}

    def outcome_of(decision: Decision) -> CandidateOutcome:
        return outcomes[decision.candidate_id]

    simulated_outcomes = [outcome_of(decision) for decision in simulated]
    passes_simulated = sum(1 for outcome in simulated_outcomes if outcome.is_pass)
    corr_simulated = sum(1 for outcome in simulated_outcomes if outcome.corr_pass)
    active_simulated = sum(1 for outcome in simulated_outcomes if outcome.active)
    passes_all = sum(1 for outcome in outcomes.values() if outcome.is_pass)
    simulations_used = sum(decision.slot_cost for decision in decisions)

    first_pass = next(
        (index + 1 for index, decision in enumerate(decisions) if outcome_of(decision).is_pass), None
    )
    skeletons_seen: set[str] = set()
    duplicate_simulations = 0
    for decision in decisions:
        item = environment.item(decision.candidate_id)
        key = item.card.skeleton_hash or f"candidate:{item.candidate_id}"
        if decision.slot_cost > 0 and key in skeletons_seen:
            duplicate_simulations += 1
        skeletons_seen.add(key)

    k = max(1, int(budget))
    oracle = sorted(environment.items, key=lambda item: (-item.outcome.objective, item.card.creation_order))[:k]
    oracle_ids = {item.candidate_id for item in oracle}
    selected_ids = {decision.candidate_id for decision in decisions}
    wasted = sum(1 for outcome in simulated_outcomes if not outcome.is_pass)
    correlation_failures = sum(1 for outcome in simulated_outcomes if outcome.bucket in {"correlation_fail", "submission_fail"})
    turnover_failures = sum(1 for outcome in simulated_outcomes if "TURNOVER" in outcome.failure_reason.upper())
    adjusted = robustness_adjustment([
        {"sharpe": outcome.sharpe, "fitness": outcome.fitness,
         "turnover": outcome.turnover, "self_corr": outcome.self_corr,
         "skeleton_hash": environment.item(decision.candidate_id).card.skeleton_hash or decision.candidate_id}
        for decision, outcome in zip(simulated, simulated_outcomes)
    ])
    families = {environment.item(decision.candidate_id).card.family for decision in decisions}
    datasets = {environment.item(decision.candidate_id).card.dataset for decision in decisions}
    return {
        "decisions": len(decisions),
        "simulations_used": simulations_used,
        "cache_hit_decisions": len(cache_hits),
        "local_reject_decisions": len(simulated_local_rejects),
        "is_pass_total": passes_all,
        "is_pass_from_simulated": passes_simulated,
        "cache_hit_passes": sum(1 for outcome in (outcome_of(decision) for decision in cache_hits) if outcome.is_pass),
        "corr_pass_from_simulated": corr_simulated,
        "active_from_simulated": active_simulated,
        "simulations_to_first_is_pass": first_pass,
        "is_pass_per_simulation": round(passes_simulated / simulations_used, 6) if simulations_used else None,
        "corr_pass_per_simulation": round(corr_simulated / simulations_used, 6) if simulations_used else None,
        "active_per_simulation": round(active_simulated / simulations_used, 6) if simulations_used else None,
        "top_k": k,
        "top_k_success_recall": round(len(selected_ids & oracle_ids) / k, 6),
        "oracle_objective_version": ORACLE_OBJECTIVE_VERSION,
        "wasted_variants": wasted,
        "wasted_share": round(wasted / simulations_used, 6) if simulations_used else None,
        "duplicate_simulations": duplicate_simulations,
        "family_diversity": len(families),
        "dataset_diversity": len({dataset for dataset in datasets if dataset}),
        "niche_diversity": len(skeletons_seen),
        "correlation_failure_rate": round(correlation_failures / simulations_used, 6) if simulations_used else None,
        "turnover_failure_rate": round(turnover_failures / simulations_used, 6) if simulations_used else None,
        "robustness_adjusted_quality": adjusted.get("deflated_sharpe_proxy"),
        "robustness_adjustment": adjusted,
    }


def robustness_adjustment(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Discount the selected results for the size of the selection (advisory)."""
    import robustness

    return robustness.multiple_testing(rows)


def verify_no_leakage(decisions: Sequence[Decision], environment: ReplayEnvironment) -> dict[str, Any]:
    """Re-check the point-in-time contract after the fact; any failure is a bug."""
    problems: list[str] = []
    for decision in decisions:
        item = environment.item(decision.candidate_id)
        if decision.cache_hit and (item.settled_clock is None or item.settled_clock >= decision.clock):
            problems.append(f"cache hit on a candidate not settled before step {decision.step}")
        for candidate_id in decision.considered:
            other = environment.item(candidate_id)
            if other is None or other.created_clock > decision.clock:
                problems.append(f"step {decision.step} considered a candidate created after its clock")
        if decision.candidate_id not in decision.considered:
            problems.append(f"step {decision.step} chose a candidate it could not see")
    for item in environment.items:
        extra = set(item.card.as_dict()) - VISIBLE_CARD_FIELDS
        if extra:
            problems.append(f"card exposes non-visible fields: {sorted(extra)}")
    return {"status": "passed" if not problems else "failed", "problems": problems[:10]}


# ---------------------------------------------------------------------------
# Comparison and persistence
# ---------------------------------------------------------------------------


def compare(
    db: research_db.ResearchDB,
    policy_names: Sequence[str],
    *,
    budget: int | None = None,
    campaign_id: str | None = None,
    window: Mapping[str, Any] | None = None,
    scope: Mapping[str, Any] | None = None,
    seed: int = 0,
    allow_cache_hits: bool = True,
    settled_only: bool = False,
    training_cutoff: str | None = None,
    baseline: str = "fifo",
    persist: bool = True,
) -> dict[str, Any]:
    """Run several policies over one corpus and score each against the baseline."""
    environment = ReplayEnvironment.from_db(
        db, budget=budget, campaign_id=campaign_id, window=window, scope=scope,
        allow_cache_hits=allow_cache_hits, settled_only=settled_only,
        training_cutoff=training_cutoff,
    )
    # Microseconds, not seconds: two comparisons in the same second must not share an id.
    comparison_id = f"cmp-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{seed}"
    runs: dict[str, dict[str, Any]] = {}
    for name in policy_names:
        policy = build_policy(name, environment=environment, seed=seed)
        runs[name] = replay(environment, policy, budget=environment.budget)
    baseline_metrics = runs[baseline]["metrics"] if baseline in runs else {}
    result: dict[str, Any] = {
        "replay_version": REPLAY_VERSION,
        "comparison_id": comparison_id,
        "clock": "events.id",
        "corpus_size": len(environment.items),
        "settled_corpus_size": sum(1 for item in environment.items if item.settled_clock is not None),
        "clock_fallbacks": environment.clock_fallbacks,
        "simulation_budget": environment.budget,
        "seed": int(seed),
        "window": environment.window,
        "scope": dict(scope) if scope else None,
        "training_cutoff": environment.training_cutoff,
        "baseline": baseline if baseline in runs else None,
        "allow_cache_hits": bool(allow_cache_hits),
        "policies": {},
    }
    for name, run in runs.items():
        deltas = {
            metric: _delta(run["metrics"].get(metric), baseline_metrics.get(metric), metric)
            for metric in sorted(run["metrics"])
            if metric in baseline_metrics or metric in LOWER_IS_BETTER
        }
        result["policies"][name] = {**run, "delta_vs_baseline": deltas}
        if persist:
            db.record_policy_replay(
                policy_name=name,
                policy_version=str(run.get("policy_version") or "1"),
                parameters=run.get("policy_parameters"),
                training_cutoff=environment.training_cutoff,
                evaluation_window=environment.window,
                corpus_size=len(environment.items),
                simulation_budget=environment.budget,
                seed=seed,
                metrics={"metrics": run["metrics"], "delta_vs_baseline": deltas,
                         "leakage_check": run["leakage_check"]},
                comparison_id=comparison_id,
                is_baseline=name == baseline,
            )
    return result


def _delta(value: Any, base: Any, metric: str) -> dict[str, Any] | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not isinstance(base, (int, float)) or isinstance(base, bool):
        return None
    difference = float(value) - float(base)
    improved = difference < 0 if metric in LOWER_IS_BETTER else difference > 0
    return {"value": round(float(value), 6), "baseline": round(float(base), 6),
            "delta": round(difference, 6), "improved": bool(improved) if difference else None}


# ---------------------------------------------------------------------------
# Would a proposed screening policy have improved the funnel?
# ---------------------------------------------------------------------------

VERDICT_IMPROVED = "improved"
VERDICT_NO_GAIN = "no_gain"
VERDICT_REGRESSION = "regression"
VERDICT_MISSING_EVIDENCE = "insufficient_evidence"
VERDICT_LEAKAGE = "leakage_failed"
VERDICT_DECLINES_EVERYTHING = "declines_everything"
VERDICT_NOTHING = "nothing_to_enforce"

#: The only verdict that may be enforced without a human override.
ENFORCEABLE_VERDICTS = frozenset({VERDICT_IMPROVED})


def evaluate_severity_policy(
    db: research_db.ResearchDB,
    severity_policy: Mapping[str, str],
    *,
    budget: int | None = None,
    campaign_id: str | None = None,
    window: Mapping[str, Any] | None = None,
    scope: Mapping[str, Any] | None = None,
    baselines: Sequence[str] = ("fifo", "ranking"),
    environment: "ReplayEnvironment | None" = None,
    record_details: bool = False,
) -> dict[str, Any]:
    """Ask the corpus whether enforcing ``severity_policy`` would have improved the funnel.

    This is the counterfactual the approval step needs: the same corpus is replayed with the
    candidates the policy would reject declined, and the result is compared against the
    baselines on *passes* and *capacity spent*. Nothing about the decision uses an outcome — a
    finding is a pre-simulation fact and declining is a response to it — but the *verdict* is
    allowed to judge the counterfactual, which is exactly what a benchmark is for.

    Two asymmetries make this stricter than a rejection rate:

    * declining work that would have passed is a regression, however many refusals the rule
      predicted;
    * a rule that declines the whole corpus proves nothing, because there is no surviving work
      to compare. It is reported as such instead of being scored as a perfect funnel.
    """
    codes = {str(code) for code, severity in severity_policy.items()
             if str(severity) == validate.SEVERITY_ERROR}
    if not codes:
        return {
            "verdict": VERDICT_NOTHING,
            "codes": [],
            "enforceable": False,
            "reasons": ["the proposed policy marks no finding as an error"],
        }
    environment = environment or ReplayEnvironment.from_db(
        db, budget=budget, campaign_id=campaign_id, window=window, scope=scope,
    )
    if budget is None:
        # An approval is about the work the campaign actually did, so the counterfactual has to
        # reach all of it: with the scarce default budget the baseline would only exercise the
        # first generation wave and report "no evidence" for every rule that was tripped later.
        environment.budget = len(environment.items)
    # Baseline decision logs are needed to know which of their choices the gate would refuse.
    runs = {
        name: replay(environment, build_policy(name, environment=environment), record_details=True)
        for name in baselines
    }
    gate = replay(
        environment,
        build_policy("finding_gate", environment=environment, parameters={"codes": sorted(codes)}),
        record_details=record_details,
    )
    flagged = {
        item.candidate_id for item in environment.items
        if set(item.card.finding_codes) & codes
    }

    def decisions_of(run: Mapping[str, Any]) -> list[Any]:
        return list(run.get("decisions") or [])

    # Which of the baselines' choices the gate would have refused (only available with details).
    refused = sorted({
        entry["candidate_id"] for name, run in runs.items()
        for entry in decisions_of(run)
        if entry["candidate_id"] in flagged
    })
    evidence: dict[str, Any] = {
        "codes": sorted(codes),
        "corpus_size": len(environment.items),
        "budget": environment.budget,
        "flagged_candidates": len(flagged),
        "refused_decisions": refused,
        "baselines": {name: run["metrics"] for name, run in runs.items()},
        "gate": gate["metrics"],
        "leakage": {name: run["leakage_check"]["status"] for name, run in runs.items()} | {
            "finding_gate": gate["leakage_check"]["status"]
        },
    }
    if record_details:
        evidence["gate_decisions"] = gate.get("decisions")
    reasons: list[str] = []
    if any(status != "passed" for status in evidence["leakage"].values()):
        verdict = VERDICT_LEAKAGE
        reasons.append("a replay run failed its leakage check; no verdict can be trusted")
    else:
        gate_metrics = gate["metrics"]
        if not refused:
            verdict = VERDICT_MISSING_EVIDENCE
            reasons.append(
                "no baseline ever spent a slot on a candidate carrying these findings, so the "
                "corpus cannot say whether refusing them would have helped"
            )
        elif gate_metrics["simulations_used"] == 0:
            verdict = VERDICT_DECLINES_EVERYTHING
            reasons.append("the policy would decline every candidate the baselines chose")
        else:
            # A funnel is better if it delivers more, or the same for less. Losing a pass is
            # worse regardless of how much capacity it saved.
            worse = False
            better = False
            for name, run in runs.items():
                base = run["metrics"]
                gate_passes = gate_metrics["is_pass_from_simulated"]
                base_passes = base["is_pass_from_simulated"]
                gate_sims = gate_metrics["simulations_used"]
                base_sims = base["simulations_used"]
                if gate_passes < base_passes:
                    worse = True
                    reasons.append(
                        f"the gate would lose passes against {name} ({gate_passes} < {base_passes})"
                    )
                elif gate_passes > base_passes:
                    better = True
                    reasons.append(
                        f"the gate delivers {gate_passes - base_passes} more pass(es) than {name} "
                        f"for the same capacity"
                    )
                elif gate_sims < base_sims:
                    better = True
                    reasons.append(
                        f"the gate reaches the same {base_passes} pass(es) as {name} "
                        f"on {base_sims - gate_sims} fewer simulation(s)"
                    )
                elif gate_sims > base_sims:
                    worse = True
                    reasons.append(f"the gate spends more capacity than {name} without more passes")
                else:
                    reasons.append(f"identical funnel to {name}: no measurable gain")
            verdict = VERDICT_REGRESSION if worse else (VERDICT_IMPROVED if better else VERDICT_NO_GAIN)
    evidence["verdict"] = verdict
    evidence["reasons"] = reasons
    evidence["enforceable"] = verdict in ENFORCEABLE_VERDICTS
    return evidence


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline research-policy benchmark (no BRAIN calls)")
    parser.add_argument("--db")
    parser.add_argument("--list", action="store_true", help="show the policy registry")
    parser.add_argument("--run", action="append", default=[], help="policy to replay (repeatable)")
    parser.add_argument("--compare", action="store_true", help="replay every baseline plus the candidate policies")
    parser.add_argument("--budget", type=int, help="simulation slots the policy may spend")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--campaign")
    parser.add_argument("--scope", help="JSON scope filter")
    parser.add_argument("--window-from")
    parser.add_argument("--window-to")
    parser.add_argument("--training-cutoff", help="ISO timestamp; calibration may use only earlier outcomes")
    parser.add_argument("--no-cache-hits", action="store_true", help="charge a slot even for already-settled work")
    parser.add_argument("--settled-only", action="store_true",
                        help="benchmark against resolved history instead of the live pool")
    parser.add_argument("--baseline", default="fifo")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.list:
        print(json.dumps({
            name: {"version": policy.version, "baseline": policy.is_baseline,
                   "description": policy.description}
            for name, policy in sorted(POLICIES.items())
        }, indent=2, sort_keys=True))
        return 0
    scope = json.loads(args.scope) if args.scope else None
    window = {key: value for key, value in (("from", args.window_from), ("to", args.window_to)) if value}
    names = list(args.run)
    if args.compare or not names:
        names = [name for name in BASELINE_POLICIES] + [name for name in ("coverage", "calibrated_rank", "calibrated_skip") if name not in BASELINE_POLICIES]
    names = [name for name in dict.fromkeys(names)]
    with research_db.ResearchDB.open(args.db) as db:
        result = compare(
            db, names, budget=args.budget, campaign_id=args.campaign, window=window, scope=scope,
            seed=args.seed, allow_cache_hits=not args.no_cache_hits,
            settled_only=args.settled_only,
            training_cutoff=args.training_cutoff, baseline=args.baseline,
            persist=not args.no_persist,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
