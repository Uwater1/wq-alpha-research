"""Batch-simulate alpha expressions from a CSV against WorldQuant BRAIN.

Modernized replacement for the old WQ-Brain `main.py` batch loop.

Input CSV (default data/input.csv; see data/input.example.csv):
    code,neutralization,decay,truncation,delay,universe,region
    "rank(ts_mean(close, 20))",SUBINDUSTRY,10,0.1,1,TOP3000,USA
    ...

Run:
    ./.venv/bin/python legacy/wq_brain/batch_simulate.py [input.csv] [--workers N]
                                               [--max-wait SECONDS] [--skip-done]
                                               [--retries N] [--retry-delay SECONDS]
                                               [--db PATH] [--no-db]

Behavior:
    - One thread per worker (default 3 = BRAIN's concurrent simulation limit).
    - Every completed simulation is appended to data/results_<timestamp>.csv
      immediately, so a Ctrl-C never loses finished work.
    - With the local store (default: <repo root>/research.db, TODO P0/P1) each row is
      normalized and keyed before submission: an exact completed request is served
      from cache and never re-sent to BRAIN, a request another process is already
      running is skipped, and each result is committed to SQLite the moment it lands.
      Use --no-db for the legacy CSV-only behavior.
    - Rows that fail to start or hold an invalid settings cell are logged (with
      the reason) and skipped.
    - Each simulation has a hard wall-clock deadline (--max-wait, default 1800s),
      so a stuck simulation cannot pin a worker forever.

Wall-clock notes:
    A BRAIN simulation takes roughly 1-3 minutes of *server* time and the platform
    only runs 3 concurrently, so a batch of N rows costs about (N/3) * 2 minutes and
    nothing client-side changes that. What this script does control:
      * --skip-done skips expressions that already have a result in data/results_*.csv,
        so re-running a batch (or resuming after an interrupt) never re-simulates
        finished work and never burns quota on it;
      * rate-limited rows are retried after --retry-delay instead of being dropped;
      * polling backs off from 3s to 15s, which keeps completion latency low without
        hammering the API.
    For a long batch, detach it so a closed terminal does not kill the run:
        setsid ./.venv/bin/python legacy/wq_brain/batch_simulate.py input.csv \\
            --workers 3 --skip-done > data/batch.out 2>&1 < /dev/null &
    Each finished result is flushed to the CSV as it lands, so you can just poll the
    file; the alphas themselves also stay on BRAIN as UNSUBMITTED and
    scrape_submittable.py will find them even if the run is killed.

Output CSV columns:
    passed, sharpe, fitness, turnover, weight, subsharpe, correlation,
    delay, region, neutralization, decay, truncation, universe, link, code
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import canonical  # noqa: E402
import research_db  # noqa: E402
from wq_session import (  # noqa: E402
    API_BASE,
    RateLimitError,
    SessionExpiredError,
    api_get,
    api_post,
    get_session,
)

DATA_DIR = SCRIPT_DIR / "data"
DEFAULT_INPUT = DATA_DIR / "input.csv"
DEFAULT_LEASE_SECONDS = 1800.0

RESULT_COLUMNS = [
    "passed", "sharpe", "fitness", "turnover", "weight", "subsharpe",
    "correlation", "delay", "region", "neutralization", "decay", "truncation",
    "universe", "link", "code",
]

POLL_INTERVAL_INITIAL = 3.0
POLL_INTERVAL_MAX = 15.0
DEFAULT_MAX_WAIT_SECONDS = 1800.0
DEFAULT_RETRIES = 1
DEFAULT_RETRY_DELAY = 30.0

# Settings columns shared by the input CSV and the results CSV; a row with the same
# signature has already been simulated, so --skip-done can skip it.
DONE_KEY_FIELDS = ("code", "neutralization", "decay", "truncation", "delay", "universe", "region")
SIM_FIELDS = DONE_KEY_FIELDS + ("nanHandling",)
SIM_SETTINGS_COLUMNS = (
    "instrumentType", "region", "universe", "delay", "decay", "neutralization",
    "truncation", "pasteurization", "unitHandling", "nanHandling", "language",
)


def sim_signature(sim: dict, fields: tuple[str, ...] = SIM_FIELDS) -> str:
    """Stable identity for one expression + settings combination."""
    return json.dumps([str(sim.get(f, "") or "").strip() for f in fields])


def load_done_signatures() -> set[str]:
    """Signatures of every expression that already has a result in data/results_*.csv."""
    done: set[str] = set()
    for path in sorted(DATA_DIR.glob("results_*.csv")):
        try:
            with path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if (row.get("code") or "").strip():
                        done.add(sim_signature(row, DONE_KEY_FIELDS))
        except OSError:
            continue
    return done


def setup_logging(csv_path: Path) -> None:
    log_path = csv_path.with_suffix(".log")
    for handler in logging.root.handlers:
        logging.root.removeHandler(handler)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_path)],
    )


def build_settings(sim: dict) -> dict:
    """Build a BRAIN settings payload from one CSV row.

    Defaults, coercion and canonicalization live in scripts/canonical.py so the
    simulation cache key and the request sent to BRAIN can never drift apart.
    """
    settings = canonical.normalize_settings({field: sim.get(field) for field in SIM_SETTINGS_COLUMNS})
    return {**settings, "visualization": False}


def run_simulation(session, sim: dict, max_wait: float = DEFAULT_MAX_WAIT_SECONDS) -> dict:
    """Simulate one expression and return a metrics dict (or an error entry)."""
    code = str(sim.get("code", "")).strip()
    if not code:
        return {**sim, "error": "empty code"}
    try:
        settings = build_settings(sim)
    except ValueError as exc:
        return {**sim, "error": f"bad_settings: {exc}"}

    try:
        resp = api_post(
            session,
            f"{API_BASE}/simulations",
            json={"type": "REGULAR", "settings": settings, "regular": code},
        )
    except SessionExpiredError:
        raise
    except RateLimitError:
        return {**sim, "error": "rate_limited"}
    except RuntimeError as exc:
        return {**sim, "error": f"post_failed: {exc}"}

    sim_url = resp.headers.get("Location", "").rstrip("/")
    if not sim_url:
        return {**sim, "error": f"no simulation location header: HTTP {resp.status_code}"}

    # Poll until the simulation completes, with a wall-clock deadline so a stuck
    # simulation (or an unexpected response body) cannot pin a worker forever.
    deadline = time.monotonic() + max_wait
    interval = POLL_INTERVAL_INITIAL
    while True:
        try:
            data = api_get(session, sim_url).json()
        except Exception as exc:  # RuntimeError from api_get, JSON errors, ...
            return {**sim, "error": f"poll_failed: {exc}"}
        if "alpha" in data:
            alpha_id = data["alpha"]
            break
        if data.get("status") in ("ERROR", "FAILED"):
            return {**sim, "error": f"simulation_error: {data.get('message', 'unknown')}"}
        progress = data.get("progress")
        if isinstance(progress, (int, float)):
            logging.info(f"[{threading.current_thread().name}] {code[:60]!r} -> {int(100 * progress)}%")
        if time.monotonic() >= deadline:
            status = data.get("status", "unknown")
            return {**sim, "error": f"simulation_timeout after {int(max_wait)}s (status={status})"}
        time.sleep(interval)
        interval = min(interval * 1.5, POLL_INTERVAL_MAX)

    alpha = api_get(session, f"{API_BASE}/alphas/{alpha_id}").json()
    logging.info(f"[{threading.current_thread().name}] alpha done: https://platform.worldquantbrain.com/alpha/{alpha_id}")

    is_ = alpha.get("is", {})
    checks = is_.get("checks", [])
    passed = sum(c.get("result") == "PASS" for c in checks)
    weight = next((c.get("result", "") for c in checks if c.get("name") == "CONCENTRATED_WEIGHT"), "")
    subsharpe = next((c.get("value", "") for c in checks if c.get("name") == "LOW_SUB_UNIVERSE_SHARPE"), "")
    corr = next((c.get("value", "") for c in checks if c.get("name") == "SELF_CORRELATION"), "")

    return {
        **sim,
        "passed": passed,
        "sharpe": is_.get("sharpe", ""),
        "fitness": is_.get("fitness", ""),
        "turnover": round(100 * is_.get("turnover", 0), 2) if isinstance(is_.get("turnover"), (int, float)) else "",
        "turnover_fraction": is_.get("turnover") if isinstance(is_.get("turnover"), (int, float)) else "",
        "drawdown": is_.get("drawdown", ""),
        "weight": weight,
        "subsharpe": subsharpe,
        "correlation": corr,
        "link": f"https://platform.worldquantbrain.com/alpha/{alpha_id}",
        "alpha_id": alpha_id,
        "simulation_id": sim_url.rsplit("/", 1)[-1] or sim_url,
        # Kept out of the results CSV by extrasaction="ignore"; used by research.db.
        "checks": [
            {k: c.get(k) for k in ("name", "result", "value", "limit") if k in c}
            for c in checks
            if isinstance(c, dict)
        ],
    }


def open_store(path: str | None) -> research_db.ResearchDB | None:
    """Open research.db, degrading to CSV-only mode if the store cannot be used."""
    try:
        return research_db.ResearchDB.open(path)
    except (sqlite3.Error, OSError) as exc:
        logging.warning(f"research.db unavailable ({exc}) — continuing without the local store")
        return None


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def cached_result_row(sim: dict, cached: dict) -> dict:
    """Turn a cached research.db result back into a results-CSV row (no BRAIN call)."""
    checks = [c for c in (cached.get("checks") or []) if isinstance(c, dict)]
    turnover = _number(cached.get("turnover"))
    alpha_id = cached.get("brain_alpha_id")
    return {
        **{field: sim.get(field, "") or "" for field in DONE_KEY_FIELDS},
        "passed": sum(1 for c in checks if c.get("result") == "PASS"),
        "sharpe": cached.get("sharpe") if cached.get("sharpe") is not None else "",
        "fitness": cached.get("fitness") if cached.get("fitness") is not None else "",
        "turnover": round(100 * turnover, 2) if turnover is not None else "",
        "weight": next((c.get("result", "") for c in checks if c.get("name") == "CONCENTRATED_WEIGHT"), ""),
        "subsharpe": next((c.get("value", "") for c in checks if c.get("name") == "LOW_SUB_UNIVERSE_SHARPE"), ""),
        "correlation": next((c.get("value", "") for c in checks if c.get("name") == "SELF_CORRELATION"), ""),
        "link": f"https://platform.worldquantbrain.com/alpha/{alpha_id}" if alpha_id else "",
    }


def record_result_in_store(
    store: research_db.ResearchDB, result: dict, candidate_ids: dict[str, int], retry_delay_seconds: float | None = None
) -> None:
    """Persist one outcome the moment it lands; a store problem must never lose the CSV row."""
    code = str(result.get("code", "")).strip()
    if not code:
        return
    try:
        settings = build_settings(result)
    except ValueError as exc:
        logging.warning(f"not persisting {code[:60]!r}: {exc}")
        return
    key = canonical.canonical_key(code, settings)
    try:
        if "error" in result:
            store.record_simulation_result(
                candidate_id=candidate_ids.get(key), canonical_key=key, status="ERROR",
                error=str(result["error"]), retry_delay_seconds=retry_delay_seconds,
            )
        else:
            store.record_simulation_result(
                candidate_id=candidate_ids.get(key), canonical_key=key, status="DONE",
                metrics={
                    "sharpe": _number(result.get("sharpe")),
                    "fitness": _number(result.get("fitness")),
                    "turnover": _number(result.get("turnover_fraction")),
                    "drawdown": _number(result.get("drawdown")),
                },
                checks=result.get("checks") or [],
                brain_alpha_id=result.get("alpha_id"),
                simulation_id=result.get("simulation_id"),
            )
    except (sqlite3.Error, ValueError, KeyError) as exc:
        logging.warning(f"could not persist {code[:60]!r} into research.db: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", nargs="?", default=str(DEFAULT_INPUT), help="input CSV path")
    parser.add_argument("--workers", type=int, default=3, help="parallel simulations (BRAIN caps at 3)")
    parser.add_argument(
        "--max-wait",
        type=float,
        default=DEFAULT_MAX_WAIT_SECONDS,
        help=f"per-simulation wall-clock limit in seconds (default {int(DEFAULT_MAX_WAIT_SECONDS)})",
    )
    parser.add_argument(
        "--skip-done",
        action="store_true",
        help="skip expressions that already have a result in data/results_*.csv (resume without re-simulating)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"extra passes over rate-limited rows (default {DEFAULT_RETRIES})",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY,
        help=f"seconds to wait before retrying rate-limited rows (default {int(DEFAULT_RETRY_DELAY)})",
    )
    parser.add_argument(
        "--db",
        help=f"local state store (default: ${research_db.DB_ENV_VAR} or <repo root>/research.db)",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="disable the local store/cache and keep the legacy CSV-only behavior",
    )
    parser.add_argument(
        "--lease-seconds",
        type=float,
        default=DEFAULT_LEASE_SECONDS,
        help=f"how long a claimed candidate stays leased to this process (default {int(DEFAULT_LEASE_SECONDS)})",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        parser.error(f"input CSV not found: {input_path}")

    with input_path.open(newline="", encoding="utf-8") as f:
        sims = [row for row in csv.DictReader(f) if row.get("code", "").strip()]
    if not sims:
        print("No rows with a 'code' column found in the input CSV.")
        return 1
    print(f"Loaded {len(sims)} simulations from {input_path}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    results_path = DATA_DIR / f"results_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    setup_logging(results_path)
    session = get_session()

    store = None if args.no_db else open_store(args.db)
    if store is not None:
        try:
            recovered = store.recover_expired_leases(args.lease_seconds)
        except sqlite3.Error as exc:
            logging.warning(f"could not recover leases ({exc})")
        else:
            if recovered["simulations"] or recovered["submissions"]:
                logging.info(f"recovered expired leases: {recovered}")

    # ---- P1: normalize + dedup + cache check before spending BRAIN capacity ----
    invalid: list[str] = []
    pending: list[dict] = []
    candidate_ids: dict[str, int] = {}
    reused: list[tuple[dict, dict]] = []
    skipped = {"in_flight": 0, "skipped_final": 0}

    if store is None:
        pending = list(sims)
    else:
        for sim in sims:
            code = str(sim.get("code", "")).strip()
            try:
                settings = build_settings(sim)
            except ValueError as exc:
                invalid.append(f"{code[:60]!r}: {exc}")
                continue
            try:
                outcome = store.queue_candidate(code, settings, source=input_path.name)
            except (sqlite3.Error, ValueError, KeyError) as exc:
                invalid.append(f"{code[:60]!r}: store rejected the candidate ({exc})")
                continue
            if outcome.action == "cache_hit":
                reused.append((sim, outcome.cached or {}))
            elif outcome.needs_simulation:
                pending.append(sim)
                if outcome.candidate_id is not None:
                    candidate_ids[outcome.canonical_key] = outcome.candidate_id
            else:
                skipped[outcome.action] = skipped.get(outcome.action, 0) + 1

        if reused:
            print(f"Reusing {len(reused)} cached result(s) from {store.path.name} — no BRAIN call")
        if skipped["in_flight"]:
            print(f"Skipping {skipped['in_flight']} candidate(s) already queued/running elsewhere")
        if skipped["skipped_final"]:
            print(f"Skipping {skipped['skipped_final']} candidate(s) that already reached a final state")
    for problem in invalid:
        print(f"SKIPPED {problem}", file=sys.stderr)

    if args.skip_done:
        done = load_done_signatures()
        if done:
            before = len(pending)
            pending = [s for s in pending if sim_signature(s, DONE_KEY_FIELDS) not in done]
            print(f"Skipping {before - len(pending)} expression(s) already simulated in a previous results CSV")

    if not pending:
        if reused:
            with results_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                for sim, cached in reused:
                    writer.writerow(cached_result_row(sim, cached))
                f.flush()
            print(f"Nothing to simulate — {len(reused)} cached result(s) -> {results_path}")
            if store is not None:
                store.close()
            return 0
        print("Nothing to do — every expression in this input already has a result.")
        if store is not None:
            store.close()
        return 0

    processed: list[dict] = []
    failed: dict[str, dict] = {}
    stop = threading.Event()

    with results_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for sim, cached in reused:
            writer.writerow(cached_result_row(sim, cached))
        f.flush()

        def persist(result: dict) -> None:
            key = sim_signature(result)
            if store is not None:
                # Commit to SQLite first: an interrupt anywhere after this point can no
                # longer lose the result or force BRAIN to redo it (TODO P0/P1).
                record_result_in_store(store, result, candidate_ids, args.retry_delay)
            if "error" in result:
                failed[key] = result  # last attempt for this row wins
                logging.warning(f"SKIPPED {result['code'][:60]!r}: {result['error']}")
            else:
                failed.pop(key, None)
                writer.writerow(result)
                f.flush()
                processed.append(result)

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for attempt in range(args.retries + 1):
                if stop.is_set():
                    break
                futures = [executor.submit(run_simulation, session, sim, args.max_wait) for sim in pending]
                persisted: set = set()

                def drain_finished() -> None:
                    """Persist results of any finished futures exactly once."""
                    for future in futures:
                        if future in persisted:
                            continue
                        if future.done() and not future.cancelled():
                            persisted.add(future)
                            try:
                                persist(future.result())
                            except Exception:
                                logging.exception("Unexpected worker failure")

                try:
                    for future in as_completed(futures):
                        try:
                            persist(future.result())
                            persisted.add(future)
                        except SessionExpiredError:
                            logging.error("Credentials rejected by BRAIN — stopping early. Results so far are saved.")
                            stop.set()
                            break
                except KeyboardInterrupt:
                    print("\nInterrupted — finished results are already saved; rerun with --skip-done to retry the rest.")
                    stop.set()
                finally:
                    if stop.is_set():
                        for fut in futures:
                            fut.cancel()
                    drain_finished()

                # BRAIN runs 3 simulations at a time, so applying more than that at
                # once is expected: keep those rows for a later pass instead of losing them.
                retryable = [r for r in failed.values() if r.get("error") == "rate_limited"]
                if not retryable or attempt == args.retries or stop.is_set():
                    break
                logging.info(
                    f"retrying {len(retryable)} rate-limited expression(s) after {int(args.retry_delay)}s"
                )
                # Re-queue only the input fields: a failed result dict also carries the
                # previous attempt's `error` key, which would mark the retry as failed too.
                pending = []
                for row in retryable:
                    pending.append({field: row.get(field, "") for field in SIM_FIELDS})
                    if store is not None:
                        try:
                            outcome = store.queue_candidate(
                                str(row.get("code", "")).strip(),
                                build_settings(row),
                                source=input_path.name,
                                requeue=True,
                            )
                        except (sqlite3.Error, ValueError, KeyError) as exc:
                            logging.warning(f"store could not requeue {str(row.get('code'))[:60]!r}: {exc}")
                        else:
                            if outcome.candidate_id is not None:
                                candidate_ids[outcome.canonical_key] = outcome.candidate_id
                time.sleep(args.retry_delay)

    if store is not None:
        store.close()

    summary = f"Done. {len(processed)} results -> {results_path}"
    if reused:
        summary += f" | {len(reused)} served from cache"
    print(f"{summary} | {len(failed)} skipped (see log)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
