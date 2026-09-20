"""Persistent BRAIN simulation scheduler.

Replaces the one-shot `ThreadPoolExecutor` batch loop with a dispatcher that:

    * keeps exactly MAX_SIMULATION_SLOTS simulations in flight and refills a freed
      slot on the next pass, so BRAIN capacity is never idle while work is queued;
    * polls running simulations and submits new ones in the same pass but through
      separate paths, so a slow simulation cannot block a free slot;
    * honours `Retry-After` from 429s and backs off exponentially on transient
      failures instead of retrying in a tight loop;
    * re-authenticates once when BRAIN rejects the session;
    * adopts simulations left running by a killed process (their BRAIN ids are in
      research.db), so a restart resumes work instead of losing it;
    * budgets each structure's variant search once the queue is large enough, so a
      parameter grid cannot monopolize all three slots before its base signal proves
       useful (variant gate);
    * writes every state transition to research.db — a crash never loses queue state.

Priority is not FIFO: candidates are scored by `scripts/ranking.py`
(quality + novelty + information gain + family diversity - duplicate penalty -
failure risk) and the components are persisted for later model training (Priority 2).

Usage:
    ./.venv/bin/python scripts/sim_scheduler.py                  # drain the queue
    ./.venv/bin/python scripts/sim_scheduler.py --max-runtime 30 # bounded cron-style run
    ./.venv/bin/python scripts/sim_scheduler.py --once           # one fill+poll pass
    ./.venv/bin/python scripts/sim_scheduler.py --dry-run        # rank only, no BRAIN calls

Queue work first, for example with `research_db.py queue <input.csv>`.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import brain_api  # noqa: E402
import ranking  # noqa: E402
import research_db  # noqa: E402
import staged_search  # noqa: E402
import successive_halving  # noqa: E402

MAX_SIMULATION_SLOTS = 3
POLL_INTERVAL_INITIAL = 3.0
POLL_INTERVAL_MAX = 30.0
DEFAULT_LEASE_SECONDS = 1800.0
DEFAULT_SIM_TIMEOUT = 1800.0
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0
MAX_POLL_FAILURES = 4
DEFAULT_RUNTIME_SECONDS = 3600.0
QUEUE_SCAN_LIMIT = 500
RANKING_CONTEXT_TTL = 30.0


class SimulationScheduler:
    """Drains the research.db queue into BRAIN, three slots at a time."""

    def __init__(
        self,
        db: research_db.ResearchDB,
        client: brain_api.BrainClient,
        *,
        worker_id: str = "scheduler",
        slots: int = MAX_SIMULATION_SLOTS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        sim_timeout: float = DEFAULT_SIM_TIMEOUT,
        poll_interval: float = POLL_INTERVAL_INITIAL,
        poll_interval_max: float = POLL_INTERVAL_MAX,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
        max_runtime: float | None = DEFAULT_RUNTIME_SECONDS,
        max_simulations: int | None = None,
        adopt_orphans: bool = True,
        halving: bool = True,
        early_attempts: int = successive_halving.DEFAULT_EARLY_ATTEMPTS,
        variant_horizon_minutes: float = successive_halving.DEFAULT_HORIZON_MINUTES,
        staged_search: bool = True,
        staged_volume_threshold: int = staged_search.DEFAULT_VOLUME_THRESHOLD,
        staged_max_variants: int = staged_search.DEFAULT_MAX_VARIANTS,
        staged_min_pass_ratio: float = staged_search.DEFAULT_MIN_PASS_RATIO,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db = db
        self.client = client
        self.worker_id = worker_id
        self.slots = max(1, int(slots))
        self.lease_seconds = lease_seconds
        self.sim_timeout = sim_timeout
        self.poll_interval = poll_interval
        self.poll_interval_max = poll_interval_max
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_runtime = max_runtime
        self.max_simulations = max_simulations
        self.adopt_orphans = adopt_orphans
        self.halving = halving
        self.early_attempts = early_attempts
        self.variant_horizon_minutes = variant_horizon_minutes
        self.staged = staged_search
        self.staged_volume_threshold = staged_volume_threshold
        self.staged_max_variants = staged_max_variants
        self.staged_min_pass_ratio = staged_min_pass_ratio
        self._clock = clock
        self._sleep = sleep

        self.inflight: dict[int, brain_api.SimulationHandle] = {}
        self._submitted_at: dict[int, float] = {}
        self._poll_failures: dict[int, int] = {}
        self._context: ranking.RankingContext | None = None
        self._context_at = 0.0
        self.blocked_until = 0.0
        self.started_at = self._clock()

        self.submitted = 0
        self.completed = 0
        self.failed = 0
        self.rate_limited = 0
        self.reauths = 0
        self.adopted = 0
        self.released = 0
        self.passes = 0

    # -- entry point -------------------------------------------------------

    def run(self, *, once: bool = False) -> int:
        """Drain the queue (or take a single pass). Returns a process exit code."""
        self.started_at = self._clock()
        if self.adopt_orphans:
            self.adopt_running()
        requeued = self.db.requeue_due_retries()
        if requeued:
            print(f"[scheduler] re-queued {len(requeued)} candidate(s) whose retry window elapsed")
        counters = self.review_halving()
        if counters["promoted_total"] or counters["deferred"]:
            print(f"[scheduler] successive halving: promoted {counters['promoted_total']}, "
                  f"deferred {counters['deferred']} variant(s) of unproven structures")
        staged = self.review_staged()
        if staged["engaged"] and (staged["deferred"] or staged["promoted"] or staged["stopped"]):
            print(f"[scheduler] staged search: {staged['structures']} structure(s), "
                  f"deferred {staged['deferred']}, promoted {staged['promoted']}, "
                  f"stopped {staged['stopped']} (queue={staged['queued']})")
        try:
            while True:
                self.fill_slots()
                if once:
                    self.poll_round()
                    break
                if not self._work_remaining() or self._should_stop():
                    break
                wait = self.poll_round()
                if not self._work_remaining() or self._should_stop():
                    break
                self._sleep(self._next_delay(wait))
        except KeyboardInterrupt:
            print("Interrupted — submitted simulations stay leased in research.db and are adopted later.")
        return 0

    def _next_delay(self, wait: float) -> float:
        """How long to sleep: refill at once when a slot is free, else wait for BRAIN."""
        delay = max(0.0, wait)
        if len(self.inflight) < self.slots and self.db.list_queued(limit=1, due_only=True):
            return 0.05  # a slot is free and work is waiting — come straight back
        block_left = self.blocked_until - self._clock()
        if block_left > 0 and not self.inflight:
            # Nothing to poll, so honour the whole Retry-After instead of hot-looping on 429.
            return min(block_left, self.backoff_max)
        return delay

    def review_halving(self) -> dict[str, int]:
        """Apply the variant gate; called at start and after every verdict."""
        if not self.halving:
            return {"promoted_total": 0, "promoted_proven": 0, "promoted_horizon": 0, "deferred": 0,
                    "blocked_structures": 0, "waiting": 0}
        return successive_halving.review(
            self.db, early_attempts=self.early_attempts, horizon_minutes=self.variant_horizon_minutes
        )

    def review_staged(self) -> dict[str, Any]:
        """Apply the volume-gated staged-search funnel; no-op below the threshold."""
        if not self.staged_search_enabled():
            return {"engaged": False, "queued": 0, "structures": 0, "deferred": 0, "promoted": 0,
                    "stopped": 0, "budget_rows": 0}
        return staged_search.review(
            self.db,
            volume_threshold=self.staged_volume_threshold,
            max_variants=self.staged_max_variants,
            min_pass_ratio=self.staged_min_pass_ratio,
            horizon_minutes=self.variant_horizon_minutes * 2,
        )

    def staged_search_enabled(self) -> bool:
        return bool(self.staged)

    def _work_remaining(self) -> bool:
        if self.inflight:
            return True
        return bool(self.db.list_queued(limit=1, due_only=True))

    def _should_stop(self) -> bool:
        if self.max_simulations is not None and self.completed >= self.max_simulations:
            return True
        if self.max_runtime is not None and self._clock() - self.started_at >= self.max_runtime:
            return True
        return False

    # -- slots -------------------------------------------------------------

    def fill_slots(self) -> int:
        """Submit until every slot is busy, the queue is empty, or BRAIN says stop."""
        started = 0
        while len(self.inflight) < self._slot_budget():
            if self.blocked_until > self._clock():
                break
            candidate = self.select_next()
            if candidate is None:
                break
            try:
                if self.start(candidate):
                    started += 1
            except Exception as exc:  # never leave a claim dangling on an unexpected error
                self.db.release_claim(int(candidate["id"]), reason="submit_crashed")
                raise exc
        return started

    def _slot_budget(self) -> int:
        """How many simulations may be open at once (slots, capped by --max-simulations)."""
        if self.max_simulations is None:
            return self.slots
        return max(0, min(self.slots, self.max_simulations - self.completed))

    def select_next(self) -> dict[str, Any] | None:
        """Score the claimable candidates and atomically claim the best one."""
        rows = self.db.list_queued(limit=QUEUE_SCAN_LIMIT, due_only=True)
        if not rows:
            return None
        context = self.ranking_context()
        best_row, best_score = ranking.rank(rows, context)[0]
        candidate = self.db.claim_simulation(self.worker_id, self.lease_seconds, candidate_id=int(best_row["id"]))
        if candidate is None:  # another worker took it between the read and the claim
            return None
        self.db.record_ranking(candidate["id"], best_score.components(), best_score.priority, best_score.reasons)
        return candidate

    def ranking_context(self) -> ranking.RankingContext:
        """Rebuild the ranking context at most every RANKING_CONTEXT_TTL seconds."""
        now = self._clock()
        if self._context is None or now - self._context_at > RANKING_CONTEXT_TTL:
            self._context = ranking.build_context(self.db)
            self._context_at = now
        return self._context

    def start(self, candidate: Mapping[str, Any]) -> bool:
        """Submit one claimed candidate; on failure the claim is released or retired."""
        candidate_id = int(candidate["id"])
        expression = str(candidate.get("normalized_expression") or candidate.get("expression") or "")
        settings = self._settings(candidate)
        try:
            handle = self._submit_with_reauth(expression, settings, candidate_id)
        except brain_api.RateLimitError as exc:
            self.rate_limited += 1
            self.released += 1
            self.blocked_until = self._clock() + max(exc.retry_after, self.backoff_base)
            self.db.release_claim(candidate_id, reason="rate_limited")
            self.db.log_event(
                "candidate", candidate_id, "submit_rate_limited", payload={"retry_after": exc.retry_after},
            )
            print(f"[scheduler] BRAIN asked to wait {exc.retry_after:.0f}s before submitting again")
            return False
        except brain_api.SessionExpiredError as exc:
            # Already retried with a fresh session inside _submit_with_reauth.
            self._retire(candidate_id, f"session_expired: {exc}")
            return False
        except brain_api.BrainAPIError as exc:
            self._retire(candidate_id, str(exc))
            return False

        self._log_transport(
            candidate_id=candidate_id, simulation_id=handle.simulation_id,
            operation="simulation.submit", result_class="accepted",
        )
        self.db.mark_simulation_started(
            candidate_id, handle.simulation_id, worker_id=self.worker_id, lease_seconds=self.lease_seconds
        )
        handle.next_poll_at = self._clock() + self.poll_interval
        self.inflight[candidate_id] = handle
        self._submitted_at[candidate_id] = self._clock()
        self.submitted += 1
        print(f"[scheduler] slot {len(self.inflight)}/{self.slots}: {expression[:60]!r} -> {handle.simulation_id}")
        return True

    def _log_transport(self, *, candidate_id: int | None = None, simulation_id: str | None = None,
                       submission_id: int | None = None, operation: str, result_class: str) -> None:
        """Persist non-sensitive HTTP telemetry when the client exposes it."""
        request = getattr(self.client, "last_request", {}) or {}
        self.db.log_event(
            "transport", candidate_id or simulation_id or submission_id, "http",
            operation=operation, candidate_id=candidate_id, simulation_id=simulation_id,
            submission_id=submission_id, http_status=request.get("http_status"),
            error_category=request.get("error_category"), retry_count=int(request.get("retry_count") or 0),
            latency_ms=request.get("latency_ms"), rate_limit_seconds=request.get("rate_limit_seconds"),
            result_class=result_class,
        )

    def _submit_with_reauth(self, expression: str, settings: Mapping[str, Any], candidate_id: int):
        try:
            return self.client.submit(expression, settings, candidate_id=candidate_id)
        except brain_api.SessionExpiredError:
            self.reauthenticate()
            return self.client.submit(expression, settings, candidate_id=candidate_id)

    def _settings(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        try:
            settings = json.loads(str(candidate.get("settings_json") or "{}"))
        except ValueError:
            settings = {}
        return {k: v for k, v in settings.items() if k != "visualization"}

    def _retire(self, candidate_id: int, reason: str) -> None:
        """Recoverable failure: keep the candidate, retry later with backoff."""
        self.failed += 1
        delay = self._backoff(self._poll_failures.get(candidate_id, 0))
        try:
            self.db.record_simulation_result(
                candidate_id=candidate_id, status="ERROR", error=reason, retry_delay_seconds=delay
            )
        except (KeyError, ValueError):
            self.db.release_claim(candidate_id, reason="retired")
        print(f"[scheduler] candidate {candidate_id} retired for {delay:.0f}s: {reason[:120]}")

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_base * (2**attempt), self.backoff_max)

    def reauthenticate(self) -> None:
        self.reauths += 1
        print("[scheduler] session rejected by BRAIN — re-authenticating")
        self.client.reauthenticate()
        self.db.log_event("worker", self.worker_id, "reauthenticated")

    # -- restart support ---------------------------------------------------

    def adopt_running(self) -> int:
        """Pick up simulations a previous process left running."""
        now = self._clock()
        for row in self.db.running_simulations():
            candidate_id = int(row["id"])
            same_worker = row["worker_id"] == self.worker_id
            live_lease = bool(row["lease_until"]) and str(row["lease_until"]) > research_db.now_iso()
            if not same_worker and live_lease:
                # A different live worker owns this simulation; polling it too would only
                # duplicate the result. A restart of *this* worker resumes its own work.
                continue
            handle = brain_api.SimulationHandle(
                candidate_id=candidate_id,
                canonical_key=str(row["canonical_key"]),
                expression=str(row["normalized_expression"] or row["expression"]),
                simulation_id=str(row["simulation_id"]),
                url=f"{brain_api.API_BASE}/simulations/{row['simulation_id']}",
                submitted_at=now,
            )
            handle.next_poll_at = now
            self.inflight[candidate_id] = handle
            self._submitted_at[candidate_id] = now
            self.db.mark_simulation_started(
                candidate_id, handle.simulation_id, worker_id=self.worker_id, lease_seconds=self.lease_seconds,
            )
            self.adopted += 1
        if self.adopted:
            print(f"[scheduler] adopted {self.adopted} simulation(s) left running by a previous process")
        return self.adopted

    # -- polling -----------------------------------------------------------

    def poll_round(self) -> float:
        """Poll every due simulation once; returns the seconds to wait before the next round."""
        self.passes += 1
        now = self._clock()
        wait = self.poll_interval
        for candidate_id in list(self.inflight):
            handle = self.inflight[candidate_id]
            if handle.next_poll_at > now:
                wait = min(wait, handle.next_poll_at - now)
                continue
            if self._timed_out(candidate_id):
                self._finish(candidate_id, error=f"simulation_timeout after {int(self.sim_timeout)}s")
                continue
            try:
                state = self.client.poll(handle)
                self._log_transport(candidate_id=candidate_id, simulation_id=handle.simulation_id,
                                    operation="simulation.poll", result_class=state.status.lower())
            except brain_api.RateLimitError as exc:
                self.rate_limited += 1
                handle.next_poll_at = self._clock() + max(exc.retry_after, self.poll_interval)
                wait = min(wait, max(exc.retry_after, self.poll_interval))
                continue
            except brain_api.SessionExpiredError:
                self.reauthenticate()
                handle.next_poll_at = self._clock() + self.poll_interval
                continue
            except brain_api.BrainAPIError as exc:
                failures = self._poll_failures[candidate_id] = self._poll_failures.get(candidate_id, 0) + 1
                if failures > MAX_POLL_FAILURES:
                    self._finish(candidate_id, error=f"poll_failed: {exc}")
                else:
                    delay = self._backoff(failures - 1)
                    handle.next_poll_at = self._clock() + delay
                    wait = min(wait, delay)
                continue

            self._poll_failures[candidate_id] = 0
            if state.status == "DONE" and state.alpha_id:
                self._finish(candidate_id, alpha_id=state.alpha_id)
            elif state.status == "ERROR":
                self._finish(candidate_id, error=state.message or "simulation error")
            else:
                handle.next_poll_at = self._clock() + min(self.poll_interval * 1.5, self.poll_interval_max)
                if state.progress is not None and state.progress - handle.last_progress >= 0.1:
                    handle.last_progress = state.progress
                    print(f"[scheduler] {handle.expression[:40]!r} {int(100 * state.progress)}%")
        return wait

    def _timed_out(self, candidate_id: int) -> bool:
        started = self._submitted_at.get(candidate_id)
        return started is not None and self._clock() - started > self.sim_timeout

    def _finish(self, candidate_id: int, *, alpha_id: str | None = None, error: str | None = None) -> None:
        """Record a terminal simulation outcome and free the slot."""
        handle = self.inflight.pop(candidate_id, None)
        latency = self._clock() - (self._submitted_at.pop(candidate_id, self._clock()))
        self._poll_failures.pop(candidate_id, None)
        if handle is None:
            return

        if error is not None:
            self.failed += 1
            self.db.record_simulation_result(
                candidate_id=candidate_id, status="ERROR", error=error,
                simulation_id=handle.simulation_id, retry_delay_seconds=self.backoff_base,
            )
            print(f"[scheduler] simulation failed after {latency:.0f}s: {error[:120]}")
            return

        if alpha_id is None:
            return
        try:
            metrics = self.client.alpha_metrics(alpha_id)
        except brain_api.SessionExpiredError:
            self.reauthenticate()
            metrics = self.client.alpha_metrics(alpha_id)
        except brain_api.BrainAPIError as exc:
            self.failed += 1
            self.db.record_simulation_result(
                candidate_id=candidate_id, status="ERROR", error=f"alpha_fetch_failed: {exc}",
                simulation_id=handle.simulation_id, brain_alpha_id=alpha_id,
                retry_delay_seconds=self.backoff_base,
            )
            return

        candidate = self.db.record_simulation_result(
            candidate_id=candidate_id,
            status="DONE",
            metrics=metrics,
            checks=metrics.get("checks") or [],
            brain_alpha_id=alpha_id,
            simulation_id=handle.simulation_id,
        )
        self.completed += 1
        # A verdict just landed: the structure is either proven (siblings promote now) or
        # unproven (siblings keep waiting), which is exactly what the variant gate tracks.
        self.review_halving()
        self.review_staged()
        status = (candidate or {}).get("status", "?")
        print(
            f"[scheduler] done in {latency:.0f}s: {status} "
            f"sharpe={metrics.get('sharpe')} fitness={metrics.get('fitness')} turnover={metrics.get('turnover')}"
        )

    # -- reporting ---------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "slots": self.slots,
            "passes": self.passes,
            "submitted": self.submitted,
            "adopted": self.adopted,
            "completed": self.completed,
            "failed": self.failed,
            "rate_limited": self.rate_limited,
            "released": self.released,
            "reauthentications": self.reauths,
            "queued_remaining": len(self.db.list_queued()),
            "in_flight": len(self.inflight),
            "runtime_seconds": round(self._clock() - self.started_at, 1),
        }


def dry_run(db: research_db.ResearchDB, limit: int = 10) -> int:
    """Score the queue without touching BRAIN (ranking sanity check)."""
    rows = db.list_queued(limit=QUEUE_SCAN_LIMIT)
    if not rows:
        print("Queue is empty — nothing to rank.")
        return 0
    context = ranking.build_context(db)
    for row, score in ranking.rank(rows, context)[:limit]:
        print(
            f"{score.priority:7.3f}  id={row['id']:<5} quality={score.expected_quality:.2f} "
            f"novelty={score.novelty:.2f} gain={score.information_gain:.2f} "
            f"dupe={score.duplicate_penalty:.2f} risk={score.failure_risk:.2f}  {str(row['expression'])[:70]!r}"
        )
    print(f"\n{len(rows)} queued candidate(s); showing the top {min(limit, len(rows))}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help=f"path to research.db (default: ${research_db.DB_ENV_VAR} or repo root)")
    parser.add_argument("--slots", type=int, default=MAX_SIMULATION_SLOTS, help="concurrent simulations (BRAIN caps at 3)")
    parser.add_argument("--worker-id", default="scheduler", help="lease owner id recorded in research.db")
    parser.add_argument("--lease-seconds", type=float, default=DEFAULT_LEASE_SECONDS, help="lease length per simulation")
    parser.add_argument("--sim-timeout", type=float, default=DEFAULT_SIM_TIMEOUT, help="per-simulation wall clock limit")
    parser.add_argument("--max-runtime", type=float, default=DEFAULT_RUNTIME_SECONDS / 60.0,
                        help="stop after this many minutes (0 = run until the queue drains)")
    parser.add_argument("--max-simulations", type=int, help="stop after this many completed simulations")
    parser.add_argument("--once", action="store_true", help="single fill+poll pass (cron/worker style)")
    parser.add_argument("--no-halving", action="store_true",
                        help="disable the successive-halving variant gate")
    parser.add_argument("--early-attempts", type=int, default=successive_halving.DEFAULT_EARLY_ATTEMPTS,
                        help="simulations a structure may spend before its variants wait")
    parser.add_argument("--variant-horizon", type=float, default=successive_halving.DEFAULT_HORIZON_MINUTES,
                        help="minutes a deferred variant waits before automatic release")
    parser.add_argument("--no-staged", action="store_true",
                        help="disable the volume-gated staged-search funnel")
    parser.add_argument("--staged-threshold", type=int, default=staged_search.DEFAULT_VOLUME_THRESHOLD,
                        help="queued candidates required before staged expansion engages")
    parser.add_argument("--staged-max-variants", type=int, default=staged_search.DEFAULT_MAX_VARIANTS,
                        help="variants a proven structure may run")
    parser.add_argument("--dry-run", action="store_true", help="rank the queue and print it; never call BRAIN")
    parser.add_argument("--json", action="store_true", help="print the run summary as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        if args.dry_run:
            return dry_run(db)
        client = brain_api.BrainClient()
        scheduler = SimulationScheduler(
            db,
            client,
            worker_id=args.worker_id,
            slots=args.slots,
            lease_seconds=args.lease_seconds,
            sim_timeout=args.sim_timeout,
            max_runtime=None if not args.max_runtime else args.max_runtime * 60.0,
            max_simulations=args.max_simulations,
            halving=not args.no_halving,
            early_attempts=args.early_attempts,
            variant_horizon_minutes=args.variant_horizon,
            staged_search=not args.no_staged,
            staged_volume_threshold=args.staged_threshold,
            staged_max_variants=args.staged_max_variants,
        )
        try:
            code = scheduler.run(once=args.once)
        finally:
            client.close()
        summary = scheduler.summary()
        print(json.dumps(summary, indent=2, sort_keys=True) if args.json else
              f"slots={summary['slots']} submitted={summary['submitted']} completed={summary['completed']} "
              f"failed={summary['failed']} rate_limited={summary['rate_limited']} "
              f"queued_remaining={summary['queued_remaining']} runtime={summary['runtime_seconds']}s")
        return code


if __name__ == "__main__":
    sys.exit(main())
