"""Diversity measurement for Generator V3 (P0).

This module answers "how different is this research, really?" with facts that are
independent of one another:

* :func:`derive_source_profile` — which fields, datasets, categories and field types a
  candidate actually reads, plus its ``primary_family`` and ``cross_dataset`` flag. It is
  always derived from the *child* expression, so a cross-dataset mutation can never inherit
  the parent's stale family label.
* :func:`effective_count` — entropy-based effective counts, so "20 motifs exist but 95% of
  candidates use one" cannot look diverse.
* :func:`novelty_context` / :func:`screen_novelty` — the pre-screen the generation policy and
  (later) the ranking layer consume. The screen never silently discards work: it returns a
  KEEP / DOWNWEIGHT / SKIP_REDUNDANT decision with a reason, and the caller still records a
  research trial for every proposal.

No BRAIN calls, no credentials, no database writes beyond the read-only queries the caller
passes in.
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import canonical  # noqa: E402
import expression_grammar as grammar  # noqa: E402

FIELD_CATALOG_PATH = REPO_ROOT / "references" / "wq_usa_top3000_delay1_data_fields.json"

#: Novelty pre-screen outcomes.
KEEP = "KEEP"
DOWNWEIGHT = "DOWNWEIGHT"
SKIP_REDUNDANT = "SKIP_REDUNDANT"

#: The screen only skips work the generator *asked* to be novel when it is a pure duplicate.
SKIP_SCORE = 0.02
DOWNWEIGHT_SCORE = 0.25


@dataclass(frozen=True)
class FieldInfo:
    """Minimal field metadata used by source profiles and skeletons."""

    field_id: str
    value_type: str
    dataset: str
    category: str

    @property
    def name(self) -> str:
        return self.field_id

    @property
    def field_type(self) -> str:
        return self.value_type


def _identifier(value: Any) -> str:
    """Normalize a dataset/category value that may be a bare id or an ``{id, name}`` mapping."""
    if value is None:
        return "unknown"
    if isinstance(value, Mapping):
        return str(value.get("id") or "unknown")
    return str(value)


def _as_field_info(raw: Any, field_id: str | None = None) -> FieldInfo:
    if isinstance(raw, Mapping):
        return FieldInfo(
            str(raw.get("field_id") or raw.get("id") or raw.get("name") or field_id or ""),
            str(raw.get("value_type") or raw.get("field_type") or raw.get("type") or grammar.MATRIX).upper(),
            _identifier(raw.get("dataset")),
            _identifier(raw.get("category")),
        )
    return FieldInfo(
        str(getattr(raw, "field_id", None) or getattr(raw, "name", None) or field_id or ""),
        str(getattr(raw, "value_type", None) or getattr(raw, "field_type", None) or grammar.MATRIX).upper(),
        _identifier(getattr(raw, "dataset", "unknown")),
        _identifier(getattr(raw, "category", "unknown")),
    )


def metadata_from(fields: Iterable[Any]) -> dict[str, FieldInfo]:
    """Normalize any field collection (objects or mappings) into ``name -> FieldInfo``."""
    result: dict[str, FieldInfo] = {}
    for item in fields:
        info = _as_field_info(item)
        if info.field_id:
            result[info.field_id] = info
            result.setdefault(info.field_id.lower(), info)
    return result


@lru_cache(maxsize=2)
def load_field_metadata(path: str | Path = FIELD_CATALOG_PATH) -> dict[str, FieldInfo]:
    """Field metadata from the local catalog snapshot (cached); ``{}`` if unreadable.

    Used by archive/niche code that must not import the generator, so the two stay
    independently importable.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, list):
        return {}
    return metadata_from(item for item in raw if isinstance(item, Mapping) and item.get("id"))


def catalog_metadata(catalog: Any) -> dict[str, Any]:
    """Field metadata from a generator ``Catalog`` (or any object exposing ``.fields``)."""
    fields = getattr(catalog, "fields", None)
    if fields is None and isinstance(catalog, Mapping):
        return dict(catalog)
    if fields is None:
        return load_field_metadata()
    return metadata_from(fields)


# ---------------------------------------------------------------------------
# Source profile (P0.1)
# ---------------------------------------------------------------------------


def _ast(expression: str | grammar.ExprNode, metadata: Mapping[str, Any] | None) -> grammar.ExprNode | None:
    if isinstance(expression, (grammar.FieldNode, grammar.LiteralNode, grammar.CallNode)):
        return expression
    try:
        return grammar.parse_expression(str(expression), metadata)
    except grammar.GrammarError:
        return None


