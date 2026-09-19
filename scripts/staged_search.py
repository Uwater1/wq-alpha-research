"""Staged search funnel.

`successive_halving` already lets one representative variant of a structure run before its
siblings. This module closes the remaining hole: with three BRAIN slots and a freshly
generated grid, all three slots could still be spent on the same unproven structure before
anyone learns whether its *base signal* is worth anything.

The funnel is deliberately **volume-gated**. A handful of hand-written candidates does not
need staged expansion, so below `volume_threshold` queued candidates this module does
nothing and the cheap representative-variant gate is enough. Once generation volume
warrants it, the funnel gives every structure a budget derived from evidence:

    never attempted                 budget 1   -> one baseline may hold a slot
    attempted, nothing passed       budget 0   -> siblings wait for the horizon
    proven, still paying off        budget > 0 -> promote variants up to the budget
    proven/saturated, poor returns  budget 0   -> stop the family

There are **no fixed 2000 -> 800 style ratios**: a budget is `max_variants - attempts`
once a structure has passed, and zero otherwise, so it adapts to whatever the generator
actually produces. Lineage (parent/generation/mutation) already lives on the candidates;
this module adds the per-structure budget ledger in `research_db.structure_budget`.

Deferral reuses the variant-gate deferral fields (`next_attempt_at` + `gate_reason`), so a deferred
variant stays QUEUED and claimable *later*; the staged horizon is only a safety valve so a
deferred idea is never lost. Turning the funnel off (`--no-staged`) restores the plain
representative-variant behaviour.

CLI:
    ./.venv/bin/python scripts/staged_search.py --status   # the current plan, no writes
    ./.venv/bin/python scripts/staged_search.py --review   # apply budgets to the queue
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import research_db  # noqa: E402
import successive_halving  # noqa: E402

#: Queued candidates below which staged expansion is not worth the bookkeeping.
DEFAULT_VOLUME_THRESHOLD = 30
#: Variants a proven structure may run before its family is considered explored.
DEFAULT_MAX_VARIANTS = 8
#: Attempts a structure may spend before a low pass ratio marks it saturated.
DEFAULT_MIN_ATTEMPTS_TO_STOP = 6
#: Pass ratio below which a well-attempted structure stops consuming capacity.
DEFAULT_MIN_PASS_RATIO = 0.25
#: How long a staged deferral lasts before it is released regardless (safety valve).
DEFAULT_STAGED_HORIZON_MINUTES = successive_halving.DEFAULT_HORIZON_MINUTES * 2
#: Upper bound on deferrals/promotions per review, so one pass stays cheap.
DEFAULT_MAX_ACTIONS = 500

STATUS_UNPROVEN = "unproven"
STATUS_PROVEN = "proven"
STATUS_SATURATED = "saturated"

ACTION_BASELINE = "baseline"
ACTION_EXPAND = "expand"
ACTION_HOLD = "hold"
ACTION_STOP = "stop"


@dataclass
class StructurePlan:
    """One structure's search budget for this review pass."""

    skeleton: str
    family: str | None
    attempted: int
    passed: int
    queued: int
    claimable: int
    deferred: int
    running: int
    status: str
    action: str
    #: How many variants of this structure may hold a slot right now.
    allow: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "skeleton": self.skeleton[:12],
            "family": self.family,
            "attempted": self.attempted,
            "passed": self.passed,
            "queued": self.queued,
            "claimable": self.claimable,
            "deferred": self.deferred,
            "running": self.running,
            "status": self.status,
            "action": self.action,
            "allow": self.allow,
        }


