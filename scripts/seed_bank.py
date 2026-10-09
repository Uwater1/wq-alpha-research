"""Point-in-time seed bank and the structural distance ladder (P19).

The P18 diagnosis is that V3 searches structurally diverse but economically empty regions:
153 of its 204 simulations share a single-blob ``group_rank`` shape and none of them pass,
while every well-sampled multi-component composite passes. The roadmap's control experiment is
therefore not "generate more" but "can V3 reproduce and search *near* a region where the
platform already said yes?".

This module supplies the control surface:

* :func:`build_seed_bank` — historical candidates that had reached an IS/CORR gate **before**
  the target campaign clock, tagged with motif, source profile, skeleton identity, recipe,
  settings and the outcome stage they reached. Only evidence available at that time is used.
* :func:`distance_band` — the D0–D4 ladder from a proven seed (exact / parameter-only / one
  structural edit / source transfer / semantic or motif change), measured on the ordered
  operator sequence rather than a blended distance.
* :func:`distance_outcomes` — pass probability versus distance, measured over the live ledger
  so the ladder is evidence rather than a hypothesis.
* :func:`proven_recipe_prior` — the recipe cells proven seeds actually occupy, which is what
  the exploitation policy should sample instead of the uniform grid.

``build_seed_bank`` returns in-memory records that keep the private expression: the seed bank
is rebuilt from ``research.db`` at campaign time and is never written to a tracked file.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import canonical  # noqa: E402
import diversity  # noqa: E402
import expression_grammar as grammar  # noqa: E402
import research_db  # noqa: E402

SEED_BANK_VERSION = "seed-bank-v1"
#: Stages that mean the platform accepted the alpha's economics: these are the proven regions.
PASS_STAGES = ("IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE")
CORR_STAGES = ("CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE")
#: The ladder, coarse to fine. ``D0`` is the exact control (the seed itself).
BANDS = ("D0", "D1", "D2", "D3", "D4")
BAND_DESCRIPTIONS = {
    "D0": "exact control: the same expression and settings as the seed",
    "D1": "parameter-only: same topology and same sources, only recipe/settings moved",
    "D2": "one structural edit: same sources, the operator tree changed by <= 2 edits",
    "D3": "source transfer: same topology, at least one source substituted",
    "D4": "semantic/motif change: a different topology and different sources",
}
#: A single structural edit, tolerant of one incidental wrapper change (P19.3).
D2_MAX_OPERATOR_EDITS = 2
#: Recipe dimensions an exploitation step may move one at a time (P22.1).
RECIPE_DIMENSIONS = (
    "lookback", "smoothing_window", "decay", "neutralization", "group_level",
    "truncation", "normalization", "winsorization", "sign",
)


@dataclass(frozen=True)
class Seed:
    """One proven candidate, with everything known about it at the campaign clock."""

    candidate_id: int
    expression: str
    settings: Mapping[str, Any]
    stage: str
    settled_at: str
    sharpe: float | None
    fitness: float | None
    turnover: float | None
    motif_id: str
    outer_operator: str
    grammar_skeleton_hash: str
    semantic_skeleton_hash: str
    fields: tuple[str, ...]
    datasets: tuple[str, ...]
    categories: tuple[str, ...]
    recipe: Mapping[str, Any] = dataclass_field(default_factory=dict)
    generator_version: str = ""
    campaign_id: str = ""

    @property
    def quality(self) -> float:
        """Point-in-time quality used to order seeds; a gate pass outranks any raw metric."""
        sharpe = float(self.sharpe or 0.0)
        fitness = float(self.fitness or 0.0)
        turnover = float(self.turnover or 0.0)
        stage_bonus = 1.0 if self.stage in CORR_STAGES else 0.5
        return round(stage_bonus + sharpe + 0.5 * fitness - 0.25 * abs(turnover - 0.1), 6)

    @property
    def canonical_key(self) -> str:
        return canonical.canonical_key(self.expression, self.settings)

    def public(self) -> dict[str, Any]:
        """Everything except the private expression and its settings values."""
        return {
            "candidate_id": self.candidate_id,
            "stage": self.stage,
            "settled_at": self.settled_at,
            "sharpe": self.sharpe,
            "fitness": self.fitness,
            "turnover": self.turnover,
            "motif_id": self.motif_id,
            "outer_operator": self.outer_operator,
            "grammar_skeleton_hash": self.grammar_skeleton_hash,
            "semantic_skeleton_hash": self.semantic_skeleton_hash,
            "fields": list(self.fields),
            "datasets": list(self.datasets),
            "categories": list(self.categories),
            "recipe": dict(self.recipe),
            "generator_version": self.generator_version,
            "campaign_id": self.campaign_id,
        }


def _json_map(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


#: ``settings_json`` keys that are a recipe dimension under a different name: legacy rows
#: predate the recipe record, so a proven seed's settings are its recipe.
RECIPE_SETTING_KEYS = ("decay", "truncation", "neutralization")
_FIRST_WINDOW_RE = re.compile(r"(?<![\w.])(\d{2,3})(?![\w.])")


def recipe_from_settings(expression: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct the recipe of a legacy row from its settings and its first window.

    Without this the proven-seed recipe prior is empty for every V2-era seed — which is
    exactly the population the exploitation policy needs to sample from (P21.3/P22.1).
    """
    recipe: dict[str, Any] = {}
    for key in RECIPE_SETTING_KEYS:
        if settings.get(key) is not None:
            recipe[key] = settings[key]
    match = _FIRST_WINDOW_RE.search(str(expression or ""))
    if match and 2 <= int(match.group(1)) <= 504:
        recipe.setdefault("lookback", int(match.group(1)))
    return recipe


