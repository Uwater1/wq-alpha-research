"""Submission queue worker (TODO P6).

Simulation workers never submit: a candidate that clears the gates is filed as READY in
`research.db.submissions`, and this worker drains that queue independently.

    claim a leased READY row            -> only one process can own a submission (P8)
    re-check the gates                  -> metrics floors, turnover, not already ACTIVE
    POST /alphas/{id}/submit            -> 404 = already submitted, 403/409 = pending
    poll the submit endpoint + status   -> ACTIVE / SELF_CORR_FAIL / PLATFORM_REJECTED
    persist the outcome, then continue  -> the queue never stops at the first success

Ordering is not FIFO: `ranking.submission_priority` scores quality + novelty + portfolio
diversification, so a family that already owns ACTIVE alphas yields to a fresh idea.

Local self-correlation (TODO P9) is a switch, not an implementation: `--require-correlation`
makes the gate demand a fresh `self_corr` and refuse anything at or above the limit.

Usage:
    ./.venv/bin/python scripts/submission_worker.py --dry-run          # show the queue order
    ./.venv/bin/python scripts/submission_worker.py                    # submit up to 1 (default)
    ./.venv/bin/python scripts/submission_worker.py --max-submissions 3 --max-runtime 20
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import brain_api  # noqa: E402
import ranking  # noqa: E402
import research_db  # noqa: E402

DEFAULT_MAX_SUBMISSIONS_PER_RUN = 1
DEFAULT_MAX_RUNTIME_SECONDS = 900.0
DEFAULT_LEASE_SECONDS = 300.0
CHECK_POLL_SECONDS = 5.0
MAX_CHECK_POLLS = 60


class SubmissionWorker:
    """Drains `research.db.submissions` one leased candidate at a time."""

    def __init__(
        self,
        db: research_db.ResearchDB,
        client: brain_api.BrainClient,
        *,
        worker_id: str = "submitter",
        max_submissions: int = DEFAULT_MAX_SUBMISSIONS_PER_RUN,
        max_runtime: float | None = DEFAULT_MAX_RUNTIME_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        poll_interval: float = CHECK_POLL_SECONDS,
        max_polls: int = MAX_CHECK_POLLS,
        require_correlation: bool = False,
        correlation_limit: float = 0.7,
        thresholds: Mapping[str, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db = db
        self.client = client
        self.worker_id = worker_id
        self.max_submissions = max_submissions
        self.max_runtime = max_runtime
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self.require_correlation = require_correlation
        self.correlation_limit = correlation_limit
        self.thresholds = dict(thresholds or {})
        self._clock = clock
        self._sleep = sleep
        self.started_at = self._clock()
        self.submitted = 0
        self.active = 0
        self.rejected = 0
        self.retried = 0
        self.reconciled = 0
        self.skipped = 0

    # -- entry point -------------------------------------------------------

    def run(self) -> int:
        self.reconcile_pending()
        while self.submitted < self.max_submissions and not self._runtime_exceeded():
            submission = self._claim()
            if submission is None:
                break
            self._process(submission)
        return 0

    def _runtime_exceeded(self) -> bool:
        return self.max_runtime is not None and self._clock() - self.started_at >= self.max_runtime

    def _claim(self) -> dict[str, Any] | None:
        """Lease the highest-priority READY submission (priority set at enqueue time)."""
        submission = self.db.claim_submission(self.worker_id, self.lease_seconds)
        if submission is None:
            return None
        return submission

    # -- gate check --------------------------------------------------------

    def gate(self, candidate: Mapping[str, Any]) -> tuple[bool, list[str]]:
        return research_db.submission_gate(
            candidate,
            active_keys=self.db.active_keys(),
            require_correlation=self.require_correlation,
            correlation_limit=self.correlation_limit,
            thresholds=self.thresholds or None,
        )

    def enqueue(self, candidate_id: int, priority: float | None = None) -> int:
        """Push a candidate the operator wants submitted; the worker re-checks the gates."""
        return self.db.enqueue_submission(candidate_id, priority=priority)

    def enqueue_first(self, candidate_ids: Iterable[int]) -> list[int]:
        """Enqueue candidates so they are claimed in the given order, ahead of the queue.

        An operator naming a candidate means "submit this next", so each one gets a
        priority above everything currently READY (the first name wins ties).
        """
        requested = list(candidate_ids)
        if not requested:
            return []
        top = max(
            (float(row["priority"] or 0.0) for row in self.db.query(
                "SELECT priority FROM submissions WHERE status='READY'")),
            default=0.0,
        )
        for index, candidate_id in enumerate(requested):
            self.enqueue(candidate_id, priority=top + len(requested) - index)
        return requested

    # -- reconciliation ----------------------------------------------------

    def reconcile_pending(self) -> int:
        """Resolve CHECK_PENDING rows so a run that ran out of time never strands one."""
        for row in self.db.query("SELECT * FROM submissions WHERE status='CHECK_PENDING'"):
            alpha_id = row.get("brain_alpha_id")
            if not alpha_id:
                self.db.finish_submission(int(row["id"]), "RETRY", message="CHECK_PENDING without alpha id")
                self.retried += 1
                continue
            status = self.client.alpha_status(str(alpha_id))
            if status == "ACTIVE":
                self.db.finish_submission(int(row["id"]), "ACTIVE", brain_alpha_id=str(alpha_id))
                self.active += 1
            elif status in ("UNSUBMITTED", None, ""):
                self.db.finish_submission(int(row["id"]), "READY", message="reconciled: still open",
                                          brain_alpha_id=str(alpha_id))
            else:
                self.db.finish_submission(int(row["id"]), "RETRY", message=f"reconciled: status={status}")
                self.retried += 1
            self.reconciled += 1
        return self.reconciled

    # -- one submission ----------------------------------------------------

    def _process(self, submission: Mapping[str, Any]) -> None:
        submission_id = int(submission["id"])
        candidate = self.db.get_candidate(int(submission["candidate_id"]))
        if candidate is None:
            self.db.finish_submission(submission_id, "PLATFORM_REJECTED", message="candidate row missing")
            self.rejected += 1
            return

        allowed, reasons = self.gate(candidate)
        if not allowed:
            self.skipped += 1
            message = "gates failed: " + "; ".join(reasons)
            print(f"[submit] holding candidate {candidate['id']}: {message}")
            # A failed gate is not a rejection: the alpha stays queued for a later review.
            self.db.finish_submission(submission_id, "RETRY" if _retryable(reasons) else "PLATFORM_REJECTED",
                                      message=message, brain_alpha_id=candidate.get("brain_alpha_id"))
            return

        alpha_id = str(candidate["brain_alpha_id"])
        outcome = self.client.submit_alpha(alpha_id)
        print(f"[submit] alpha {_mask(alpha_id)}: {outcome['outcome']}")
        if outcome["outcome"] == "error":
            self.retried += 1
            self.db.finish_submission(submission_id, "RETRY", message=outcome["detail"], brain_alpha_id=alpha_id)
            return
        if outcome["outcome"] not in ("submitted", "already_submitted", "in_progress"):
            self.rejected += 1
            self.db.finish_submission(submission_id, "PLATFORM_REJECTED", message=outcome["detail"],
                                      brain_alpha_id=alpha_id)
            return

        self.submitted += 1
        status, message, max_corr = self._await_result(alpha_id, submission_id)
        self.db.finish_submission(submission_id, status, message=message, max_corr=max_corr,
                                  brain_alpha_id=alpha_id)
        if status == "ACTIVE":
            self.active += 1
        elif status == "RETRY":
            self.retried += 1
        else:
            self.rejected += 1

    def _await_result(self, alpha_id: str, submission_id: int) -> tuple[str, str, float | None]:
        """Poll within a bounded window; never predict a verdict we did not read."""
        max_corr: float | None = None
        for _ in range(self.max_polls):
            if self._runtime_exceeded():
                return "CHECK_PENDING", "run out of time while checks were pending", max_corr
            checks = self.client.submit_checks(alpha_id)
            self_corr = next((c for c in checks if c.get("name") == "SELF_CORRELATION"), None)
            if self_corr is not None:
                value = self_corr.get("value")
                max_corr = float(value) if isinstance(value, (int, float)) else max_corr
                if str(self_corr.get("result", "")).upper() == "FAIL":
                    return "SELF_CORR_FAIL", "platform SELF_CORRELATION check failed", max_corr
                if str(self_corr.get("result", "")).upper() == "PASS":
                    break
            status = self.client.alpha_status(alpha_id)
            if status == "ACTIVE":
                return "ACTIVE", "confirmed ACTIVE", max_corr
            if status in ("REJECTED", "DELETED"):
                return "PLATFORM_REJECTED", f"alpha status={status}", max_corr
            self._sleep(self.poll_interval)

        status = self.client.alpha_status(alpha_id)
        if status == "ACTIVE":
            return "ACTIVE", "confirmed ACTIVE", max_corr
        if status in ("REJECTED", "DELETED"):
            return "PLATFORM_REJECTED", f"alpha status={status}", max_corr
        return "CHECK_PENDING", "checks still pending after the poll budget", max_corr

    # -- reporting ---------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "submitted": self.submitted,
            "active": self.active,
            "rejected": self.rejected,
            "retried": self.retried,
            "skipped_by_gate": self.skipped,
            "reconciled": self.reconciled,
            "queue_remaining": self.db.counts("submissions").get("READY", 0),
            "runtime_seconds": round(self._clock() - self.started_at, 1),
        }


def _retryable(reasons: list[str]) -> bool:
    """A gate that can change (metrics/correlation) is retryable; a duplicate is not."""
    return any("correlation" in reason or "sharpe" in reason or "fitness" in reason or "turnover" in reason
               for reason in reasons)


def _mask(alpha_id: str) -> str:
    """Never print a raw account-linked alpha id."""
    return alpha_id[:2] + "…" if alpha_id else "?"


def dry_run(db: research_db.ResearchDB, limit: int = 10) -> int:
    """Show the submission order without touching BRAIN."""
    rows = db.query(
        "SELECT s.*, c.expression, c.sharpe, c.fitness, c.turnover, c.signal_family, c.canonical_key "
        "FROM submissions s JOIN candidates c ON c.id = s.candidate_id "
        "WHERE s.status='READY' ORDER BY s.priority DESC, s.id ASC LIMIT ?",
        (limit,),
    )
    if not rows:
        print("Submission queue is empty.")
        return 0
    context = ranking.build_context(db)
    for row in rows:
        allowed, reasons = research_db.submission_gate(row, active_keys=db.active_keys())
        flag = "ready" if allowed else "held: " + ",".join(reasons)
        print(f"{row['priority']:7.3f}  id={row['id']:<5} {flag:<40} {str(row['expression'])[:60]!r}")
    print(f"\n{len(rows)} READY submission(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help=f"path to research.db (default: ${research_db.DB_ENV_VAR} or repo root)")
    parser.add_argument("--worker-id", default="submitter", help="lease owner id recorded in research.db")
    parser.add_argument("--max-submissions", type=int, default=DEFAULT_MAX_SUBMISSIONS_PER_RUN,
                        help="submissions to attempt in this run (the queue continues afterwards)")
    parser.add_argument("--max-runtime", type=float, default=DEFAULT_MAX_RUNTIME_SECONDS / 60.0,
                        help="stop after this many minutes (0 = no limit)")
    parser.add_argument("--lease-seconds", type=float, default=DEFAULT_LEASE_SECONDS,
                        help="how long this worker owns a claimed submission")
    parser.add_argument("--require-correlation", action="store_true",
                        help="demand a fresh local self-correlation before submitting (TODO P9)")
    parser.add_argument("--min-sharpe", type=float, default=research_db.IS_THRESHOLDS["sharpe"],
                        help="submission floor for Sharpe")
    parser.add_argument("--min-fitness", type=float, default=research_db.IS_THRESHOLDS["fitness"],
                        help="submission floor for Fitness")
    parser.add_argument("--min-turnover", type=float, default=research_db.IS_THRESHOLDS["turnover_min"],
                        help="submission floor for turnover (fraction, 0.01 = 1%%)")
    parser.add_argument("--max-turnover", type=float, default=research_db.IS_THRESHOLDS["turnover_max"],
                        help="submission ceiling for turnover (fraction, 0.20 = 20%%)")
    parser.add_argument("--submit-candidate", type=int, action="append", default=None,
                        help="enqueue this candidate id before draining (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="print the queue order; never call BRAIN")
    parser.add_argument("--json", action="store_true", help="print the run summary as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        if args.dry_run:
            return dry_run(db)
        client = brain_api.BrainClient()
        worker = SubmissionWorker(
            db,
            client,
            worker_id=args.worker_id,
            max_submissions=args.max_submissions,
            max_runtime=None if not args.max_runtime else args.max_runtime * 60.0,
            lease_seconds=args.lease_seconds,
            require_correlation=args.require_correlation,
            thresholds={
                "sharpe": args.min_sharpe,
                "fitness": args.min_fitness,
                "turnover_min": args.min_turnover,
                "turnover_max": args.max_turnover,
            },
        )
        for candidate_id in args.submit_candidate or []:
            print(f"[submit] enqueued candidate {candidate_id} on request")
        worker.enqueue_first(args.submit_candidate or [])
        try:
            code = worker.run()
        finally:
            client.close()
        summary = worker.summary()
        print(json.dumps(summary, indent=2, sort_keys=True) if args.json else
              f"submitted={summary['submitted']} active={summary['active']} rejected={summary['rejected']} "
              f"retried={summary['retried']} held={summary['skipped_by_gate']} "
              f"queue_remaining={summary['queue_remaining']}")
        return code


if __name__ == "__main__":
    sys.exit(main())
