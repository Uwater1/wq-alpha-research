"""Empirical field and operator intelligence for P4.

The reference files describe what BRAIN exposes; this module records what the local
research process has actually attempted and observed. Two properties are deliberate:

* **Scope identity.** Coverage is keyed by ``field + catalog_version + scope_hash``, so
  evidence gathered in one region/universe/delay can never be pooled with another. The
  same field name can mean different data in a different scope.
* **Evidence, not status.** A pipeline stage is derived from what actually happened
  (a ``validation_passed`` event, a terminal simulation row, a submission row) instead of
  being inferred from the candidate's current status. A statically rejected candidate is
  therefore never counted as simulated.

Operator type rules come from ``scripts/compatibility.py`` — the same model the validator
and the generator use — so this table and local rejection cannot disagree.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical
import compatibility
from generator import Catalog, Field
import research_db

OPERATORS_PATH = compatibility.OPERATORS_PATH

#: Scope the supplied field snapshot describes.
CATALOG_SCOPE: dict[str, Any] = {"region": "USA", "universe": "TOP3000", "delay": 1}

#: Candidate statuses that mean the IS gate was passed.
IS_PASS_STATUSES = frozenset({"IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"})
CORR_PASS_STATUSES = frozenset({"CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"})
ACTIVE_STATUSES = frozenset({"ACTIVE"})

#: ``corr_status`` values that mean a fresh local correlation check was recorded
#: (mirrors correlation.FRESH_STATUSES) and the default local limit it is judged against.
CORR_FRESH_STATUSES = frozenset({"ok", "empty_book"})
CORRELATION_PASS_LIMIT = 0.7


def catalog_version(path: Path) -> str:
    return compatibility.reference_version(path)


def operator_compatibility(path: Path = OPERATORS_PATH) -> list[dict[str, Any]]:
    """Machine-readable operator constraints, derived from the shared model.

    The persisted ``operator_compatibility`` table is a materialized view of this list, so
    the validator (which reads the model directly) and the table cannot drift apart.
    """
    version = catalog_version(path)
    raw_by_name = {str(item["name"]).lower(): item for item in compatibility.operator_definitions(path)}
    rows: list[dict[str, Any]] = []
    for name, spec in sorted(compatibility.constraints(path).items()):
        source = raw_by_name.get(name, {})
        rows.append({
            **spec.as_dict(),
            "catalog_version": version,
            "source": {
                "category": source.get("category"),
                "definition": source.get("definition"),
                "scope": source.get("scope"),
            },
        })
    return rows


# ---------------------------------------------------------------------------
# Candidate evidence
# ---------------------------------------------------------------------------


def _parse_json(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw)) if raw else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _row_scope(row: Mapping[str, Any]) -> dict[str, Any]:
    return canonical.scope_from_settings(_parse_json(row.get("settings_json")))


def _candidate_index(db: research_db.ResearchDB) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Group every candidate by ``(field, scope_hash)`` and remember each scope."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    scopes: dict[str, dict[str, Any]] = {}
    for row in db.query("SELECT * FROM candidates ORDER BY created_at, id"):
        scope = _row_scope(row)
        scope_key = canonical.scope_hash(scope)
        scopes[scope_key] = scope
        expression = str(row.get("normalized_expression") or row.get("expression") or "")
        for name in canonical.fields_of(expression):
            grouped[(name, scope_key)].append(dict(row))
    return grouped, scopes


def _history_counts(db: research_db.ResearchDB, scope_key: str) -> dict[str, int]:
    """Attempts per field, recomputed from candidate history in one scope."""
    counts: dict[str, int] = defaultdict(int)
    for row in db.query("SELECT normalized_expression, settings_json FROM candidates"):
        if canonical.scope_hash(canonical.scope_from_settings(_parse_json(row.get("settings_json")))) != scope_key:
            continue
        for name in canonical.fields_of(str(row.get("normalized_expression") or "")):
            counts[name] += 1
    return dict(counts)


def _evidence_maps(db: research_db.ResearchDB) -> dict[str, set[int]]:
    """Stage membership derived from real artefacts, never from current status alone."""
    # `validation_passed` is logged with the candidate id in entity_id, not in the
    # transport column, so read it from there rather than inventing a new event.
    validated = {
        int(row["id"]) for row in db.query(
            "SELECT DISTINCT CAST(entity_id AS INTEGER) AS id FROM events "
            "WHERE event='validation_passed' AND entity='candidate' AND entity_id IS NOT NULL"
        )
    }
    simulated = {
        int(row["candidate_id"]) for row in db.query(
            "SELECT DISTINCT candidate_id FROM simulations "
            "WHERE status='DONE' AND candidate_id IS NOT NULL"
        )
    }
    submitted = {
        int(row["candidate_id"]) for row in db.query(
            "SELECT DISTINCT candidate_id FROM submissions WHERE candidate_id IS NOT NULL"
        )
    }
    is_pass: set[int] = set()
    corr_pass: set[int] = set()
    active: set[int] = set()
    rejected: set[int] = set()
    for row in db.query("SELECT id, status, is_pass, corr_status, self_corr FROM candidates"):
        candidate_id = int(row["id"])
        status = str(row["status"] or "")
        if status in IS_PASS_STATUSES or int(row["is_pass"] or 0) == 1:
            is_pass.add(candidate_id)
        corr_status = str(row["corr_status"] or "").lower()
        self_corr = row["self_corr"]
        passed = self_corr is None or float(self_corr) <= CORRELATION_PASS_LIMIT
        if status in CORR_PASS_STATUSES or (corr_status in CORR_FRESH_STATUSES and passed):
            corr_pass.add(candidate_id)
        if status in ACTIVE_STATUSES:
            active.add(candidate_id)
        if status == "REJECTED":
            rejected.add(candidate_id)
    return {
        "validated": validated,
        "simulated": simulated,
        "is_pass": is_pass,
        "corr_pass": corr_pass,
        "submitted": submitted,
        "active": active,
        "rejected": rejected,
    }


def _stage_counts(rows: Sequence[Mapping[str, Any]], evidence: Mapping[str, set[int]]) -> dict[str, int]:
    """How many of ``rows`` reached each pipeline stage, according to real evidence."""
    ids = {int(row["id"]) for row in rows}
    return {stage: len(ids & members) for stage, members in evidence.items()}


# ---------------------------------------------------------------------------
# Refresh / query
# ---------------------------------------------------------------------------


def refresh(db: research_db.ResearchDB, catalog: Catalog | None = None) -> dict[str, int]:
    """Recompute scope-aware field coverage and the operator compatibility table."""
    catalog = catalog or Catalog()
    version = catalog.version
    catalog_scope = {key: catalog.scope[key] for key in canonical.SCOPE_SETTINGS_FIELDS}
    catalog_scope_key = canonical.scope_hash(catalog_scope)
    grouped, observed_scopes = _candidate_index(db)
    evidence = _evidence_maps(db)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    targets: dict[tuple[str, str], dict[str, Any]] = {}
    for field in catalog.fields:
        targets[(field.name, catalog_scope_key)] = {"field": field, "scope": catalog_scope, "cataloged": 1}
    for (name, scope_key), _rows in grouped.items():
        if (name, scope_key) in targets:
            continue
        scope = observed_scopes.get(scope_key) or catalog_scope
        known = catalog.get(name)
        targets[(name, scope_key)] = {
            "field": known or Field(name=name, category="observed", dataset="observed", field_type="UNKNOWN", coverage=None),
            "scope": scope,
            "cataloged": int(known is not None and scope_key == catalog_scope_key),
        }

    fields_written = 0
    with db._tx() as conn:
        for (name, scope_key), target in targets.items():
            rows = grouped.get((name, scope_key), [])
            stages = _stage_counts(rows, evidence)
            reasons = sorted({str(row.get("failure_reason") or "") for row in rows if row.get("failure_reason")})
            sharpe = [float(row["sharpe"]) for row in rows if isinstance(row.get("sharpe"), (int, float))]
            fitness = [float(row["fitness"]) for row in rows if isinstance(row.get("fitness"), (int, float))]
            turnover = [float(row["turnover"]) for row in rows if isinstance(row.get("turnover"), (int, float))]
            field = target["field"]
            conn.execute(
                """INSERT INTO field_coverage(
                       field_id, dataset, category, field_type, catalog_version, scope_hash, scope_json,
                       cataloged, validated, simulated, is_pass, corr_pass, submitted, active, rejected,
                       attempts, median_sharpe, median_fitness, median_turnover, failure_reasons_json,
                       last_tested_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(field_id, catalog_version, scope_hash)
                   DO UPDATE SET
                       dataset=excluded.dataset, category=excluded.category, field_type=excluded.field_type,
                       cataloged=excluded.cataloged, validated=excluded.validated, simulated=excluded.simulated,
                       is_pass=excluded.is_pass, corr_pass=excluded.corr_pass, submitted=excluded.submitted,
                       active=excluded.active, rejected=excluded.rejected, attempts=excluded.attempts,
                       median_sharpe=excluded.median_sharpe, median_fitness=excluded.median_fitness,
                       median_turnover=excluded.median_turnover, failure_reasons_json=excluded.failure_reasons_json,
                       last_tested_at=excluded.last_tested_at""",
                (
                    name, field.dataset, field.category, field.field_type, version, scope_key,
                    json.dumps(target["scope"], sort_keys=True), target["cataloged"],
                    stages["validated"], stages["simulated"], stages["is_pass"], stages["corr_pass"],
                    stages["submitted"], stages["active"], stages["rejected"], len(rows),
                    median(sharpe) if sharpe else None, median(fitness) if fitness else None,
                    median(turnover) if turnover else None, json.dumps(reasons),
                    max((str(row.get("updated_at")) for row in rows), default=None),
                ),
            )
            fields_written += 1

        operators = operator_compatibility()
        for item in operators:
            conn.execute(
                """INSERT INTO operator_compatibility(operator_name,catalog_version,input_type,output_type,requires_group,requires_vector,min_args,max_args,source_json)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(operator_name,catalog_version) DO UPDATE SET
                   input_type=excluded.input_type, output_type=excluded.output_type, requires_group=excluded.requires_group,
                   requires_vector=excluded.requires_vector, min_args=excluded.min_args, max_args=excluded.max_args,
                   source_json=excluded.source_json""",
                (item["operator_name"], item["catalog_version"], item["input_type"], item["output_type"],
                 item["requires_group"], item["requires_vector"], item["min_args"], item["max_args"],
                 json.dumps(item["source"], sort_keys=True, default=str)),
            )
    return {
        "fields": fields_written,
        "cataloged_fields": len(catalog.fields),
        "scopes": len({scope_key for _name, scope_key in targets}),
        "operators": len(operators),
        "catalog_version": version,
    }


def coverage_attempts(
    db: research_db.ResearchDB,
    fields: Iterable[Field] | None = None,
    *,
    scope: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Attempts per field inside one scope; candidate history is authoritative.

    ``field_coverage`` is used only as a fallback for fields with no candidate evidence in
    that scope (for example rows restored from an older database), so a stale aggregate can
    never outrank freshly observed history.
    """
    scope_key = canonical.scope_hash(scope or CATALOG_SCOPE)
    names = {field.name for field in fields} if fields is not None else None
    counts: dict[str, int] = {}
    for row in db.query("SELECT field_id, attempts FROM field_coverage WHERE scope_hash=?", (scope_key,)):
        counts[str(row["field_id"])] = int(row["attempts"] or 0)
    for name, attempts in _history_counts(db, scope_key).items():
        counts[name] = attempts
    if names is not None:
        return {name: counts.get(name, 0) for name in names}
    return counts


def under_tested(
    db: research_db.ResearchDB,
    fields: Iterable[Field],
    *,
    scope: Mapping[str, Any] | None = None,
) -> list[Field]:
    """Fields ordered by attempts, then dataset/name, for coverage-aware generation."""
    candidates = list(fields)
    counts = coverage_attempts(db, candidates, scope=scope)
    return sorted(candidates, key=lambda field: (counts.get(field.name, 0), field.dataset, field.name))


def summary(db: research_db.ResearchDB) -> dict[str, Any]:
    return {
        "datasets": db.query(
            "SELECT dataset, COUNT(*) AS fields, SUM(attempts) AS attempts, SUM(is_pass) AS passes, "
            "SUM(active) AS active FROM field_coverage GROUP BY dataset ORDER BY dataset"
        ),
        "scopes": db.query(
            "SELECT scope_json, COUNT(*) AS fields, SUM(attempts) AS attempts FROM field_coverage "
            "GROUP BY scope_json ORDER BY scope_json"
        ),
        "operators": db.query(
            "SELECT input_type, COUNT(*) AS operators FROM operator_compatibility GROUP BY input_type ORDER BY input_type"
        ),
        "catalog_versions": db.query(
            "SELECT catalog_version, COUNT(*) AS fields FROM field_coverage GROUP BY catalog_version"
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh/query empirical field and operator coverage")
    parser.add_argument("--db")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        if args.refresh:
            print(json.dumps(refresh(db), indent=2, sort_keys=True))
        print(json.dumps(summary(db), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
