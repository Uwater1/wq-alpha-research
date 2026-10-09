"""Generation policy: campaign planning and independent recipe sampling (P3/P4/P5).

The policy layer decides *what to search* before anything is generated. It turns a campaign
budget into an explicit, deterministic list of :class:`PlanSlot` items that consume the
quality-diversity archive:

* family budgets come from :func:`archive.allocate_families` (exploration reserve included);
* mutation/crossover parents come from :func:`archive.parents` (diverse niches, not one
  top-Sharpe lineage);
* each slot records its generation mode, family, motif, chosen sources, ``recipe_index`` and
  a human-readable reason.

Recipes are sampled per *proposal* from :func:`recipe_seed`, so lookback/window/decay/
neutralization/truncation are no longer phase-locked and adding an unrelated proposal cannot
perturb an existing one. The policy never queues anything: materialization and the queue
boundary live in ``scripts/generator.py``.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
import sys
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import archive  # noqa: E402
import diversity  # noqa: E402
import expression_grammar as grammar  # noqa: E402
import quality_prior  # noqa: E402

GENERATION_POLICY_VERSION = "generation-policy-v1"
#: ``meta`` key holding the last archive-refresh failure (P16). Readable without the DB logs,
#: so a campaign can never look archive-informed while its derived state is stale.
ARCHIVE_REFRESH_FAILURE_KEY = "archive_refresh_failed"
#: SQLite messages that genuinely mean "this store has no derived archive yet". Anything else
#: (schema drift, locking, corruption) is a real failure and must fail fast (P16).
ARCHIVE_UNAVAILABLE_MARKERS = (
    "no such table: archive_cells",
    "no such table: candidates",
    "no such table: research_trials",
)


class ArchiveRefreshError(RuntimeError):
    """The derived archive could not be rebuilt for a reason that is not merely "absent".

    Planning against a silently stale archive produces a normal-looking, archive-informed
    plan from state that no longer reflects reality (P16), so this is raised instead of
    being swallowed by the caller.
    """

    def __init__(self, message: str, *, diagnostic: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostic: dict[str, Any] = dict(diagnostic or {})

#: Initial V3 defaults (P17). Configurable, and later learnable.
STRATEGY_WEIGHTS: dict[str, float] = {
    "explore": 0.40,
    "exploit": 0.25,
    "mutate": 0.25,
    "crossover": 0.10,
}

#: Requested strategy label -> concrete generation mode.
STRATEGY_ALIASES: dict[str, str] = {
    "coverage": "explore",
    "explore": "explore",
    "exploit": "exploit",
    "mutate": "mutate",
    "crossover": "crossover",
    "mixed": "mixed",
}

GENERATION_MODES = ("explore", "exploit", "mutate", "crossover")

#: Recipe dimension grids. Kept here so the search space is explicit and testable.
LOOKBACKS = (20, 60, 126, 252)
SMOOTHING_WINDOWS = (5, 10, 22)
DECAYS = (4, 6, 10, 20)
NEUTRALIZATIONS = ("SUBINDUSTRY", "INDUSTRY", "SECTOR")
GROUP_LEVELS = ("subindustry", "industry", "sector")
TRUNCATIONS = (0.05, 0.08, 0.1, 0.15)
NORMALIZATIONS = ("rank", "zscore")

#: Ladder rungs a warm-started campaign can budget (P19.3). Re-exported from the seed bank
#: vocabulary so the policy never invents a band the ladder cannot measure.
WARM_START_BANDS = ("D0", "D1", "D2", "D3", "D4")

#: Concrete V3 structural mutation operations (P4.4). The planning vocabulary for adaptive
#: mutation allocation (P9.2); the typed edits that realize them live in the generator.
V3_MUTATION_OPERATIONS = (
    "dataset_swap", "motif_change", "normalization_change", "group_change",
    "subtree_replace", "add_component",
)

DEFAULT_MAX_FAMILY_SHARE = 0.45
DEFAULT_EXPLORATION_RESERVE = 0.20
#: Bounded adaptive motif allocation (P9): under-tested motifs keep a floor of the budget and
#: no motif may take more than this share, however well it has performed.
DEFAULT_MOTIF_EXPLORATION_FLOOR = 0.25
DEFAULT_MOTIF_MAX_SHARE = 0.40


@dataclass(frozen=True)
class PlanSlot:
    """One planned research slot. Materialization turns this into an AST + settings."""

    slot: int
    generation_mode: str
    family: str
    motif_id: str
    recipe_index: int
    reason: str
    fields: tuple[str, ...] = ()
    datasets: tuple[str, ...] = ()
    parent_ids: tuple[int, ...] = ()
    target_family: str = ""
    #: Concrete structural edit pinned by the adaptive mutation allocation (P9.2).
    mutation_operation: str = ""
    #: Exact planned skeleton hashes (P4.2) for motif-materialized slots: the structure the
    #: materializer must reproduce. Empty for mutation/crossover slots, whose child structure
    #: is an edit of a parent rather than a planned motif.
    planned_grammar_hash: str = ""
    planned_semantic_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "generation_mode": self.generation_mode,
            "family": self.family,
            "motif_id": self.motif_id,
            "recipe_index": self.recipe_index,
            "reason": self.reason,
            "fields": list(self.fields),
            "datasets": list(self.datasets),
            "parent_ids": list(self.parent_ids),
            "target_family": self.target_family,
            "mutation_operation": self.mutation_operation,
            "planned_grammar_hash": self.planned_grammar_hash,
            "planned_semantic_hash": self.planned_semantic_hash,
        }


@dataclass
class Plan:
    """A deterministic campaign plan plus the policy/version provenance that produced it."""

    campaign_id: str
    mode: str
    budget: int
    seed: int
    slots: tuple[PlanSlot, ...]
    weights: Mapping[str, float]
    family_allocation: tuple[Mapping[str, Any], ...] = ()
    generator_version: str = ""
    policy_version: str = GENERATION_POLICY_VERSION
    grammar_version: str = grammar.GRAMMAR_VERSION
    motif_registry_version: str = grammar.MOTIF_REGISTRY_VERSION
    max_family_share: float = DEFAULT_MAX_FAMILY_SHARE
    motif_allocation: Mapping[str, int] = dataclass_field(default_factory=dict)
    #: Bounded adaptive mutation allocation across concrete edits (P9.2).
    mutation_allocation: Mapping[str, int] = dataclass_field(default_factory=dict)
    #: Whether the derived archive actually rebuilt before this plan was built (P16).
    archive_refresh: bool = False
    #: The forced motif the whole plan was built for, when one was pinned (P16).
    forced_motif: str = ""
    #: Bounded conditional motif allocation with the evidence behind each share (P21.1).
    quality_allocation: Mapping[str, Any] = dataclass_field(default_factory=dict)
    #: Quality-prior identity used for this plan, empty when the plan was unconditioned.
    prior_version: str = ""
    #: Proven recipe value counts (dimension -> value -> count) the plan's recipes are sampled
    #: from (P21.3). Empty means the uniform local grid.
    recipe_prior: Mapping[str, Mapping[str, int]] = dataclass_field(default_factory=dict)
    #: How the measured emitted-shape preference was spent (P21.1/P20.2): the bounded seat
    #: budget, the seats it actually claimed, the earning bar, and the evidence itself.
    operator_preference: Mapping[str, Any] = dataclass_field(default_factory=dict)
    #: Mode weights before and after quality conditioning (P22.3).
    mode_weights: Mapping[str, float] = dataclass_field(default_factory=dict)
    #: Measured parent-quality retention per mutation operation (P22.1).
    mutation_quality: Mapping[str, Any] = dataclass_field(default_factory=dict)

    @property
    def planned_budget(self) -> int:
        return len(self.slots)

    def distribution(self, dimension: str) -> dict[str, int]:
        """Count slots by one dimension (``generation_mode``, ``family``, ``motif_id``, ...)."""
        counts: dict[str, int] = {}
        for slot in self.slots:
            if dimension in {"field", "dataset"}:
                values = slot.fields if dimension == "field" else slot.datasets
                for value in values or ["unknown"]:
                    counts[value] = counts.get(value, 0) + 1
                continue
            value = getattr(slot, dimension, None)
            counts[str(value)] = counts.get(str(value), 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "mode": self.mode,
            "budget": self.budget,
            "planned_budget": self.planned_budget,
            "seed": self.seed,
            "weights": dict(self.weights),
            "family_allocation": [dict(row) for row in self.family_allocation],
            "generator_version": self.generator_version,
            "policy_version": self.policy_version,
            "grammar_version": self.grammar_version,
            "motif_registry_version": self.motif_registry_version,
            "max_family_share": self.max_family_share,
            "motif_allocation": dict(self.motif_allocation),
            "mutation_allocation": dict(self.mutation_allocation),
            "archive_refresh": bool(self.archive_refresh),
            "forced_motif": self.forced_motif,
            "quality_allocation": dict(self.quality_allocation),
            "prior_version": self.prior_version,
            "mode_weights": dict(self.mode_weights),
            "recipe_prior": {str(dimension): dict(counts)
                             for dimension, counts in self.recipe_prior.items()},
            "recipe_prior_dimensions": sorted(str(name) for name in self.recipe_prior),
            "operator_preference": dict(self.operator_preference),
            "mutation_quality": dict(self.mutation_quality),
            "slots": [slot.as_dict() for slot in self.slots],
        }


# ---------------------------------------------------------------------------
# Deterministic per-proposal RNG (P3.1)
# ---------------------------------------------------------------------------


def recipe_seed(
    campaign_id: str,
    global_seed: int,
    field_ids: Sequence[str],
    motif_id: str,
    recipe_index: int,
    parent_ids: Sequence[int] = (),
) -> int:
    """``SHA256(campaign + seed + fields + motif + recipe_index + parents)`` as an int.

    Local to one proposal: generating a different proposal elsewhere changes none of these
    inputs, so existing outputs cannot move.
    """
    payload = "|".join(
        [
            str(campaign_id),
            str(int(global_seed)),
            ",".join(str(value) for value in field_ids),
            str(motif_id),
            str(int(recipe_index)),
            ",".join(str(int(value)) for value in parent_ids),
        ]
    )
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16], 16)


#: Share of recipe draws a *learned* dimension still spends on values the proven bank has never
#: shown. Strong evidence conditions sampling; it never closes the grid, so a regime change
#: stays detectable (P21.3).
RECIPE_EXPLORATION_FLOOR = 0.25


def _coerce_value(value: str, exemplar: Any) -> Any:
    """Best-effort type match for a value learned from the ledger (which stores strings)."""
    if isinstance(exemplar, bool):
        return str(value).strip().lower() in {"true", "1", "yes"}
    try:
        if isinstance(exemplar, float):
            return float(value)
        if isinstance(exemplar, int):
            return int(float(value))
    except (TypeError, ValueError):
        return value
    return value


def _inside_grid_range(value: Any, originals: Sequence[Any]) -> bool:
    """Whether a learned numeric value lies inside the local grid's own range.

    The ledger may extend the grid — that is how ``0.08`` becomes reachable — but only *between*
    the designed endpoints. A value outside them is evidence about a setting the local search
    space deliberately does not contain, and admitting it would spend capacity outside the
    space the validator and the complexity budget were written for.
    """
    numbers = [float(item) for item in originals if isinstance(item, (int, float))
               and not isinstance(item, bool)]
    if not numbers or not isinstance(value, (int, float)) or isinstance(value, bool):
        return True
    return min(numbers) <= float(value) <= max(numbers)


def recipe_value_weights(
    values: Sequence[Any],
    counts: Mapping[Any, int] | None = None,
    *,
    exploration_floor: float = RECIPE_EXPLORATION_FLOOR,
) -> tuple[list[Any], list[float]]:
    """``(values, weights)`` for one recipe dimension conditioned on proven evidence (P21.3).

    Every value the proven bank has actually shown is added to the grid even when the local grid
    cannot express it — V3's truncation grid had no ``0.08`` while 120 of 139 gate-reaching
    seeds used exactly that — as long as it stays inside the grid's own range
    (:func:`_inside_grid_range`). Observed values share ``1 - floor`` of the mass in proportion
    to how often the platform accepted them; ``floor`` is spread over the values never observed.
    """
    grid = list(dict.fromkeys(values))
    originals = list(grid)
    exemplar = originals[0] if originals else None
    observed: dict[str, int] = {str(value): 0 for value in grid}
    for key, count in (counts or {}).items():
        key = str(key)
        if key not in observed:
            candidate = _coerce_value(key, exemplar)
            if not _inside_grid_range(candidate, originals):
                continue  # the evidence is real, the setting is outside our search space
            grid.append(candidate)
            observed[key] = 0
        observed[key] = observed[key] + max(0, int(count))
    total = sum(observed.values())
    if not grid or total <= 0:
        return grid, [1.0 / len(grid)] * len(grid) if grid else []
    floor = max(0.0, min(1.0, float(exploration_floor)))
    unobserved = sum(1 for value in grid if not observed[str(value)])
    weights: list[float] = []
    for value in grid:
        count = observed[str(value)]
        if count:
            weights.append((1.0 - floor) * count / total)
        else:
            weights.append(floor / unobserved if unobserved else 0.0)
    grand = sum(weights) or 1.0
    return grid, [weight / grand for weight in weights]


def recipe_choice(
    rng: random.Random,
    values: Sequence[Any],
    counts: Mapping[Any, int] | None,
    *,
    exploration_floor: float = RECIPE_EXPLORATION_FLOOR,
) -> Any:
    """One conditioned recipe draw.

    Without evidence this is exactly ``rng.choice(values)`` — same value, same RNG
    consumption — so an unconditioned campaign's recipes do not move (P3.1).
    """
    if not counts or not any(int(count) for count in counts.values()):
        return rng.choice(list(values))
    grid, weights = recipe_value_weights(values, counts, exploration_floor=exploration_floor)
    return rng.choices(grid, weights=weights, k=1)[0]


def sample_recipe(
    rng: random.Random,
    motif_id: str,
    *,
    truncate: float | None = None,
    prior: Mapping[str, Mapping[str, int]] | None = None,
) -> grammar.Recipe:
    """Sample every recipe dimension for one proposal.

    ``prior`` maps a recipe dimension to proven value counts (see
    :func:`seed_bank.proven_recipe_counts`). Dimensions with evidence are drawn from the
    conditioned grid with an exploration floor; dimensions without it are drawn uniformly over
    the local grid exactly as before (P21.3).
    """
    prior = prior or {}
    return grammar.Recipe(
        lookback=recipe_choice(rng, LOOKBACKS, prior.get("lookback")),
        smoothing_window=rng.choice(SMOOTHING_WINDOWS),
        decay=recipe_choice(rng, DECAYS, prior.get("decay")),
        neutralization=recipe_choice(rng, NEUTRALIZATIONS, prior.get("neutralization")),
        group_level=rng.choice(GROUP_LEVELS),
        truncation=(
            float(truncate) if truncate is not None
            else recipe_choice(rng, TRUNCATIONS, prior.get("truncation"))
        ),
        normalization=recipe_choice(rng, NORMALIZATIONS, prior.get("normalization")),
        winsorization=(
            recipe_choice(rng, (False, True), prior.get("winsorization"))
            if prior.get("winsorization") else rng.random() < 0.4
        ),
        rank_or_zscore=rng.choice(NORMALIZATIONS),
        sign=recipe_choice(rng, (1, -1), prior.get("sign")) if prior.get("sign") else (
            1 if rng.random() < 0.8 else -1
        ),
    )


# ---------------------------------------------------------------------------
# Mode and family budgets
# ---------------------------------------------------------------------------


def resolve_strategy(strategy: str) -> str:
    key = str(strategy or "mixed").strip().lower()
    if key not in STRATEGY_ALIASES:
        raise ValueError(f"unknown strategy {strategy!r}; known: {sorted(STRATEGY_ALIASES)}")
    return STRATEGY_ALIASES[key]


def mode_allocation(budget: int, mode: str, weights: Mapping[str, float] | None = None) -> list[str]:
    """Per-slot generation modes; the list length is exactly ``budget``.

    A single strategy repeats; ``mixed`` allocates the configured shares with a deterministic
    largest-remainder fill and interleaves them so one mode cannot front-load the campaign.
    """
    budget = int(budget)
    if budget <= 0:
        return []
    mode = resolve_strategy(mode)
    if mode != "mixed":
        return [mode] * budget
    weights = dict(weights or STRATEGY_WEIGHTS)
    active = {name: max(0.0, float(weights.get(name, 0.0))) for name in GENERATION_MODES}
    total = sum(active.values())
    if total <= 0:
        active = {"explore": 1.0}
        total = 1.0
    exact = {name: budget * share / total for name, share in active.items()}
    counts = {name: int(math.floor(value)) for name, value in exact.items()}
    remainder = budget - sum(counts.values())
    order = sorted(
        GENERATION_MODES,
        key=lambda name: (-(exact[name] - counts[name]), GENERATION_MODES.index(name)),
    )
    for name in order[:remainder]:
        counts[name] += 1
    # Interleave: one slot from each mode in turn, so exploration is spread through the run.
    queues = {name: [name] * counts[name] for name in GENERATION_MODES}
    result: list[str] = []
    while len(result) < budget:
        progressed = False
        for name in GENERATION_MODES:
            if queues[name]:
                result.append(queues[name].pop())
                progressed = True
                if len(result) >= budget:
                    break
        if not progressed:
            break
    return result[:budget]


#: How a warm-started exploitation campaign spends its slots across the distance ladder
#: (P19.3/P22.1). The bulk stays close to a proven seed: one controlled change at a time. D4
#: keeps a real exploration floor, because a ladder that never leaves the proven region cannot
#: notice that the region stopped working.
#:
#: ``D0`` is deliberately **not** budgeted by default. A byte-identical child is an exact
#: duplicate of an existing candidate, so the canonical cache refuses it and it can never
#: consume a BRAIN simulation: spending campaign budget on it would shrink the arm. It stays a
#: defined band (and a measurable rung) and is opt-in through explicit weights, e.g. when the
#: control arm's job is to prove the harness reproduces a seed exactly.
WARM_START_BAND_WEIGHTS: dict[str, float] = {
    "D0": 0.0,
    "D1": 0.32,
    "D2": 0.37,
    "D3": 0.21,
    "D4": 0.10,
}


def warm_start_schedule(
    budget: int,
    weights: Mapping[str, float] | None = None,
    *,
    seed: int = 0,
) -> list[str]:
    """Per-slot ladder rungs for a warm-started campaign, interleaved and deterministic.

    Same contract as :func:`mode_allocation`: the returned list length is exactly ``budget``,
    the largest-remainder fill is deterministic, and the rungs are interleaved so no rung
    front-loads the campaign.
    """
    budget = int(budget)
    if budget <= 0:
        return []
    active = {band: max(0.0, float((weights or WARM_START_BAND_WEIGHTS).get(band, 0.0))) for band in WARM_START_BANDS}
    total = sum(active.values())
    if total <= 0:
        active = dict.fromkeys(WARM_START_BANDS, 0.0)
        active["D1"] = 1.0
        total = 1.0
    # Every band keeps an entry, so the largest-remainder fill can look all of them up even
    # when a weighting zeroes some out.
    exact = {band: budget * active[band] / total for band in WARM_START_BANDS}
    counts = {band: int(math.floor(value)) for band, value in exact.items()}
    remainder = budget - sum(counts.values())
    order = sorted(WARM_START_BANDS, key=lambda band: (-(exact[band] - counts[band]), WARM_START_BANDS.index(band)))
    for band in order[:remainder]:
        counts[band] += 1
    queues = {band: [band] * counts[band] for band in WARM_START_BANDS}
    result: list[str] = []
    while len(result) < budget:
        progressed = False
        for band in WARM_START_BANDS:
            if queues[band]:
                result.append(queues[band].pop())
                progressed = True
                if len(result) >= budget:
                    break
        if not progressed:
            break
    return result[:budget]


def _expand_family_slots(allocation: Sequence[Mapping[str, Any]], budget: int) -> list[str]:
    """Interleaved per-slot families from an ``allocate_families`` result."""
    queues: dict[str, int] = {}
    for row in allocation:
        remaining = int(row.get("budget") or 0)
        if remaining > 0:
            queues[str(row["family"])] = remaining
    slots: list[str] = []
    names = sorted(queues)
    while len(slots) < budget and queues:
        progressed = False
        for name in list(names):
            if queues.get(name, 0) > 0:
                slots.append(name)
                queues[name] -= 1
                progressed = True
                if len(slots) >= budget:
                    break
        if not progressed:
            break
    return slots[:budget]


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def _catalog_fields(catalog: Any, family: str) -> list[Any]:
    try:
        fields = list(catalog.select(family))
    except (AttributeError, TypeError):
        fields = list(getattr(catalog, "fields", ()) or ())
    sources = [
        item for item in fields
        if str(getattr(item, "field_type", grammar.MATRIX)).upper() in grammar.SOURCE_TYPES
    ]
    return sources or list(getattr(catalog, "fields", ()) or ())


def _families(catalog: Any, family: str | None) -> list[str]:
    if family and family.lower() not in {"all", "*"}:
        return [family]
    datasets = sorted({str(getattr(item, "dataset", "unknown")) for item in getattr(catalog, "fields", ())})
    return [name for name in datasets if name and name != "unknown"] or ["unknown"]


def motif_outcome_stats(db: Any, *, generator_version: str | None = None) -> dict[str, tuple[int, int]]:
    """``motif_id -> (attempts, passes)`` from the persisted counters, empty when unbuilt."""
    if db is None:
        return {}
    outcomes = getattr(db, "motif_outcomes", None)
    if outcomes is None:
        return {}
    try:
        return dict(outcomes(generator_version=generator_version))
    except Exception:  # pragma: no cover - advisory only
        return {}


def allocate_motifs(
    motifs: Sequence[str],
    budget: int,
    stats: Mapping[str, tuple[int, int]] | None = None,
    *,
    seed: int = 0,
    exploration_floor: float = DEFAULT_MOTIF_EXPLORATION_FLOOR,
    max_share: float = DEFAULT_MOTIF_MAX_SHARE,
) -> dict[str, int]:
    """Bounded Thompson allocation of a campaign budget across motifs.

    Guarantees: slots sum to exactly ``budget``; untested motifs receive a floor of the
    budget so exploration never disappears; no motif may exceed ``max_share`` unless the cap
    makes the budget impossible to place. This is an advisory allocation the planner consumes,
    never a hard rejection system.
    """
    names = sorted({str(motif) for motif in motifs if str(motif)})
    budget = int(budget)
    if not names or budget <= 0:
        return {}
    stats = stats or {}
    rng = random.Random(int(seed))
    allocation = {name: 0 for name in names}
    untested = [name for name in names if stats.get(name, (0, 0))[0] == 0]
    if untested:
        reserve = min(budget, max(1, int(round(budget * max(0.0, min(1.0, exploration_floor))))))
        for index in range(reserve):
            allocation[untested[index % len(untested)]] += 1
    remaining = budget - sum(allocation.values())
    cap = max(1, int(math.ceil(budget * max(0.0, min(1.0, max_share)))), int(math.ceil(budget / len(names))))
    while remaining > 0:
        allowed = [name for name in names if allocation[name] < cap]
        if not allowed:
            cap += 1
            continue
        picked = max(
            allowed,
            key=lambda name: (
                rng.betavariate(1.0 + stats.get(name, (0, 0))[1],
                                1.0 + max(0, stats.get(name, (0, 0))[0] - stats.get(name, (0, 0))[1])),
                names.index(name),
            ),
        )
        allocation[picked] += 1
        remaining -= 1
    return allocation


def mutation_operation_outcome_stats(db: Any, *, generator_version: str | None = None) -> dict[str, tuple[int, int]]:
    """``mutation_operation -> (attempts, passes)`` from the persisted counters (P9.2)."""
    if db is None:
        return {}
    reader = getattr(db, "mutation_operation_outcomes", None)
    if reader is None:
        return {}
    try:
        return dict(reader(generator_version=generator_version))
    except Exception:  # pragma: no cover - advisory only
        return {}


#: Weight of the P22.1 destruction penalty: a destructive operation gives up this fraction of
#: its observed passes before the Thompson draw. Bounded below by the exploration floor, so a
#: penalized edit is retested rather than deleted.
DESTRUCTION_PENALTY = 0.5


def penalized_operation_stats(
    stats: Mapping[str, tuple[int, int]] | None,
    destruction: Mapping[str, Mapping[str, Any]] | None,
    *,
    penalty: float = DESTRUCTION_PENALTY,
) -> dict[str, tuple[int, int]]:
    """Discount an operation's observed passes by how often it dismantled its parent (P22.1).

    ``allocate_mutation_operations`` ranks edits by raw pass count, which cannot tell a real
    discovery from an edit that spends slots while destroying the parent's quality. The
    retention measured in :func:`seed_bank.operation_quality_retention` is spent here: an
    operation whose children drop below their parent loses ``destruction x penalty`` of its
    passes. The result never goes negative, an unmeasured or thin operation is left untouched,
    and the exploration floor in :func:`allocate_motifs` still guarantees it is retested.
    """
    base = {str(name): (int(value[0]), int(value[1])) for name, value in (stats or {}).items()}
    table = destruction or {}
    adjusted: dict[str, tuple[int, int]] = {}
    for name, (attempts, passes) in base.items():
        info = table.get(name) if isinstance(table, Mapping) else None
        if not info or info.get("destruction") is None or info.get("low_confidence"):
            adjusted[name] = (attempts, passes)
            continue
        cut = int(round(float(info["destruction"]) * attempts * max(0.0, float(penalty))))
        adjusted[name] = (attempts, max(0, passes - cut))
    return adjusted


def allocate_mutation_operations(
    operations: Sequence[str],
    budget: int,
    stats: Mapping[str, tuple[int, int]] | None = None,
    *,
    seed: int = 0,
    exploration_floor: float = DEFAULT_MOTIF_EXPLORATION_FLOOR,
    max_share: float = DEFAULT_MOTIF_MAX_SHARE,
    destruction: Mapping[str, Mapping[str, Any]] | None = None,
    penalty: float = DESTRUCTION_PENALTY,
) -> dict[str, int]:
    """Bounded adaptive allocation of mutate slots across concrete operations (P9.2/P22.1).

    Same guarantees as :func:`allocate_motifs`, for the structural mutation vocabulary:
    slots sum to exactly ``budget``, untested operations keep an exploration floor so a new
    edit can still be discovered, and a successful operation earns more budget without ever
    monopolizing the campaign. ``destruction`` (the P22.1 retention table, keyed by operation)
    discounts edits whose live children repeatedly destroy their parent's quality.
    """
    adjusted = penalized_operation_stats(stats, destruction, penalty=penalty)
    return allocate_motifs(
        operations, budget, adjusted,
        seed=seed, exploration_floor=exploration_floor, max_share=max_share,
    )


#: An emitted shape earns preference only when its measured rate is at least this multiple of
#: the campaign's own global rate — a self-calibrating bar, not a magic constant (P20.2).
OPERATOR_PREFERENCE_RATIO = 1.5
#: And it may claim at most this share of explore seats. The rest stays novelty-first, so the
#: measured shape is preferred *without* collapsing the search onto one motif (P20.2).
OPERATOR_PREFERENCE_SHARE = 0.6

#: A mode needs this many simulations before its measured rate may move its weight.
DEFAULT_MODE_MIN_EVIDENCE = 5
#: No mode may take more than this share of a campaign, however well it has performed (P20.2).
DEFAULT_MODE_MAX_SHARE = 0.55
#: Every mode keeps at least this share, so a mode that looks bad early can still be retested.
DEFAULT_MODE_FLOOR = 0.05
#: Quality evidence may move a mode's weight by at most this factor from its base share, so
#: quality conditions the search without collapsing the campaign onto a single mode.
MODE_WEIGHT_RATIO = 3.0


def mode_outcomes(
    db: Any,
    *,
    generator_version: str | None = None,
) -> dict[str, tuple[int, int]]:
    """``generation_mode -> (simulations, is_pass)`` from the persisted counters.

    Modes are compared on *simulations*, not attempts: a slot that was planned as a mutation
    and realized as exploration must not be charged against mutation's record (P9.2).
    """
    if db is None:
        return {}
    reader = getattr(db, "generation_stats", None)
    if reader is None:
        return {}
    try:
        rows = reader(generator_version=generator_version or None)
    except Exception:  # pragma: no cover - advisory only
        return {}
    totals: dict[str, list[int]] = {}
    for row in rows:
        mode = str(row.get("generation_mode") or "none")
        entry = totals.setdefault(mode, [0, 0])
        entry[0] += int(row.get("simulated") or 0)
        entry[1] += int(row.get("is_pass") or 0)
    return {mode: (counts[0], counts[1]) for mode, counts in totals.items()}


def _project_onto_bounds(
    shares: Mapping[str, float],
    lower: Mapping[str, float],
    upper: Mapping[str, float],
) -> dict[str, float]:
    """Scale ``shares`` onto ``{sum == 1, lower <= w <= upper}`` (water-filling).

    Clamping *before* normalizing does not bound the result: renormalizing pushes the clamped
    coordinate back outside its bound. This fixes the coordinates whose scaled value violates a
    bound and rescales the rest, repeating until the set is stable, so the returned weights
    satisfy the bounds and sum to one exactly.
    """
    names = list(shares)
    low = {name: max(0.0, float(lower.get(name, 0.0))) for name in names}
    high = {name: float(upper.get(name, 1.0)) for name in names}
    # Guard degenerate bounds so the projection always terminates with a usable vector: an
    # infeasible set is repaired rather than silently returning weights that do not sum to one.
    low_total = sum(low.values())
    if low_total > 1.0:
        low = {name: value / low_total for name, value in low.items()}
    high_total = sum(high.values())
    if high_total < 1.0:
        widest = max(names, key=lambda name: high[name])
        high = dict(high)
        high[widest] = high[widest] + (1.0 - high_total)
    fixed: dict[str, float] = {}
    free = {name: max(0.0, float(shares[name])) for name in names}
    for _ in range(len(names) + 2):
        if not free:
            break
        budget = 1.0 - sum(fixed.values())
        free_total = sum(free.values())
        if free_total <= 0.0:
            free = {name: budget / len(free) for name in free}
            free_total = sum(free.values())
        scale = budget / free_total if free_total else 0.0
        scaled = {name: value * scale for name, value in free.items()}
        violated = {
            name: (low[name] if value < low[name] else high[name] if value > high[name] else None)
            for name, value in scaled.items()
        }
        if not any(bound is not None for bound in violated.values()):
            fixed.update(scaled)
            free = {}
            break
        for name, bound in violated.items():
            if bound is not None:
                fixed[name] = bound
                del free[name]
    fixed.update({name: value for name, value in free.items()})
    total = sum(fixed.values()) or 1.0
    return {name: fixed.get(name, 0.0) / total for name in names}


def quality_conditioned_weights(
    base: Mapping[str, float] | None = None,
    stats: Mapping[str, tuple[int, int]] | None = None,
    *,
    min_evidence: int = DEFAULT_MODE_MIN_EVIDENCE,
    floor: float = DEFAULT_MODE_FLOOR,
    max_share: float = DEFAULT_MODE_MAX_SHARE,
    ratio: float = MODE_WEIGHT_RATIO,
) -> dict[str, float]:
    """Mode weights conditioned on measured mode quality, bounded at both ends (P22.3/P20.2).

    * a mode with too little evidence keeps its base share — no verdict from a handful of runs;
    * an evidenced mode's share is scaled by its posterior pass rate relative to the pooled
      rate, clamped to ``1/ratio .. ratio`` so quality cannot collapse the campaign onto one
      mode or delete another;
    * the final weights are projected onto ``floor``/``max_share`` **and** the ratio band
      around each mode's base share, so the bound holds on what a caller actually consumes.
      Clamping before renormalizing would push the clamped mode straight back out of band.
    """
    weights = {name: max(0.0, float((base or STRATEGY_WEIGHTS).get(name, 0.0))) for name in GENERATION_MODES}
    total = sum(weights.values())
    if total <= 0:
        weights = dict.fromkeys(GENERATION_MODES, 0.0)
        weights["explore"] = 1.0
        total = 1.0
    # ``plain`` is the unconditioned share the ratio band is measured against: the band must
    # bound the *final* weight relative to where the mode started, not relative to the
    # already-conditioned number (which would make the bound vacuous).
    plain = {name: weights[name] / total for name in GENERATION_MODES}
    shares = dict(plain)
    observed = {
        name: value for name, value in (stats or {}).items()
        if name in shares and int(value[0]) >= max(1, int(min_evidence))
    }
    if observed:
        pooled_sims = sum(int(value[0]) for value in observed.values())
        pooled_passes = sum(int(value[1]) for value in observed.values())
        pooled_rate = pooled_passes / pooled_sims if pooled_sims else 0.0
        for name, (simulations, passes) in observed.items():
            mean, _, _ = quality_prior._beta_interval(
                int(passes), int(simulations), 1.0, 19.0,
            )
            if pooled_rate <= 0:
                continue
            factor = min(ratio, max(1.0 / ratio, mean / pooled_rate))
            shares[name] = shares[name] * factor
    total = sum(shares.values())
    if total <= 0:
        return {name: 1.0 / len(GENERATION_MODES) for name in GENERATION_MODES}
    shares = {name: shares[name] / total for name in GENERATION_MODES}
    lower = {name: max(floor, plain[name] / ratio) for name in GENERATION_MODES}
    upper = {name: min(max_share, plain[name] * ratio) for name in GENERATION_MODES}
    projected = _project_onto_bounds(shares, lower, upper)
    return {name: round(projected[name], 8) for name in GENERATION_MODES}


def allocate_motifs_conditioned(
    motifs: Sequence[str],
    datasets: Sequence[str],
    budget: int,
    prior: Any,
    *,
    seed: int = 0,
    exploration_floor: float = DEFAULT_MOTIF_EXPLORATION_FLOOR,
    max_share: float = DEFAULT_MOTIF_MAX_SHARE,
    min_evidence: int = quality_prior.DEFAULT_MIN_EVIDENCE,
) -> tuple[dict[str, int], dict[str, Any]]:
    """Bounded allocation from conditional evidence rather than a global motif count (P21.1).

    Every motif is scored in the *dataset* context it would actually run in, through the
    hierarchical prior, so a motif that pays in one source and not another keeps its budget
    where the evidence puts it. Guarantees are the same as :func:`allocate_motifs`: the slots
    sum to exactly ``budget``, an unproven motif keeps an exploration floor and no motif may
    exceed ``max_share``.
    """
    names = sorted({str(motif) for motif in motifs if str(motif)})
    budget = int(budget)
    if not names or budget <= 0 or prior is None:
        return {}, {}
    sources = sorted({str(name) for name in datasets if str(name)}) or ["unknown"]
    scored: dict[str, dict[str, Any]] = {}
    for motif in names:
        best: dict[str, Any] | None = None
        for dataset in sources:
            context = quality_prior.Context(motif_id=motif, dataset=dataset)
            score, look = prior.score(context)
            detail = {
                "score": round(float(score), 8),
                "dataset": dataset,
                "level": look.level,
                "specific_level": look.specific_level,
                "simulations": look.simulations,
                "specific_simulations": look.specific.simulations,
                "mean": round(look.mean, 8),
                "upper": round(look.upper, 8),
            }
            if best is None or detail["score"] > best["score"]:
                best = detail
        if best is not None:
            scored[motif] = best
    if not scored:
        return {}, {}
    # An untested motif is not a bad motif: it holds the exploration floor (P20.2).
    untested = [motif for motif, detail in scored.items() if detail["specific_simulations"] == 0]
    allocation = {motif: 0 for motif in names}
    reserve = min(budget, max(1, int(round(budget * max(0.0, min(1.0, exploration_floor))))))
    for index in range(reserve):
        if untested:
            allocation[untested[index % len(untested)]] += 1
    remaining = budget - sum(allocation.values())
    reference = max(detail["score"] for detail in scored.values()) or 1.0
    while remaining > 0:
        cap = max(
            1, int(math.ceil(budget * max(0.0, min(1.0, max_share)))),
            int(math.ceil(budget / len(names))),
        )
        allowed = [motif for motif in names if allocation[motif] < cap]
        if not allowed:
            break
        # Score-proportional with a deterministic tie-break by remaining deficit: the
        # highest-scoring motif takes the next slot until it reaches its cap.
        picked = max(
            allowed,
            key=lambda motif: (
                scored[motif]["score"] / reference,
                -allocation[motif],
                names.index(motif),
            ),
        )
        allocation[picked] += 1
        remaining -= 1
    if remaining > 0:
        for index in range(remaining):
            allocation[names[index % len(names)]] += 1
    report = {
        "version": quality_prior.QUALITY_PRIOR_VERSION,
        "min_evidence": int(min_evidence),
        "exploration_floor": float(exploration_floor),
        "max_share": float(max_share),
        "untested": sorted(untested),
        "contexts": scored,
    }
    return allocation, report


def _proven_motifs(db: Any, families: Sequence[str]) -> dict[str, set[str]]:
    """Motifs with at least one settled pass, per family — the exploit prior."""
    proven: dict[str, set[str]] = {family: set() for family in families}
    if db is None:
        return proven
    rows = db.query(
        "SELECT signal_family, mutation_parameters_json, status FROM candidates WHERE mutation_parameters_json IS NOT NULL"
    )
    passing = {"IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"}
    for row in rows:
        try:
            parameters = json.loads(str(row["mutation_parameters_json"] or "{}"))
        except ValueError:
            continue
        motif = parameters.get("motif_id") if isinstance(parameters, Mapping) else None
        if not motif:
            continue
        family = str(row["signal_family"] or "")
        if row["status"] in passing:
            proven.setdefault(family, set()).add(str(motif))
        else:
            proven.setdefault(family, set())
    return proven


def _structure_counts(db: Any, catalog: Any = None) -> tuple[dict[str, int], dict[str, int]]:
    """``grammar/semantic skeleton hash -> attempts`` from local history (P4.2 budgeting).

    Hashes are derived from the stored expressions like :func:`diversity.novelty_context`
    does (falling back to the stored columns), so rows written before the V3 columns existed
    still count as the structures they are.
    """
    grammar_counts: dict[str, int] = {}
    semantic_counts: dict[str, int] = {}
    if db is None:
        return grammar_counts, semantic_counts
    metadata = diversity.catalog_metadata(catalog) if catalog is not None else diversity.load_field_metadata()
    try:
        rows = db.query(
            "SELECT normalized_expression, grammar_skeleton_hash, semantic_skeleton_hash FROM candidates"
        )
    except Exception:  # pragma: no cover - advisory only
        return grammar_counts, semantic_counts
    for row in rows:
        expression = str(row["normalized_expression"] or "")
        grammar_key = str(row["grammar_skeleton_hash"] or "")
        semantic_key = str(row["semantic_skeleton_hash"] or "")
        if not grammar_key and expression:
            grammar_key = grammar.grammar_skeleton_hash(expression, metadata)
        if not semantic_key and expression:
            semantic_key = grammar.semantic_skeleton_hash(expression, metadata)
        if grammar_key:
            grammar_counts[grammar_key] = grammar_counts.get(grammar_key, 0) + 1
        if semantic_key:
            semantic_counts[semantic_key] = semantic_counts.get(semantic_key, 0) + 1
    return grammar_counts, semantic_counts


def planned_recipe(
    campaign_id: str,
    seed: int,
    field_ids: Sequence[str],
    motif_id: str,
    recipe_index: int,
    parent_ids: Sequence[int] = (),
    *,
    prior: Mapping[str, Mapping[str, int]] | None = None,
) -> grammar.Recipe:
    """The exact recipe materialization samples for this slot (P3.1/P4.2).

    Same ``recipe_seed`` inputs *and the same recipe prior* as ``generator.materialize`` uses,
    so the planner and the materializer cannot disagree about lookback/window/decay or the
    topology-changing dimensions (rank vs zscore, winsorization, sign).
    """
    rng = random.Random(recipe_seed(campaign_id, seed, list(field_ids), motif_id, recipe_index, parent_ids))
    return sample_recipe(rng, motif_id, prior=prior)


def _structure_hashes(
    motif_id: str,
    fields: Sequence[Any],
    *,
    recipe: grammar.Recipe | None = None,
) -> tuple[str, str] | None:
    """``(grammar, semantic)`` skeleton hashes of one motif over these fields.

    Built through the same typed grammar the materializer uses. ``recipe`` must be the
    exact recipe materialization will sample for this slot (see :func:`planned_recipe`);
    the default recipe is a coarse proxy that can hash a different topology (P4.2).
    """
    try:
        node = grammar.build_motif(motif_id, list(fields), recipe or grammar.Recipe(), limits=_PLANNING_LIMITS)
    except grammar.GrammarError:
        return None
    return grammar.grammar_skeleton_hash(node), grammar.semantic_skeleton_hash(node)


#: The V3 materialization budget, reused so planning and materialization agree on what fits.
_PLANNING_LIMITS = grammar.ComplexityLimits(max_depth=5, max_nodes=16, max_fields=2, max_binary_ops=3)


def _structure_novelty(
    hashes: tuple[str, str] | None,
    *,
    grammar_counts: Mapping[str, int],
    semantic_counts: Mapping[str, int],
    occupancy: Mapping[str, int],
    used: Mapping[str, Mapping[str, int]],
    rng: random.Random,
) -> tuple[int, int, int, int, int, float]:
    """Explore priority of one motif: unseen grammar first, then sparse archive cells, then
    unseen semantics. ``hashes`` are the *exact* planned skeleton hashes (P4.2) of the
    structure materialization will build; ``None`` ranks last because it cannot be planned."""
    if hashes is None:
        return (2, 0, 0, 1, 0, rng.random())
    grammar_key, semantic_key = hashes
    grammar_seen = grammar_counts.get(grammar_key, 0) + used["grammar"].get(grammar_key, 0)
    semantic_seen = semantic_counts.get(semantic_key, 0) + used["semantic"].get(semantic_key, 0)
    return (
        0 if grammar_seen == 0 else 1,
        grammar_seen,
        int(occupancy.get(grammar_key, 0)),
        0 if semantic_seen == 0 else 1,
        semantic_seen,
        rng.random(),
    )


def _record_archive_refresh_failure(db: Any, error: BaseException, *, expected: bool) -> dict[str, Any]:
    """Persist a visible ``archive_refresh_failed`` diagnostic; never mask ``error``.

    The diagnostic goes to ``meta`` (cheap to read back, no schema change) and to the event
    log. Both writes are best-effort: a store that is too broken to accept the diagnostic is
    exactly the case where the original exception must reach the caller intact.
    """
    diagnostic: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error_type": type(error).__name__,
        "error": str(error)[:500],
        "expected_unavailable": bool(expected),
    }
    try:
        set_meta = getattr(db, "set_meta", None)
        if set_meta is not None:
            set_meta(ARCHIVE_REFRESH_FAILURE_KEY, json.dumps(diagnostic, sort_keys=True))
    except Exception:  # pragma: no cover - the store itself rejected the diagnostic
        pass
    try:
        log_event = getattr(db, "log_event", None)
        if log_event is not None:
            log_event(
                "archive", "archive_cells", "archive_refresh_failed",
                error_category=diagnostic["error_type"], payload=diagnostic,
            )
    except Exception:  # pragma: no cover - event log unavailable
        pass
    return diagnostic


def refresh_archive(db: Any) -> bool:
    """Rebuild the derived archive so planning sees every candidate settled so far (P5).

    ``archive_cells`` is derived state, so refreshing it at the planning boundary makes
    newly settled candidates influence the next campaign without an undocumented manual
    rebuild.

    Returns whether a rebuild ran. A store that has no archive tables yet is the only case
    that returns ``False`` without raising: that is "no archive", not "stale archive". Any
    other failure (schema drift, locking, corruption) persists an ``archive_refresh_failed``
    diagnostic and raises :class:`ArchiveRefreshError`, because planning must not continue
    against derived state that may not describe the candidate population any more (P16).
    """
    if db is None:
        return False
    rebuild = getattr(archive, "rebuild", None)
    if rebuild is None:  # pragma: no cover - archive is always importable here
        return False
    try:
        rebuild(db)
    except sqlite3.OperationalError as error:
        message = str(error).lower()
        if any(marker in message for marker in ARCHIVE_UNAVAILABLE_MARKERS):
            _record_archive_refresh_failure(db, error, expected=True)
            return False
        diagnostic = _record_archive_refresh_failure(db, error, expected=False)
        raise ArchiveRefreshError(
            f"archive refresh failed: {error}", diagnostic=diagnostic,
        ) from error
    except Exception as error:  # pragma: no cover - defensive: any real rebuild failure
        diagnostic = _record_archive_refresh_failure(db, error, expected=False)
        raise ArchiveRefreshError(
            f"archive refresh failed: {error}", diagnostic=diagnostic,
        ) from error
    return True


def _planned_partner(primary: Any, sources: Sequence[Any], global_sources: Sequence[Any]) -> Any | None:
    """The extra source a two-input motif will actually be built with (P16).

    The materializer appends a partner field when the plan supplies too few sources; if the
    planner cannot say which field that will be, the planned skeleton hash describes a
    different tree than the emitted one. This resolves the *same* cross-dataset preference in
    the same order as ``generator._partner_fields`` so planning and materialization agree.
    """
    primary_id = str(getattr(primary, "name", ""))
    primary_dataset = str(getattr(primary, "dataset", ""))

    def usable(item: Any) -> bool:
        return (
            str(getattr(item, "name", "")) != primary_id
            and str(getattr(item, "field_type", grammar.MATRIX)).upper() in grammar.SOURCE_TYPES
        )

    def key(item: Any) -> tuple[str, str]:
        return (str(getattr(item, "dataset", "")), str(getattr(item, "name", "")))

    def cross(pool: Sequence[Any]) -> list[Any]:
        return sorted(
            (item for item in pool if usable(item) and str(getattr(item, "dataset", "")) != primary_dataset),
            key=key,
        )

    picked = cross(sources) or cross(global_sources)
    if picked:
        return picked[0]
    same = sorted((item for item in sources if usable(item)), key=key)
    return same[0] if same else None


def _ordered_sources(db: Any, catalog: Any, family: str, scope: Mapping[str, Any] | None) -> list[Any]:
    """Fields of a family, least-tested first (coverage-aware, seeded tie-break left to caller)."""
    fields = _catalog_fields(catalog, family)
    try:
        import field_intelligence
    except ImportError:  # pragma: no cover - part of this repo
        return fields
    try:
        attempts = field_intelligence.coverage_attempts(db, fields, scope=scope or getattr(catalog, "scope", None))
    except Exception:  # pragma: no cover - advisory only
        return fields
    return sorted(fields, key=lambda item: (attempts.get(str(getattr(item, "name", "")), 0), str(getattr(item, "name", ""))))


def plan_campaign(
    db: Any,
    catalog: Any,
    campaign_id: str,
    budget: int,
    seed: int = 0,
    mode: str = "mixed",
    *,
    family: str | None = None,
    max_family_share: float = DEFAULT_MAX_FAMILY_SHARE,
    exploration_reserve: float = DEFAULT_EXPLORATION_RESERVE,
    exploration_floor: float = DEFAULT_MOTIF_EXPLORATION_FLOOR,
    motif_max_share: float = DEFAULT_MOTIF_MAX_SHARE,
    weights: Mapping[str, float] | None = None,
    parent_pool: int = 24,
    generator_version: str = "",
    refresh_archive_state: bool = True,
    force_motif: str | None = None,
    prior: Any = None,
    recipe_prior: Mapping[str, Mapping[str, int]] | None = None,
) -> Plan:
    """Turn a campaign budget into an explicit, archive-informed generation plan.

    Guarantees: ``plan.planned_budget == budget``; family caps from
    :func:`archive.allocate_families` are respected; the same DB snapshot + seed + versions
    produce an identical plan.

    ``refresh_archive_state`` rebuilds the derived archive before reading it, so a newly
    settled candidate is visible to the very next plan (P5 archive lifecycle). A rebuild
    that fails for a real reason raises through :func:`refresh_archive` instead of letting
    planning read stale derived state (P16).

    ``force_motif`` pins *every* slot to one motif **during planning** (P16): the planned
    motif and skeleton hashes are then computed from that motif, the resolved sources and the
    exact recipe materialization will sample, so ``plan.slots[i].planned_grammar_hash``
    describes the tree that is actually emitted. Lineage modes are not planned under a pin,
    because a mutation or crossover child has no planner-known motif.

    ``recipe_prior`` conditions recipe sampling on the values the platform has actually accepted
    (P21.3). It is stored on the plan so materialization samples the same recipes the planned
    skeleton hashes were computed over.
    """
    budget = max(0, int(budget))
    if force_motif is not None and force_motif not in grammar.MOTIF_BY_ID:
        raise ValueError(f"unknown motif {force_motif!r}")
    archive_refreshed = refresh_archive(db) if refresh_archive_state else False
    resolved_mode = resolve_strategy(mode)
    if force_motif is not None:
        # A pinned motif is generated, never edited, so the effective mode is exploration.
        resolved_mode = "explore"
    resolved_weights = {name: float((weights or STRATEGY_WEIGHTS).get(name, 0.0)) for name in GENERATION_MODES}
    # Mode shares are conditioned on measured mode quality when a prior is supplied (P22.3):
    # a mode that keeps paying earns budget, a mode with no positive marginal value is shrunk
    # toward the exploration floor rather than switched off.
    mode_quality: dict[str, Any] = {}
    if prior is not None:
        stats = mode_outcomes(db, generator_version=generator_version or None)
        conditioned = quality_conditioned_weights(weights or STRATEGY_WEIGHTS, stats)
        mode_quality = {"weights": conditioned, "measured": {name: list(value) for name, value in stats.items()}}
        resolved_weights = conditioned
    families = _families(catalog, family)
    scope = getattr(catalog, "scope", None)
    # Real field metadata for structural identities: without it every source parses as
    # dataset/category ``unknown`` (P6).
    metadata = diversity.catalog_metadata(catalog)

    allocation: list[Mapping[str, Any]] = []
    if db is not None and budget > 0:
        allocation = archive.allocate_families(
            db, families, budget=budget, seed=seed,
            exploration_reserve=exploration_reserve, max_family_share=max_family_share,
            allocation_key=f"{campaign_id}:{GENERATION_POLICY_VERSION}",
        )
    if not allocation and budget > 0:
        allocation = [{"family": families[0], "budget": budget, "share": 1.0,
                       "exploration": int(len(families) > 1), "max_family_share": float(max_family_share)}]

    recipe_prior = {
        str(dimension): {str(value): int(count) for value, count in counts.items()}
        for dimension, counts in dict(recipe_prior or {}).items()
    }
    if budget == 0:
        return Plan(campaign_id, resolved_mode, budget, seed, (), resolved_weights, tuple(allocation),
                    generator_version, max_family_share=max_family_share,
                    archive_refresh=archive_refreshed, forced_motif=force_motif or "",
                    recipe_prior=recipe_prior)

    family_slots = _expand_family_slots(allocation, budget)
    if len(family_slots) < budget:
        family_slots.extend([family_slots[-1] if family_slots else families[0]] * (budget - len(family_slots)))
    modes = mode_allocation(budget, resolved_mode, resolved_weights)
    if force_motif is not None:
        modes = ["explore"] * budget

    context = diversity.novelty_context(db, catalog) if db is not None else diversity.NoveltyContext.empty()
    parents = archive.parents(db, count=parent_pool, seed=seed) if db is not None else []
    # Lineage modes descend from parents whose *outcome* says the region is real (P22.2);
    # exploration keeps the diversity-aware pool. When no elite has reached a gate yet the
    # lineage pool is empty and the diversity pool is used unchanged, so a young campaign's
    # mode mix is not silently rewritten.
    lineage_parents: list[Mapping[str, Any]] = []
    if db is not None:
        try:
            lineage_parents = archive.exploitation_parents(db, count=parent_pool, seed=seed)
        except Exception:  # pragma: no cover - advisory only
            lineage_parents = []
    proven = _proven_motifs(db, sorted(set(family_slots)))
    # P4.3 (narrowed contract): evidence is not keyed to one family. A *motif* proven
    # anywhere may seed a compatible new source here before any unproven structure is
    # considered. This is motif-level transfer, not a general proven-semantic-skeleton
    # transfer mechanism.
    proven_anywhere: set[str] = set().union(*proven.values()) if proven else set()
    grammar_counts, semantic_counts = _structure_counts(db, catalog)
    occupancy: dict[str, int] = {}
    if db is not None:
        try:
            occupancy = archive.structure_occupancy(db)
        except Exception:  # pragma: no cover - advisory only
            occupancy = {}
    used_structures: dict[str, dict[str, int]] = {"grammar": {}, "semantic": {}}
    global_sources = _ordered_sources(db, catalog, "all", scope)
    # Bounded adaptive allocation (P9): under-tested motifs keep a floor, proven motifs gain
    # budget, and no motif can monopolize the campaign.
    if db is not None:
        refresh = getattr(db, "refresh_generation_stats", None)
        if refresh is not None:
            try:
                refresh(generator_version=generator_version or None)
            except Exception:  # pragma: no cover - advisory only
                pass
    motif_stats = motif_outcome_stats(db, generator_version=generator_version or None)
    family_motif_budgets: dict[str, dict[str, int]] = {}
    if prior is not None:
        # Do NOT allocate against the best-performing dataset and spend those seats globally:
        # a motif can succeed in A and fail in B. Each family's motif budget is conditioned
        # on that family's own evidence, and consumed only by slots in the same family.
        family_counts = {name: family_slots.count(name) for name in sorted(set(family_slots))}
        family_reports: dict[str, Any] = {}
        for name, family_budget in family_counts.items():
            allocated, report = allocate_motifs_conditioned(
                list(grammar.MOTIF_BY_ID), [name], family_budget, prior,
                seed=seed, exploration_floor=exploration_floor, max_share=motif_max_share,
            )
            family_motif_budgets[name] = allocated
            family_reports[name] = report
        motif_budget = {
            motif: sum(counts.get(motif, 0) for counts in family_motif_budgets.values())
            for motif in sorted(grammar.MOTIF_BY_ID)
        }
        # Preserve the previous public report keys. Context keys now include their source
        # family because one motif can have different evidence and weight in different data.
        flattened_contexts = {
            f"{name}:{motif}": context
            for name, report in family_reports.items()
            for motif, context in report.get("contexts", {}).items()
        }
        quality_report = {
            "version": quality_prior.QUALITY_PRIOR_VERSION,
            "by_family": family_reports,
            "contexts": flattened_contexts,
        }
    else:
        quality_report = {}
        motif_budget = allocate_motifs(list(grammar.MOTIF_BY_ID), budget, motif_stats,
                                       seed=seed, exploration_floor=exploration_floor,
                                       max_share=motif_max_share)
    if force_motif is not None:
        # The pin is the allocation, even when a prior was passed: never report quotas the
        # forced plan cannot spend (P16).
        motif_budget = {force_motif: budget}
        family_motif_budgets = {
            name: {force_motif: family_slots.count(name)}
            for name in sorted(set(family_slots))
        }
        if prior is not None:
            quality_report = {
                "version": quality_prior.QUALITY_PRIOR_VERSION,
                "forced_motif": force_motif,
                "by_family": {},
                "contexts": {},
            }
    remaining_motif_budget = dict(motif_budget)
    remaining_family_motif_budgets = {name: dict(counts) for name, counts in family_motif_budgets.items()}
    # Bounded adaptive mutation allocation (P9.2): mutate slots are budgeted across the
    # concrete structural edits from their corrected historical outcomes, so a successful
    # operation earns more budget while an untested one keeps an exploration floor.
    mutation_stats = mutation_operation_outcome_stats(db, generator_version=generator_version or None)
    # P22.1: measure how often each edit keeps its parent's quality, so the allocation can price
    # a destroyer below a discoverer with the same raw pass count.
    destruction: dict[str, Any] = {}
    if db is not None:
        try:
            import seed_bank  # local: keeps the module import graph acyclic

            destruction = seed_bank.operation_quality_retention(
                db, child_versions=[generator_version] if generator_version else None,
            )
        except Exception:  # pragma: no cover - advisory only
            destruction = {}
    mutation_budget = allocate_mutation_operations(
        list(V3_MUTATION_OPERATIONS), sum(1 for mode in modes if mode == "mutate"), mutation_stats,
        seed=seed, exploration_floor=exploration_floor, max_share=motif_max_share,
        destruction=destruction,
    )
    remaining_mutation_budget = dict(mutation_budget)

    slots: list[PlanSlot] = []
    used_motifs: dict[str, int] = {}
    used_operations: dict[str, int] = {}
    used_parents: dict[int, int] = {}
    # Reachability of a genuine cross-dataset structure must not depend on the novelty
    # tiebreak's random draw (P4.2): once a campaign has begun exploring, the first explore
    # slot that can build such a structure *and* that structure is structurally unseen seats
    # it deterministically. The opening slot is left to ordinary novelty so the plan is not
    # reordered around the reservation.
    cross_dataset_seated = False
    explore_seats = 0
    # Campaign-level counters must not reset every slot: they enforce the quality share cap
    # and back the Plan.operator_preference telemetry.
    operator_evidence = _operator_evidence(prior) if prior is not None else {}
    explore_slots = sum(1 for slot_mode in modes if slot_mode == "explore")
    paid_seats = int(math.ceil(explore_slots * OPERATOR_PREFERENCE_SHARE)) if operator_evidence else 0
    paid_floor = 0.0
    if operator_evidence:
        baseline = float(getattr(prior, "global_prior", 0.0) or 0.0)
        paid_floor = (max(operator_evidence.values()) if baseline <= 0
                      else OPERATOR_PREFERENCE_RATIO * baseline)
    paid_used = 0
    for index, (slot_family, generation_mode) in enumerate(zip(family_slots, modes)):
        slot_rng = random.Random(recipe_seed(campaign_id, seed, (), generation_mode, index, ()))
        sources = _ordered_sources(db, catalog, slot_family, scope)
        if not sources:
            continue
        # Coverage ordering dominates; the slot rng only breaks ties inside a coverage bucket.
        # Resolve the mode against the *actual* archive before choosing a motif: a slot whose
        # parents do not exist cannot be a mutation or a crossover, and labelling it as one would
        # put a false lineage in the ledger. Substituted slots are planned as exploration.
        effective_mode = generation_mode
        parent_ids: tuple[int, ...] = ()
        target_family = ""
        mutation_operation = ""
        substitution = ""
        if generation_mode == "mutate":
            parent = _pick_parent(lineage_parents or parents, used_parents, slot_rng, families)
            if parent is None:
                effective_mode, substitution = "explore", "no archive elite to mutate"
            else:
                parent_ids = (int(parent),)
                target_family = str(slot_family)
                if remaining_mutation_budget:
                    mutation_operation = max(
                        sorted(remaining_mutation_budget),
                        key=lambda name: (remaining_mutation_budget.get(name, 0),
                                          -used_operations.get(name, 0), name),
                    )
                    used_operations[mutation_operation] = used_operations.get(mutation_operation, 0) + 1
                    remaining_mutation_budget[mutation_operation] = max(
                        0, remaining_mutation_budget.get(mutation_operation, 0) - 1,
                    )
        elif generation_mode == "crossover":
            pair = _pick_crossover_pair(lineage_parents or parents, used_parents, slot_rng,
                                        metadata=metadata)
            if len(pair) < 2:
                effective_mode, substitution = "explore", "no distant parent pair in the archive"
            else:
                parent_ids = pair
                target_family = "x".join(sorted({str(used) for used in pair})) or ""

        bucket_limit = min(4, len(sources))
        chosen = sources[slot_rng.randrange(bucket_limit)]
        primary = grammar.field_node_from(chosen)
        others = [item for item in sources if str(getattr(item, "name", "")) != primary.field_id]
        distinct = [item for item in others if str(getattr(item, "dataset", "")) != primary.dataset]
        if not distinct:
            # Cross-dataset motifs must be reachable in ordinary planning (P4.2): widen the
            # partner pool to the global source pool when the family itself is single-dataset.
            distinct = [item for item in global_sources
                        if str(getattr(item, "dataset", "")) != primary.dataset
                        and str(getattr(item, "name", "")) != primary.field_id]
        partner_pool = distinct or others
        partner = grammar.field_node_from(partner_pool[slot_rng.randrange(min(4, len(partner_pool)))]) if partner_pool else None
        if partner is None:
            # A two-input motif stays planable exactly (P16): resolve the same partner the
            # materializer would append, so the planned skeleton hash is not computed over a
            # one-source plan that silently grows a second source later.
            fallback_partner = _planned_partner(chosen, sources, global_sources)
            if fallback_partner is not None:
                partner = grammar.field_node_from(fallback_partner)
        single_source = list(grammar.eligible_motifs([primary]))
        two_source = [
            motif for motif in (grammar.eligible_motifs([primary, partner]) if partner else ())
            if len(motif.input_roles) == 2
        ]
        eligible = single_source + two_source

        def motif_fields(name: str) -> list[Any]:
            roles = len(grammar.motif_by_id(name).input_roles)
            return [primary, partner] if roles == 2 and partner is not None else [primary]

        recipe_index = index % 8

        def hashes_for(name: str, stage_fields: Sequence[Any]) -> tuple[str, str] | None:
            """Exact planned skeleton hashes of ``name`` over ``stage_fields`` (P4.2).

            Derives the recipe materialization will sample from the same recipe-seed inputs,
            so the planner budgets the structure it will actually build.
            """
            field_ids = [node.field_id for node in stage_fields]
            recipe = planned_recipe(campaign_id, seed, field_ids, name, recipe_index, parent_ids,
                                    prior=recipe_prior)
            return _structure_hashes(name, stage_fields, recipe=recipe)

        def emitted_operator(name: str) -> str:
            """The root operator ``name`` would actually emit over this slot's sources.

            Built with the same typed grammar and the same planned recipe the materializer uses,
            so the preference is about the expression that will be emitted, not the motif's name.
            """
            stage_fields = motif_fields(name)
            try:
                recipe = planned_recipe(
                    campaign_id, seed, [node.field_id for node in stage_fields], name,
                    recipe_index, parent_ids, prior=recipe_prior,
                )
                node = grammar.build_motif(name, stage_fields, recipe, limits=_PLANNING_LIMITS)
            except grammar.GrammarError:
                return ""
            return str(node.operator).lower() if isinstance(node, grammar.CallNode) else ""

        def cross_dataset_option() -> str | None:
            """A constructible, structurally unseen cross-dataset motif for this slot, if any."""
            field_set = motif_fields("cross_dataset_composite")
            if len({node.dataset for node in field_set}) < 2:
                return None
            hashes = hashes_for("cross_dataset_composite", field_set)
            if hashes is None:
                return None
            grammar_key = hashes[0]
            if grammar_counts.get(grammar_key, 0) or used_structures["grammar"].get(grammar_key, 0):
                return None  # a repeat: never spend the reachability seat on a known structure
            return "cross_dataset_composite"

        def _explore_key(name: str) -> tuple:
            """Explore ordering: quality-reserved shape, per-family quota, then novelty.

            Without evidence, preserve the original novelty-first tuple and RNG calls exactly.
            With evidence, spend the paid-shape reserve first, consume eligible family quota,
            and retain novelty as the ordering *within* the eligible quota class.
            """
            novelty = _structure_novelty(
                hashes_for(name, motif_fields(name)),
                grammar_counts=grammar_counts, semantic_counts=semantic_counts,
                occupancy=occupancy, used=used_structures, rng=slot_rng,
            )
            # With no outcome evidence, leave the old novelty-first ordering unchanged.
            # A quality-conditioned plan with evidence must *spend* its family-specific motif
            # quota on explore slots too; otherwise P21 allocation is just a report while the
            # largest generation mode ignores it. When no eligible motif has quota left, the
            # ranking naturally falls back to sparse/novel structures.
            global_evidence = getattr(prior, "global_evidence", None)
            if prior is None or (
                global_evidence is not None and int(global_evidence.simulations) == 0
            ):
                return novelty
            family_remaining = remaining_family_motif_budgets.get(slot_family, {})
            quota_rank = 0 if family_remaining.get(name, 0) > 0 else 1
            paid = operator_evidence.get(emitted_operator(name), 0.0)
            if prefer_paid:
                return (0 if paid >= paid_floor else 1, quota_rank,
                        *novelty[:5], -paid, novelty[5])
            # Off the earned-shape reserve, respect the family quota first and rank
            # eligible within-quota motifs by the old novelty key.
            return (quota_rank, *novelty[:5], -paid, novelty[5])

        if force_motif is not None:
            # Pinned at planning time: identical to what materialization will build (P16).
            motif_id = force_motif
        elif not eligible:
            motif_id = "cross_sectional_level"
        elif effective_mode == "explore":
            # Budget grammar/semantic novelty *before* materialization (P4.2): explore slots go
            # to structures that are unseen or sparse in the archive, never to a repeat while
            # an untested structure is still reachable.
            # Quality may claim a bounded, explicit share of explore seats ahead of archive
            # novelty (P20.2): the ledger's only paying shape so far is a multi-component
            # composite, which pure novelty-first ordering is structurally biased against,
            # because "this shape already exists" is exactly what novelty penalises.
            prefer_paid = bool(operator_evidence) and explore_seats < paid_seats
            reachable_cross = (
                cross_dataset_option() if not cross_dataset_seated and explore_seats >= 1 else None
            )
            if reachable_cross is not None:
                motif_id = reachable_cross
                cross_dataset_seated = True
            else:
                motif_id = min(
                    (motif.id for motif in eligible),
                    key=lambda name: _explore_key(name),
                )
            if prefer_paid and operator_evidence.get(emitted_operator(motif_id), 0.0) >= paid_floor:
                # Only a seat that actually landed on an earning shape is counted, so the
                # diagnostic cannot claim a preference the plan did not spend (P21.1).
                paid_used += 1
            explore_seats += 1
        else:
            # exploit / mutate / crossover: spend the bounded adaptive allocation, preferring a
            # motif that still has budget and has performed well enough to earn more.
            local = [m.id for m in eligible if m.id in proven.get(slot_family, set())]
            transferred = [m.id for m in eligible if m.id in proven_anywhere]
            pool = local or transferred or [m.id for m in eligible]
            slot_budget = remaining_family_motif_budgets.get(slot_family, remaining_motif_budget)
            motif_id = max(
                pool,
                key=lambda name: (slot_budget.get(name, 0), -used_motifs.get(name, 0), name),
            )
        used_motifs[motif_id] = used_motifs.get(motif_id, 0) + 1
        remaining_motif_budget[motif_id] = max(0, remaining_motif_budget.get(motif_id, 0) - 1)
        if slot_family in remaining_family_motif_budgets:
            remaining = remaining_family_motif_budgets[slot_family]
            remaining[motif_id] = max(0, remaining.get(motif_id, 0) - 1)
        field_nodes = [primary, partner] if len(grammar.motif_by_id(motif_id).input_roles) == 2 and partner else [primary]
        hashes = hashes_for(motif_id, motif_fields(motif_id))
        planned_grammar_hash = planned_semantic_hash = ""
        if hashes is not None:
            grammar_key, semantic_key = hashes
            used_structures["grammar"][grammar_key] = used_structures["grammar"].get(grammar_key, 0) + 1
            used_structures["semantic"][semantic_key] = used_structures["semantic"].get(semantic_key, 0) + 1
            if effective_mode in {"explore", "exploit"}:
                # Only motif-materialized slots have a planner-known structure (P4.2).
                planned_grammar_hash, planned_semantic_hash = grammar_key, semantic_key

        reason = "under-tested semantic niche"
        if force_motif is not None:
            reason = f"forced motif {force_motif} on planned sources"
        elif effective_mode == "explore":
            reason = "unseen motif for this campaign" if motif_id not in context.motifs else "sparse archive niche"
        elif effective_mode == "exploit":
            if motif_id in proven.get(slot_family, set()):
                reason = "proven motif on a new source"
            elif motif_id in proven_anywhere:
                reason = "proven motif transferred to a compatible new dataset"
            else:
                reason = "exploit allocation without family-local evidence"
        elif effective_mode == "mutate":
            reason = "repair/perturb a diverse archive elite"
        elif effective_mode == "crossover":
            reason = "combine two distant archive elites"
        if substitution:
            reason = f"{reason} (planned {generation_mode}: {substitution})"

        slots.append(PlanSlot(
            slot=index,
            generation_mode=effective_mode,
            family=slot_family,
            motif_id=motif_id,
            recipe_index=recipe_index,
            reason=reason,
            fields=tuple(node.field_id for node in field_nodes),
            datasets=tuple(sorted({node.dataset for node in field_nodes})),
            parent_ids=parent_ids,
            target_family=target_family,
            mutation_operation=mutation_operation,
            planned_grammar_hash=planned_grammar_hash,
            planned_semantic_hash=planned_semantic_hash,
        ))

    # Never plan fewer slots than requested: repeat the last slot family deterministically if
    # the catalog could not seat every family.
    while len(slots) < budget:
        index = len(slots)
        family_name = family_slots[index] if index < len(family_slots) else families[0]
        sources = _ordered_sources(db, catalog, family_name, scope)
        if not sources:
            break
        node = grammar.field_node_from(sources[index % len(sources)])
        slots.append(PlanSlot(
            slot=index, generation_mode=resolve_strategy(mode) if resolved_mode != "mixed" else "explore",
            family=family_name, motif_id="cross_sectional_level", recipe_index=index % 8,
            reason="budget fill", fields=(node.field_id,), datasets=(node.dataset,),
        ))

    return Plan(
        campaign_id=campaign_id,
        mode=resolved_mode,
        budget=budget,
        seed=int(seed),
        slots=tuple(slots[:budget]),
        weights=resolved_weights,
        family_allocation=tuple(dict(row) for row in allocation),
        generator_version=generator_version,
        max_family_share=float(max_family_share),
        motif_allocation=dict(motif_budget),
        mutation_allocation=dict(mutation_budget),
        mutation_quality=dict(destruction),
        archive_refresh=archive_refreshed,
        forced_motif=force_motif or "",
        quality_allocation=quality_report,
        prior_version=quality_prior.QUALITY_PRIOR_VERSION if prior is not None else "",
        mode_weights={str(key): float(value) for key, value in mode_quality.get("weights", {}).items()},
        recipe_prior=recipe_prior,
        operator_preference={
            "seats": paid_seats,
            "used": paid_used,
            "earning_bar": round(float(paid_floor), 6),
            "explore_seats": explore_slots,
            "evidence": {name: round(value, 6) for name, value in sorted(operator_evidence.items())},
        },
    )


def _operator_evidence(prior: Any) -> dict[str, float]:
    """Measured pass rate per *emitted root operator*, from a conditional prior (P21.1).

    This is the one cell in the hierarchy that spans every generator version — a motif name is
    vocabulary-local, an outer operator is not — and the ledger separates it sharply:
    multi-component ``add`` composites pass at a far higher rate than single normalized blobs.
    Only cells with minimum evidence count, and a prior without the level (or without tables at
    all, as a test stub has) yields no preference at all.
    """
    tables = getattr(prior, "tables", None)
    if not isinstance(tables, Mapping):
        return {}
    table = tables.get("outer_operator") or {}
    minimum = int(getattr(prior, "min_evidence", 1) or 1)
    evidence: dict[str, float] = {}
    for key, cell in table.items():
        simulations = int(getattr(cell, "simulations", 0) or 0)
        if simulations < minimum:
            continue
        evidence[str(key).strip().lower()] = int(getattr(cell, "is_pass", 0) or 0) / simulations
    return evidence


def _pick_parent(
    parents: Sequence[Mapping[str, Any]],
    used: dict[int, int],
    rng: random.Random,
    families: Sequence[str],
) -> int | None:
    if not parents:
        return None
    ranked = sorted(parents, key=lambda row: (used.get(int(row["elite_candidate_id"]), 0), str(row["cell_key"])))
    window = ranked[: max(1, min(4, len(ranked)))]
    choice = window[rng.randrange(len(window))]
    parent_id = int(choice["elite_candidate_id"])
    used[parent_id] = used.get(parent_id, 0) + 1
    return parent_id


def _parent_grammar_hash(row: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> str:
    """The elite's own grammar skeleton hash (topology + field types, fields masked)."""
    expression = str(row.get("normalized_expression") or "")
    if not expression:
        return str((row.get("dimensions_json") and row.get("cell_key")) or "")
    return grammar.grammar_skeleton_hash(expression, metadata)


