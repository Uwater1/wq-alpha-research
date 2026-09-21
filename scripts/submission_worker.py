"""Submission queue worker.

Simulation workers never submit: a candidate that clears the gates is filed as READY in
`research.db.submissions`, and this worker drains that queue independently.

    recover + reconcile uncertain rows  -> BRAIN, not the local row, decides a retry
    claim a leased READY/RETRY row      -> only one process can own a submission
    re-check the gates                  -> metrics floors, turnover, correlation, not ACTIVE
    POST /alphas/{id}/submit            -> 404 = already submitted, 403/409 = pending
    poll the submit endpoint + status   -> ACTIVE / SELF_CORR_FAIL / PLATFORM_REJECTED
    persist the outcome, then continue  -> the queue never stops at the first success

Ordering is not FIFO: `ranking.submission_priority` scores quality + novelty + portfolio
diversification, so a family that already owns ACTIVE alphas yields to a fresh idea.

Recovery is explicit: an expired lease returns to READY only when the POST never
left the process; after a POST the row is CHECK_PENDING and is reconciled against BRAIN
before any retry. Transient failures back off automatically and retire as EXHAUSTED once
their attempt budget is spent, so nothing has to be re-enqueued or deleted by hand.

Local self-correlation is a real gate: `--require-correlation` syncs the ACTIVE
book, fetches candidate PnL, checks a fresh local `self_corr` and refuses anything at or
above the limit; BRAIN's own SELF_CORRELATION check stays the final confirmation.

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
import correlation  # noqa: E402
import ranking  # noqa: E402
import research_db  # noqa: E402

DEFAULT_MAX_SUBMISSIONS_PER_RUN = 1
DEFAULT_MAX_RUNTIME_SECONDS = 900.0
DEFAULT_LEASE_SECONDS = 300.0
CHECK_POLL_SECONDS = 5.0
MAX_CHECK_POLLS = 60

#: Submission statuses that mean "the platform is still evaluating the POST".
PENDING_ALPHA_STATUSES = frozenset({"", "UNSUBMITTED"})
REJECTED_ALPHA_STATUSES = frozenset({"REJECTED", "DELETED"})


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
        max_submission_attempts: int = research_db.DEFAULT_SUBMISSION_MAX_ATTEMPTS,
        require_correlation: bool = False,
        correlation_limit: float = 0.7,
        correlation_exception_ratio: float = research_db.CORRELATION_EXCEPTION_RATIO,
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
        self.max_submission_attempts = max(int(max_submission_attempts), 1)
        self.require_correlation = require_correlation
        self.correlation_limit = correlation_limit
        self.correlation_exception_ratio = correlation_exception_ratio
        self.thresholds = dict(thresholds or {})
        self._clock = clock
        self._sleep = sleep
        self._correlation: correlation.CorrelationService | None = None
        self.started_at = self._clock()
        self.submitted = 0
        self.active = 0
        self.rejected = 0
        self.retried = 0
        self.reconciled = 0
        self.skipped = 0
        self.correlations = 0

    # -- entry point -------------------------------------------------------

    def run(self) -> int:
        # Recovery first: expired leases, spent attempt budgets, then BRAIN
        # reconciliation of anything whose POST outcome is still unknown.
        recovered = self.db.recover_expired_leases()
        if recovered["submissions"]:
            print(f"[submit] recovered {recovered['submissions']} expired submission lease(s)")
        retired = self.db.expire_exhausted_submissions(self.max_submission_attempts)
        if retired:
            print(f"[submit] retired {retired} submission(s) past their attempt budget")
        if self.require_correlation:
            # A cached correlation is only fresh relative to a successfully refreshed
            # ACTIVE book. Hold the whole run when that refresh fails.
            if not self._sync_active_book():
                return 0
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
        """Lease the highest-priority claimable submission (READY, or a due RETRY)."""
        submission = self.db.claim_submission(
            self.worker_id, self.lease_seconds, max_attempts=self.max_submission_attempts
        )
        if submission is None:
            return None
        return submission

    # -- correlation -------------------------------------------------------

    def correlation_service(self) -> correlation.CorrelationService:
        """Lazily build the local correlation pipeline for this worker."""
        if self._correlation is None:
            self._correlation = correlation.CorrelationService(
                self.db, self.client, sleep=self._sleep, log=print
            )
        return self._correlation

    def _sync_active_book(self) -> bool:
        """Refresh the ACTIVE book once per run so cached checks are current."""
        sync = self.correlation_service().sync_active_book()
        if sync.error:
            print(f"[submit] ACTIVE book sync failed: {sync.error}")
            return False
        print(
            f"[submit] ACTIVE book v{sync.version}: {sync.fetched} alpha(s) "
            f"(+{sync.added}/-{sync.removed}), {sync.pnl_cached} PnL cache fill(s), "
            f"{sync.stale_checks} stale check(s)"
        )
        return True

    def _ensure_correlation(self, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        """Re-check a stale/missing local correlation immediately before submitting."""
        if not self.require_correlation:
            return candidate
        result = self.correlation_service().check_candidate(candidate)
        self.correlations += 1
        if result.from_cache:
            return candidate
        print(
            f"[submit] correlation for candidate {candidate['id']}: {result.status} "
            f"max={result.max_corr if result.max_corr is None else round(result.max_corr, 3)} "
            f"({result.compared} compared{'; ' + result.reason if result.reason else ''})"
        )
        return self.db.get_candidate(int(candidate["id"])) or candidate

    # -- gate check --------------------------------------------------------

    def gate(self, candidate: Mapping[str, Any]) -> tuple[bool, list[str]]:
        return research_db.submission_gate(
            candidate,
            active_keys=self.db.active_keys(),
            require_correlation=self.require_correlation,
            correlation_limit=self.correlation_limit,
            active_set_version=self.db.active_set_version() if self.require_correlation else None,
            # The 7.2 exception compares against the alpha we correlate with, so the gate
            # needs the book's Sharpes -- which the ACTIVE sync above just refreshed.
            active_sharpes=self.db.active_alpha_sharpes() if self.require_correlation else None,
            correlation_exception_ratio=self.correlation_exception_ratio,
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
        """Resolve CHECK_PENDING rows against BRAIN before anything is retried.

        BRAIN is the source of truth after an uncertain POST: ACTIVE and rejected alphas are
        final, a pending check keeps the row reconcilable, and READY is used only when BRAIN
        shows the alpha is not submitted and exposes no pending submission checks.
        """
        for row in self.db.query("SELECT * FROM submissions WHERE status='CHECK_PENDING'"):
            submission_id = int(row["id"])
            alpha_id = row.get("brain_alpha_id") or self._candidate_alpha_id(int(row["candidate_id"]))
            if not alpha_id:
                self.db.finish_submission(submission_id, "RETRY",
                                          message="CHECK_PENDING without an alpha id; nothing to reconcile",
                                          max_attempts=self.max_submission_attempts)
                self.retried += 1
                self.reconciled += 1
                continue

            status = self.client.alpha_status(str(alpha_id))
            checks = self.client.submit_checks(str(alpha_id))
            verdict = _self_correlation_verdict(checks)
            if status == "ACTIVE":
                self.db.finish_submission(submission_id, "ACTIVE", message="reconciled: confirmed ACTIVE",
                                          max_corr=verdict["value"], brain_alpha_id=str(alpha_id))
                self.active += 1
            elif status in REJECTED_ALPHA_STATUSES:
                self.db.finish_submission(submission_id, "PLATFORM_REJECTED",
                                          message=f"reconciled: alpha status={status}", brain_alpha_id=str(alpha_id))
                self.rejected += 1
            elif verdict["result"] == "FAIL":
                self.db.finish_submission(submission_id, "SELF_CORR_FAIL",
                                          message="reconciled: platform SELF_CORRELATION failed",
                                          max_corr=verdict["value"], brain_alpha_id=str(alpha_id))
                self.rejected += 1
            elif _checks_pending(checks):
                self.db.finish_submission(submission_id, "CHECK_PENDING",
                                          message="reconciled: submission checks still pending",
                                          brain_alpha_id=str(alpha_id))
            elif status in PENDING_ALPHA_STATUSES:
                self.db.finish_submission(submission_id, "READY",
                                          message="reconciled: BRAIN reports the alpha is not submitted",
                                          brain_alpha_id=str(alpha_id))
                self.retried += 1
            else:
                self.db.finish_submission(submission_id, "RETRY", message=f"reconciled: status={status}",
                                          brain_alpha_id=str(alpha_id),
                                          max_attempts=self.max_submission_attempts)
                self.retried += 1
            self.reconciled += 1
        return self.reconciled

    def _candidate_alpha_id(self, candidate_id: int) -> str | None:
        candidate = self.db.get_candidate(candidate_id)
        return str(candidate["brain_alpha_id"]) if candidate and candidate.get("brain_alpha_id") else None

    # -- one submission ----------------------------------------------------

    def _process(self, submission: Mapping[str, Any]) -> None:
        submission_id = int(submission["id"])
        candidate = self.db.get_candidate(int(submission["candidate_id"]))
        if candidate is None:
            self.db.finish_submission(submission_id, "PLATFORM_REJECTED", message="candidate row missing")
            self.rejected += 1
            return

        # Local correlation is re-checked here, not trusted from an earlier run.
        candidate = self._ensure_correlation(candidate)
        allowed, reasons = self.gate(candidate)
        if not allowed:
            self.skipped += 1
            message = "gates failed: " + "; ".join(reasons)
            print(f"[submit] holding candidate {candidate['id']}: {message}")
            # A failed gate is not a rejection: the alpha stays queued for a later review.
            self.db.finish_submission(submission_id, "RETRY" if _retryable(reasons) else "PLATFORM_REJECTED",
                                      message=message, brain_alpha_id=candidate.get("brain_alpha_id"),
                                      max_attempts=self.max_submission_attempts)
            return

        alpha_id = str(candidate["brain_alpha_id"])
        # Write-ahead marker: if this process dies during the POST, recovery knows the
        # outcome is unknown and must be reconciled instead of blindly retried.
        self.db.mark_submission_posted(submission_id)
        outcome = self.client.submit_alpha(alpha_id)
        attempts = []
        drain = getattr(self.client, "drain_attempts", None)
        if callable(drain):
            try:
                attempts = drain() or []
            except Exception:
                attempts = []
        if attempts:
            for entry in attempts:
                self.db.log_event(
                    "transport", submission_id, "http", operation="submission.submit",
                    candidate_id=int(candidate["id"]), submission_id=submission_id,
                    http_status=entry.get("http_status"), error_category=entry.get("error_category"),
                    retry_count=int(entry.get("attempt") or 0), latency_ms=entry.get("latency_ms"),
                    rate_limit_seconds=entry.get("rate_limit_seconds"),
                    result_class=outcome.get("outcome") if entry.get("final") else "retry",
                )
        else:
            request = getattr(self.client, "last_request", {}) or {}
            self.db.log_event(
                "transport", submission_id, "http", operation="submission.submit",
                candidate_id=int(candidate["id"]), submission_id=submission_id,
                http_status=request.get("http_status"), error_category=request.get("error_category"),
                retry_count=int(request.get("retry_count") or 0), latency_ms=request.get("latency_ms"),
                rate_limit_seconds=request.get("rate_limit_seconds"), result_class=outcome.get("outcome"),
            )
        print(f"[submit] alpha {_mask(alpha_id)}: {outcome['outcome']}")
        if outcome["outcome"] == "uncertain":
            # The POST may have reached BRAIN even though its response was lost. Do not
            # turn that ambiguity into a retry; reconciliation must decide first.
            self.db.finish_submission(submission_id, "CHECK_PENDING", message=outcome["detail"],
                                      brain_alpha_id=alpha_id)
            return
        if outcome["outcome"] == "error":
            self.retried += 1
            self.db.finish_submission(submission_id, "RETRY", message=outcome["detail"], brain_alpha_id=alpha_id,
                                      max_attempts=self.max_submission_attempts)
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
            "correlations": self.correlations,
            "active_set_version": self.db.active_set_version(),
            "queue_remaining": self.db.counts("submissions").get("READY", 0),
            "runtime_seconds": round(self._clock() - self.started_at, 1),
        }


def _self_correlation_verdict(checks: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Read the SELF_CORRELATION check from a submit payload."""
    check = next((c for c in checks if str(c.get("name", "")).upper() == "SELF_CORRELATION"), None)
    if check is None:
        return {"result": None, "value": None}
    value = check.get("value")
    return {"result": str(check.get("result", "")).upper(),
            "value": float(value) if isinstance(value, (int, float)) else None}


