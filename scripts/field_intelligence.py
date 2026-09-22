"""Empirical field and operator intelligence for P4.

The reference files describe what BRAIN exposes; this module records what the local
research process has actually attempted and observed. Scope and catalog hashes are stored
with every row so a refreshed catalog cannot silently rewrite historical meaning.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical
from generator import Catalog, Field
import research_db

OPERATORS_PATH = Path(__file__).resolve().parents[1] / "references" / "wq_operators.json"


def catalog_version(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _operator_arity(definition: str) -> tuple[int | None, int | None]:
    match = re.search(r"\(([^)]*)\)", definition or "")
    if not match:
        return None, None
    args = [part.strip() for part in match.group(1).split(",") if part.strip()]
    if not args:
        return 0, 0
    required = sum(1 for arg in args if "=" not in arg and "..." not in arg)
    maximum = None if any("..." in arg for arg in args) else len(args)
    return required, maximum


def operator_compatibility(path: Path = OPERATORS_PATH) -> list[dict[str, Any]]:
    """Convert the operator snapshot into conservative machine-readable constraints."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    version = catalog_version(path)
    result = []
    for item in raw:
        name = str(item.get("name") or "").lower()
        if not name:
            continue
        category = str(item.get("category") or "").lower()
        min_args, max_args = _operator_arity(str(item.get("definition") or ""))
        requires_vector = category == "vector"
        requires_group = category == "group" or name.startswith("group_")
        result.append({
            "operator_name": name,
            "catalog_version": version,
            "input_type": "VECTOR" if requires_vector else ("GROUP" if requires_group else "MATRIX"),
            "output_type": "MATRIX",
            "requires_group": int(requires_group),
            "requires_vector": int(requires_vector),
            "min_args": min_args,
            "max_args": max_args,
            "source": {"category": item.get("category"), "definition": item.get("definition"), "scope": item.get("scope")},
        })
    return result


def _candidate_field_rows(db: research_db.ResearchDB) -> dict[str, list[dict[str, Any]]]:
    rows = db.query("SELECT * FROM candidates ORDER BY created_at, id")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for field in canonical.fields_of(str(row.get("normalized_expression") or row.get("expression") or "")):
            grouped[field].append(row)
    return grouped


def refresh(db: research_db.ResearchDB, catalog: Catalog | None = None) -> dict[str, int]:
    catalog = catalog or Catalog()
    version = catalog.version
    grouped = _candidate_field_rows(db)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fields_written = 0
    for field in catalog.fields:
        rows = grouped.get(field.name, [])
        simulations = [row for row in rows if row.get("status") not in {"GENERATED", "VALIDATED", "QUEUED", "RETRY"}]
        passes = [row for row in rows if row.get("status") in {"IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"}]
        corr = [row for row in rows if row.get("status") in {"CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"}]
        submitted = [row for row in rows if row.get("status") in {"SUBMITTING", "ACTIVE"}]
        active = [row for row in rows if row.get("status") == "ACTIVE"]
        reasons = sorted({str(row.get("failure_reason") or "") for row in rows if row.get("failure_reason")})
        sharpe = [float(row["sharpe"]) for row in rows if isinstance(row.get("sharpe"), (int, float))]
        fitness = [float(row["fitness"]) for row in rows if isinstance(row.get("fitness"), (int, float))]
        turnover = [float(row["turnover"]) for row in rows if isinstance(row.get("turnover"), (int, float))]
        scope = {"region": "USA", "universe": "TOP3000", "delay": 1}
        with db._tx() as conn:
            conn.execute(
                """INSERT INTO field_coverage(field_id,dataset,category,field_type,catalog_version,scope_json,cataloged,validated,simulated,is_pass,corr_pass,submitted,active,rejected,attempts,median_sharpe,median_fitness,median_turnover,failure_reasons_json,last_tested_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(field_id,catalog_version) DO UPDATE SET
                   validated=excluded.validated, simulated=excluded.simulated, is_pass=excluded.is_pass, corr_pass=excluded.corr_pass,
                   submitted=excluded.submitted, active=excluded.active, rejected=excluded.rejected, attempts=excluded.attempts,
                   median_sharpe=excluded.median_sharpe, median_fitness=excluded.median_fitness, median_turnover=excluded.median_turnover,
                   failure_reasons_json=excluded.failure_reasons_json, last_tested_at=excluded.last_tested_at""",
                (field.name, field.dataset, field.category, field.field_type, version, json.dumps(scope, sort_keys=True),
                 1, int(bool(rows)), int(bool(simulations)), int(bool(passes)), int(bool(corr)), int(bool(submitted)),
                 int(bool(active)), sum(1 for row in rows if row.get("status") == "REJECTED"), len(rows),
                 median(sharpe) if sharpe else None, median(fitness) if fitness else None, median(turnover) if turnover else None,
                 json.dumps(reasons), max((str(row.get("updated_at")) for row in rows), default=None)),
            )
        fields_written += 1
    operators = operator_compatibility()
    with db._tx() as conn:
        for item in operators:
            conn.execute(
                """INSERT INTO operator_compatibility(operator_name,catalog_version,input_type,output_type,requires_group,requires_vector,min_args,max_args,source_json)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(operator_name,catalog_version) DO UPDATE SET
                   input_type=excluded.input_type, output_type=excluded.output_type, requires_group=excluded.requires_group,
                   requires_vector=excluded.requires_vector, min_args=excluded.min_args, max_args=excluded.max_args, source_json=excluded.source_json""",
                (item["operator_name"], item["catalog_version"], item["input_type"], item["output_type"], item["requires_group"],
                 item["requires_vector"], item["min_args"], item["max_args"], json.dumps(item["source"], sort_keys=True, default=str)),
            )
    return {"fields": fields_written, "operators": len(operators), "catalog_version": version}


def under_tested(db: research_db.ResearchDB, fields: Iterable[Field]) -> list[Field]:
    """Return fields ordered by attempts, then dataset, for coverage-aware generation."""
    counts = {str(row["field_id"]): int(row["attempts"]) for row in db.query(
        "SELECT field_id, attempts FROM field_coverage"
    )}
    # Work before the first refresh too: candidate history is already enough to avoid
    # selecting a field known to have been attempted over a never-tested field.
    for row in db.query("SELECT normalized_expression FROM candidates"):
        for name in canonical.fields_of(str(row.get("normalized_expression") or "")):
            counts[name] = counts.get(name, 0) + 1
    return sorted(fields, key=lambda field: (counts.get(field.name, 0), field.dataset, field.name))


def summary(db: research_db.ResearchDB) -> dict[str, Any]:
    return {
        "datasets": db.query("SELECT dataset, COUNT(*) AS fields, SUM(attempts) AS attempts, SUM(is_pass) AS passes, SUM(active) AS active FROM field_coverage GROUP BY dataset ORDER BY dataset"),
        "operators": db.query("SELECT input_type, COUNT(*) AS operators FROM operator_compatibility GROUP BY input_type ORDER BY input_type"),
        "catalog_versions": db.query("SELECT catalog_version, COUNT(*) AS fields FROM field_coverage GROUP BY catalog_version"),
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
