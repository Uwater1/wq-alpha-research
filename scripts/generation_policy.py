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
import sys
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import archive  # noqa: E402
import diversity  # noqa: E402
import expression_grammar as grammar  # noqa: E402

GENERATION_POLICY_VERSION = "generation-policy-v1"

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
TRUNCATIONS = (0.05, 0.1, 0.15)
NORMALIZATIONS = ("rank", "zscore")

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


def sample_recipe(rng: random.Random, motif_id: str, *, truncate: float | None = None) -> grammar.Recipe:
    """Sample every recipe dimension independently for one proposal."""
    return grammar.Recipe(
        lookback=rng.choice(LOOKBACKS),
        smoothing_window=rng.choice(SMOOTHING_WINDOWS),
        decay=rng.choice(DECAYS),
        neutralization=rng.choice(NEUTRALIZATIONS),
        group_level=rng.choice(GROUP_LEVELS),
        truncation=float(truncate) if truncate is not None else rng.choice(TRUNCATIONS),
        normalization=rng.choice(NORMALIZATIONS),
        winsorization=rng.random() < 0.4,
        rank_or_zscore=rng.choice(NORMALIZATIONS),
        sign=1 if rng.random() < 0.8 else -1,
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


def allocate_mutation_operations(
    operations: Sequence[str],
    budget: int,
    stats: Mapping[str, tuple[int, int]] | None = None,
    *,
    seed: int = 0,
    exploration_floor: float = DEFAULT_MOTIF_EXPLORATION_FLOOR,
    max_share: float = DEFAULT_MOTIF_MAX_SHARE,
) -> dict[str, int]:
    """Bounded adaptive allocation of mutate slots across concrete operations (P9.2).

    Same guarantees as :func:`allocate_motifs`, for the structural mutation vocabulary:
    slots sum to exactly ``budget``, untested operations keep an exploration floor so a new
    edit can still be discovered, and a successful operation earns more budget without ever
    monopolizing the campaign.
    """
    return allocate_motifs(
        operations, budget, stats,
        seed=seed, exploration_floor=exploration_floor, max_share=max_share,
    )


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


def _structure_hashes(motif_id: str, fields: Sequence[Any]) -> tuple[str, str] | None:
    """Representative ``(grammar, semantic)`` skeleton hashes of one motif over these fields.

    Built through the same typed grammar the materializer uses, with a default recipe, so
    the planner budgets the structure it will actually ask for.
    """
    try:
        node = grammar.build_motif(motif_id, list(fields), grammar.Recipe(), limits=_PLANNING_LIMITS)
    except grammar.GrammarError:
        return None
    return grammar.grammar_skeleton_hash(node), grammar.semantic_skeleton_hash(node)


#: The V3 materialization budget, reused so planning and materialization agree on what fits.
_PLANNING_LIMITS = grammar.ComplexityLimits(max_depth=5, max_nodes=16, max_fields=2, max_binary_ops=3)


def _structure_novelty(
    motif_id: str,
    fields: Sequence[Any],
    *,
    context: "diversity.NoveltyContext",
    grammar_counts: Mapping[str, int],
    semantic_counts: Mapping[str, int],
    occupancy: Mapping[str, int],
    used: Mapping[str, Mapping[str, int]],
    rng: random.Random,
) -> tuple[int, int, int, int, int, float]:
    """Explore priority of one motif: unseen grammar first, then sparse archive cells, then
    unseen semantics. A structure that cannot even be planned ranks last (P4.2)."""
    hashes = _structure_hashes(motif_id, fields)
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
) -> Plan:
    """Turn a campaign budget into an explicit, archive-informed generation plan.

    Guarantees: ``plan.planned_budget == budget``; family caps from
    :func:`archive.allocate_families` are respected; the same DB snapshot + seed + versions
    produce an identical plan.
    """
    budget = max(0, int(budget))
    resolved_mode = resolve_strategy(mode)
    resolved_weights = {name: float((weights or STRATEGY_WEIGHTS).get(name, 0.0)) for name in GENERATION_MODES}
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

    if budget == 0:
        return Plan(campaign_id, resolved_mode, budget, seed, (), resolved_weights, tuple(allocation),
                    generator_version, max_family_share=max_family_share)

    family_slots = _expand_family_slots(allocation, budget)
    if len(family_slots) < budget:
        family_slots.extend([family_slots[-1] if family_slots else families[0]] * (budget - len(family_slots)))
    modes = mode_allocation(budget, resolved_mode, resolved_weights)

    context = diversity.novelty_context(db, catalog) if db is not None else diversity.NoveltyContext.empty()
    parents = archive.parents(db, count=parent_pool, seed=seed) if db is not None else []
    proven = _proven_motifs(db, sorted(set(family_slots)))
    # P4.3: evidence is not keyed to one family. A motif proven anywhere may seed a
    # compatible new dataset here before any unproven structure is considered.
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
    motif_budget = allocate_motifs(list(grammar.MOTIF_BY_ID), budget, motif_stats,
                                   seed=seed, exploration_floor=exploration_floor, max_share=motif_max_share)
    remaining_motif_budget = dict(motif_budget)
    # Bounded adaptive mutation allocation (P9.2): mutate slots are budgeted across the
    # concrete structural edits from their corrected historical outcomes, so a successful
    # operation earns more budget while an untested one keeps an exploration floor.
    mutation_stats = mutation_operation_outcome_stats(db, generator_version=generator_version or None)
    mutation_budget = allocate_mutation_operations(
        list(V3_MUTATION_OPERATIONS), sum(1 for mode in modes if mode == "mutate"), mutation_stats,
        seed=seed, exploration_floor=exploration_floor, max_share=motif_max_share,
    )
    remaining_mutation_budget = dict(mutation_budget)

    slots: list[PlanSlot] = []
    used_motifs: dict[str, int] = {}
    used_operations: dict[str, int] = {}
    used_parents: dict[int, int] = {}
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
            parent = _pick_parent(parents, used_parents, slot_rng, families)
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
            pair = _pick_crossover_pair(parents, used_parents, slot_rng, metadata=metadata)
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
        single_source = list(grammar.eligible_motifs([primary]))
        two_source = [
            motif for motif in (grammar.eligible_motifs([primary, partner]) if partner else ())
            if len(motif.input_roles) == 2
        ]
        eligible = single_source + two_source

        def motif_fields(name: str) -> list[Any]:
            roles = len(grammar.motif_by_id(name).input_roles)
            return [primary, partner] if roles == 2 and partner is not None else [primary]

        if not eligible:
            motif_id = "cross_sectional_level"
        elif effective_mode == "explore":
            # Budget grammar/semantic novelty *before* materialization (P4.2): explore slots go
            # to structures that are unseen or sparse in the archive, never to a repeat while
            # an untested structure is still reachable.
            motif_id = min(
                (motif.id for motif in eligible),
                key=lambda name: _structure_novelty(
                    name, motif_fields(name), context=context,
                    grammar_counts=grammar_counts, semantic_counts=semantic_counts,
                    occupancy=occupancy, used=used_structures, rng=slot_rng,
                ),
            )
        else:
            # exploit / mutate / crossover: spend the bounded adaptive allocation, preferring a
            # motif that still has budget and has performed well enough to earn more.
            local = [m.id for m in eligible if m.id in proven.get(slot_family, set())]
            transferred = [m.id for m in eligible if m.id in proven_anywhere]
            pool = local or transferred or [m.id for m in eligible]
            motif_id = max(
                pool,
                key=lambda name: (remaining_motif_budget.get(name, 0), -used_motifs.get(name, 0), name),
            )
        used_motifs[motif_id] = used_motifs.get(motif_id, 0) + 1
        remaining_motif_budget[motif_id] = max(0, remaining_motif_budget.get(motif_id, 0) - 1)
        field_nodes = [primary, partner] if len(grammar.motif_by_id(motif_id).input_roles) == 2 and partner else [primary]
        hashes = _structure_hashes(motif_id, motif_fields(motif_id))
        if hashes is not None:
            grammar_key, semantic_key = hashes
            used_structures["grammar"][grammar_key] = used_structures["grammar"].get(grammar_key, 0) + 1
            used_structures["semantic"][semantic_key] = used_structures["semantic"].get(semantic_key, 0) + 1

        reason = "under-tested semantic niche"
        if effective_mode == "explore":
            reason = "unseen motif for this campaign" if motif_id not in context.motifs else "sparse archive niche"
        elif effective_mode == "exploit":
            if motif_id in proven.get(slot_family, set()):
                reason = "proven motif on a new source"
            elif motif_id in proven_anywhere:
                reason = "proven structure transferred to a new dataset"
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
            recipe_index=index % 8,
            reason=reason,
            fields=tuple(node.field_id for node in field_nodes),
            datasets=tuple(sorted({node.dataset for node in field_nodes})),
            parent_ids=parent_ids,
            target_family=target_family,
            mutation_operation=mutation_operation,
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
    )


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
