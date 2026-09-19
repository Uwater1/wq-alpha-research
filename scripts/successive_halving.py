"""Successive halving for parameter variants (TODO P5, and the diversity gate of P4.3).

The expensive habit this replaces is queueing a whole grid at once:

    ts_mean(close, 20/40/60/120/250) x decay(0/2/4/8/16)

BRAIN gives three slots, so a grid like that occupies the pipeline for an hour before
anyone knows whether the *base signal* (the window grid itself) is worth anything. The
halving gate instead admits one representative variant per structure and defers its
siblings until the base signal reports:

    skeleton has a passed member          -> promote every deferred sibling
    skeleton attempted, nothing passed    -> keep deferring (until the horizon)
    sibling horizon elapsed               -> promote anyway (an idea is never lost)

Deferral is stored on the existing lifecycle rather than a new status: the candidate
stays QUEUED with ``next_attempt_at`` in the future plus a ``gate_reason``, and the
scheduler's claim filter skips it. The horizon is the safety valve: a bad heuristic can
delay a variant, never strand it.

The TODO's full funnel (2000 -> 800 -> baseline -> survivors -> tune) needs a mass
generator that does not exist in this repo yet; this module implements the part that
pays off today, and leaves the funnel composition to the generator that arrives with it.

CLI:
    ./.venv/bin/python scripts/successive_halving.py --status   # what is deferred and why
    ./.venv/bin/python scripts/successive_halving.py --review   # apply the gate to the queue
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import research_db  # noqa: E402

#: Simulations a structure may spend before its siblings have to wait for a verdict.
DEFAULT_EARLY_ATTEMPTS = 1
#: How long a deferred variant waits before it is released regardless of the verdict.
DEFAULT_HORIZON_MINUTES = 240.0
#: Upper bound on deferrals applied per review, so one pass stays cheap.
DEFAULT_MAX_DEFERRALS = 500


def _horizon(now: datetime, minutes: float) -> str:
    return (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def review(
    db: research_db.ResearchDB,
    *,
    early_attempts: int = DEFAULT_EARLY_ATTEMPTS,
    horizon_minutes: float = DEFAULT_HORIZON_MINUTES,
    max_deferrals: int = DEFAULT_MAX_DEFERRALS,
    now: datetime | None = None,
) -> dict[str, int]:
    """Promote proven structures, defer variants of unproven ones. Returns counters."""
    moment = now or datetime.now(timezone.utc)
    timestamp = moment.isoformat(timespec="seconds")
    outcomes = db.skeleton_outcomes()

    deferred_rows = db.query(
        "SELECT id, skeleton_hash, expression FROM candidates WHERE status='QUEUED' AND gate_reason IS NOT NULL"
    )

    # 1. A structure with a passing member has proven itself: release its siblings.
    proven_skeletons = {skeleton for skeleton, stats in outcomes.items() if stats["passed"] > 0}
    released = [
        int(row["id"])
        for row in deferred_rows
        if str(row["skeleton_hash"] or "") in proven_skeletons
    ]
    promoted_count = db.promote_deferred(released, reason="base_signal_passed")

    # 2. Structures whose horizon elapsed are released by the claim filter anyway; clear
    #    the bookkeeping so `--status` does not keep reporting stale deferrals.
    elapsed = [
        int(row["id"])
        for row in db.query(
            "SELECT id FROM candidates WHERE status='QUEUED' AND gate_reason IS NOT NULL "
            "AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?",
            (timestamp,),
        )
    ]
    promoted_count += db.promote_deferred(elapsed, reason="horizon_elapsed")

    # 3. Defer variants of structures that consumed attempts without producing a pass.
    blocked = [
        skeleton for skeleton, stats in outcomes.items()
        if skeleton and stats["attempted"] >= early_attempts and stats["passed"] == 0
    ]
    deferred_count = 0
    if blocked and max_deferrals > 0:
        placeholders = ",".join("?" for _ in blocked)
        rows = db.query(
            f"""
            SELECT id, skeleton_hash FROM candidates
            WHERE status='QUEUED' AND attempt_count = 0 AND gate_reason IS NULL
                  AND skeleton_hash IN ({placeholders})
            ORDER BY id LIMIT ?
            """,
            (*blocked, max_deferrals),
        )
        for row in rows:
            stats = outcomes.get(str(row["skeleton_hash"]), {})
            db.defer_candidate(
                int(row["id"]),
                reason="variant_search",
                until=_horizon(moment, horizon_minutes),
            )
            deferred_count += 1

    return {
        "promoted_proven": len(released),
        "promoted_horizon": len(elapsed),
        "promoted_total": promoted_count,
        "deferred": deferred_count,
        "blocked_structures": len(blocked),
        "waiting": len(deferred_rows),
    }


def status(db: research_db.ResearchDB) -> dict[str, Any]:
    """Human-readable view of what the gate is holding back and why."""
    rows = db.query(
        """
        SELECT COALESCE(skeleton_hash,'') AS skeleton, COUNT(*) AS waiting,
               MIN(next_attempt_at) AS next_release, MIN(expression) AS sample
        FROM candidates WHERE status='QUEUED' AND gate_reason IS NOT NULL
        GROUP BY skeleton ORDER BY waiting DESC
        """
    )
    outcomes = db.skeleton_outcomes()
    return {
        "deferred_candidates": sum(int(row["waiting"]) for row in rows),
        "deferred_structures": [
            {
                "skeleton": row["skeleton"][:12],
                "waiting": int(row["waiting"]),
                "next_release": row["next_release"],
                "attempted": outcomes.get(row["skeleton"], {}).get("attempted", 0),
                "passed": outcomes.get(row["skeleton"], {}).get("passed", 0),
                "sample": str(row["sample"])[:80],
            }
            for row in rows
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help=f"path to research.db (default: ${research_db.DB_ENV_VAR} or repo root)")
    parser.add_argument("--review", action="store_true", help="apply the gate to the queue now")
    parser.add_argument("--status", action="store_true", help="show deferred candidates")
    parser.add_argument("--early-attempts", type=int, default=DEFAULT_EARLY_ATTEMPTS,
                        help="simulations a structure may spend before siblings wait")
    parser.add_argument("--horizon-minutes", type=float, default=DEFAULT_HORIZON_MINUTES,
                        help="how long a deferred variant waits before automatic release")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        if args.status or not args.review:
            print(json.dumps(status(db), indent=2, sort_keys=True))
            return 0
        counters = review(db, early_attempts=args.early_attempts, horizon_minutes=args.horizon_minutes)
        print(json.dumps(counters, indent=2, sort_keys=True))
        return 0


if __name__ == "__main__":
    sys.exit(main())