def _outer_operator(expression: str) -> str:
    try:
        node = grammar.parse_expression(expression, None)
    except grammar.GrammarError:
        node = None
    return str(node.operator).lower() if isinstance(node, grammar.CallNode) else "unknown"


def seed_from_candidate(row: Mapping[str, Any]) -> Seed:
    """Tag one candidate row as a seed using only pre-campaign evidence."""
    expression = str(row.get("normalized_expression") or "")
    parameters = _json_map(row.get("mutation_parameters_json"))
    settings = _json_map(row.get("settings_json"))
    recipe = _json_map(row.get("recipe_json")) or _json_map(parameters.get("recipe"))
    if not recipe:
        recipe = recipe_from_settings(expression, settings)
    profile = _json_map(row.get("source_profile_json")) or diversity.derive_source_profile(expression)
    fields = tuple(str(name) for name in (profile.get("field_ids") or canonical.fields_of(expression)))
    return Seed(
        candidate_id=int(row.get("id") or 0),
        expression=expression,
        settings=settings,
        stage=str(row.get("status") or ""),
        settled_at=str(row.get("completed_at") or row.get("updated_at") or ""),
        sharpe=row.get("sharpe"),
        fitness=row.get("fitness"),
        turnover=row.get("turnover"),
        motif_id=str(row.get("motif_id") or parameters.get("motif_id") or "none"),
        outer_operator=_outer_operator(expression),
        grammar_skeleton_hash=str(row.get("grammar_skeleton_hash") or grammar.grammar_skeleton_hash(expression)),
        semantic_skeleton_hash=str(row.get("semantic_skeleton_hash") or grammar.semantic_skeleton_hash(expression)),
        fields=fields,
        datasets=tuple(str(name) for name in (profile.get("datasets") or [])),
        categories=tuple(str(name) for name in (profile.get("categories") or [])),
        recipe=recipe,
        generator_version=str(row.get("generator_version") or ""),
        campaign_id=str(row.get("campaign_id") or ""),
    )