def derive_source_profile(
    expression: str | grammar.ExprNode,
    catalog: Any = None,
) -> dict[str, Any]:
    """Derive the economic source profile from an expression or AST.

    Returns ``field_ids``, ``datasets``, ``categories``, ``field_types``,
    ``primary_family`` and ``cross_dataset``. A single recognized dataset becomes the
    primary family; multiple datasets become a stable composite such as
    ``multi:analyst4+fundamental2``; no recognized source yields ``unknown``.
    """
    metadata = catalog_metadata(catalog) if catalog is not None else load_field_metadata()
    node = _ast(expression, metadata)
    if node is not None:
        sources = grammar.source_fields(node)
    else:
        # Unparseable text: fall back to the regex field reader so the profile is still
        # derived from the child expression rather than inherited.
        sources = tuple(
            grammar.field_node_from(metadata.get(name) or metadata.get(name.lower()), fallback_id=name)
            for name in canonical.fields_of(str(expression or ""))
        )
    known = [field_node for field_node in sources if field_node.dataset not in ("", "unknown")]
    chosen = known or list(sources)
    field_ids = sorted({field_node.field_id for field_node in sources})
    datasets = sorted({field_node.dataset for field_node in known})
    categories = sorted({field_node.category for field_node in known})
    field_types = sorted({field_node.value_type for field_node in sources})
    cross_dataset = len(datasets) > 1
    if cross_dataset:
        primary_family = "multi:" + "+".join(datasets)
    elif datasets:
        primary_family = datasets[0]
    else:
        primary_family = "unknown"
    return {
        "field_ids": field_ids,
        "datasets": datasets,
        "categories": categories,
        "field_types": field_types,
        "primary_family": primary_family,
        "cross_dataset": cross_dataset,
    }


def family_for_profile(profile: Mapping[str, Any], parent_family: str | None = None) -> str:
    """Resolve a research family, never inheriting a stale label after a field change.

    A cross-dataset child always takes its composite family. A single-dataset child keeps the
    parent label only while that label still describes the child's own dataset or category —
    so a mutation that moved to another dataset is relabelled from the child.
    """
    primary = str(profile.get("primary_family") or "unknown")
    if not parent_family:
        return primary
    if profile.get("cross_dataset"):
        return primary
    datasets = list(profile.get("datasets") or [])
    categories = list(profile.get("categories") or [])
    if not datasets:
        # No recognized source (unknown field): the child cannot be attributed to a new
        # family, so the parent label is the only honest description available.
        return parent_family or primary
    if parent_family in datasets or parent_family in categories:
        return parent_family
    return primary


def derive_family(expression: str | grammar.ExprNode, catalog: Any = None, parent_family: str | None = None) -> str:
    """Convenience wrapper: source profile + family resolution in one call."""
    return family_for_profile(derive_source_profile(expression, catalog), parent_family)


# ---------------------------------------------------------------------------
# Entropy / effective counts
# ---------------------------------------------------------------------------


def shannon_entropy(counts: Mapping[str, int] | Sequence[int]) -> float:
    """Shannon entropy (base e) of a count distribution; ``0.0`` for an empty one."""
    values = list(counts.values()) if isinstance(counts, Mapping) else list(counts)
    total = float(sum(values))
    if total <= 0:
        return 0.0
    entropy = 0.0
    for value in values:
        if value <= 0:
            continue
        probability = value / total
        entropy -= probability * math.log(probability)
    return entropy


def effective_count(counts: Mapping[str, int] | Sequence[int]) -> float:
    """``exp(entropy)``: the number of equally-common categories with the same spread.

    An empty distribution has zero effective categories; a single category has one.
    """
    values = list(counts.values()) if isinstance(counts, Mapping) else list(counts)
    if sum(value for value in values if value > 0) <= 0:
        return 0.0
    return round(math.exp(shannon_entropy(values)), 4)


