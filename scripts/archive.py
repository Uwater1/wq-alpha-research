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
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical
import diversity
import expression_grammar as grammar
import research_db

REWARD_VERSION = "is-pass-v1"
#: Bumped whenever the niche definition changes, so old cells can be told apart from new ones.
NICHE_VERSION = "archive-niche-v4"
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


def _parameters(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(str(row.get("mutation_parameters_json") or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def niche(row: Mapping[str, Any]) -> dict[str, Any]:
    """Stable, topology-preserving niche dimensions; no raw expression or alpha id.

    The main structural identity is now the grammar/semantic skeleton hash and the derived
    source profile, not a sorted operator set: two different ASTs can share an operator set,
    while the same topology with different fields is genuinely the same hypothesis. The
    operator set is retained only as descriptive metadata.
    """
    expression = str(row.get("normalized_expression") or "")
    structural = {}
    try:
        structural = json.loads(str(row.get("structural_json") or "{}"))
    except ValueError:
        pass
    operators = tuple(structural.get("operators") or canonical.operators_of(expression))
    fields = tuple(structural.get("fields") or canonical.fields_of(expression))
    profile = diversity.derive_source_profile(expression)
    parameters = _parameters(row)
    # Hash in the canonical metadata space (field types and dataset:category identities), so
    # niche occupancy lines up with the generator columns and the planner's lookups.
    metadata = diversity.load_field_metadata()
    return {
        "niche_version": NICHE_VERSION,
        "primary_family": str(profile.get("primary_family") or "unknown"),
        "dataset_set": "+".join(profile.get("datasets") or []),
        "category_set": "+".join(profile.get("categories") or []),
        "motif_id": str(parameters.get("motif_id") or "none"),
        "grammar_skeleton_hash": grammar.grammar_skeleton_hash(expression, metadata),
        "semantic_skeleton_hash": grammar.semantic_skeleton_hash(expression, metadata),
        "depth_bucket": min(canonical.expression_depth(expression), 8),
        "field_count": len(profile.get("field_ids") or fields),
        "cross_dataset": bool(profile.get("cross_dataset")),
        "turnover_bucket": _bucket_turnover(row.get("turnover")),
        "mutation_type": str(row.get("mutation_type") or "manual"),
        "generation_mode": str(parameters.get("generation_mode") or row.get("source") or "legacy"),
        # Descriptive only: a sorted operator set is not a structural identity.
        "operator_set": ",".join(operators),
    }


def cell_key(dimensions: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(dimensions), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: The turnover centre the proven book actually occupies (P19.3 recipe prior: the passing
#: alphas cluster around 0.09-0.11). Turnover is a *refusal* band, not a preference: being far
#: from it on either side is what BRAIN refuses, so distance from this centre is penalised.
TURNOVER_TARGET = 0.10


def elite_score(row: Mapping[str, Any]) -> float:
    """Quality-diversity objective, not a submission gate."""
    sharpe = float(row.get("sharpe") or 0.0)
    fitness = float(row.get("fitness") or 0.0)
    turnover = float(row.get("turnover") or 0.0)
    corr = abs(float(row.get("self_corr") or 0.0))
    return round(sharpe + 0.75 * fitness - 0.25 * turnover - 0.25 * corr, 6)


def quality_elite_score(row: Mapping[str, Any]) -> float:
    """Point-in-time **quality** evidence for ranking inside one niche (P20.1).

    The archive's job stopped being "keep the most novel member" once the ledger showed that
    novelty without local competition spends capacity on junk. Inside a niche the retained
    elite is now the candidate with the best evidence that the platform accepts alphas of this
    kind:

    * **stage reached is the primary term** (a gate pass, a bare simulation, or nothing). It is
      deliberately worth more than the whole metric term can add, so a gate pass can never be
      outranked by a large number from a candidate that never cleared a gate;
    * **metrics are a bounded secondary term**: Sharpe and Fitness clamped to ``[-1, 3]`` each so
      one outlier cannot outweigh a niche, turnover *acceptability* as a bounded bonus (distance
      from :data:`TURNOVER_TARGET` on either side is what BRAIN refuses), and the
      self-correlation distance so a proven-but-redundant member is not the elite.

    The turnover distance is normalized by the wider admissible side rather than by the
    distance itself, so the bonus is strictly decreasing in either direction instead of
    saturating for every turnover more than one target-width away.
    """
    status = str(row.get("status") or "")
    if status in PASSING:
        stage = 1.0
    elif status in {"SIMULATED"}:
        stage = 0.35
    else:
        stage = 0.0
    sharpe = min(3.0, max(-1.0, float(row.get("sharpe") or 0.0)))
    fitness = min(3.0, max(-1.0, float(row.get("fitness") or 0.0)))
    turnover = float(row.get("turnover") or 0.0)
    corr = abs(float(row.get("self_corr") or 0.0))
    span = max(TURNOVER_TARGET, 1.0 - TURNOVER_TARGET)
    turnover_fit = 1.0 - min(1.0, abs(turnover - TURNOVER_TARGET) / span)
    metric_term = 0.2 * sharpe + 0.1 * fitness + 0.2 * turnover_fit - 0.1 * corr
    return round(stage + min(0.9, max(-0.5, metric_term)), 6)


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
        # ``archive_cells`` is derived state: replace it atomically. A cell from an obsolete
        # niche definition (or one whose members are gone) must not linger, or occupancy and
        # parent selection would keep reading pre-current-version niches forever (P5/P15).
        conn.execute("DELETE FROM archive_cells")
        for key, cell_members in members.items():
            # Local competition on quality evidence (P20.1), not on novelty or sparsity.
            winner = max(cell_members, key=lambda row: (quality_elite_score(row), -int(row["id"])))
            conn.execute(
                """INSERT INTO archive_cells(cell_key, dimensions_json, elite_candidate_id, elite_score, member_count, updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(cell_key) DO UPDATE SET
                   dimensions_json=excluded.dimensions_json, elite_candidate_id=excluded.elite_candidate_id,
                   elite_score=excluded.elite_score, member_count=excluded.member_count, updated_at=excluded.updated_at""",
                (key, json.dumps(dimensions[key], sort_keys=True), int(winner["id"]),
                 quality_elite_score(winner), len(cell_members), now),
            )
    return {"cells": len(members), "members": len(rows)}


def quality_elites(
    db: research_db.ResearchDB,
    *,
    per_niche: int = 1,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    """The best ``per_niche`` members of every niche by quality evidence (P20.1).

    ``archive_cells`` stores one elite per niche; exploitation sometimes needs a couple of
    independent starting points inside the same niche, so this re-derives the members and keeps
    the top few. Point-in-time safety: only candidates settled at or before ``as_of`` count.
    """
    per_niche = max(1, int(per_niche))
    rows = db.query(
        "SELECT c.*, s.completed_at AS completed_at FROM candidates c"
        " LEFT JOIN simulations s ON s.id=(SELECT id FROM simulations WHERE candidate_id=c.id ORDER BY id DESC LIMIT 1)"
    )
    members: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if as_of and str(row.get("completed_at") or "") > as_of:
            continue
        members[cell_key(niche(row))].append(row)
    elites: list[dict[str, Any]] = []
    for key in sorted(members):
        ordered = sorted(
            members[key], key=lambda row: (-quality_elite_score(row), -int(row["id"])),
        )
        for row in ordered[:per_niche]:
            elites.append({
                "candidate_id": int(row["id"]),
                "cell_key": key,
                "quality": quality_elite_score(row),
                "members": len(members[key]),
                "signal_family": str(row.get("signal_family") or "unknown"),
            })
    return elites


def under_tested_niches(
    db: research_db.ResearchDB,
    *,
    limit: int = 8,
    max_members: int = 1,
) -> list[dict[str, Any]]:
    """Niches with the least occupancy: where the exploration reserve should be spent (P20.1).

    An empty niche cannot be enumerated (it has no members), so the reserve is measured as the
    niches that exist with the fewest members and reported as such rather than fabricated.
    """
    cells = db.query(
        "SELECT cell_key, dimensions_json, member_count, elite_score FROM archive_cells"
        " ORDER BY member_count ASC, cell_key ASC LIMIT ?",
        (max(0, int(limit)),),
    )
    return [
        {
            "cell_key": str(row["cell_key"]),
            "members": int(row["member_count"]),
            "elite_score": float(row["elite_score"]),
            "sparse": int(row["member_count"]) <= max_members,
        }
        for row in cells
    ]


def structure_occupancy(db: research_db.ResearchDB) -> dict[str, int]:
    """``grammar_skeleton_hash -> total archive member count`` across niche cells.

    This is real niche occupancy, not a frequency proxy (P4.2/P8): it counts how many
    settled candidates ever occupied each topology-preserving structure. Used by the
    planner to reserve exploration for sparse cells and by ranking as archive sparsity.
    """
    return diversity.archive_occupancy(db)


def parent_score(row: Mapping[str, Any]) -> float:
    """Quality-diversity parent score: quality plus how sparse/novel the niche is.

    ``elite_score`` alone rewards whichever niche currently holds the best alpha, which
    collapses generation onto one family. Sparse niches (few members) and thinly tested
    families get a bounded bonus so exploration does not depend on luck.
    """
    quality = float(row.get("elite_score") or 0.0)
    member_count = max(1, int(row.get("member_count") or 1))
    novelty = 1.0 / (1.0 + member_count)
    generation = max(0, int(row.get("generation") or 0))
    # A deep descendant has already been mutated a lot; prefer fresher material.
    lineage_penalty = min(0.5, 0.05 * generation)
    return round(quality + 0.5 * novelty - lineage_penalty, 6)


def parents(db: research_db.ResearchDB, *, count: int = 10, seed: int = 0) -> list[dict[str, Any]]:
    """Round-robin across families and niches, best-first inside each, seeded.

    The previous implementation shuffled and then immediately re-sorted by elite score,
    which made the shuffle meaningless: the result was deterministic global top-elite
    selection. Selection is now explicitly diversity-aware.
    """
    rows = db.query(
        "SELECT a.*, c.signal_family, c.sharpe, c.fitness, c.turnover, c.self_corr, c.generation, "
        "       c.normalized_expression, c.motif_id, c.generation_mode "
        "FROM archive_cells a JOIN candidates c ON c.id=a.elite_candidate_id ORDER BY a.cell_key"
    )
    wanted = max(0, int(count))
    if not rows or wanted == 0:
        return []
    rng = random.Random(seed)
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        families[str(row["signal_family"] or "unknown")].append(dict(row))
    for members in families.values():
        rng.shuffle(members)  # seeded tie-break between equally scored niches
        members.sort(key=lambda row: (-parent_score(row), str(row["cell_key"])))
    order = sorted(families)
    rng.shuffle(order)  # which family leads the rotation is seeded, not quality-ordered
    selected: list[dict[str, Any]] = []
    round_index = 0
    while len(selected) < wanted:
        progressed = False
        for family in order:
            members = families[family]
            if round_index >= len(members):
                continue
            selected.append(members[round_index])
            progressed = True
            if len(selected) >= wanted:
                break
        if not progressed:
            break
        round_index += 1
    return selected


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))


def allocate_families(
    db: research_db.ResearchDB,
    families: Sequence[str],
    *,
    budget: int,
    exploration_reserve: float = 0.20,
    max_family_share: float = 0.5,
    minimum_exploration_slots: int = 1,
    seed: int = 0,
    allocation_key: str = "default",
) -> list[dict[str, Any]]:
    """Allocate integer slots that always sum to exactly ``budget``.

    Guarantees:

    * ``sum(budgets) == budget`` for every input (including budget < family count, where
      only a bounded subset of families is seated instead of giving everyone one slot);
    * the under-tested families hold the exploration reserve;
    * no family can exceed ``max_family_share`` of the budget unless the cap makes the
      budget impossible to place, in which case it is raised by the smallest feasible step.

    Slots are handed out with sequential Thompson draws, so allocation adapts to observed
    pass rates while remaining deterministic for a fixed seed.
    """
    names = sorted({str(f) for f in families if str(f)})
    budget = int(budget)
    if not names or budget <= 0:
        return []
    rng = random.Random(seed)
    outcomes = {str(row["family"]): (int(row["settled"] or 0), int(row["passed"] or 0)) for row in db.query(
        """SELECT COALESCE(signal_family,'') AS family,
                  SUM(CASE WHEN status IN ('IS_PASS','CORR_PASS','SUBMISSION_READY','SUBMITTING','ACTIVE','REJECTED') THEN 1 ELSE 0 END) settled,
                  SUM(CASE WHEN status IN ('IS_PASS','CORR_PASS','SUBMISSION_READY','SUBMITTING','ACTIVE') THEN 1 ELSE 0 END) passed
           FROM candidates GROUP BY family"""
    )}
    params: dict[str, tuple[float, float]] = {}
    draws: dict[str, float] = {}
    for family in names:
        settled, passed = outcomes.get(family, (0, 0))
        alpha, beta = 1.0 + passed, 1.0 + max(0, settled - passed)
        params[family] = (alpha, beta)
        draws[family] = rng.betavariate(alpha, beta)

    untested = [family for family in names if outcomes.get(family, (0, 0))[0] == 0]
    exploration_target = min(budget, max(int(minimum_exploration_slots), int(round(budget * _clamp(exploration_reserve, 0.0, 1.0)))))
    # Under-tested families are seated first: with a budget below the family count this is
    # exactly the bounded exploration reserve, and with a larger budget they still hold
    # their reserve slots before exploitation fills the rest.
    exploration_order = sorted(untested, key=lambda family: (-draws[family], family))
    exploitation_order = sorted((family for family in names if family not in set(untested)), key=lambda family: (-draws[family], family))
    seat_order = exploration_order + exploitation_order
    seated = set(names) if budget >= len(names) else set(seat_order[:budget])
    allocation = {family: int(family in seated) for family in names}
    pseudo: dict[str, int] = {family: 0 for family in names}

    share_cap = max(
        1,
        int(math.ceil(budget * _clamp(max_family_share, 1.0 / len(names), 1.0))),
        int(math.ceil(budget / len(names))),
    )
    remaining = budget - sum(allocation.values())
    while remaining > 0:
        allowed = [family for family in names if allocation[family] < share_cap]
        if not allowed:
            share_cap += 1
            continue
        # Sequential Thompson draw: slots already spent in a family count as non-passes,
        # so a single lucky draw cannot consume the whole remaining budget.
        picked = max(
            allowed,
            key=lambda family: (
                rng.betavariate(params[family][0], params[family][1] + pseudo[family]),
                draws[family],
                family,
            ),
        )
        allocation[picked] += 1
        pseudo[picked] += 1
        remaining -= 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db._tx() as conn:
        for family in names:
            alpha, beta = params[family]
            conn.execute(
                "INSERT INTO family_allocations(allocation_key,family,budget,exploration,reward_version,alpha,beta,seed,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (allocation_key, family, allocation[family], int(family in untested and allocation[family] > 0),
                 REWARD_VERSION, alpha, beta, int(seed), now),
            )
    return [{
        "family": family,
        "budget": allocation[family],
        "exploration": int(family in untested and allocation[family] > 0),
        "share": round(allocation[family] / budget, 6),
        "draw": round(draws[family], 6),
        "alpha": params[family][0],
        "beta": params[family][1],
        "reward_version": REWARD_VERSION,
        "exploration_target": exploration_target,
        "max_family_share": float(max_family_share),
    } for family in names]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quality-diversity archive and family allocation")
    parser.add_argument("--db")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--parents", type=int)
    parser.add_argument("--allocate", type=int, metavar="BUDGET")
    parser.add_argument("--families", nargs="*", default=[])
    parser.add_argument("--max-family-share", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        if args.rebuild:
            print(json.dumps(rebuild(db), indent=2, sort_keys=True))
        if args.parents is not None:
            print(json.dumps(parents(db, count=args.parents, seed=args.seed), indent=2, sort_keys=True))
        if args.allocate is not None:
            families = args.families or [str(row["signal_family"]) for row in db.query("SELECT DISTINCT signal_family FROM candidates WHERE signal_family IS NOT NULL")]
            print(json.dumps(allocate_families(
                db, families, budget=args.allocate, seed=args.seed,
                max_family_share=args.max_family_share,
            ), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
