"""Quality-diversity archive and interpretable family allocation.

The archive is advisory: membership never implies IS, correlation, or submission readiness.
It keeps the best observed candidate in each reproducible niche and samples parents across
niches. Family allocation uses a seeded Beta-Bernoulli Thompson draw with an explicit
exploration reserve; results are persisted for audit but do not alter the scheduler unless
a caller explicitly consumes the returned plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical
import research_db

REWARD_VERSION = "is-pass-v1"
PASSING = {"IS_PASS", "CORR_PASS", "SUBMISSION_READY", "SUBMITTING", "ACTIVE"}


def _bucket_turnover(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    value = float(value)
    if value < 0.05:
        return "low"
    if value <= 0.15:
        return "medium"
    return "high"


def niche(row: Mapping[str, Any]) -> dict[str, Any]:
    """Stable niche dimensions; no raw expression or private alpha data is included."""
    expression = str(row.get("normalized_expression") or "")
    structural = {}
    try:
        structural = json.loads(str(row.get("structural_json") or "{}"))
    except ValueError:
        pass
    operators = tuple(structural.get("operators") or canonical.operators_of(expression))
    fields = tuple(structural.get("fields") or canonical.fields_of(expression))
    return {
        "family": str(row.get("signal_family") or "unknown"),
        "operator_skeleton": ",".join(operators),
        "field_category": str(row.get("signal_family") or "unknown").split("_")[0],
        "depth_bucket": min(canonical.expression_depth(expression), 8),
        "turnover_bucket": _bucket_turnover(row.get("turnover")),
        "mutation_type": str(row.get("mutation_type") or "manual"),
        "field_count": len(fields),
    }


def cell_key(dimensions: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(dimensions), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def elite_score(row: Mapping[str, Any]) -> float:
    """Quality-diversity objective, not a submission gate."""
    sharpe = float(row.get("sharpe") or 0.0)
    fitness = float(row.get("fitness") or 0.0)
    turnover = float(row.get("turnover") or 0.0)
    corr = abs(float(row.get("self_corr") or 0.0))
    return round(sharpe + 0.75 * fitness - 0.25 * turnover - 0.25 * corr, 6)


def rebuild(db: research_db.ResearchDB) -> dict[str, int]:
    """Rebuild archive cells from all settled candidates without deleting trial history."""
    rows = db.query(
        "SELECT * FROM candidates WHERE status IN ('SIMULATED','IS_PASS','CORR_PASS','SUBMISSION_READY','SUBMITTING','ACTIVE','REJECTED')"
    )
    members: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    dimensions: dict[str, dict[str, Any]] = {}
    for row in rows:
        dims = niche(row)
        key = cell_key(dims)
        members[key].append(row)
        dimensions[key] = dims
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db._tx() as conn:
        for key, cell_members in members.items():
            winner = max(cell_members, key=lambda row: (elite_score(row), -int(row["id"])))
            conn.execute(
                """INSERT INTO archive_cells(cell_key, dimensions_json, elite_candidate_id, elite_score, member_count, updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(cell_key) DO UPDATE SET
                   dimensions_json=excluded.dimensions_json, elite_candidate_id=excluded.elite_candidate_id,
                   elite_score=excluded.elite_score, member_count=excluded.member_count, updated_at=excluded.updated_at""",
                (key, json.dumps(dimensions[key], sort_keys=True), int(winner["id"]), elite_score(winner), len(cell_members), now),
            )
    return {"cells": len(members), "members": len(rows)}


def parents(db: research_db.ResearchDB, *, count: int = 10, seed: int = 0) -> list[dict[str, Any]]:
    """Select at most one parent per cell per round, then fill by quality-diversity score."""
    rows = db.query(
        "SELECT a.*, c.signal_family, c.sharpe, c.fitness, c.turnover, c.self_corr, c.generation "
        "FROM archive_cells a JOIN candidates c ON c.id=a.elite_candidate_id ORDER BY a.cell_key"
    )
    rng = random.Random(seed)
    weighted = list(rows)
    rng.shuffle(weighted)
    weighted.sort(key=lambda row: (-float(row["elite_score"]), str(row["cell_key"])))
    return [dict(row) for row in weighted[: max(0, int(count))]]


def allocate_families(
    db: research_db.ResearchDB,
    families: Sequence[str],
    *,
    budget: int,
    exploration_reserve: float = 0.20,
    seed: int = 0,
    allocation_key: str = "default",
) -> list[dict[str, Any]]:
    """Allocate integer slots with a guaranteed reserve for under-tested families."""
    names = sorted({str(f) for f in families if str(f)})
    if not names or budget <= 0:
        return []
    reserve = min(
        budget,
        max(len(names), int(round(budget * max(0.0, min(1.0, exploration_reserve))))),
    )
    rng = random.Random(seed)
    outcomes = {str(row["family"]): (int(row["settled"] or 0), int(row["passed"] or 0)) for row in db.query(
        """SELECT COALESCE(signal_family,'') AS family,
                  SUM(CASE WHEN status IN ('IS_PASS','CORR_PASS','SUBMISSION_READY','SUBMITTING','ACTIVE','REJECTED') THEN 1 ELSE 0 END) settled,
                  SUM(CASE WHEN status IN ('IS_PASS','CORR_PASS','SUBMISSION_READY','SUBMITTING','ACTIVE') THEN 1 ELSE 0 END) passed
           FROM candidates GROUP BY family"""
    )}
    draws: dict[str, float] = {}
    params: dict[str, tuple[float, float]] = {}
    for family in names:
        settled, passed = outcomes.get(family, (0, 0))
        alpha, beta = 1.0 + passed, 1.0 + max(0, settled - passed)
        params[family] = (alpha, beta)
        draws[family] = rng.betavariate(alpha, beta)
    allocation = {family: 1 for family in names}
    exploration_families = {family for family in names if outcomes.get(family, (0, 0))[0] == 0}
    remaining = max(0, budget - reserve)
    for family in sorted(names, key=lambda item: (-draws[item], item))[:remaining]:
        allocation[family] += 1
    # Fill any rounding gap by highest draw, preserving the reserve.
    while sum(allocation.values()) < budget:
        allocation[max(names, key=lambda item: (draws[item], item))] += 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db._tx() as conn:
        for family in names:
            alpha, beta = params[family]
            conn.execute(
                "INSERT INTO family_allocations(allocation_key,family,budget,exploration,reward_version,alpha,beta,seed,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (allocation_key, family, allocation[family], int(allocation[family] == 1), REWARD_VERSION, alpha, beta, int(seed), now),
            )
    return [{"family": family, "budget": allocation[family], "exploration": int(family in exploration_families),
             "draw": round(draws[family], 6), "alpha": params[family][0], "beta": params[family][1],
             "reward_version": REWARD_VERSION} for family in names]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quality-diversity archive and family allocation")
    parser.add_argument("--db")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--parents", type=int)
    parser.add_argument("--allocate", type=int, metavar="BUDGET")
    parser.add_argument("--families", nargs="*", default=[])
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        if args.rebuild:
            print(json.dumps(rebuild(db), indent=2, sort_keys=True))
        if args.parents is not None:
            print(json.dumps(parents(db, count=args.parents, seed=args.seed), indent=2, sort_keys=True))
        if args.allocate is not None:
            families = args.families or [str(row["signal_family"]) for row in db.query("SELECT DISTINCT signal_family FROM candidates WHERE signal_family IS NOT NULL")]
            print(json.dumps(allocate_families(db, families, budget=args.allocate, seed=args.seed), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