# ---------------------------------------------------------------------------
# Novelty context and pre-screen (P7 groundwork, consumed by the P4/P5 planner)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoveltyContext:
    """Sets of what local research has already seen, plus archive niche occupancy."""

    canonical_keys: frozenset[str]
    skeleton_hashes: frozenset[str]
    grammar_hashes: frozenset[str]
    semantic_hashes: frozenset[str]
    datasets: frozenset[str]
    motifs: frozenset[str]
    candidate_count: int = 0
    #: Category history tracked separately from datasets (P7).
    categories: frozenset[str] = frozenset()
    #: ``grammar_skeleton_hash -> archive member count``: real niche occupancy (P7/P8).
    archive_occupancy: Mapping[str, int] = dataclasses_field(default_factory=dict)

    @classmethod
    def empty(cls) -> "NoveltyContext":
        return cls(frozenset(), frozenset(), frozenset(), frozenset(), frozenset(), frozenset(), 0)

    def known(self, field: str, value: str | None) -> bool:
        if value is None:
            return False
        if field == "datasets":
            return value in self.datasets
        if field == "categories":
            return value in self.categories
        container = {
            "skeleton_hash": self.skeleton_hashes,
            "grammar_hash": self.grammar_hashes,
            "semantic_hash": self.semantic_hashes,
            "motif_id": self.motifs,
            "canonical_key": self.canonical_keys,
        }[field]
        return value in container


def _median(values: Sequence[float]) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 6)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 6)


def campaign_diversity_report(db: Any, campaign_id: str, catalog: Any = None) -> dict[str, Any]:
    """Entropy-aware diversity report for one campaign (P13).

    Every dimension is measured *separately* — exact, current-skeleton, grammar and semantic
    diversity — so field-name variation cannot masquerade as structural diversity. Effective
    counts use ``exp(Shannon entropy)``: "20 motifs exist but 95% of candidates use one" is
    reported as an effective motif count near 1. Nothing here calls BRAIN.
    """
    metadata = catalog_metadata(catalog) if catalog is not None else load_field_metadata()
    trials = db.trials(campaign_id)
    candidate_ids = sorted({int(row["candidate_id"]) for row in trials if row["candidate_id"] is not None})
    # Lineage is read across campaigns: a parent may well be an earlier campaign's elite, and a
    # parent->child distance that cannot see the parent would silently report nothing at all.
    lineage_ids: set[int] = set(candidate_ids)
    for row in trials:
        try:
            lineage_ids.update(int(pid) for pid in json.loads(str(row["parent_ids_json"] or "[]")))
        except (ValueError, TypeError):
            continue
    lookup_ids = sorted(lineage_ids)
    candidates: dict[int, dict[str, Any]] = {}
    if lookup_ids:
        placeholders = ",".join("?" for _ in lookup_ids)
        for row in db.query(f"SELECT * FROM candidates WHERE id IN ({placeholders})", tuple(lookup_ids)):
            candidates[int(row["id"])] = dict(row)

    def counts_of(values: Iterable[str]) -> dict[str, int]:
        result: dict[str, int] = {}
        for value in values:
            if value:
                result[str(value)] = result.get(str(value), 0) + 1
        return result

    fields: set[str] = set()
    datasets: set[str] = set()
    categories: set[str] = set()
    cross_dataset = 0
    for candidate_id in candidate_ids:
        row = candidates.get(candidate_id)
        if not row:
            continue
        profile = None
        raw_profile = row.get("source_profile_json")
        if raw_profile:
            try:
                profile = json.loads(str(raw_profile))
            except ValueError:
                profile = None
        if not isinstance(profile, Mapping) or not profile.get("field_ids"):
            profile = derive_source_profile(str(row.get("normalized_expression") or ""), metadata)
        fields.update(profile.get("field_ids") or [])
        datasets.update(profile.get("datasets") or [])
        categories.update(profile.get("categories") or [])
        cross_dataset += int(bool(profile.get("cross_dataset")))

    skeleton_counts = counts_of(
        str(candidates[cid].get("skeleton_hash") or "") for cid in candidate_ids if cid in candidates
    )
    grammar_counts = counts_of(str(row["grammar_skeleton_hash"] or "") for row in trials)
    semantic_counts = counts_of(str(row["semantic_skeleton_hash"] or "") for row in trials)
    motif_counts = counts_of(str(row["motif_id"] or "") for row in trials)
    dataset_counts = counts_of(datasets)
    family_counts = counts_of(str(row["signal_family"] or "") for row in trials)
    mode_counts = counts_of(str(row["generation_mode"] or "") for row in trials)

    def pass_by(dimension: str, statuses: frozenset[str]) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in trials:
            if str(row.get("candidate_status") or "") in statuses:
                key = str(row.get(dimension) or "none")
                result[key] = result.get(key, 0) + 1
        return dict(sorted(result.items(), key=lambda item: (-item[1], item[0])))

    is_pass_statuses = frozenset({"IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"})
    corr_pass_statuses = frozenset({"CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"})

    # Parent -> child grammar distance, only where both sides are known.
    distances: list[float] = []
    for row in trials:
        child_id = row["candidate_id"]
        if child_id is None or int(child_id) not in candidates:
            continue
        try:
            parent_ids = json.loads(str(row["parent_ids_json"] or "[]"))
        except ValueError:
            parent_ids = []
        child_hash = str(row["grammar_skeleton_hash"] or "")
        if not parent_ids or not child_hash:
            continue
        for parent_id in parent_ids:
            parent = candidates.get(int(parent_id))
            if not parent:
                continue
            distance = grammar.grammar_distance(
                str(parent.get("normalized_expression") or ""),
                str(candidates[int(child_id)].get("normalized_expression") or ""),
                fields=metadata,
            )
            if distance > 0.0:
                distances.append(distance)

    correlation = _median_pairwise_correlation(db, candidate_ids, candidates)
    duplicates = sum(1 for row in trials if row["is_duplicate"])
    near_duplicates = sum(
        1 for cid in candidate_ids
        if cid in candidates and candidates[cid].get("near_duplicate_of") is not None
    )
    total = len(trials)
    return {
        "campaign_id": campaign_id,
        "trial_count": total,
        "unique_exact_candidates": len(candidate_ids),
        "unique_fields": len(fields),
        "unique_datasets": len(datasets),
        "unique_categories": len(categories),
        "unique_current_skeletons": len(skeleton_counts),
        "unique_grammar_skeletons": len(grammar_counts),
        "unique_semantic_skeletons": len(semantic_counts),
        "unique_motifs": len(motif_counts),
        "effective_motif_count": effective_count(motif_counts),
        "effective_dataset_count": effective_count(dataset_counts),
        "effective_grammar_count": effective_count(grammar_counts),
        "effective_semantic_count": effective_count(semantic_counts),
        "effective_family_count": effective_count(family_counts),
        "cross_dataset_share": round(cross_dataset / len(candidate_ids), 6) if candidate_ids else None,
        "duplicate_rate": round(duplicates / total, 6) if total else None,
        "near_duplicate_rate": round(near_duplicates / len(candidate_ids), 6) if candidate_ids else None,
        "median_parent_child_grammar_distance": _median(distances),
        "parent_child_pairs": len(distances),
        "median_pairwise_pnl_correlation": correlation,
        "generation_mode": mode_counts,
        "motif": motif_counts,
        "is_pass_by_motif": pass_by("motif_id", is_pass_statuses),
        "corr_pass_by_motif": pass_by("motif_id", corr_pass_statuses),
        "active_by_motif": pass_by("motif_id", frozenset({"ACTIVE"})),
        "is_pass_by_grammar": pass_by("grammar_skeleton_hash", is_pass_statuses),
        "corr_pass_by_semantic": pass_by("semantic_skeleton_hash", corr_pass_statuses),
    }


