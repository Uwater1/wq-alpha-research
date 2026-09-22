"""Catalog-driven candidate generation and explicit mutation operators.

This module deliberately contains no BRAIN calls and no LLM trust boundary. It turns the
local field/operator snapshots into reproducible proposals, then hands every proposal to
ResearchDB so validation, canonical deduplication, privacy, and lineage are centralized.
The catalog is scope-limited; generated candidates retain the catalog version in lineage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical
import research_db

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO_ROOT / "references" / "wq_usa_top3000_delay1_data_fields.json"
GENERATOR_VERSION = "catalog-generator-v1"
DEFAULT_WINDOWS = (20, 60, 126, 252)
DEFAULT_DECAYS = (4, 6, 10, 20)


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

    def select(self, family: str = "all", dataset: str | None = None) -> list[Field]:
        family = family.lower()
        selected = [field for field in self.fields if family in {"all", "*"} or field.category == family or field.dataset == family]
        if dataset:
            selected = [field for field in selected if field.dataset == dataset]
        return selected


class CandidateGenerator:
    """Deterministic templates plus explainable failure-directed mutations."""

    def __init__(self, db: research_db.ResearchDB, catalog: Catalog | None = None, *, seed: int = 0) -> None:
        self.db = db
        self.catalog = catalog or Catalog()
        self.seed = int(seed)

    def proposals(
        self,
        *,
        count: int,
        family: str = "all",
        dataset: str | None = None,
        all_fields: bool = False,
    ) -> list[Proposal]:
        fields = self.catalog.select(family, dataset)
        rng = random.Random(self.seed)
        # Coverage intelligence is advisory and read-only: unknown/unrefreshed fields
        # retain deterministic seed ordering, while refreshed catalogs prioritize gaps.
        try:
            from field_intelligence import under_tested
            fields = under_tested(self.db, fields)
        except (ImportError, sqlite3.Error):
            pass
        rng.shuffle(fields)
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

    def queue(self, campaign_id: str, proposals: Iterable[Proposal]) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for proposal in proposals:
            outcome = self.db.queue_candidate(
                proposal.expression,
                proposal.settings,
                source="generator",
                signal_family=proposal.family,
                parent_id=proposal.parent_ids[0] if proposal.parent_ids else None,
                parent_ids=proposal.parent_ids,
                generation=1 if proposal.parent_ids else 0,
                mutation_type=proposal.mutation_type,
                campaign_id=campaign_id,
                mutation_parameters={**proposal.parameters, "catalog_version": self.catalog.version},
                generator_version=GENERATOR_VERSION,
                reason=proposal.reason,
            )
            outcomes.append({
                "expression": proposal.expression,
                "family": proposal.family,
                "mutation_type": proposal.mutation_type,
                "action": outcome.action,
                "candidate_id": outcome.candidate_id,
                "status": outcome.status,
                "issues": outcome.issues,
            })
        return outcomes

    def mutate(self, parent: Mapping[str, Any], *, count: int = 4, campaign_id: str = "mutation") -> list[Proposal]:
        expression = str(parent.get("normalized_expression") or parent.get("expression") or "")
        parent_id = int(parent["id"]) if parent.get("id") is not None else None
        family = str(parent.get("signal_family") or "mutation")
        failure = " ".join(str(parent.get(key) or "") for key in ("failure_reason", "gate_reason")).lower()
        settings = _settings(parent)
        proposals: list[Proposal] = []
        if "turnover" in failure or "turnover" in str(parent.get("failure_reason") or "").lower():
            for hump in (0.005, 0.01):
                proposals.append(Proposal(
                    f"hump({expression}, {hump})", settings, family, "turnover_repair",
                    {"hump": hump}, (parent_id,) if parent_id else (), "repair diagnosed high turnover",
                ))
        elif "corr" in failure or "correlation" in failure:
            for field in self.catalog.select("all")[: max(1, count)]:
                if field.name not in canonical.fields_of(expression) and field.field_type == "MATRIX":
                    proposals.append(Proposal(
                        f"add({expression}, rank(ts_rank({field.name}, 60)))", settings, family, "correlation_repair",
                        {"replacement_field": field.name}, (parent_id,) if parent_id else (), "change the economic return path",
                    ))
        else:
            fields = self.catalog.select("all")
            for field in fields:
                if field.name not in canonical.fields_of(expression) and field.field_type in {"MATRIX", "VECTOR"}:
                    replacement = f"vec_avg({field.name})" if field.field_type == "VECTOR" else field.name
                    proposals.append(Proposal(
                        expression.replace(next(iter(canonical.fields_of(expression)), ""), replacement, 1),
                        settings, family, "field_swap", {"replacement_field": field.name},
                        (parent_id,) if parent_id else (), "replace the data source after a weak result",
                    ))
                if len(proposals) >= count:
                    break
        return proposals[:count]

    def _field_proposal(self, field: Field) -> Proposal | None:
        window = DEFAULT_WINDOWS[(self.seed + len(field.name)) % len(DEFAULT_WINDOWS)]
        decay = DEFAULT_DECAYS[(self.seed + len(field.dataset)) % len(DEFAULT_DECAYS)]
        value = field.name
        if field.field_type == "VECTOR":
            value = f"vec_avg({value})"
        elif field.field_type in {"GROUP", "UNIVERSE", "SYMBOL"}:
            return None
        expression = f"group_rank(ts_rank({value}, {window}), subindustry)"
        return Proposal(
            expression,
            {"decay": decay},
            field.dataset,
            "dataset_coverage",
            {"field": field.name, "dataset": field.dataset, "field_type": field.field_type, "window": window, "decay": decay},
            reason="catalog coverage across supplied BRAIN datasets",
        )


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