def _pair_distance(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> float:
    """Structural distance between two archive elites, motif ids included when known.

    ``metadata`` is the field catalog (``name -> FieldInfo``). Without it the source fields
    parse as ``dataset/category = unknown`` and the dataset/category Jaccard components
    collapse to zero, so parent selection cannot tell a same-dataset pair from a
    cross-dataset one (P6).
    """
    motif_left = str(left.get("motif_id") or "") or None
    motif_right = str(right.get("motif_id") or "") or None
    try:
        return grammar.grammar_distance(
            str(left.get("normalized_expression") or ""),
            str(right.get("normalized_expression") or ""),
            fields=metadata,
            motif_left=motif_left, motif_right=motif_right,
        )
    except grammar.GrammarError:
        return 0.0


def _pick_crossover_pair(
    parents: Sequence[Mapping[str, Any]],
    used: dict[int, int],
    rng: random.Random,
    *,
    min_distance: float = 0.0,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[int, ...]:
    """Two parents from different families/niches, rejecting near-identical pairs by default.

    Near-identity is judged on two independent facts: the grammar skeleton hash (same
    topology and field types) and :func:`expression_grammar.grammar_distance`, which also
    accounts for the datasets/categories actually used. A pair that is identical on both is
    the least informative crossover possible and is not selected while a more distant pair
    exists.
    """
    if len(parents) < 2:
        parent = _pick_parent(parents, used, rng, ())
        return (parent,) if parent is not None else ()
    ordered = sorted(parents, key=lambda row: (str(row.get("cell_key")), int(row["elite_candidate_id"])))
    candidates: list[tuple[float, Mapping[str, Any], Mapping[str, Any]]] = []
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            if str(left.get("signal_family") or "") == str(right.get("signal_family") or ""):
                continue
            if _parent_grammar_hash(left, metadata) == _parent_grammar_hash(right, metadata):
                continue
            distance = _pair_distance(left, right, metadata=metadata)
            if distance < min_distance:
                continue
            candidates.append((distance, left, right))
    if not candidates:
        # Nothing distant enough: still allow a distinct-family pair so a planned crossover
        # slot is not silently dropped, but prefer the most distant one available.
        best: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
        best_distance = -1.0
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                if str(left.get("signal_family") or "") == str(right.get("signal_family") or ""):
                    continue
                distance = _pair_distance(left, right, metadata=metadata)
                if distance > best_distance:
                    best, best_distance = (left, right), distance
        candidates = [(best_distance, *best)] if best is not None else []
    if not candidates:
        parent = _pick_parent(parents, used, rng, ())
        return (parent,) if parent is not None else ()
    # Distance is a real selection objective (P6.2), not only a filter: the structurally
    # most distant eligible pair is chosen, with the seeded rng breaking exact ties only.
    top = max(distance for distance, _left, _right in candidates)
    tied = [(left, right) for distance, left, right in candidates if distance == top]
    left, right = tied[rng.randrange(len(tied))]
    pair = sorted({int(left["elite_candidate_id"]), int(right["elite_candidate_id"])})
    for parent_id in pair:
        used[parent_id] = used.get(parent_id, 0) + 1
    return tuple(pair)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - thin CLI
    import argparse

    import generator
    import research_db

    parser = argparse.ArgumentParser(description="Plan a Generator V3 campaign (no BRAIN calls)")
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--strategy", default="mixed")
    parser.add_argument("--family")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        plan = plan_campaign(
            db, generator.Catalog(), args.campaign, args.count, args.seed, args.strategy,
            family=args.family, generator_version=generator.GENERATOR_VERSION,
        )
        print(json.dumps({
            "planned_budget": plan.planned_budget,
            "generation_mode": plan.distribution("generation_mode"),
            "family": plan.distribution("family"),
            "motif": plan.distribution("motif_id"),
            "dataset": plan.distribution("datasets"),
        }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