def _median_pairwise_correlation(
    db: Any,
    candidate_ids: Sequence[int],
    candidates: Mapping[int, Mapping[str, Any]],
) -> float | None:
    """Median pairwise daily-return correlation across the campaign, when PnL is cached."""
    try:
        import correlation
    except ImportError:  # pragma: no cover - part of this repo
        return None
    cached_pnl = db.active_pnl()
    series: list[list[float]] = []
    for candidate_id in candidate_ids:
        row = candidates.get(candidate_id)
        alpha_id = row.get("brain_alpha_id") if row else None
        if not alpha_id:
            continue
        cached = cached_pnl.get(str(alpha_id))
        if not cached:
            continue
        _dates, levels = cached[0], cached[1]
        returns = correlation.daily_returns([float(value) for value in levels])
        if len(returns) >= 20:
            series.append(returns)
    if len(series) < 2:
        return None
    values: list[float] = []
    for index, left in enumerate(series):
        for right in series[index + 1:]:
            length = min(len(left), len(right))
            coefficient = correlation.safe_corrcoef(left[-length:], right[-length:])
            if coefficient is not None:
                values.append(abs(float(coefficient)))
    return _median(values)


def archive_occupancy(db: Any) -> dict[str, int]:
    """``grammar_skeleton_hash -> archive member count`` from the niche cells (P7/P8).

    Real archive-cell occupancy, not a frequency proxy; read with a tolerant query so a
    store without the archive tables simply reports an empty occupancy.
    """
    occupancy: dict[str, int] = {}
    try:
        rows = db.query("SELECT dimensions_json, member_count FROM archive_cells")
    except Exception:  # pragma: no cover - advisory only
        return occupancy
    for row in rows:
        try:
            dimensions = json.loads(str(row["dimensions_json"] or "{}"))
        except ValueError:
            continue
        key = str((dimensions or {}).get("grammar_skeleton_hash") or "")
        if key:
            occupancy[key] = occupancy.get(key, 0) + max(0, int(row["member_count"] or 0))
    return occupancy


