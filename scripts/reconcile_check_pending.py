"""Reconcile CHECK_PENDING submissions against BRAIN.

Mirrors submission_worker.reconcile_pending but is a standalone script so the DB state is
easy to inspect between runs. Three guards keep repeated runs from turning a slow platform
check into a 429 storm:

* ``--max-concurrent`` admits at most N concurrent runs through a DB-backed gate; a run
  that finds the gate full exits immediately instead of duplicating every BRAIN call.
* every row is claimed with ``--reconcile-ttl`` seconds before BRAIN is queried, so two
  overlapping runs cannot ask about the same alpha, and a row BRAIN still reports PENDING
  is left alone until the TTL elapses;
* a conclusive cached ``submission_checks`` snapshot is reused (and a cached
  SELF_CORRELATION failure settles locally), so already-evaluated rows cost zero BRAIN
  calls and fresh fetches are persisted for the next session.

Usage:
    ./.venv/bin/python scripts/reconcile_check_pending.py
    ./.venv/bin/python scripts/reconcile_check_pending.py --max-concurrent 2 --reconcile-ttl 300
    ./.venv/bin/python scripts/reconcile_check_pending.py --reconciliations 20 --seconds 120
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import brain_api  # noqa: E402
import research_db  # noqa: E402
from submission_worker import _checks_pending, _self_correlation_verdict  # noqa: E402

DEFAULT_BUDGET_RECONCILIATIONS = 20
DEFAULT_BUDGET_SECONDS = 120.0
#: Gate name shared by every reconcile process talking to the same research.db.
GATE_NAME = "reconcile_check_pending"


def reconcile_one(
    db: research_db.ResearchDB,
    client: brain_api.BrainClient,
    submission_id: int,
    *,
    ttl_seconds: float = research_db.DEFAULT_RECONCILE_TTL_SECONDS,
    owner: str | None = None,
) -> int:
    """Reconcile one row; returns 1 when it was settled/re-checked, 0 when skipped.

    The TTL claim happens before any BRAIN call, so a still-PENDING row is not queried
    again by this run, a parallel run, or the next one inside the TTL window.
    """
    if not db.claim_reconcile(submission_id, ttl_seconds=ttl_seconds, owner=owner):
        return 0

    rows = db.query("SELECT * FROM submissions WHERE id=?", (submission_id,))
    if not rows:
        return 0
    row = rows[0]
    alpha_id = row.get("brain_alpha_id")
    if not alpha_id:
        db.finish_submission(
            submission_id, "RETRY",
            message="CHECK_PENDING without an alpha id; nothing to reconcile",
        )
        return 1

    cached = db.latest_submission_checks(str(alpha_id))
    verdict = _self_correlation_verdict(cached)
    if verdict["result"] == "FAIL":
        # BRAIN already failed this alpha on the platform correlation check; nothing left
        # to ask and no reason to re-POST it.
        db.finish_submission(
            submission_id, "SELF_CORR_FAIL",
            message="reconciled: cached platform SELF_CORRELATION failed",
            max_corr=verdict["value"], brain_alpha_id=str(alpha_id),
        )
        return 1

    try:
        status = client.alpha_status(str(alpha_id))
        if cached and not _checks_pending(cached):
            # Checks already settled in an earlier session; only the status is open.
            checks: list[Mapping[str, Any]] = cached
        else:
            checks = client.submit_checks(str(alpha_id))
            db.record_submission_checks(
                str(alpha_id), checks,
                submission_id=submission_id, candidate_id=row.get("candidate_id"),
            )
    except Exception as exc:
        print(f"[reconcile] {submission_id} {alpha_id}: ERROR {exc}")
        return 0

    verdict = _self_correlation_verdict(checks)
    if status == "ACTIVE":
        db.finish_submission(submission_id, "ACTIVE", message="reconciled: confirmed ACTIVE",
                             max_corr=verdict["value"], brain_alpha_id=str(alpha_id))
    elif status in ("REJECTED", "DELETED"):
        db.finish_submission(submission_id, "PLATFORM_REJECTED",
                             message=f"reconciled: alpha status={status}", brain_alpha_id=str(alpha_id))
    elif verdict["result"] == "FAIL":
        db.finish_submission(submission_id, "SELF_CORR_FAIL",
                             message="reconciled: platform SELF_CORRELATION failed",
                             max_corr=verdict["value"], brain_alpha_id=str(alpha_id))
    elif _checks_pending(checks):
        db.finish_submission(submission_id, "CHECK_PENDING",
                             message="reconciled: submission checks still pending", brain_alpha_id=str(alpha_id))
    elif status in ("", "UNSUBMITTED"):
        db.finish_submission(submission_id, "READY",
                             message="reconciled: BRAIN reports the alpha is not submitted",
                             brain_alpha_id=str(alpha_id))
    else:
        db.finish_submission(submission_id, "RETRY", message=f"reconciled: status={status}",
                             brain_alpha_id=str(alpha_id))
    return 1


def reconcile_all(db: research_db.ResearchDB, client: brain_api.BrainClient, *,
                  budget_reconciliations: int, budget_seconds: float,
                  ttl_seconds: float, owner: str | None = None) -> dict[str, int]:
    """Sweep the CHECK_PENDING queue within the request/time budget."""
    started = time.monotonic()
    ids = [int(r["id"]) for r in db.query(
        "SELECT * FROM submissions WHERE status='CHECK_PENDING' ORDER BY priority DESC, id ASC"
    )]
    print(f"[reconcile] CHECK_PENDING rows to scan: {len(ids)} (ttl={ttl_seconds:g}s)")
    done = 0
    skipped = 0
    for sid in ids:
        if done >= budget_reconciliations:
            break
        if time.monotonic() - started > budget_seconds:
            break
        if reconcile_one(db, client, sid, ttl_seconds=ttl_seconds, owner=owner):
            done += 1
            if done % 5 == 0:
                print(f"[reconcile] progressed {done} reconciled...")
        else:
            skipped += 1
    print(f"[reconcile] finished: {done} reconciled, {skipped} skipped in {time.monotonic() - started:.1f}s")
    return {"reconciled": done, "skipped": skipped}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help=f"path to research.db (default: ${research_db.DB_ENV_VAR} or repo root)")
    # The original flag was misspelled; keep it working and accept the correct spelling too.
    parser.add_argument("--reconcilations", "--reconciliations", dest="budget_reconciliations",
                        type=int, default=DEFAULT_BUDGET_RECONCILIATIONS,
                        help="reconciliations to attempt in this run")
    parser.add_argument("--seconds", type=float, default=DEFAULT_BUDGET_SECONDS,
                        help="stop after this many seconds")
    parser.add_argument("--max-concurrent", type=int, default=1,
                        help="reconcile runs allowed to be active at once; a run that cannot "
                             "claim a slot exits without calling BRAIN")
    parser.add_argument("--reconcile-ttl", type=float, default=research_db.DEFAULT_RECONCILE_TTL_SECONDS,
                        help="seconds before a CHECK_PENDING row is reconciled against BRAIN again "
                             "(0 disables the throttle)")
    parser.add_argument("--gate-lease-seconds", type=float, default=research_db.DEFAULT_GATE_LEASE_SECONDS,
                        help="how long a run may hold its gate slot before it is reclaimed")
    parser.add_argument("--owner", help="gate slot owner id (default: pid + random suffix)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    owner = args.owner or f"{GATE_NAME}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    with research_db.ResearchDB.open(args.db) as db:
        if not db.acquire_gate(GATE_NAME, owner, slots=max(args.max_concurrent, 1),
                               lease_seconds=args.gate_lease_seconds):
            print(f"[reconcile] gate full: {max(args.max_concurrent, 1)} concurrent run(s) already "
                  f"active; exiting without calling BRAIN")
            return 0
        try:
            client = brain_api.BrainClient()
            try:
                reconcile_all(
                    db, client,
                    budget_reconciliations=max(args.budget_reconciliations, 0),
                    budget_seconds=max(args.seconds, 0.0),
                    ttl_seconds=max(args.reconcile_ttl, 0.0),
                    owner=owner,
                )
            finally:
                client.close()
            return 0
        finally:
            db.release_gate(GATE_NAME, owner)


if __name__ == "__main__":
    sys.exit(main())