def _structure_rows(db: research_db.ResearchDB) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT COALESCE(skeleton_hash, '') AS skeleton,
               MIN(signal_family) AS family,
               COUNT(*) AS total,
               SUM(CASE WHEN status='QUEUED' THEN 1 ELSE 0 END) AS queued,
               SUM(CASE WHEN status='QUEUED' AND gate_reason IS NULL THEN 1 ELSE 0 END) AS claimable,
               SUM(CASE WHEN status='QUEUED' AND gate_reason IS NOT NULL THEN 1 ELSE 0 END) AS deferred,
               SUM(CASE WHEN status='SIMULATING' THEN 1 ELSE 0 END) AS running
        FROM candidates
        GROUP BY skeleton
        ORDER BY queued DESC, skeleton
        """
    )


def decide(
    attempted: int,
    passed: int,
    *,
    max_variants: int = DEFAULT_MAX_VARIANTS,
    min_attempts_to_stop: int = DEFAULT_MIN_ATTEMPTS_TO_STOP,
    min_pass_ratio: float = DEFAULT_MIN_PASS_RATIO,
) -> tuple[str, str, int]:
    """Budget decision for one structure: (status, action, how many may hold a slot)."""
    if attempted <= 0:
        return STATUS_UNPROVEN, ACTION_BASELINE, 1
    if passed <= 0:
        if attempted >= min_attempts_to_stop:
            return STATUS_SATURATED, ACTION_STOP, 0
        return STATUS_UNPROVEN, ACTION_HOLD, 0
    if attempted >= min_attempts_to_stop and (passed / attempted) < min_pass_ratio:
        return STATUS_SATURATED, ACTION_STOP, 0
    remaining = max(0, max_variants - attempted)
    return (STATUS_PROVEN, ACTION_EXPAND, remaining) if remaining else (STATUS_PROVEN, ACTION_STOP, 0)


def plan(
    db: research_db.ResearchDB,
    *,
    max_variants: int = DEFAULT_MAX_VARIANTS,
    min_attempts_to_stop: int = DEFAULT_MIN_ATTEMPTS_TO_STOP,
    min_pass_ratio: float = DEFAULT_MIN_PASS_RATIO,
) -> list[StructurePlan]:
    """Per-structure budgets; pure read, no writes."""
    outcomes = db.skeleton_outcomes()
    plans: list[StructurePlan] = []
    for row in _structure_rows(db):
        skeleton = str(row["skeleton"])
        if not skeleton:
            continue
        stats = outcomes.get(skeleton, {})
        attempted = int(stats.get("attempted", 0))
        passed = int(stats.get("passed", 0))
        status, action, allow = decide(
            attempted, passed,
            max_variants=max_variants,
            min_attempts_to_stop=min_attempts_to_stop,
            min_pass_ratio=min_pass_ratio,
        )
        plans.append(StructurePlan(
            skeleton=skeleton,
            family=row["family"],
            attempted=attempted,
            passed=passed,
            queued=int(row["queued"] or 0),
            claimable=int(row["claimable"] or 0),
            deferred=int(row["deferred"] or 0),
            running=int(row["running"] or 0),
            status=status,
            action=action,
            allow=allow,
        ))
    return plans


def _queue_rows(db: research_db.ResearchDB, skeleton: str) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT id, priority, next_attempt_at, gate_reason FROM candidates
        WHERE status='QUEUED' AND skeleton_hash=? AND attempt_count = 0
        ORDER BY priority DESC, id ASC
        """,
        (skeleton,),
    )


