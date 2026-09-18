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

Behavior:
    - One thread per worker (default 3 = BRAIN's concurrent simulation limit).
    - Every completed simulation is appended to data/results_<timestamp>.csv
      immediately, so a Ctrl-C never loses finished work.
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
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from wq_session import (
    API_BASE,
    RateLimitError,
    SessionExpiredError,
    api_get,
    api_post,
    get_session,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
DEFAULT_INPUT = DATA_DIR / "input.csv"

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


def _coerce(value, cast, default, field: str):
    """Coerce one CSV cell; a blank cell means 'use the default'.

    An empty degree/truncation column used to raise ValueError straight out of the
    worker and kill the whole batch, so a bad cell now becomes a clear per-row error.
    """
    if value is None or str(value).strip() == "":
        return default
    try:
        return cast(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}={value!r} is not a valid {cast.__name__}") from exc


def build_settings(sim: dict) -> dict:
    """Build a BRAIN settings payload from one CSV row."""
    return {
        "instrumentType": "EQUITY",
        "region": str(sim.get("region") or "USA").strip().upper(),
        "universe": str(sim.get("universe") or "TOP3000").strip().upper(),
        "delay": _coerce(sim.get("delay"), int, 1, "delay"),
        "decay": _coerce(sim.get("decay"), int, 6, "decay"),
        "neutralization": str(sim.get("neutralization") or "SUBINDUSTRY").strip().upper(),
        "truncation": _coerce(sim.get("truncation"), float, 0.1, "truncation"),
        "pasteurization": "ON",
        "unitHandling": "VERIFY",
        "nanHandling": str(sim.get("nanHandling") or "OFF").strip().upper(),
        "language": "FASTEXPR",
        "visualization": False,
    }


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
        "weight": weight,
        "subsharpe": subsharpe,
        "correlation": corr,
        "link": f"https://platform.worldquantbrain.com/alpha/{alpha_id}",
    }


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

    pending = list(sims)
    if args.skip_done:
        done = load_done_signatures()
        if done:
            before = len(pending)
            pending = [s for s in pending if sim_signature(s, DONE_KEY_FIELDS) not in done]
            print(f"Skipping {before - len(pending)} expression(s) already simulated in a previous results CSV")
    if not pending:
        print("Nothing to do — every expression in this input already has a result.")
        return 0

    processed: list[dict] = []
    failed: dict[str, dict] = {}
    stop = threading.Event()

    with results_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        f.flush()

        def persist(result: dict) -> None:
            key = sim_signature(result)
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
                pending = [{field: row.get(field, "") for field in SIM_FIELDS} for row in retryable]
                time.sleep(args.retry_delay)

    print(f"Done. {len(processed)} results -> {results_path} | {len(failed)} skipped (see log)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
