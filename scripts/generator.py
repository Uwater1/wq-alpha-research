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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical
import compatibility
import research_db

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO_ROOT / "references" / "wq_usa_top3000_delay1_data_fields.json"
GENERATOR_VERSION = "catalog-generator-v2"
DEFAULT_WINDOWS = (20, 60, 126, 252)
DEFAULT_DECAYS = (4, 6, 10, 20)
DEFAULT_NEUTRALIZATIONS = ("SUBINDUSTRY", "INDUSTRY", "SECTOR")
#: Groups a repair can broaden a signal to, from narrowest to widest.
GROUP_BROADENING = ("subindustry", "industry", "sector", "market")
SMOOTHING_WINDOWS = (5, 10, 22)

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
    ) -> list[Proposal]:
        fields = self.catalog.select(family, dataset)
        fields = self.coverage_ordered(fields, rng=random.Random(self.seed))
        if not all_fields:
            fields = fields[: max(0, int(count))]
        proposals: list[Proposal] = []
        for field in fields:
            proposal = self._field_proposal(field)
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
                mutation_parameters={**proposal.parameters, "catalog_version": self.catalog.version},
                generator_version=GENERATOR_VERSION,
                reason=proposal.reason,
                field_catalog_version=self.catalog.version,
                operator_catalog_version=self.catalog.operator_version,
                provenance={"scope": self.catalog.scope, "seed": self.seed, "snapshot": self.catalog.snapshot},
            )
            outcomes.append({
                "expression": proposal.expression,
                "family": proposal.family,
                "mutation_type": proposal.mutation_type,
                "generation": generation,
                "action": outcome.action,
                "candidate_id": outcome.candidate_id,
                "status": outcome.status,
                "issues": outcome.issues,
            })
        return outcomes

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
        return Proposal(
            expression=expression,
            settings=settings,
            family=family,
            mutation_type=mutation_type,
            parameters=dict(parameters),
            parent_ids=parent_ids,
            reason=reason,
            generation=generation,
        )

    def _turnover_repair(self, parent, expression, settings, family, parent_ids, generation) -> list[Proposal]:
        base_decay = int(settings.get("decay", 6) or 6)
        repair_settings = {**settings, "decay": min(512, base_decay + 4)}
        proposals = [
            self._proposal(
                f"hump({expression}, hump={hump})", repair_settings, family, "turnover_repair",
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
        proposals = self._combine_signals(
            parent, expression, settings, family, parent_ids, generation, count=2,
        )
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
        proposals: list[Proposal] = []
        for field in self._alternatives(used, parent=parent, limit=count):
            proposals.append(self._proposal(
                f"add({expression}, group_rank(ts_rank({self._field_reference(field)}, 126), subindustry))",
                settings, family, "combine_signals",
                {"added_field": field.name, "added_dataset": field.dataset, "window": 126},
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

    def _field_proposal(self, field: Field) -> Proposal | None:
        window = DEFAULT_WINDOWS[(self.seed + len(field.name)) % len(DEFAULT_WINDOWS)]
        decay = DEFAULT_DECAYS[(self.seed + len(field.dataset)) % len(DEFAULT_DECAYS)]
        value = field.name
        if field.field_type == compatibility.VECTOR:
            value = f"vec_avg({value})"
        elif field.field_type in {"GROUP", "UNIVERSE", "SYMBOL"}:
            return None
        expression = f"group_rank(ts_rank({value}, {window}), subindustry)"
        return Proposal(
            expression,
            {"decay": decay},
            field.dataset,
            "dataset_coverage",
            {"field": field.name, "dataset": field.dataset, "field_type": field.field_type,
             "window": window, "decay": decay},
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
    (``hump_smoothing``, ``window_change``, ``field_swap``, ``combine_signals``, ...), so a
    repair stays explainable without losing which failure drove it.
    """
    parameters = dict(proposal.parameters)
    parameters.setdefault("operation", proposal.mutation_type)
    return replace(proposal, mutation_type=repair_type, parameters=parameters)


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
    generate.add_argument("--db", type=Path)
    mutate = sub.add_parser("mutate")
    mutate.add_argument("candidate_id", type=int)
    mutate.add_argument("--campaign", required=True)
    mutate.add_argument("--count", type=int, default=4)
    mutate.add_argument("--seed", type=int, default=0)
    mutate.add_argument("--db", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        generator = CandidateGenerator(db, seed=args.seed)
        if args.command == "generate":
            proposals = generator.proposals(count=args.count, family=args.family, dataset=args.dataset, all_fields=args.all_fields)
        else:
            parent = db.get_candidate(args.candidate_id)
            if not parent:
                raise SystemExit(f"candidate {args.candidate_id} not found")
            proposals = generator.mutate(parent, count=args.count, campaign_id=args.campaign)
        print(json.dumps(generator.queue(args.campaign, proposals), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