def review(
    db: research_db.ResearchDB,
    *,
    volume_threshold: int = DEFAULT_VOLUME_THRESHOLD,
    max_variants: int = DEFAULT_MAX_VARIANTS,
    min_attempts_to_stop: int = DEFAULT_MIN_ATTEMPTS_TO_STOP,
    min_pass_ratio: float = DEFAULT_MIN_PASS_RATIO,
    horizon_minutes: float = DEFAULT_STAGED_HORIZON_MINUTES,
    max_actions: int = DEFAULT_MAX_ACTIONS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply the staged funnel: budget each structure, defer or promote its variants."""
    moment = now or datetime.now(timezone.utc)
    timestamp = moment.isoformat(timespec="seconds")
    queued_depth = int(db.query("SELECT COUNT(*) AS n FROM candidates WHERE status='QUEUED'")[0]["n"])
    plans = plan(
        db,
        max_variants=max_variants,
        min_attempts_to_stop=min_attempts_to_stop,
        min_pass_ratio=min_pass_ratio,
    )
    counters: dict[str, Any] = {
        "engaged": queued_depth >= volume_threshold,
        "queued": queued_depth,
        "structures": len(plans),
        "deferred": 0,
        "promoted": 0,
        "stopped": sum(1 for p in plans if p.action in (ACTION_HOLD, ACTION_STOP)),
        "budget_rows": 0,
    }
    if not counters["engaged"]:
        # Below the volume threshold the cheap representative-variant gate is enough.
        return counters

    horizon = (moment + timedelta(minutes=horizon_minutes)).isoformat(timespec="seconds")
    actions = 0
    for structure in plans:
        if structure.queued == 0:
            continue
        rows = _queue_rows(db, structure.skeleton)
        claimable = [row for row in rows if not row["gate_reason"]]
        deferred = [row for row in rows if row["gate_reason"]]

        keep = claimable[: structure.allow]
        excess = claimable[structure.allow:]
        for row in excess[: max(0, max_actions - actions)]:
            # Deferrals of an unproven/saturated structure outlive the halving horizon on
            # purpose: the whole point is that a grid may not monopolize slots before its
            # base signal proves useful.
            until = max(str(row["next_attempt_at"] or horizon), horizon)
            db.defer_candidate(int(row["id"]), reason=f"staged:{structure.action}", until=until)
            counters["deferred"] += 1
            actions += 1

        if structure.allow > len(keep):
            promote = deferred[: structure.allow - len(keep)]
            promoted = db.promote_deferred([int(row["id"]) for row in promote], reason="staged:expand")
            counters["promoted"] += promoted
            actions += promoted

        if actions >= max_actions:
            break

    counters["budget_rows"] = db.record_structure_budget([
        {
            "skeleton": structure.skeleton,
            "family": structure.family,
            "attempts": structure.attempted,
            "passes": structure.passed,
            "variants": structure.attempted,
            "budget": structure.allow,
            "status": structure.status,
        }
        for structure in plans
    ])
    counters["timestamp"] = timestamp
    return counters


def status(db: research_db.ResearchDB) -> dict[str, Any]:
    """Current plan, read-only (what would be deferred/promoted and why)."""
    plans = plan(db)
    stored = {str(row["skeleton_hash"]): row for row in db.structure_budgets()}
    queued_depth = int(db.query("SELECT COUNT(*) AS n FROM candidates WHERE status='QUEUED'")[0]["n"])
    return {
        "queued": queued_depth,
        "engaged": queued_depth >= DEFAULT_VOLUME_THRESHOLD,
        "structures": [
            {**structure.as_dict(),
             "stored_status": (stored.get(structure.skeleton) or {}).get("status"),
             "stored_attempts": (stored.get(structure.skeleton) or {}).get("attempts")}
            for structure in plans
        ],
    }


def lineage(db: research_db.ResearchDB) -> list[dict[str, Any]]:
    """Per-family search spend, combining the budget ledger with candidate lineage."""
    return db.query(
        """
        SELECT COALESCE(signal_family, '') AS family,
               COUNT(*) AS candidates,
               COUNT(DISTINCT skeleton_hash) AS structures,
               SUM(CASE WHEN attempt_count > 0 THEN 1 ELSE 0 END) AS attempts,
               SUM(CASE WHEN status IN ('IS_PASS','CORR_PASS','SUBMISSION_READY','SUBMITTING','ACTIVE')
                        THEN 1 ELSE 0 END) AS passes,
               SUM(CASE WHEN generation > 0 THEN 1 ELSE 0 END) AS variants,
               MAX(generation) AS max_generation
        FROM candidates GROUP BY family ORDER BY attempts DESC, candidates DESC
        """
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help=f"path to research.db (default: ${research_db.DB_ENV_VAR} or repo root)")
    parser.add_argument("--review", action="store_true", help="apply the staged budgets now")
    parser.add_argument("--status", action="store_true", help="print the plan without writing")
    parser.add_argument("--lineage", action="store_true", help="print per-family search spend")
    parser.add_argument("--volume-threshold", type=int, default=DEFAULT_VOLUME_THRESHOLD,
                        help="queued candidates required before staged expansion engages")
    parser.add_argument("--max-variants", type=int, default=DEFAULT_MAX_VARIANTS,
                        help="variants a proven structure may run")
    parser.add_argument("--min-pass-ratio", type=float, default=DEFAULT_MIN_PASS_RATIO,
                        help="pass ratio below which a well-attempted structure stops")
    parser.add_argument("--horizon-minutes", type=float, default=DEFAULT_STAGED_HORIZON_MINUTES,
                        help="how long a staged deferral lasts before automatic release")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        if args.lineage:
            print(json.dumps(lineage(db), indent=2, sort_keys=True))
            return 0
        if args.review:
            counters = review(
                db,
                volume_threshold=args.volume_threshold,
                max_variants=args.max_variants,
                min_pass_ratio=args.min_pass_ratio,
                horizon_minutes=args.horizon_minutes,
            )
            print(json.dumps(counters, indent=2, sort_keys=True))
            return 0
        print(json.dumps(status(db), indent=2, sort_keys=True))
        return 0


if __name__ == "__main__":
    sys.exit(main())