def build_seed_bank(
    db: research_db.ResearchDB,
    *,
    as_of: str | None = None,
    stages: Sequence[str] = PASS_STAGES,
    scope: Mapping[str, Any] | None = None,
    datasets: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[Seed]:
    """Proven candidates as of ``as_of``, best first.

    Point-in-time safety is the whole point: a seed must have been *already* accepted before
    the campaign clock, otherwise the control experiment would be warm-started from the
    future. The settlement timestamp is the simulation's ``completed_at``, never ``updated_at``
    alone, so a candidate that only reached its stage later cannot leak in.
    """
    stages = tuple(stages)
    placeholders = ",".join("?" for _ in stages)
    rows = db.query(
        "SELECT c.*, s.completed_at AS completed_at FROM candidates c"
        " LEFT JOIN simulations s ON s.id=(SELECT id FROM simulations WHERE candidate_id=c.id ORDER BY id DESC LIMIT 1)"
        f" WHERE c.status IN ({placeholders})",
        tuple(stages),
    )
    seeds: list[Seed] = []
    for row in rows:
        seed = seed_from_candidate(row)
        if not seed.expression:
            continue
        if as_of and str(seed.settled_at) > as_of:
            continue
        if scope:
            settings = canonical.normalize_settings(seed.settings)
            if any(settings.get(key) != value for key, value in scope.items()):
                continue
        if datasets and not (set(seed.datasets) & set(datasets)):
            continue
        seeds.append(seed)
    seeds.sort(key=lambda seed: (-seed.quality, seed.candidate_id))
    return seeds[:limit] if limit else seeds


def proven_recipe_counts(
    db: research_db.ResearchDB,
    *,
    as_of: str | None = None,
    stages: Sequence[str] = PASS_STAGES,
    scope: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, int]]:
    """Recipe value counts among gate-reaching candidates, straight from the ledger (P21.3).

    The same evidence as :func:`proven_recipe_prior` without materializing seed records: one
    query, and each recipe is the stored one or reconstructed from settings plus the first
    window. This is what lets *ordinary* (non-warm-started) generation learn that truncation
    ``0.08`` is a value the platform accepted, instead of sampling a grid that cannot express it.
    """
    stages = tuple(stages)
    if not stages:
        return {}
    placeholders = ", ".join("?" for _ in stages)
    rows = db.query(
        "SELECT c.id, c.normalized_expression, c.settings_json, c.mutation_parameters_json,"
        " t.recipe_json AS recipe_json, s.completed_at AS completed_at FROM candidates c"
        " LEFT JOIN simulations s ON s.id=(SELECT id FROM simulations WHERE candidate_id=c.id ORDER BY id DESC LIMIT 1)"
        " LEFT JOIN research_trials t ON t.id=(SELECT id FROM research_trials WHERE candidate_id=c.id ORDER BY id DESC LIMIT 1)"
        f" WHERE c.status IN ({placeholders})",
        tuple(stages),
    )
    prior: dict[str, dict[str, int]] = {}
    for row in rows:
        if as_of and str(row.get("completed_at") or "") > as_of:
            continue
        settings = _json_map(row.get("settings_json"))
        if scope:
            normalized = canonical.normalize_settings(settings)
            if any(normalized.get(key) != value for key, value in scope.items()):
                continue
        parameters = _json_map(row.get("mutation_parameters_json"))
        recipe = _json_map(row.get("recipe_json")) or _json_map(parameters.get("recipe"))
        if not recipe:
            recipe = recipe_from_settings(str(row.get("normalized_expression") or ""), settings)
        for dimension, value in recipe.items():
            if value is None:
                continue
            counts = prior.setdefault(str(dimension), {})
            key = str(value)
            counts[key] = counts.get(key, 0) + 1
    return prior


