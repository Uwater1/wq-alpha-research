"""Catalog-driven candidate generation and explicit mutation operators.

This module deliberately contains no BRAIN calls and no LLM trust boundary. It turns the
local field/operator snapshots into reproducible proposals, then hands every proposal to
ResearchDB so validation, canonical deduplication, privacy, lineage, and the permanent
trial ledger are centralized. The catalog is scope-limited; generated candidates retain
the catalog version *and* the scope they were generated for.

Mutation operators are structured: every child records its ``mutation_type`` and the
parameters that produced it, so a repair can be reviewed later instead of being an opaque
new expression. Failure-directed repair dispatches on the diagnosed failure mode
(turnover, Sharpe, Fitness, weight concentration, sub-universe, correlation) rather than
perturbing numbers and hoping.

Coverage-aware ordering is a *primary* sort: fields are ordered by how often they have
already been attempted, and the seeded random draw only breaks ties inside one
coverage bucket. A field that has been tested many times can never jump ahead of an
unseen field because of a shuffle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sqlite3
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical
import compatibility
import diversity
import expression_grammar as grammar
import generation_policy
import research_db

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO_ROOT / "references" / "wq_usa_top3000_delay1_data_fields.json"
#: Canonical generator identity (P11).
GENERATOR_VERSION = "catalog-generator-v3"
#: The template generator that produced V2 campaigns. It is still the CLI default until replay
#: evidence justifies promotion (P16), and rows it produces are labelled honestly as V2.
LEGACY_GENERATOR_VERSION = "catalog-generator-v2"
#: Alias kept for callers that name the V3 identity explicitly.
GENERATOR_VERSION_V3 = GENERATOR_VERSION
#: Version stamps for the sub-systems that can change proposal distribution independently.
GRAMMAR_VERSION = grammar.GRAMMAR_VERSION
MOTIF_REGISTRY_VERSION = grammar.MOTIF_REGISTRY_VERSION
GENERATION_POLICY_VERSION = generation_policy.GENERATION_POLICY_VERSION
#: V3 complexity budget: the same shape limits as the recommended defaults, with room for a
#: structured two-source motif (e.g. normalized_difference needs three binary operators).
V3_LIMITS = grammar.ComplexityLimits(max_depth=5, max_nodes=16, max_fields=2, max_binary_ops=3)
#: Crossover composes two complete parent trees, so it gets one extra layer of headroom.
#: Absolute ceiling for a crossover child, whatever its parents look like. The relative budget
#: below lets two evolved elites be combined; this cap keeps the result bounded.
CROSSOVER_LIMITS = grammar.ComplexityLimits(max_depth=8, max_nodes=40, max_fields=6, max_binary_ops=6)


def _crossover_limits(left: grammar.ExprNode, right: grammar.ExprNode) -> grammar.ComplexityLimits:
    """Budget a crossover child against its parents instead of a fresh-generation budget.

    Archive elites have usually been mutated once or twice, so a fixed 16-node budget refuses
    almost every real pair and silently turns the whole crossover allocation into exploration.
    The child is allowed what its parents need plus a small margin, never more than
    ``CROSSOVER_LIMITS``.
    """
    return grammar.ComplexityLimits(
        max_depth=min(CROSSOVER_LIMITS.max_depth, max(grammar.node_depth(left), grammar.node_depth(right)) + 2),
        max_nodes=min(CROSSOVER_LIMITS.max_nodes, grammar.node_count(left) + grammar.node_count(right) + 4),
        max_fields=min(CROSSOVER_LIMITS.max_fields,
                       len(grammar.source_fields(left)) + len(grammar.source_fields(right))),
        max_binary_ops=min(CROSSOVER_LIMITS.max_binary_ops,
                           grammar.binary_op_count(left) + grammar.binary_op_count(right) + 2),
    )
#: Motifs tried, in order, when an ineligible/over-budget motif cannot be materialized.
FALLBACK_MOTIFS = (
    "cross_sectional_level", "change", "ranked_level", "group_relative", "time_series_level",
    "difference_of_ranks", "spread", "confirming_signals",
)
#: Initial crossover forms (P6 groundwork): typed compositions of two parent expressions.
CROSSOVER_FORMS = ("add_rank", "subtract_rank", "add_zscore", "multiply_rank")
DEFAULT_WINDOWS = (20, 60, 126, 252)
DEFAULT_DECAYS = (4, 6, 10, 20)
DEFAULT_NEUTRALIZATIONS = ("SUBINDUSTRY", "INDUSTRY", "SECTOR")
#: Groups a repair can broaden a signal to, from narrowest to widest.
GROUP_BROADENING = ("subindustry", "industry", "sector", "market")
SMOOTHING_WINDOWS = (5, 10, 22)

#: Concrete V3 structural mutation operations (P4.4). Every one is a typed AST edit that is
#: re-validated against the operator catalog and a parent-scaled complexity budget before a
#: child exists, and every child records which edit produced it in ``parameters["operation"]``.
V3_MUTATION_OPERATIONS = (
    "dataset_swap", "motif_change", "normalization_change", "group_change",
    "subtree_replace", "add_component",
)
#: Normalizers ``normalization_change`` flips between (same arity, so the swap is type-safe).
NORMALIZATION_SWAPS: dict[str, str] = {
    "rank": "zscore", "zscore": "rank",
    "group_rank": "group_zscore", "group_zscore": "group_rank",
}
#: ``group_change`` moves a group literal one step around this cycle.
GROUP_CYCLE: dict[str, str] = {
    "subindustry": "industry", "industry": "sector", "sector": "market", "market": "subindustry",
}
#: Legacy operation spellings normalized into the stable vocabulary before aggregation, so
#: ledger rows written before the rename still count as the same concrete edit.
MUTATION_OPERATION_ALIASES: dict[str, str] = {
    "combine_signals": "add_component",
    "signal_combination": "add_component",
}

#: Diagnosed failure mode -> structured mutation type emitted by the repair dispatch.
FAILURE_MUTATION_TYPES: dict[str, str] = {
    "HIGH_TURNOVER": "turnover_repair",
    "LOW_TURNOVER": "low_turnover_repair",
    "LOW_SHARPE": "sharpe_repair",
    "LOW_FITNESS": "fitness_repair",
    "CONCENTRATED_WEIGHT": "concentration_repair",
    "LOW_SUB_UNIVERSE_SHARPE": "sub_universe_repair",
    "SELF_CORRELATION": "correlation_repair",
    "CORR_FAIL": "correlation_repair",
}

_WINDOW_RE = re.compile(r"(?<![\w.])(\d{2,4})(?![\w.])")
_HUMP_RE = re.compile(r"hump\s*\(", re.IGNORECASE)
_FIRST_CALL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*\(")


@dataclass(frozen=True)
class Field:
    name: str
    category: str
    dataset: str
    field_type: str
    coverage: float | None


@dataclass(frozen=True)
class SignalTemplate:
    """One structural shape a signal value can be rendered into.

    ``marker`` is the operator substring that identifies the shape inside an
    existing expression, so a correlation repair can pick a template the parent
    does not already use.
    """

    id: str
    marker: str
    pattern: str

    def render(self, value: str, window: int) -> str:
        return self.pattern.format(v=value, w=window)


#: Structural shapes used across generation and repair. A book built from one
#: template correlates with itself no matter which fields it consumes, so field
#: choice alone cannot clear the self-correlation gate; shapes rotate with it.
#: Every operator here is in the local catalog and every group argument is a
#: valid ``Unit[Group]`` literal, so rendered expressions pass static validation.
SIGNAL_TEMPLATES = (
    SignalTemplate("group_ts_rank", "group_rank(ts_rank(",
                   "group_rank(ts_rank({v}, {w}), subindustry)"),
    SignalTemplate("group_ts_mean_zscore", "group_zscore(",
                   "group_zscore(ts_mean({v}, {w}), subindustry)"),
    SignalTemplate("neutral_ts_zscore", "ts_zscore(",
                   "group_neutralize(ts_zscore({v}, {w}), subindustry)"),
    SignalTemplate("group_ts_av_diff", "ts_av_diff(",
                   "group_rank(ts_av_diff({v}, {w}), subindustry)"),
    SignalTemplate("winsorized_delta", "winsorize(",
                   "winsorize(zscore(ts_delta({v}, {w})), std=4)"),
    SignalTemplate("smoothed_rank", "ts_decay_linear(",
                   "rank(ts_decay_linear({v}, {w}))"),
)


def _template_by_id(template_id: str) -> SignalTemplate:
    for template in SIGNAL_TEMPLATES:
        if template.id == template_id:
            return template
    raise ValueError(f"unknown signal template {template_id!r}")


@dataclass(frozen=True)
class Proposal:
    expression: str
    settings: Mapping[str, Any]
    family: str
    mutation_type: str
    parameters: Mapping[str, Any]
    parent_ids: tuple[int, ...] = ()
    reason: str = "catalog coverage"
    #: Explicit generation when the caller knows it; otherwise derived from the parents.
    generation: int | None = None
    # -- Generator V3 provenance (all optional; V2 proposals simply leave them empty) --
    motif_id: str = ""
    recipe_index: int = 0
    generation_mode: str = ""
    strategy: str = ""
    source_profile: Mapping[str, Any] = field(default_factory=dict)
    grammar_skeleton_hash: str = ""
    semantic_skeleton_hash: str = ""
    recipe: Mapping[str, Any] = field(default_factory=dict)
    # -- novelty pre-screen (P7); empty means "not screened" --
    novelty_score: float = 0.0
    novelty_decision: str = ""
    skip_reason: str = ""


class Catalog:
    """Read-only view of the supplied BRAIN field snapshot."""

    def __init__(self, path: str | Path = CATALOG_PATH) -> None:
        self.path = Path(path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("field catalog must be a JSON list")
        self.fields = tuple(
            Field(
                name=str(item["id"]),
                category=str((item.get("category") or {}).get("id") or "unknown"),
                dataset=str((item.get("dataset") or {}).get("id") or "unknown"),
                field_type=str(item.get("type") or "MATRIX").upper(),
                coverage=float(item["coverage"]) if item.get("coverage") is not None else None,
            )
            for item in raw
            if isinstance(item, Mapping) and item.get("id")
        )
        if not self.fields:
            raise ValueError("field catalog is empty")
        self.version = hashlib.sha256(self.path.read_bytes()).hexdigest()[:16]
        #: The snapshot is scope-limited; every generated candidate records this scope.
        self.scope: dict[str, Any] = {"region": "USA", "universe": "TOP3000", "delay": 1}
        self.operator_version = compatibility.reference_version(compatibility.OPERATORS_PATH)
        self.field_types = {item.name.lower(): item.field_type for item in self.fields}
        self._by_name = {item.name: item for item in self.fields}
        #: Snapshot metadata recorded with every generated research trial, so a candidate
        #: stays interpretable after the reference files are refreshed.
        self.snapshot = self._snapshot_metadata()

    def select(self, family: str = "all", dataset: str | None = None) -> list[Field]:
        family = family.lower()
        selected = [field for field in self.fields if family in {"all", "*"} or field.category == family or field.dataset == family]
        if dataset:
            selected = [field for field in selected if field.dataset == dataset]
        return selected

    def get(self, name: str) -> Field | None:
        return self._by_name.get(name)

    def _snapshot_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "field_reference": self.path.name,
            "field_catalog_version": self.version,
            "field_count": len(self.fields),
            "operator_reference": compatibility.OPERATORS_PATH.name,
            "operator_catalog_version": self.operator_version,
            "operator_count": len(compatibility.constraints()),
            "scope": dict(self.scope),
        }
        summary_path = self.path.with_name(f"{self.path.stem}_summary.json")
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return metadata
        if isinstance(summary, Mapping):
            metadata["query"] = summary.get("query")
            metadata["pages"] = summary.get("pages")
        return metadata


class CandidateGenerator:
    """Deterministic templates plus explainable failure-directed mutations."""

    def __init__(self, db: research_db.ResearchDB, catalog: Catalog | None = None, *, seed: int = 0) -> None:
        self.db = db
        self.catalog = catalog or Catalog()
        self.seed = int(seed)

    # -- generation --------------------------------------------------------

    def proposals(
        self,
        *,
        count: int,
        family: str = "all",
        dataset: str | None = None,
        all_fields: bool = False,
        template: str | None = None,
        truncation: float | None = None,
    ) -> list[Proposal]:
        """``template``/``truncation`` pin the recipe (e.g. a proven structure applied
        to fresh drivers); both are recorded in the lineage parameters."""
        fields = self.catalog.select(family, dataset)
        fields = self.coverage_ordered(fields, rng=random.Random(self.seed))
        if not all_fields:
            fields = fields[: max(0, int(count))]
        proposals: list[Proposal] = []
        for field in fields:
            proposal = self._field_proposal(field, template=template, truncation=truncation)
            if proposal:
                proposals.append(proposal)
            if not all_fields and len(proposals) >= count:
                break
        return proposals if all_fields else proposals[:count]

    def coverage_ordered(self, fields: Sequence[Field], *, rng: random.Random | None = None) -> list[Field]:
        """Order by attempts, then by seeded tie-break *inside* one attempts bucket.

        The previous implementation shuffled the whole list after sorting by coverage,
        which destroyed the ordering it had just computed. Randomness is now a tie-breaker
        only, so a repeatedly tested field cannot jump ahead of never-tested fields.
        """
        rng = rng or random.Random(self.seed)
        attempts = self._attempt_counts(fields)
        buckets: dict[int, list[Field]] = {}
        for field in fields:
            buckets.setdefault(int(attempts.get(field.name, 0)), []).append(field)
        ordered: list[Field] = []
        for attempt_count in sorted(buckets):
            bucket = buckets[attempt_count]
            rng.shuffle(bucket)
            ordered.extend(bucket)
        return ordered

    def _attempt_counts(self, fields: Sequence[Field]) -> dict[str, int]:
        try:
            import field_intelligence
        except ImportError:  # pragma: no cover - module is part of this repo
            return {}
        try:
            return field_intelligence.coverage_attempts(self.db, fields, scope=self.catalog.scope)
        except (sqlite3.Error, ValueError):
            return {}

    def queue(self, campaign_id: str, proposals: Iterable[Proposal]) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for proposal in proposals:
            parent_ids = tuple(proposal.parent_ids)
            generation = proposal.generation
            if generation is None:
                generation = self.db.next_generation(parent_ids)
            v3 = bool(proposal.strategy)
            if proposal.novelty_decision == diversity.SKIP_REDUNDANT:
                # The pre-screen refused it: keep the decision auditable, spend no capacity.
                trial_id = self.db.record_generation_decision(
                    proposal.expression, proposal.settings, campaign_id=campaign_id,
                    decision=proposal.novelty_decision, skip_reason=proposal.skip_reason,
                    signal_family=proposal.family, generator_version=GENERATOR_VERSION_V3,
                    generator_strategy=proposal.strategy or None,
                    generation_mode=proposal.generation_mode or None,
                    motif_id=proposal.motif_id or None, recipe=dict(proposal.recipe) or None,
                    recipe_index=proposal.recipe_index if proposal.motif_id else None,
                    grammar_skeleton_hash=proposal.grammar_skeleton_hash or None,
                    semantic_skeleton_hash=proposal.semantic_skeleton_hash or None,
                    source_profile=dict(proposal.source_profile) or None,
                    generator_policy_version=generation_policy.GENERATION_POLICY_VERSION,
                    grammar_version=grammar.GRAMMAR_VERSION,
                )
                outcomes.append({
                    "expression": proposal.expression, "family": proposal.family,
                    "mutation_type": proposal.mutation_type, "motif_id": proposal.motif_id,
                    "generation_mode": proposal.generation_mode, "generation": None,
                    "action": "skipped_redundant", "candidate_id": None, "status": None,
                    "issues": [], "trial_id": trial_id,
                })
                continue
            parameters = {**proposal.parameters, "catalog_version": self.catalog.version}
            if proposal.motif_id:
                parameters.setdefault("motif_id", proposal.motif_id)
            if proposal.recipe_index:
                parameters.setdefault("recipe_index", proposal.recipe_index)
            if proposal.generation_mode:
                parameters.setdefault("generation_mode", proposal.generation_mode)
            if proposal.recipe:
                parameters.setdefault("recipe", dict(proposal.recipe))
            provenance: dict[str, Any] = {
                "scope": self.catalog.scope, "seed": self.seed, "snapshot": self.catalog.snapshot,
            }
            if v3:
                provenance.update({
                    "strategy": proposal.strategy,
                    "generation_mode": proposal.generation_mode,
                    "motif_id": proposal.motif_id,
                    "recipe_index": proposal.recipe_index,
                    "source_profile": dict(proposal.source_profile),
                    "grammar_skeleton_hash": proposal.grammar_skeleton_hash,
                    "semantic_skeleton_hash": proposal.semantic_skeleton_hash,
                    "policy_version": generation_policy.GENERATION_POLICY_VERSION,
                    "grammar_version": grammar.GRAMMAR_VERSION,
                    "motif_registry_version": grammar.MOTIF_REGISTRY_VERSION,
                })
            outcome = self.db.queue_candidate(
                proposal.expression,
                proposal.settings,
                source="generator",
                signal_family=proposal.family,
                parent_id=parent_ids[0] if parent_ids else None,
                parent_ids=parent_ids,
                generation=generation,
                mutation_type=proposal.mutation_type,
                campaign_id=campaign_id,
                mutation_parameters=parameters,
                generator_version=GENERATOR_VERSION if v3 else LEGACY_GENERATOR_VERSION,
                reason=proposal.reason,
                field_catalog_version=self.catalog.version,
                operator_catalog_version=self.catalog.operator_version,
                provenance=provenance,
                generator_strategy=proposal.strategy or None,
                generation_mode=proposal.generation_mode or None,
                motif_id=proposal.motif_id or None,
                recipe_index=proposal.recipe_index if proposal.motif_id else None,
                recipe=dict(proposal.recipe) if proposal.recipe else None,
                grammar_skeleton_hash=proposal.grammar_skeleton_hash or None,
                semantic_skeleton_hash=proposal.semantic_skeleton_hash or None,
                source_profile=dict(proposal.source_profile) if proposal.source_profile else None,
                generator_policy_version=generation_policy.GENERATION_POLICY_VERSION if v3 else None,
                grammar_version=grammar.GRAMMAR_VERSION if v3 else None,
                decision=proposal.novelty_decision or None,
                skip_reason=proposal.skip_reason or None,
            )
            outcomes.append({
                "expression": proposal.expression,
                "family": proposal.family,
                "mutation_type": proposal.mutation_type,
                "motif_id": proposal.motif_id,
                "generation_mode": proposal.generation_mode,
                "generation": generation,
                "action": outcome.action,
                "candidate_id": outcome.candidate_id,
                "status": outcome.status,
                "issues": outcome.issues,
            })
        return outcomes

    # -- Generator V3: campaign planning + materialization (P3/P4/P5) -------

    def metadata(self) -> dict[str, Field]:
        """Field metadata as ``name -> Field`` for the grammar parser."""
        return dict(self.catalog._by_name)

    def plan(
        self,
        *,
        campaign_id: str,
        budget: int,
        seed: int = 0,
        mode: str = "mixed",
        family: str | None = None,
        max_family_share: float = generation_policy.DEFAULT_MAX_FAMILY_SHARE,
        exploration_reserve: float = generation_policy.DEFAULT_EXPLORATION_RESERVE,
        parent_pool: int = 24,
    ) -> generation_policy.Plan:
        """Build an archive-informed, deterministic campaign plan (no BRAIN calls, no queue writes)."""
        return generation_policy.plan_campaign(
            self.db, self.catalog, campaign_id, budget, seed, mode,
            family=family, max_family_share=max_family_share,
            exploration_reserve=exploration_reserve, parent_pool=parent_pool,
            generator_version=GENERATOR_VERSION_V3,
        )

    def _material_field_node(self, name: str) -> grammar.FieldNode:
        field = self.catalog.get(name)
        return grammar.field_node_from(field, fallback_id=name)

    def _partner_fields(
        self,
        used: Sequence[grammar.FieldNode],
        family: str,
        count: int,
    ) -> list[grammar.FieldNode]:
        """Additional source fields for a two-source motif, preferring a different dataset.

        A cross-dataset motif must stay reachable even when the planned family is
        single-dataset: when the family pool holds no other-dataset partner, the global
        catalog is searched before any same-dataset fallback (P4.2).
        """
        used_ids = {node.field_id for node in used}
        datasets = {node.dataset for node in used}

        def usable(field: Field) -> bool:
            return (
                field.name not in used_ids
                and field.field_type in {compatibility.MATRIX, compatibility.VECTOR}
            )

        family_pool = sorted(
            (field for field in self.catalog.select(family) if usable(field)),
            key=lambda field: (field.dataset in datasets, field.dataset, field.name),
        )
        cross_family = [field for field in family_pool if field.dataset not in datasets]
        if cross_family:
            return [grammar.field_node_from(field) for field in cross_family[: max(0, count)]]
        global_cross = sorted(
            (field for field in self.catalog.fields if usable(field) and field.dataset not in datasets),
            key=lambda field: (field.dataset, field.name),
        )
        pool = global_cross or family_pool
        return [grammar.field_node_from(field) for field in pool[: max(0, count)]]

    def _fallback_motif(
        self,
        fields: Sequence[grammar.FieldNode],
        failed_motif: str,
        recipe: grammar.Recipe,
        limits: grammar.ComplexityLimits,
    ) -> tuple[str, grammar.CallNode] | None:
        for candidate in FALLBACK_MOTIFS:
            if candidate == failed_motif:
                continue
            if not grammar.motif_eligible(grammar.motif_by_id(candidate), fields,
                                          distinct_datasets=candidate == "cross_dataset_composite"):
                continue
            try:
                return candidate, grammar.build_motif(candidate, fields, recipe, limits=limits)
            except grammar.GrammarError:
                continue
        return None

    def materialize(
        self,
        slot: generation_policy.PlanSlot,
        *,
        campaign_id: str,
        seed: int = 0,
        limits: grammar.ComplexityLimits = V3_LIMITS,
        force_motif: str | None = None,
    ) -> Proposal | None:
        """Turn one planned slot into a validated, provenance-complete proposal.

        Mutation slots delegate to the existing failure-directed repair; crossover slots build
        a typed two-parent composition; everything else samples an independent recipe and builds
        the motif AST. A motif that cannot be realized falls back rather than dropping a slot.
        """
        strategy = slot.generation_mode
        motif_id = force_motif or slot.motif_id
        if force_motif is not None:
            strategy = "explore"
        if force_motif is None and slot.generation_mode == "mutate" and slot.parent_ids:
            parent = self.db.get_candidate(int(slot.parent_ids[0]))
            if parent:
                # A per-slot seed keeps two mutate slots on the same parent from producing the
                # same child (the repair path is deterministic in the generator seed alone).
                slot_seed = generation_policy.recipe_seed(campaign_id, seed, (), "mutate", slot.recipe_index, slot.parent_ids)
                mutator = CandidateGenerator(self.db, self.catalog, seed=slot_seed % (2 ** 31))
                child = mutator._mutate_child(parent, campaign_id=campaign_id)
                if child is not None:
                    operation = str(child.parameters.get("operation") or child.mutation_type)
                    return replace(
                        child, generation_mode="mutate", strategy="mutate",
                        motif_id=f"mutation:{operation}", recipe_index=slot.recipe_index,
                        source_profile=diversity.derive_source_profile(child.expression, self.catalog),
                        grammar_skeleton_hash=grammar.grammar_skeleton_hash(child.expression, self.metadata()),
                        semantic_skeleton_hash=grammar.semantic_skeleton_hash(child.expression, self.metadata()),
                    )
        # When a lineage-producing mode cannot be realized, the same slot is still materialized so
        # the planned budget equals the materialized count. The realized mode is then reported
        # honestly as exploration with no parent ids: a report must never count a child as a
        # crossover or a mutation of a parent that did not actually produce it.
        planned_mode = generation_policy.resolve_strategy(slot.generation_mode)
        realized_mode = planned_mode
        if force_motif is None and planned_mode == "mutate" and slot.parent_ids:
            # The repair path above declined this slot; it is exploration now.
            realized_mode = "explore"
        if force_motif is None and slot.generation_mode == "crossover" and len(slot.parent_ids) >= 2:
            proposal = self._crossover_proposal(slot, campaign_id=campaign_id, seed=seed)
            if proposal is not None:
                return proposal
            realized_mode = "explore"
        lineage_fallback = realized_mode != slot.generation_mode

        fields = [self._material_field_node(name) for name in slot.fields if name]
        needed = len(grammar.motif_by_id(motif_id).input_roles) if motif_id in grammar.MOTIF_BY_ID else 1
        if len(fields) < needed:
            fields.extend(self._partner_fields(fields, slot.family, needed - len(fields)))
        if not fields:
            return None
        rng = random.Random(generation_policy.recipe_seed(
            campaign_id, seed, [node.field_id for node in fields], motif_id, slot.recipe_index, slot.parent_ids,
        ))
        recipe = generation_policy.sample_recipe(rng, motif_id)
        try:
            node = grammar.build_motif(motif_id, fields, recipe, limits=limits)
        except grammar.GrammarError:
            fallback = self._fallback_motif(fields, motif_id, recipe, limits)
            if fallback is None:
                return None
            motif_id, node = fallback
        expression = grammar.render(node)
        settings = {"decay": recipe.decay, "neutralization": recipe.neutralization,
                   "truncation": recipe.truncation}
        parameters: dict[str, Any] = {
            "field": fields[0].field_id,
            "fields": [item.field_id for item in fields],
            "datasets": sorted({item.dataset for item in fields}),
            "motif_id": motif_id,
            "recipe_index": slot.recipe_index,
            "recipe": recipe.as_dict(),
            "generation_mode": realized_mode,
            "planned_family": slot.family,
            "generator_strategy": realized_mode if force_motif is None else strategy,
        }
        if lineage_fallback:
            # The planned lineage mode could not be realized here; keep the intent auditable
            # while the realized mode and the (empty) parent ids state what actually happened.
            parameters["planned_generation_mode"] = slot.generation_mode
            parameters["lineage_fallback"] = True
        parent_ids = () if lineage_fallback else tuple(slot.parent_ids)
        profile = diversity.derive_source_profile(expression, self.catalog)
        return Proposal(
            expression=expression,
            settings=settings,
            family=diversity.family_for_profile(profile, slot.family),
            mutation_type="motif_generation",
            parameters=parameters,
            parent_ids=parent_ids,
            reason=slot.reason + (" (lineage fallback)" if lineage_fallback else ""),
            generation=None,
            motif_id=motif_id,
            recipe_index=slot.recipe_index,
            generation_mode=realized_mode,
            strategy=realized_mode if force_motif is None else strategy,
            source_profile=profile,
            grammar_skeleton_hash=grammar.grammar_skeleton_hash(expression, self.metadata()),
            semantic_skeleton_hash=grammar.semantic_skeleton_hash(expression, self.metadata()),
            recipe=recipe.as_dict(),
        )

    def _crossover_proposal(
        self,
        slot: generation_policy.PlanSlot,
        *,
        campaign_id: str,
        seed: int,
    ) -> Proposal | None:
        """Initial crossover: a typed composition of two distant archive parents (P6 groundwork)."""
        parents = [self.db.get_candidate(int(pid)) for pid in slot.parent_ids]
        parents = [row for row in parents if row]
        if len(parents) < 2:
            return None
        rng = random.Random(generation_policy.recipe_seed(
            campaign_id, seed, (), "crossover", slot.recipe_index, slot.parent_ids,
        ))
        form = CROSSOVER_FORMS[rng.randrange(len(CROSSOVER_FORMS))]
        metadata = self.metadata()
        try:
            left = grammar.parse_expression(str(parents[0].get("normalized_expression") or ""), metadata)
            right = grammar.parse_expression(str(parents[1].get("normalized_expression") or ""), metadata)
            left_wrapped = _crossover_leg(left, form)
            right_wrapped = _crossover_leg(right, form)
            if form == "multiply_rank":
                # The local operator signature requires three positional args for multiply.
                node = grammar.make_call("multiply", [left_wrapped, right_wrapped, grammar.literal(1)])
            elif form == "subtract_rank":
                node = grammar.make_call("subtract", [left_wrapped, right_wrapped])
            else:
                node = grammar.make_call("add", [left_wrapped, right_wrapped])
        except grammar.GrammarError:
            return None
        if grammar.check_complexity(node, _crossover_limits(left, right)):
            return None
        expression = grammar.render(node)
        parent_ids = tuple(int(row["id"]) for row in parents[:2])
        # The child family comes from the *child's* final sources (P0.1/P6.3): a crossover
        # of two families must never keep the first parent's stale label.
        profile = diversity.derive_source_profile(expression, self.catalog)
        return Proposal(
            expression=expression,
            settings={"decay": 6},
            family=str(profile.get("primary_family") or "unknown"),
            mutation_type="crossover",
            parameters={
                "motif_id": f"crossover_{form}", "recipe_index": slot.recipe_index,
                "crossover_form": form, "parent_grammar_hashes": [
                    grammar.grammar_skeleton_hash(str(row.get("normalized_expression") or ""), metadata)
                    for row in parents[:2]
                ],
                "generation_mode": "crossover", "generator_strategy": "crossover",
            },
            parent_ids=parent_ids,
            reason=slot.reason,
            motif_id=f"crossover_{form}",
            recipe_index=slot.recipe_index,
            generation_mode="crossover",
            strategy="crossover",
            source_profile=profile,
            grammar_skeleton_hash=grammar.grammar_skeleton_hash(expression, metadata),
            semantic_skeleton_hash=grammar.semantic_skeleton_hash(expression, metadata),
        )

    def generate(
        self,
        *,
        campaign_id: str,
        count: int,
        seed: int = 0,
        strategy: str = "mixed",
        family: str | None = None,
        motif: str | None = None,
        max_family_share: float = generation_policy.DEFAULT_MAX_FAMILY_SHARE,
        limits: grammar.ComplexityLimits = V3_LIMITS,
        screen: bool = True,
    ) -> tuple[generation_policy.Plan, list[Proposal]]:
        """Plan a campaign and materialize it into proposals (no queue writes)."""
        plan = self.plan(
            campaign_id=campaign_id, budget=count, seed=seed, mode=strategy,
            family=family, max_family_share=max_family_share,
        )
        proposals: list[Proposal] = []
        for slot in plan.slots:
            proposal = self.materialize(slot, campaign_id=campaign_id, seed=seed,
                                        limits=limits, force_motif=motif)
            if proposal is not None:
                proposals.append(proposal)
        if screen:
            proposals = self.screen_proposals(proposals, campaign_id=campaign_id, seed=seed)
        return plan, proposals

    def screen_proposals(
        self,
        proposals: Sequence[Proposal],
        *,
        campaign_id: str,
        seed: int = 0,
    ) -> list[Proposal]:
        """Attach a novelty decision to every proposal (P7); never drops one.

        The screen is advisory except for an exact duplicate produced under an explicit
        novelty request, which is marked ``SKIP_REDUNDANT`` and refused by :meth:`queue` while
        still being recorded in the trial ledger. Everything else is kept, downweighted at
        most, and the reason travels with the proposal.
        """
        context = diversity.novelty_context(self.db, self.catalog)
        screened: list[Proposal] = []
        for proposal in proposals:
            request_novelty = proposal.generation_mode in {"explore", "exploit"}
            report = diversity.screen_novelty(
                proposal.expression,
                catalog=self.catalog,
                context=context,
                settings=proposal.settings,
                motif_id=proposal.motif_id or None,
                request_novelty=request_novelty,
            )
            item = replace(
                proposal,
                novelty_score=report.score,
                novelty_decision=report.decision,
                skip_reason="" if report.decision != diversity.SKIP_REDUNDANT else report.reason,
            )
            screened.append(item)
            # A KEEP decision makes the proposal part of the seen history for later slots in
            # the same campaign, so one batch cannot fill itself with duplicates of itself.
            context = _with_seen(context, item)
        return screened

    def mutate_v3(self, parents: Sequence[Mapping[str, Any]], *, count: int = 4,
                  campaign_id: str = "mutation", seed: int = 0) -> list[Proposal]:
        """Mutate archive elites across diverse parents, labelling each child ``mutate`` (P4.4)."""
        proposals: list[Proposal] = []
        seen: set[str] = set()
        for parent in parents:
            child = self._mutate_child(parent, campaign_id=campaign_id)
            if child is None:
                continue
            child = replace(child, generation_mode="mutate", strategy="mutate",
                            source_profile=diversity.derive_source_profile(child.expression, self.catalog))
            key = canonical.canonical_key(child.expression, child.settings)
            if key in seen:
                continue
            seen.add(key)
            proposals.append(child)
            if len(proposals) >= count:
                return proposals
        return proposals[:count]

    def _mutate_child(self, parent: Mapping[str, Any], *, campaign_id: str) -> Proposal | None:
        """One V3 mutation child: failure-directed repair when a failure is diagnosed,
        otherwise a concrete structural edit (P4.4), and finally the structural field swap."""
        repairs = self.mutate(parent, count=1, campaign_id=campaign_id) if self.diagnose(parent) else []
        child = repairs[0] if repairs else self.structural_mutation(parent, campaign_id=campaign_id)
        if child is None:
            fallback = self.mutate(parent, count=1, campaign_id=campaign_id)
            child = fallback[0] if fallback else None
        return child

    def structural_mutation(
        self,
        parent: Mapping[str, Any],
        *,
        operation: str | None = None,
        campaign_id: str = "mutation",
        seed: int | None = None,
    ) -> Proposal | None:
        """One typed structural edit of ``parent`` under a stable operation name (P4.4).

        ``operation`` pins the edit; otherwise a concrete edit is sampled deterministically
        from :data:`V3_MUTATION_OPERATIONS`, skipping edits that do not apply to this parent.
        Every child is re-validated (arity/types through the operator catalog, then the
        parent-scaled complexity budget) before it becomes a :class:`Proposal`; an edit that
        cannot be realized cleanly returns ``None`` instead of a half-validated child.
        """
        expression = str(parent.get("normalized_expression") or parent.get("expression") or "")
        parent_id = int(parent["id"]) if parent.get("id") is not None else None
        parent_ids = (parent_id,) if parent_id is not None else ()
        family = str(parent.get("signal_family") or "mutation")
        settings = _settings(parent)
        generation = self.db.next_generation(parent_ids) if parent_ids else 0
        metadata = self.metadata()
        try:
            node = grammar.parse_expression(expression, metadata)
        except grammar.GrammarError:
            return None
        limits = _mutation_limits(node)
        seed_value = self.seed if seed is None else int(seed)
        rng = random.Random(generation_policy.recipe_seed(
            campaign_id, seed_value, [field.field_id for field in grammar.source_fields(node)],
            f"mutation:{operation or 'sample'}", 0, parent_ids,
        ))
        if operation is not None:
            if operation not in V3_MUTATION_OPERATIONS:
                raise ValueError(f"unknown mutation operation {operation!r}; known: {list(V3_MUTATION_OPERATIONS)}")
            order = [operation]
        else:
            order = list(V3_MUTATION_OPERATIONS)
            rng.shuffle(order)
        for name in order:
            edit = self._structural_edit(name, node, rng)
            if edit is None:
                continue
            child_node, parameters, reason = edit
            try:
                grammar.validate_tree(child_node, limits)
            except grammar.GrammarError:
                continue
            child_expression = grammar.render(child_node)
            if child_expression == expression:
                continue
            operation_parameters: dict[str, Any] = {
                "operation": name, "previous_operation": str(parent.get("mutation_type") or ""),
                **parameters,
            }
            return self._proposal(
                child_expression, settings, family, name, operation_parameters,
                parent_ids, generation, reason,
            )
        return None

    def _structural_edit(
        self,
        operation: str,
        node: grammar.ExprNode,
        rng: random.Random,
    ) -> tuple[grammar.ExprNode, dict[str, Any], str] | None:
        handler = {
            "dataset_swap": self._edit_dataset_swap,
            "motif_change": self._edit_motif_change,
            "normalization_change": self._edit_normalization_change,
            "group_change": self._edit_group_change,
            "subtree_replace": self._edit_subtree_replace,
            "add_component": self._edit_add_component,
        }[operation]
        return handler(node, rng)

    def _edit_dataset_swap(
        self, node: grammar.ExprNode, rng: random.Random,
    ) -> tuple[grammar.ExprNode, dict[str, Any], str] | None:
        """Replace one source with a same-type field from a different dataset."""
        used = {field.field_id for field in grammar.source_fields(node)}
        swappable = [field for field in grammar.source_fields(node) if self.catalog.get(field.field_id)]
        if not swappable:
            return None
        target = swappable[rng.randrange(len(swappable))]
        candidates = [
            field for field in self.catalog.fields
            if field.name not in used
            and field.dataset != target.dataset
            and field.field_type == target.value_type
            and field.field_type in {compatibility.MATRIX, compatibility.VECTOR}
        ]
        if not candidates:
            return None
        chosen = candidates[rng.randrange(min(4, len(candidates)))]
        replacement = grammar.field_node_from(chosen)
        child = _substitute_fields(node, {target.field_id: replacement})
        return child, {
            "replaced_field": target.field_id, "previous_dataset": target.dataset,
            "replacement_field": replacement.field_id, "replacement_dataset": replacement.dataset,
        }, "swap one source for a same-type field from another dataset"

    def _edit_motif_change(
        self, node: grammar.ExprNode, rng: random.Random,
    ) -> tuple[grammar.ExprNode, dict[str, Any], str] | None:
        """Rebuild the whole child from the same sources through a different motif."""
        fields = [field for field in grammar.source_fields(node) if self.catalog.get(field.field_id)]
        if not fields:
            return None
        candidates = [motif for motif in grammar.eligible_motifs(fields)
                      if len(motif.input_roles) <= len(fields)]
        rng.shuffle(candidates)
        current = grammar.render(node)
        for motif in candidates:
            recipe = generation_policy.sample_recipe(rng, motif.id)
            try:
                child = grammar.build_motif(motif.id, fields, recipe, limits=_mutation_limits(node))
            except grammar.GrammarError:
                continue
            if grammar.render(child) == current:
                continue
            return child, {"motif_id": motif.id, "fields": [field.field_id for field in fields]}, \
                "rebuild the same sources through a different economic motif"
        return None

    def _edit_normalization_change(
        self, node: grammar.ExprNode, rng: random.Random,
    ) -> tuple[grammar.ExprNode, dict[str, Any], str] | None:
        """Flip a cross-sectional normalizer, or introduce one when the tree has none."""
        targets = [call for call in _call_nodes(node) if call.operator in NORMALIZATION_SWAPS]
        if targets:
            target = targets[rng.randrange(len(targets))]
            replacement = grammar.CallNode(
                NORMALIZATION_SWAPS[target.operator], target.args, target.output_type, target.keywords,
            )
            child, swapped = _swap_subtree(node, target, replacement)
            if not swapped:
                return None
            return child, {"previous_normalizer": target.operator, "normalizer": replacement.operator}, \
                "flip the cross-sectional normalizer"
        operator = "zscore" if rng.random() < 0.5 else "rank"
        try:
            child = grammar.make_call(operator, [node])
        except grammar.GrammarError:
            return None
        return child, {"previous_normalizer": "none", "normalizer": operator}, \
            "introduce a cross-sectional normalizer"

    def _edit_group_change(
        self, node: grammar.ExprNode, rng: random.Random,
    ) -> tuple[grammar.ExprNode, dict[str, Any], str] | None:
        """Move one group literal one level around :data:`GROUP_CYCLE`."""
        groups = [field for field in _field_nodes(node) if field.value_type == grammar.GROUP]
        if not groups:
            return None
        target = groups[rng.randrange(len(groups))]
        new_name = GROUP_CYCLE.get(target.field_id, "subindustry")
        if new_name == target.field_id:
            return None
        replacement = grammar.group_node(new_name, self.metadata())
        child, swapped = _swap_subtree(node, target, replacement)
        if not swapped:
            return None
        return child, {"previous_group": target.field_id, "group": new_name}, \
            "move the grouping one level"

    def _edit_subtree_replace(
        self, node: grammar.ExprNode, rng: random.Random,
    ) -> tuple[grammar.ExprNode, dict[str, Any], str] | None:
        """Replace one typed subtree with a different motif over that subtree's own sources."""
        subtrees = [
            call for call in _call_nodes(node)
            if grammar.source_fields(call)
            and all(self.catalog.get(field.field_id) for field in grammar.source_fields(call))
        ]
        if not subtrees:
            return None
        # Prefer interior subtrees: replacing the root is ``motif_change``'s job.
        interior = [call for call in subtrees if call is not node]
        rng.shuffle(interior or subtrees)
        subtrees = interior or subtrees
        for target in subtrees[:3]:
            fields = list(grammar.source_fields(target))
            candidates = [motif for motif in grammar.eligible_motifs(fields)
                          if len(motif.input_roles) <= len(fields)]
            rng.shuffle(candidates)
            for motif in candidates:
                recipe = generation_policy.sample_recipe(rng, motif.id)
                try:
                    replacement = grammar.build_motif(motif.id, fields, recipe, limits=_mutation_limits(node))
                except grammar.GrammarError:
                    continue
                if grammar.render(replacement) == grammar.render(target):
                    continue
                child, swapped = _swap_subtree(node, target, replacement)
                if not swapped:
                    continue
                return child, {
                    "replaced_operator": target.operator, "replacement_motif": motif.id,
                    "subtree_fields": [field.field_id for field in fields],
                }, "replace one typed subtree with a different motif over its sources"
        return None

    def _edit_add_component(
        self, node: grammar.ExprNode, rng: random.Random,
    ) -> tuple[grammar.ExprNode, dict[str, Any], str] | None:
        """Combine the parent with one orthogonal component from a fresh source."""
        used = {field.field_id for field in grammar.source_fields(node)}
        candidates = [
            field for field in self.catalog.fields
            if field.name not in used and field.field_type in {compatibility.MATRIX, compatibility.VECTOR}
        ]
        if not candidates:
            return None
        for chosen in rng.sample(candidates, min(4, len(candidates))):
            fresh = grammar.field_node_from(chosen)
            motifs = [motif for motif in grammar.eligible_motifs([fresh]) if len(motif.input_roles) == 1]
            rng.shuffle(motifs)
            for motif in motifs:
                recipe = generation_policy.sample_recipe(rng, motif.id)
                try:
                    component = grammar.build_motif(motif.id, [fresh], recipe, limits=_mutation_limits(node))
                    child = grammar.make_call("add", [node, component])
                except grammar.GrammarError:
                    continue
                return child, {
                    "added_field": fresh.field_id, "added_dataset": fresh.dataset,
                    "component_motif": motif.id,
                }, "combine an orthogonal component into the signal"
        return None

    # -- mutation ----------------------------------------------------------

    def diagnose(self, parent: Mapping[str, Any]) -> list[str]:
        """Ordered failure modes for a parent, from explicit checks to measured metrics."""
        text = " ".join(str(parent.get(key) or "") for key in ("failure_reason", "gate_reason")).upper()
        modes: list[str] = []
        for mode in FAILURE_MUTATION_TYPES:
            if mode in text:
                modes.append(mode)
        turnover = parent.get("turnover")
        if isinstance(turnover, (int, float)):
            if float(turnover) > 0.20 and "HIGH_TURNOVER" not in modes:
                modes.append("HIGH_TURNOVER")
            elif float(turnover) < 0.02 and "LOW_TURNOVER" not in modes:
                modes.append("LOW_TURNOVER")
        if "corr" in text.lower() or "correlation" in text.lower():
            if "correlation_repair" not in {FAILURE_MUTATION_TYPES[mode] for mode in modes}:
                modes.append("SELF_CORRELATION")
        return modes

    def mutate(self, parent: Mapping[str, Any], *, count: int = 4, campaign_id: str = "mutation") -> list[Proposal]:
        """Failure-directed repair; falls back to a structural field swap."""
        expression = str(parent.get("normalized_expression") or parent.get("expression") or "")
        parent_id = int(parent["id"]) if parent.get("id") is not None else None
        parent_ids = (parent_id,) if parent_id is not None else ()
        family = str(parent.get("signal_family") or "mutation")
        settings = _settings(parent)
        generation = self.db.next_generation(parent_ids) if parent_ids else 0
        modes = self.diagnose(parent)
        proposals: list[Proposal] = []
        seen: set[str] = set()
        for mode in modes:
            repair_type = FAILURE_MUTATION_TYPES[mode]
            for raw in self._repair(parent, mode, expression, settings, family, parent_ids, generation):
                proposal = _as_repair(raw, repair_type)
                key = canonical.canonical_key(proposal.expression, proposal.settings)
                if key in seen:
                    continue
                seen.add(key)
                proposals.append(proposal)
                if len(proposals) >= count:
                    return proposals
        if not proposals:
            proposals = self._field_swaps(
                parent, expression, settings, family, parent_ids, generation, count=count,
                reason="replace the data source after a weak result",
            )
        return proposals[:count]

    def _repair(
        self,
        parent: Mapping[str, Any],
        mode: str,
        expression: str,
        settings: Mapping[str, Any],
        family: str,
        parent_ids: tuple[int, ...],
        generation: int,
    ) -> list[Proposal]:
        mutation_type = FAILURE_MUTATION_TYPES[mode]
        builder = {
            "turnover_repair": self._turnover_repair,
            "low_turnover_repair": self._low_turnover_repair,
            "sharpe_repair": self._sharpe_repair,
            "fitness_repair": self._fitness_repair,
            "concentration_repair": self._concentration_repair,
            "sub_universe_repair": self._sub_universe_repair,
            "correlation_repair": self._correlation_repair,
        }[mutation_type]
        proposals = builder(parent, expression, settings, family, parent_ids, generation)
        return proposals

    def _child_family(self, expression: str, parent_family: str | None = None) -> str:
        """Family derived from the *child* expression, never blindly inherited from a parent."""
        return diversity.derive_family(expression, self.catalog, parent_family)

    def _proposal(
        self,
        expression: str,
        settings: Mapping[str, Any],
        family: str,
        mutation_type: str,
        parameters: Mapping[str, Any],
        parent_ids: tuple[int, ...],
        generation: int,
        reason: str,
    ) -> Proposal:
        parameters = dict(parameters)
        # The concrete structural edit is always recorded, so mutation stats can aggregate
        # operations separately from the broad repair class (P9.2).
        parameters.setdefault("operation", mutation_type)
        return Proposal(
            expression=expression,
            settings=settings,
            family=self._child_family(expression, family),
            mutation_type=mutation_type,
            parameters=parameters,
            parent_ids=parent_ids,
            reason=reason,
            generation=generation,
        )

    def _turnover_repair(self, parent, expression, settings, family, parent_ids, generation) -> list[Proposal]:
        base_decay = int(settings.get("decay", 6) or 6)
        repair_settings = {**settings, "decay": min(512, base_decay + 4)}
        proposals = [
            self._proposal(
                f"hump({expression}, hump={hump})", repair_settings, family, "hump_smoothing",
                {"hump": hump, "decay": repair_settings["decay"], "previous_decay": base_decay},
                parent_ids, generation, "repair diagnosed high turnover",
            )
            for hump in (0.005, 0.01)
        ]
        window = _first_window(expression)
        if window:
            slowed = _replace_window(expression, min(504, window * 2))
            proposals.append(self._proposal(
                slowed, repair_settings, family, "window_change",
                {"window": min(504, window * 2), "previous_window": window},
                parent_ids, generation, "slow the signal down to cut turnover",
            ))
        return proposals

    def _low_turnover_repair(self, parent, expression, settings, family, parent_ids, generation) -> list[Proposal]:
        base_decay = int(settings.get("decay", 6) or 6)
        proposals: list[Proposal] = []
        if base_decay > 1:
            proposals.append(self._proposal(
                expression, {**settings, "decay": max(1, base_decay // 2)}, family, "decay_change",
                {"decay": max(1, base_decay // 2), "previous_decay": base_decay},
                parent_ids, generation, "increase activity when turnover is too low",
            ))
        stripped = _strip_hump(expression)
        if stripped is not None:
            proposals.append(self._proposal(
                stripped, settings, family, "remove_component",
                {"component": "hump"}, parent_ids, generation, "remove the smoothing that suppressed turnover",
            ))
        if not proposals:
            proposals.extend(self._field_swaps(
                parent, expression, settings, family, parent_ids, generation, count=2,
                reason="a signal that barely trades needs a livelier data source",
            ))
        return proposals

    def _sharpe_repair(self, parent, expression, settings, family, parent_ids, generation) -> list[Proposal]:
        proposals: list[Proposal] = []
        window = _first_window(expression)
        if window:
            for new_window in (window * 3, max(5, window // 3)):
                proposals.append(self._proposal(
                    _replace_window(expression, new_window), settings, family, "window_change",
                    {"window": new_window, "previous_window": window},
                    parent_ids, generation, "lookback length is the first Sharpe lever",
                ))
        proposals.extend(self._field_swaps(
            parent, expression, settings, family, parent_ids, generation, count=2,
            reason="a weak signal usually needs a different data source, not a new parameter",
        ))
        proposals.extend(self._combine_signals(
            parent, expression, settings, family, parent_ids, generation, count=1,
        ))
        return proposals

    def _fitness_repair(self, parent, expression, settings, family, parent_ids, generation) -> list[Proposal]:
        turnover = parent.get("turnover")
        measured = float(turnover) if isinstance(turnover, (int, float)) else None
        if measured is not None and measured > 0.15:
            proposals = self._turnover_repair(parent, expression, settings, family, parent_ids, generation)
            for proposal in proposals:
                proposal.parameters["bottleneck"] = "turnover"
            return proposals
        proposals = self._sharpe_repair(parent, expression, settings, family, parent_ids, generation)
        for proposal in proposals:
            proposal.parameters["bottleneck"] = "signal"
        return proposals

    def _concentration_repair(self, parent, expression, settings, family, parent_ids, generation) -> list[Proposal]:
        proposals = [
            self._proposal(
                f"group_neutralize({expression}, subindustry)", settings, family, "add_group_transform",
                {"group": "subindustry"}, parent_ids, generation,
                "spread concentrated weight across a group",
            ),
            self._proposal(
                f"rank({expression})", settings, family, "concentration_repair",
                {"transform": "rank"}, parent_ids, generation,
                "rank normalization limits single-name weight",
            ),
        ]
        truncation = float(settings.get("truncation", 0.1) or 0.1)
        if truncation > 0.02:
            proposals.append(self._proposal(
                expression, {**settings, "truncation": max(0.01, truncation / 2)}, family,
                "concentration_repair", {"truncation": max(0.01, truncation / 2), "previous_truncation": truncation},
                parent_ids, generation, "tighten truncation to cap single-name weight",
            ))
        return proposals

    def _sub_universe_repair(self, parent, expression, settings, family, parent_ids, generation) -> list[Proposal]:
        proposals: list[Proposal] = []
        lowered = expression.lower()
        broader = next((group for group in GROUP_BROADENING if group not in lowered), "market")
        neutralization = str(settings.get("neutralization") or "SUBINDUSTRY").upper()
        proposals.append(self._proposal(
            expression, {**settings, "neutralization": _broaden_neutralization(neutralization)}, family,
            "neutralization_change",
            {"neutralization": _broaden_neutralization(neutralization), "previous_neutralization": neutralization},
            parent_ids, generation, "widen neutralization when a sub-universe fails",
        ))
        window = _first_window(expression) or 20
        proposals.append(self._proposal(
            f"group_rank(ts_mean({expression}, {min(252, max(5, window))}), {broader})", settings, family,
            "add_group_transform", {"group": broader, "window": min(252, max(5, window))},
            parent_ids, generation, "broaden the grouping so the signal survives more universes",
        ))
        return proposals

    def _correlation_repair(self, parent, expression, settings, family, parent_ids, generation) -> list[Proposal]:
        """Escape a correlation trap by changing the driver *and* the signal shape.

        Window, transform, or neutralization tweaks inside one economic family
        usually preserve the correlation, so a repair child takes a never-tested
        field from a different dataset and renders it through a structural
        template the parent expression does not already use. A re-shaped existing
        driver and an orthogonal combine leg are the fallbacks.
        """
        used = set(canonical.fields_of(expression))
        fresh = self._fresh_templates(expression)
        proposals: list[Proposal] = []
        for index, field in enumerate(self._alternatives(used, parent=parent, limit=3)):
            template = fresh[index % len(fresh)] if fresh else self._template_for(field.name)
            window = DEFAULT_WINDOWS[(self.seed + index) % len(DEFAULT_WINDOWS)]
            proposals.append(self._proposal(
                template.render(self._field_reference(field), window), settings, family, "field_swap",
                {"replacement_field": field.name, "replacement_dataset": field.dataset,
                 "replaced_field": self._first_used_field(expression, used),
                 "template": template.id, "window": window},
                parent_ids, generation,
                "change the underlying driver and signal shape to escape correlation",
            ))
        target = self._first_used_field(expression, used)
        parent_field = self.catalog.get(target) if target else None
        if parent_field is not None and fresh:
            template = fresh[0]
            window = DEFAULT_WINDOWS[(self.seed + len(fresh)) % len(DEFAULT_WINDOWS)]
            proposals.append(self._proposal(
                template.render(self._field_reference(parent_field), window), settings, family,
                "template_change",
                {"field": parent_field.name, "template": template.id, "window": window},
                parent_ids, generation,
                "re-shape an existing driver through a different structural template",
            ))
        if not proposals:
            return self._field_swaps(
                parent, expression, settings, family, parent_ids, generation, count=2,
                reason="change the economic return path to escape correlation",
            )
        return proposals

    def _field_swaps(
        self,
        parent: Mapping[str, Any],
        expression: str,
        settings: Mapping[str, Any],
        family: str,
        parent_ids: tuple[int, ...],
        generation: int,
        *,
        count: int,
        reason: str,
    ) -> list[Proposal]:
        used = set(canonical.fields_of(expression))
        proposals: list[Proposal] = []
        for field in self._alternatives(used, parent=parent, limit=count):
            replacement = self._field_reference(field)
            target = self._first_used_field(expression, used)
            if target is None:
                swapped = f"add({expression}, rank(ts_rank({replacement}, 60)))"
            else:
                swapped = re.sub(rf"\b{re.escape(target)}\b", replacement, expression, count=1)
            proposals.append(self._proposal(
                swapped, settings, family, "field_swap",
                {"replacement_field": field.name, "replacement_dataset": field.dataset,
                 "replaced_field": target},
                parent_ids, generation, reason,
            ))
        return proposals

    def _combine_signals(
        self,
        parent: Mapping[str, Any],
        expression: str,
        settings: Mapping[str, Any],
        family: str,
        parent_ids: tuple[int, ...],
        generation: int,
        *,
        count: int,
    ) -> list[Proposal]:
        used = set(canonical.fields_of(expression))
        fresh = self._fresh_templates(expression)
        proposals: list[Proposal] = []
        for index, field in enumerate(self._alternatives(used, parent=parent, limit=count)):
            template = fresh[index % len(fresh)] if fresh else SIGNAL_TEMPLATES[0]
            leg = template.render(self._field_reference(field), 126)
            proposals.append(self._proposal(
                f"add({expression}, {leg})",
                settings, family, "add_component",
                {"added_field": field.name, "added_dataset": field.dataset, "window": 126,
                 "template": template.id},
                parent_ids, generation, "combine an orthogonal data source to lower correlation",
            ))
        return proposals

    @staticmethod
    def _field_reference(field: Field) -> str:
        return f"vec_avg({field.name})" if field.field_type == compatibility.VECTOR else field.name

    @staticmethod
    def _first_used_field(expression: str, used: Iterable[str]) -> str | None:
        for token in _token_order(expression):
            if token in used:
                return token
        return None

    def _alternatives(self, used: set[str], *, parent: Mapping[str, Any], limit: int) -> list[Field]:
        """Compatible, never-tested-first fields, preferring a different dataset."""
        preferred_dataset = str(parent.get("signal_family") or "")
        fields = [
            field for field in self.catalog.fields
            if field.name not in used
            and field.field_type in {compatibility.MATRIX, compatibility.VECTOR}
        ]
        ordered = self.coverage_ordered(fields, rng=random.Random(self.seed + len(used)))
        if preferred_dataset:
            ordered.sort(key=lambda field: (field.dataset == preferred_dataset,))
        return ordered[: max(1, limit)]

    def _template_for(self, field_name: str) -> SignalTemplate:
        """Deterministic shape rotation; no field is tied to a single template."""
        offset = int(hashlib.sha256(field_name.encode("utf-8")).hexdigest()[:8], 16)
        return SIGNAL_TEMPLATES[(self.seed + offset) % len(SIGNAL_TEMPLATES)]

    @staticmethod
    def _fresh_templates(expression: str) -> list[SignalTemplate]:
        """Templates whose marker does not already appear in ``expression``."""
        return [template for template in SIGNAL_TEMPLATES if template.marker not in expression]

    def _field_proposal(
        self,
        field: Field,
        *,
        template: str | None = None,
        truncation: float | None = None,
    ) -> Proposal | None:
        window = DEFAULT_WINDOWS[(self.seed + len(field.name)) % len(DEFAULT_WINDOWS)]
        decay = DEFAULT_DECAYS[(self.seed + len(field.dataset)) % len(DEFAULT_DECAYS)]
        value = field.name
        if field.field_type == compatibility.VECTOR:
            value = f"vec_avg({value})"
        elif field.field_type in {"GROUP", "UNIVERSE", "SYMBOL"}:
            return None
        selected = self._template_for(field.name) if template is None else _template_by_id(template)
        expression = selected.render(value, window)
        settings: dict[str, Any] = {"decay": decay}
        parameters: dict[str, Any] = {
            "field": field.name, "dataset": field.dataset, "field_type": field.field_type,
            "window": window, "decay": decay, "template": selected.id,
        }
        if truncation is not None:
            settings["truncation"] = float(truncation)
            parameters["truncation"] = float(truncation)
        return Proposal(
            expression,
            settings,
            field.dataset,
            "dataset_coverage",
            parameters,
            reason="catalog coverage across supplied BRAIN datasets",
            generation=0,
        )


# ---------------------------------------------------------------------------
# Expression helpers
# ---------------------------------------------------------------------------


def _as_repair(proposal: Proposal, repair_type: str) -> Proposal:
    """Label a child with the *diagnosed failure mode* it answers.

    ``mutation_type`` is the research decision (``turnover_repair``, ``sharpe_repair``, ...)
    and ``parameters['operation']`` is the concrete structural edit that was applied
    (``hump_smoothing``, ``window_change``, ``field_swap``, ``add_component``, ...), so a
    repair stays explainable without losing which failure drove it.
    """
    parameters = dict(proposal.parameters)
    parameters.setdefault("operation", proposal.mutation_type)
    return replace(proposal, mutation_type=repair_type, parameters=parameters)


def _with_seen(context: diversity.NoveltyContext, proposal: Proposal) -> diversity.NoveltyContext:
    """Return the novelty context as if ``proposal`` had just been generated."""
    datasets = frozenset(proposal.source_profile.get("datasets") or ())
    motifs = frozenset({proposal.motif_id}) if proposal.motif_id else frozenset()
    return diversity.NoveltyContext(
        canonical_keys=context.canonical_keys | {canonical.canonical_key(proposal.expression, proposal.settings)},
        skeleton_hashes=context.skeleton_hashes | {canonical.skeleton_hash(proposal.expression)},
        grammar_hashes=context.grammar_hashes | ({proposal.grammar_skeleton_hash} if proposal.grammar_skeleton_hash else frozenset()),
        semantic_hashes=context.semantic_hashes | ({proposal.semantic_skeleton_hash} if proposal.semantic_skeleton_hash else frozenset()),
        datasets=context.datasets | datasets,
        motifs=context.motifs | motifs,
        candidate_count=context.candidate_count + 1,
    )


def _crossover_leg(node: grammar.ExprNode, form: str) -> grammar.ExprNode:
    """Wrap one parent expression in the normalizer its crossover form calls for."""
    operator = "zscore" if form == "add_zscore" else "rank"
    return grammar.make_call(operator, [node])


def _mutation_limits(parent: grammar.ExprNode) -> grammar.ComplexityLimits:
    """Budget a structural mutation child against its parent, never above the hard ceiling.

    A typed edit adds at most a handful of nodes to the parent; charging the child against a
    fresh-candidate budget would refuse edits of already-evolved elites, exactly like the
    crossover budget used to before it was scaled to its parents.
    """
    return grammar.ComplexityLimits(
        max_depth=min(CROSSOVER_LIMITS.max_depth, grammar.node_depth(parent) + 2),
        max_nodes=min(CROSSOVER_LIMITS.max_nodes, grammar.node_count(parent) + 6),
        max_fields=min(CROSSOVER_LIMITS.max_fields, len(grammar.source_fields(parent)) + 1),
        max_binary_ops=min(CROSSOVER_LIMITS.max_binary_ops, grammar.binary_op_count(parent) + 2),
    )


def _substitute_fields(
    node: grammar.ExprNode,
    replacements: Mapping[str, grammar.FieldNode],
) -> grammar.ExprNode:
    """Frozen-dataclass-safe rebuild of ``node`` with named source fields replaced."""
    if isinstance(node, grammar.FieldNode):
        return replacements.get(node.field_id, node)
    if isinstance(node, grammar.LiteralNode):
        return node
    args = tuple(_substitute_fields(argument, replacements) for argument in node.args)
    keywords = tuple((name, _substitute_fields(value, replacements)) for name, value in node.keywords)
    return grammar.CallNode(node.operator, args, node.output_type, keywords)


def _swap_subtree(
    root: grammar.ExprNode,
    target: grammar.ExprNode,
    replacement: grammar.ExprNode,
) -> tuple[grammar.ExprNode, bool]:
    """Replace the first occurrence of ``target`` (by identity) with ``replacement``."""
    if root is target:
        return replacement, True
    if isinstance(root, grammar.CallNode):
        args = list(root.args)
        keywords = list(root.keywords)
        for index, argument in enumerate(args):
            new, swapped = _swap_subtree(argument, target, replacement)
            if swapped:
                args[index] = new
                return grammar.CallNode(root.operator, tuple(args), root.output_type, tuple(keywords)), True
        for index, (name, value) in enumerate(keywords):
            new, swapped = _swap_subtree(value, target, replacement)
            if swapped:
                keywords[index] = (name, new)
                return grammar.CallNode(root.operator, tuple(args), root.output_type, tuple(keywords)), True
    return root, False


def _call_nodes(node: grammar.ExprNode) -> list[grammar.CallNode]:
    """Every call subtree of ``node``, outermost first."""
    if not isinstance(node, grammar.CallNode):
        return []
    children = [child for argument in node.args for child in _call_nodes(argument)]
    children += [child for _name, value in node.keywords for child in _call_nodes(value)]
    return [node] + children


def _field_nodes(node: grammar.ExprNode) -> list[grammar.FieldNode]:
    """Every field leaf of ``node``, source order."""
    if isinstance(node, grammar.FieldNode):
        return [node]
    if isinstance(node, grammar.LiteralNode):
        return []
    leaves = [leaf for argument in node.args for leaf in _field_nodes(argument)]
    leaves += [leaf for _name, value in node.keywords for leaf in _field_nodes(value)]
    return leaves


def _first_window(expression: str) -> int | None:
    for match in _WINDOW_RE.finditer(expression):
        value = int(match.group(1))
        if 2 <= value <= 504:
            return value
    return None


def _replace_window(expression: str, new_window: int) -> str:
    match = _WINDOW_RE.search(expression)
    if match is None:
        return expression
    start, end = match.span(1)
    return f"{expression[:start]}{int(new_window)}{expression[end:]}"


def _matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _split_top_level(inner: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    depth = 0
    for char in inner:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            args.append("".join(current))
            current = []
            continue
        current.append(char)
    args.append("".join(current))
    return args


def _strip_hump(expression: str) -> str | None:
    """Remove a ``hump(x, hump=...)`` wrapper, keeping its payload as a component edit."""
    match = _HUMP_RE.search(expression)
    if match is None:
        return None
    open_index = match.end() - 1
    close_index = _matching_paren(expression, open_index)
    if close_index is None:
        return None
    arguments = _split_top_level(expression[open_index + 1 : close_index])
    payload = arguments[0].strip() if arguments else ""
    if not payload:
        return None
    return f"{expression[:match.start()]}{payload}{expression[close_index + 1:]}"


def _broaden_neutralization(current: str) -> str:
    order = ("NONE", "SUBINDUSTRY", "INDUSTRY", "SECTOR", "MARKET")
    try:
        index = order.index(current.upper())
    except ValueError:
        return "MARKET"
    return order[min(len(order) - 1, index + 1)]


def _token_order(expression: str) -> list[str]:
    """Identifiers in source order, so a swap hits the field a human would read first."""
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expression)


def _settings(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("settings_json")
    try:
        parsed = json.loads(str(raw)) if raw else {}
    except ValueError:
        parsed = {}
    return canonical.normalize_settings(parsed if isinstance(parsed, Mapping) else {})


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate validated, lineage-tracked BRAIN candidates")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("--campaign", required=True)
    generate.add_argument("--count", type=int, default=50)
    generate.add_argument("--family", default="all", help="category or dataset id; default: all")
    generate.add_argument("--dataset")
    generate.add_argument("--seed", type=int, default=0)
    generate.add_argument("--all-fields", action="store_true", help="cover every compatible field in the catalog")
    generate.add_argument("--template", choices=[template.id for template in SIGNAL_TEMPLATES],
                          help="pin every proposal to one structural template (proven-recipe campaigns)")
    generate.add_argument("--truncation", type=float, help="override the truncation setting (e.g. 0.05)")
    generate.add_argument("--strategy", choices=sorted(generation_policy.STRATEGY_ALIASES),
                          help="Generator V3 campaign strategy (omit for the V2 template generator)")
    generate.add_argument("--motif", choices=sorted(grammar.MOTIF_BY_ID),
                          help="force every V3 proposal through one motif")
    generate.add_argument("--max-family-share", type=float,
                          default=generation_policy.DEFAULT_MAX_FAMILY_SHARE)
    generate.add_argument("--dry-plan", action="store_true",
                          help="materialize and print the V3 plan distribution without queueing")
    generate.add_argument("--db", type=Path)
    mutate = sub.add_parser("mutate")
    mutate.add_argument("candidate_id", type=int)
    mutate.add_argument("--campaign", required=True)
    mutate.add_argument("--count", type=int, default=4)
    mutate.add_argument("--seed", type=int, default=0)
    mutate.add_argument("--db", type=Path)
    crossover = sub.add_parser("crossover")
    crossover.add_argument("parent_a", type=int)
    crossover.add_argument("parent_b", type=int)
    crossover.add_argument("--campaign", required=True)
    crossover.add_argument("--count", type=int, default=4)
    crossover.add_argument("--seed", type=int, default=0)
    crossover.add_argument("--db", type=Path)
    report = sub.add_parser("diversity-report")
    report.add_argument("--campaign", required=True)
    report.add_argument("--db", type=Path)
    return parser


def _v3_distribution(plan: generation_policy.Plan, proposals: Sequence[Proposal]) -> dict[str, Any]:
    """Distribution of a materialized V3 plan, by the dimensions the dry plan must print."""
    def counts(values: Iterable[str]) -> dict[str, int]:
        result: dict[str, int] = {}
        for value in values:
            result[str(value)] = result.get(str(value), 0) + 1
        return dict(sorted(result.items(), key=lambda item: (-item[1], item[0])))

    return {
        "planned_budget": plan.planned_budget,
        "materialized": len(proposals),
        "generation_mode": counts(proposal.generation_mode for proposal in proposals),
        "family": counts(proposal.family for proposal in proposals),
        "motif": counts(proposal.motif_id for proposal in proposals),
        "dataset": counts(dataset for proposal in proposals for dataset in (proposal.source_profile.get("datasets") or ["unknown"])),
        "grammar_skeleton": counts(proposal.grammar_skeleton_hash for proposal in proposals),
        "semantic_skeleton": counts(proposal.semantic_skeleton_hash for proposal in proposals),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        if args.command == "diversity-report":
            import diversity

            print(json.dumps(diversity.campaign_diversity_report(db, args.campaign), indent=2, sort_keys=True))
            return 0
        generator = CandidateGenerator(db, seed=args.seed)
        if args.command == "generate" and (args.strategy or args.motif or args.dry_plan):
            plan, proposals = generator.generate(
                campaign_id=args.campaign, count=args.count, seed=args.seed,
                strategy=args.strategy or "mixed", family=None if args.family in (None, "all") else args.family,
                motif=args.motif, max_family_share=args.max_family_share,
            )
            if args.dry_plan:
                print(json.dumps(_v3_distribution(plan, proposals), indent=2, sort_keys=True))
                return 0
            print(json.dumps(generator.queue(args.campaign, proposals), indent=2, sort_keys=True))
        elif args.command == "generate":
            proposals = generator.proposals(count=args.count, family=args.family, dataset=args.dataset,
                                            all_fields=args.all_fields, template=args.template,
                                            truncation=args.truncation)
            print(json.dumps(generator.queue(args.campaign, proposals), indent=2, sort_keys=True))
        elif args.command == "crossover":
            for parent_id in (args.parent_a, args.parent_b):
                if not db.get_candidate(parent_id):
                    raise SystemExit(f"candidate {parent_id} not found")
            parent_ids = (int(args.parent_a), int(args.parent_b))
            slot = generation_policy.PlanSlot(
                slot=0, generation_mode="crossover", family="crossover", motif_id="crossover",
                recipe_index=0, reason="explicit crossover request", parent_ids=parent_ids,
            )
            proposals: list[Proposal] = []
            for index in range(max(1, args.count)):
                proposal = generator.materialize(
                    replace(slot, recipe_index=index), campaign_id=args.campaign, seed=args.seed)
                if proposal is not None:
                    proposals.append(proposal)
            print(json.dumps(generator.queue(args.campaign, proposals), indent=2, sort_keys=True))
        else:
            parent = db.get_candidate(args.candidate_id)
            if not parent:
                raise SystemExit(f"candidate {args.candidate_id} not found")
            proposals = generator.mutate(parent, count=args.count, campaign_id=args.campaign)
            print(json.dumps(generator.queue(args.campaign, proposals), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