def novelty_context(db: Any, catalog: Any = None) -> NoveltyContext:
    """Read the local history once: exact keys, skeleton hashes, sources and motifs."""
    metadata = catalog_metadata(catalog) if catalog is not None else load_field_metadata()
    canonical_keys: set[str] = set()
    skeleton_hashes: set[str] = set()
    grammar_hashes: set[str] = set()
    semantic_hashes: set[str] = set()
    datasets: set[str] = set()
    categories: set[str] = set()
    motifs: set[str] = set()
    rows = db.query(
        "SELECT canonical_key, skeleton_hash, normalized_expression, mutation_parameters_json, signal_family FROM candidates"
    )
    for row in rows:
        canonical_keys.add(str(row["canonical_key"]))
        if row["skeleton_hash"]:
            skeleton_hashes.add(str(row["skeleton_hash"]))
        expression = str(row["normalized_expression"] or "")
        if expression:
            grammar_hashes.add(grammar.grammar_skeleton_hash(expression, metadata))
            semantic_hashes.add(grammar.semantic_skeleton_hash(expression, metadata))
        profile = derive_source_profile(expression, metadata)
        datasets.update(profile["datasets"])
        categories.update(profile["categories"])
        if row["signal_family"]:
            datasets.add(str(row["signal_family"]))
        try:
            parameters = json.loads(str(row["mutation_parameters_json"] or "{}"))
        except ValueError:
            parameters = {}
        if isinstance(parameters, Mapping) and parameters.get("motif_id"):
            motifs.add(str(parameters["motif_id"]))
    return NoveltyContext(
        canonical_keys=frozenset(canonical_keys),
        skeleton_hashes=frozenset(skeleton_hashes),
        grammar_hashes=frozenset(grammar_hashes),
        semantic_hashes=frozenset(semantic_hashes),
        datasets=frozenset(datasets),
        motifs=frozenset(motifs),
        candidate_count=len(rows),
        categories=frozenset(categories),
        archive_occupancy=archive_occupancy(db),
    )


@dataclass(frozen=True)
class NoveltyReport:
    """Components + decision of one proposal's novelty pre-screen."""

    score: float
    decision: str
    reason: str
    exact_novel: bool
    skeleton_novel: bool
    grammar_novel: bool
    semantic_novel: bool
    dataset_novel: bool
    motif_novel: bool
    #: Category history is tracked separately from datasets (P7).
    category_novel: bool = False
    #: ``1/(1 + archive members in this grammar niche)``: real cell occupancy (P7).
    archive_sparsity: float = 0.0
    #: Numeric ``grammar_distance(parent, child)`` when a parent is known (P7). For a
    #: two-parent child this is the *minimum* distance to either parent (clone protection).
    parent_distance: float | None = None
    #: Every parent distance in parent order, so a two-parent decision is auditable (P7).
    parent_distances: tuple[float, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "decision": self.decision,
            "reason": self.reason,
            "exact_novel": self.exact_novel,
            "skeleton_novel": self.skeleton_novel,
            "grammar_novel": self.grammar_novel,
            "semantic_novel": self.semantic_novel,
            "dataset_novel": self.dataset_novel,
            "motif_novel": self.motif_novel,
            "category_novel": self.category_novel,
            "archive_sparsity": self.archive_sparsity,
            "parent_distance": self.parent_distance,
            "parent_distances": list(self.parent_distances),
        }


def _parent_distance(
    parent: Any,
    expression: str,
    metadata: Mapping[str, Any],
    parent_source_profile: Mapping[str, Any] | None,
    child_profile: Mapping[str, Any],
) -> float | None:
    """Numeric structural distance between parent and child (P7).

    Uses :func:`expression_grammar.grammar_distance` when the parent is parseable, and falls
    back to source-profile overlap (dataset/category Jaccard) when only metadata is known.
    """
    if parent is not None:
        try:
            return grammar.grammar_distance(parent, expression, fields=metadata)
        except grammar.GrammarError:
            pass
    if parent_source_profile:
        def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
            left_set, right_set = set(left), set(right)
            if not left_set and not right_set:
                return 1.0
            return len(left_set & right_set) / max(1, len(left_set | right_set))

        parent_sources = list(parent_source_profile.get("datasets") or []) + list(parent_source_profile.get("categories") or [])
        child_sources = list(child_profile.get("datasets") or []) + list(child_profile.get("categories") or [])
        return round(1.0 - jaccard(parent_sources, child_sources), 6)
    return None