def _checks_pending(checks: Iterable[Mapping[str, Any]]) -> bool:
    """True while the platform has checks that are neither PASS nor FAIL yet."""
    results = [str(c.get("result", "")).upper() for c in checks]
    if not results or "FAIL" in results:
        return False
    return not all(result == "PASS" for result in results)


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
    parser.add_argument("--max-submission-attempts", type=int, default=research_db.DEFAULT_SUBMISSION_MAX_ATTEMPTS,
                        help="retries a submission may spend before it is marked EXHAUSTED")
    parser.add_argument("--require-correlation", action="store_true",
                        help="demand a fresh local self-correlation before submitting")
    parser.add_argument("--correlation-limit", type=float, default=0.7,
                        help="reject a candidate whose |daily-return correlation| is at least this")
    parser.add_argument("--correlation-exception-ratio", type=float,
                        default=research_db.CORRELATION_EXCEPTION_RATIO,
                        help="allow a correlated candidate whose Sharpe is at least this many times "
                             "the correlated ACTIVE alpha's (SKILL.md 7.2); 0 disables the exception")
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
            max_submission_attempts=args.max_submission_attempts,
            require_correlation=args.require_correlation,
            correlation_limit=args.correlation_limit,
            correlation_exception_ratio=args.correlation_exception_ratio,
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