def distance_band(seed: Seed | Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    """Where ``candidate`` sits on the ladder from ``seed`` (P19.3).

    Bands are decided on three independent facts in this order — exact identity, the
    topology-preserving grammar skeleton, the source set, then the unblended operator-sequence
    edit count — so the same pair always lands in the same band whatever else differs.
    """
    seed_expression = str(getattr(seed, "expression", "") or (seed or {}).get("expression") or "")
    seed_settings = getattr(seed, "settings", None) or (seed or {}).get("settings") or {}
    if not seed_expression:
        raise ValueError("distance_band needs a seed with an expression")
    child_expression = str(
        candidate.get("normalized_expression") or candidate.get("expression") or ""
    )
    if not child_expression:
        raise ValueError("distance_band needs a candidate expression")
    child_settings = _json_map(candidate.get("settings_json")) or dict(candidate.get("settings") or {})
    if canonical.canonical_key(seed_expression, seed_settings) == canonical.canonical_key(
        child_expression, child_settings
    ):
        return "D0"

    seed_grammar = str(
        getattr(seed, "grammar_skeleton_hash", "") or grammar.grammar_skeleton_hash(seed_expression)
    )
    child_grammar = str(
        candidate.get("grammar_skeleton_hash") or grammar.grammar_skeleton_hash(child_expression)
    )
    seed_semantic = str(
        getattr(seed, "semantic_skeleton_hash", "") or grammar.semantic_skeleton_hash(seed_expression)
    )
    child_semantic = str(
        candidate.get("semantic_skeleton_hash") or grammar.semantic_skeleton_hash(child_expression)
    )
    seed_fields = set(getattr(seed, "fields", ())) or set(
        diversity.derive_source_profile(seed_expression).get("field_ids") or canonical.fields_of(seed_expression)
    )
    # ``canonical.fields_of`` counts group literals (``subindustry``) as identifiers, so the
    # source set must come from the profile — derived when the row predates the stored one.
    child_profile = _json_map(candidate.get("source_profile_json")) or diversity.derive_source_profile(
        child_expression
    )
    child_fields = set(child_profile.get("field_ids") or canonical.fields_of(child_expression))

    # The grammar skeleton keeps numeric literals, so a window change moves it. The ordered
    # operator sequence does not, which is what makes "parameter-only" decidable.
    edits = grammar.operator_edit_distance(seed_expression, child_expression)
    if seed_fields == child_fields:
        if edits == 0:
            return "D1"
        # Same sources, the operator tree moved by at most a couple of edits.
        if edits <= D2_MAX_OPERATOR_EDITS:
            return "D2"
        return "D4"
    if edits == 0 or seed_grammar == child_grammar:
        # Same structure, different sources: a transfer of a proven shape onto new data.
        return "D3"
    return "D4"


def ladder_cell(band: str, quality: Mapping[str, float], *, min_sample: int) -> dict[str, Any]:
    """Pass rate for one rung, with the same minimum-sample discipline as P18."""
    simulated = int(quality.get("simulations") or 0)
    passes = int(quality.get("is_pass") or 0)
    return {
        "band": band,
        "description": BAND_DESCRIPTIONS.get(band, ""),
        "attempts": int(quality.get("attempts") or 0),
        "simulations": simulated,
        "is_pass": passes,
        "is_pass_rate": round(passes / simulated, 6) if simulated else None,
        "low_confidence": simulated < min_sample,
    }


def distance_outcomes(
    db: research_db.ResearchDB,
    *,
    as_of: str | None = None,
    stages: Sequence[str] = PASS_STAGES,
    min_sample: int = 5,
    child_versions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Measured pass probability per ladder rung over the live ledger (P19.3).

    Only children whose *parent* was already a proven seed at the time count, so the row says
    something about exploitation from a proven region rather than about generation in general.

    ``child_versions`` restricts the child population to one generator. Without it the rung
    rates mix every campaign that ever mutated a proven alpha, which makes the ladder look
    better than the generator actually under test.
    """
    seeds = {seed.candidate_id: seed for seed in build_seed_bank(db, as_of=as_of, stages=stages)}
    if not seeds:
        return {"bands": [], "seeds": 0, "version": SEED_BANK_VERSION}
    rows = db.query(
        "SELECT c.*, s.is_pass AS sim_is_pass, s.status AS sim_status FROM candidates c"
        " LEFT JOIN simulations s ON s.id=(SELECT id FROM simulations WHERE candidate_id=c.id ORDER BY id DESC LIMIT 1)"
    )
    if child_versions:
        wanted = {str(version) for version in child_versions}
        rows = [row for row in rows if str(row.get("generator_version") or "") in wanted]
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unlinked = 0
    for row in rows:
        try:
            parent_ids = [int(value) for value in json.loads(str(row.get("parent_ids_json") or "[]"))]
        except ValueError:
            parent_ids = []
        seed = next((seeds[pid] for pid in parent_ids if pid in seeds), None)
        if seed is None:
            unlinked += 1
            continue
        band = distance_band(seed, row)
        simulated = str(row.get("sim_status") or "") == "DONE"
        passed = bool(row.get("sim_is_pass")) or str(row.get("status") or "") in PASS_STAGES
        buckets[band].append({"simulated": simulated, "is_pass": passed})
    cells = []
    for band in BANDS:
        members = buckets.get(band, [])
        simulated = [row for row in members if row["simulated"]]
        cells.append(ladder_cell(band, {
            "attempts": len(members),
            "simulations": len(simulated),
            "is_pass": sum(1 for row in simulated if row["is_pass"]),
        }, min_sample=min_sample))
    return {
        "bands": cells,
        "seeds": len(seeds),
        "children_without_a_seed_parent": unlinked,
        "child_versions": sorted(child_versions) if child_versions else "all",
        "min_sample": min_sample,
        "version": SEED_BANK_VERSION,
    }


#: How much a child's measured quality has to fall below its parent's before the edit is
#: counted as destroying parent quality (P22.1). Zero means "any drop counts".
DESTRUCTION_TOLERANCE = 0.0


def _pair_quality(simulation: Mapping[str, Any] | None) -> tuple[int, float] | None:
    """A comparable quality for parent/child: gate stage first, then Sharpe as the tiebreak."""
    if not simulation or str(simulation.get("status") or "") != "DONE":
        return None
    sharpe = simulation.get("sharpe")
    if not isinstance(sharpe, (int, float)) or isinstance(sharpe, bool):
        return None
    return (1 if simulation.get("is_pass") else 0, float(sharpe))


def operation_quality_retention(
    db: research_db.ResearchDB,
    *,
    child_versions: Sequence[str] | None = None,
    min_sample: int = 3,
) -> dict[str, dict[str, Any]]:
    """Per mutation operation: how often a live child kept its parent's quality (P22.1).

    ``allocate_mutation_operations`` ranks edits by raw pass count, which cannot see the
    difference between an edit that discovers a *new* good region and one that keeps spending
    slots while dismantling whatever the parent had. This measures the second directly, from
    settled lineage pairs only: a child's quality is the (gate stage, Sharpe) pair, and the
    operation is charged with a destruction whenever the child drops below its parent.
    """
    wanted = {str(value) for value in child_versions} if child_versions else None
    rows = db.query("SELECT * FROM candidates")
    by_id = {int(row["id"]): row for row in rows}
    latest: dict[int, dict[str, Any]] = {}
    for simulation in db.query("SELECT * FROM simulations ORDER BY id"):
        if simulation.get("candidate_id") is not None:
            latest[int(simulation["candidate_id"])] = dict(simulation)  # later rows win
    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # operation -> [children, retained]
    for row in rows:
        if wanted is not None and str(row.get("generator_version") or "") not in wanted:
            continue
        parameters = _json_map(row.get("mutation_parameters_json"))
        operation = canonical.normalize_mutation_operation(
            parameters.get("realized_operation") or parameters.get("operation")
            or row.get("mutation_type")
        )
        if not operation:
            continue
        try:
            parent_ids = [int(value) for value in json.loads(str(row.get("parent_ids_json") or "[]"))]
        except (TypeError, ValueError):
            parent_ids = []
        if not parent_ids:
            continue
        child_quality = _pair_quality(latest.get(int(row["id"])))
        parent_quality = _pair_quality(latest.get(parent_ids[0]))
        if child_quality is None or parent_quality is None:
            continue
        entry = tally[operation]
        entry[0] += 1
        if child_quality[0] > parent_quality[0] or (
            child_quality[0] == parent_quality[0]
            and child_quality[1] >= parent_quality[1] - DESTRUCTION_TOLERANCE
        ):
            entry[1] += 1
    report: dict[str, dict[str, Any]] = {}
    for operation, (children, retained) in sorted(tally.items()):
        rate = retained / children if children else None
        report[operation] = {
            "children": children,
            "retained": retained,
            "retention": rate,
            "destruction": (1.0 - rate) if rate is not None else None,
            "low_confidence": children < int(min_sample),
        }
    return report


def proven_recipe_prior(seeds: Sequence[Seed]) -> dict[str, dict[str, int]]:
    """Value counts per recipe dimension among proven seeds (P21.3 / P22.1).

    This is the empirical answer to "which recipe cells did the platform actually accept", and
    it replaces uniform sampling over a grid that was never observed to work.
    """
    prior: dict[str, dict[str, int]] = {}
    for dimension in RECIPE_DIMENSIONS:
        counts: Counter[str] = Counter()
        for seed in seeds:
            value = seed.recipe.get(dimension)
            if value is not None:
                counts[str(value)] += 1
        if counts:
            prior[dimension] = dict(counts.most_common())
    return prior


#: Recipe dimensions that are visible in BRAIN settings, so a D1 edit can move them without
#: touching the expression at all.
SETTINGS_RECIPE_DIMENSIONS = ("decay", "truncation", "neutralization")


def perturb_recipe(
    recipe: Mapping[str, Any],
    rng: random.Random,
    *,
    magnitude: int = 1,
    dimensions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Move exactly one recipe dimension one step, keeping the seed's economic core (P22.1).

    ``magnitude`` may be -1 (the previous grid value) or +1, so exploitation can search both
    directions around a proven recipe without ever changing two things at once. ``dimensions``
    restricts the edit, which is what makes a parameter-only D1 child actually differ.
    """
    grid = {
        "lookback": (20, 60, 126, 252),
        "smoothing_window": (5, 10, 22),
        "decay": (4, 6, 10, 20),
        "truncation": (0.05, 0.08, 0.1, 0.15),
        "neutralization": ("SUBINDUSTRY", "INDUSTRY", "SECTOR", "MARKET"),
        "group_level": ("subindustry", "industry", "sector", "market"),
        "normalization": ("rank", "zscore"),
        "winsorization": (False, True),
        "sign": (1, -1),
    }
    updated = dict(recipe)
    allowed = set(dimensions) if dimensions is not None else set(RECIPE_DIMENSIONS)
    movable = [name for name in RECIPE_DIMENSIONS if name in grid and name in allowed]
    if not movable:
        return updated
    rng.shuffle(movable)
    for name in movable:
        options = grid[name]
        current = updated.get(name)
        if current is None:
            updated[name] = options[0]
            return updated
        try:
            index = list(options).index(current)
        except ValueError:
            index = 0
        step = -abs(int(magnitude)) if rng.random() < 0.5 else abs(int(magnitude))
        target = index + step
        if target < 0 or target >= len(options):
            target = index - step
        target = max(0, min(len(options) - 1, target))
        if target == index:
            continue
        updated[name] = options[target]
        return updated
    return updated


def substitute_field_choice(
    seeds_fields: Sequence[str],
    catalog: Any,
    rng: random.Random,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[str, str] | None:
    """``(field to replace, replacement)`` for a compatible source substitution (P19.2).

    Compatibility means the same field type and at least one shared semantic role, preferring
    a *different* dataset: substituting a price field for an analyst estimate is not a source
    transfer, it is a different hypothesis. Returning the replaced field as well lets a D3
    child be a real one-field edit instead of a guess about which source moved.
    """
    metadata = metadata or diversity.catalog_metadata(catalog)
    if not seeds_fields:
        return None
    target = str(seeds_fields[rng.randrange(len(seeds_fields))])
    source = metadata.get(target) or metadata.get(target.lower())
    if source is None:
        return None
    source_dataset = str(getattr(source, "dataset", "unknown"))
    source_type = str(getattr(source, "field_type", grammar.MATRIX)).upper()
    source_roles = set(grammar.infer_field_roles(target))
    candidates: list[tuple[int, str]] = []
    for name, field in metadata.items():
        if name in set(seeds_fields):
            continue
        if str(getattr(field, "field_type", "")).upper() != source_type:
            continue
        roles = set(grammar.infer_field_roles(name))
        if not (roles & source_roles):
            continue
        different_dataset = str(getattr(field, "dataset", "unknown")) != source_dataset
        candidates.append((0 if different_dataset else 1, str(name)))
    if not candidates:
        return None
    candidates.sort()
    window = candidates[: max(1, min(4, len(candidates)))]
    return target, window[rng.randrange(len(window))][1]


def substitute_field(
    seeds_fields: Sequence[str],
    catalog: Any,
    rng: random.Random,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> str | None:
    """Just the replacement field of :func:`substitute_field_choice`, or ``None``."""
    choice = substitute_field_choice(seeds_fields, catalog, rng, metadata=metadata)
    return None if choice is None else choice[1]


def bank_summary(seeds: Sequence[Seed]) -> dict[str, Any]:
    """A sanitized summary of a bank: counts and distributions, never an expression."""
    by_stage: Counter[str] = Counter(seed.stage for seed in seeds)
    by_dataset: Counter[str] = Counter(dataset for seed in seeds for dataset in (seed.datasets or ["unknown"]))
    by_motif: Counter[str] = Counter(seed.motif_id for seed in seeds)
    by_version: Counter[str] = Counter(seed.generator_version or "unknown" for seed in seeds)
    by_operator: Counter[str] = Counter(seed.outer_operator for seed in seeds)
    return {
        "version": SEED_BANK_VERSION,
        "seeds": len(seeds),
        "by_stage": dict(by_stage.most_common()),
        "by_dataset": dict(by_dataset.most_common(20)),
        "by_motif": dict(by_motif.most_common(20)),
        "by_outer_operator": dict(by_operator.most_common(20)),
        "by_generator_version": dict(by_version.most_common()),
        "recipe_prior": proven_recipe_prior(seeds),
        "grammar_skeletons": len({seed.grammar_skeleton_hash for seed in seeds}),
        "semantic_skeletons": len({seed.semantic_skeleton_hash for seed in seeds}),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Point-in-time seed bank and distance ladder (P19)")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--as-of", dest="as_of", help="campaign clock (ISO); the bank uses only earlier evidence")
    parser.add_argument("--corr-only", action="store_true", help="restrict to seeds that reached correlation")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ladder", action="store_true", help="also measure pass rate by distance band")
    parser.add_argument("--child-version", action="append", dest="child_versions",
                        help="restrict the ladder's children to this generator (repeatable)")
    parser.add_argument("--min-sample", type=int, default=5)
    args = parser.parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        seeds = build_seed_bank(
            db, as_of=args.as_of, stages=CORR_STAGES if args.corr_only else PASS_STAGES,
            limit=args.limit,
        )
        report: dict[str, Any] = {"bank": bank_summary(seeds), "as_of": args.as_of or "now"}
        if args.ladder:
            report["distance_ladder"] = distance_outcomes(
                db, as_of=args.as_of, min_sample=args.min_sample,
                child_versions=args.child_versions,
            )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