def screen_novelty(
    expression: str,
    *,
    catalog: Any = None,
    context: NoveltyContext | None = None,
    canonical_key: str | None = None,
    settings: Mapping[str, Any] | None = None,
    motif_id: str | None = None,
    parent: Any = None,
    parents: Sequence[Any] | None = None,
    parent_grammar_hash: str | None = None,
    parent_source_profile: Mapping[str, Any] | None = None,
    request_novelty: bool = False,
) -> NoveltyReport:
    """Pre-screen a generated proposal against local history.

    The screen returns KEEP by default. ``SKIP_REDUNDANT`` is only reachable when the caller
    explicitly asked for novelty *and* the proposal is an exact duplicate; otherwise a
    duplicate is merely downweighted. Nothing here rejects a candidate outright.
    """
    context = context or NoveltyContext.empty()
    metadata = catalog_metadata(catalog) if catalog is not None else load_field_metadata()
    if canonical_key is None:
        canonical_key = canonical.canonical_key(expression, settings or {})
    skeleton = canonical.skeleton_hash(expression)
    grammar_hash = grammar.grammar_skeleton_hash(expression, metadata)
    semantic_hash = grammar.semantic_skeleton_hash(expression, metadata)
    profile = derive_source_profile(expression, metadata)
    motif = motif_id or ""

    exact_novel = canonical_key not in context.canonical_keys
    skeleton_novel = skeleton not in context.skeleton_hashes
    grammar_novel = grammar_hash not in context.grammar_hashes
    semantic_novel = semantic_hash not in context.semantic_hashes
    dataset_novel = bool(profile["datasets"]) and any(dataset not in context.datasets for dataset in profile["datasets"])
    category_novel = bool(profile["categories"]) and any(
        category not in context.categories for category in profile["categories"]
    )
    motif_novel = bool(motif) and motif not in context.motifs
    # Real archive-cell occupancy, not a grammar-frequency proxy (P7/P8).
    archive_sparsity = round(1.0 / (1.0 + context.archive_occupancy.get(grammar_hash, 0)), 6)

    # A two-parent child can be far from one lineage branch and a near-clone of the other, so
    # clone protection uses the minimum distance across all known parents (P7); the full
    # vector is kept for diagnostics.
    parent_distances: list[float] = []
    if parent is not None:
        first = _parent_distance(parent, expression, metadata, parent_source_profile, profile)
        if first is not None:
            parent_distances.append(first)
    for extra in (parents or ()):
        if extra is None:
            continue
        distance = _parent_distance(extra, expression, metadata, None, profile)
        if distance is not None:
            parent_distances.append(distance)
    parent_distance = min(parent_distances) if parent_distances else None
    if parent_distance is None and parent_grammar_hash is not None:
        parent_distance = 0.0 if grammar_hash == parent_grammar_hash else 1.0

    score = 0.0
    score += 0.30 if exact_novel else 0.0
    score += 0.12 if skeleton_novel else 0.0
    score += 0.18 if grammar_novel else 0.0
    score += 0.12 if semantic_novel else 0.0
    score += 0.08 if dataset_novel else 0.0
    score += 0.05 if category_novel else 0.0
    score += 0.05 if motif_novel else 0.0
    score += 0.05 * archive_sparsity
    if parent_distance is not None:
        # Graded, not equality-only (P7): a parent-child clone is the least informative
        # attempt and pays the full penalty; a structurally distant child pays none.
        score += 0.10 * (parent_distance - 1.0)
    score = round(max(0.0, min(1.0, score)), 6)

    components = (exact_novel, skeleton_novel, grammar_novel, semantic_novel, dataset_novel,
                  motif_novel, category_novel, archive_sparsity, parent_distance)
    distances = tuple(parent_distances)
    if not exact_novel and request_novelty:
        return NoveltyReport(score, SKIP_REDUNDANT, "exact duplicate under an explicit novelty request",
                             *components, parent_distances=distances)
    if score < DOWNWEIGHT_SCORE and not exact_novel:
        return NoveltyReport(score, DOWNWEIGHT, "closely related to existing work",
                             *components, parent_distances=distances)
    return NoveltyReport(score, KEEP, "distinct research hypothesis", *components, parent_distances=distances)
