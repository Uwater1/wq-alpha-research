"""Batch-simulate alpha expressions from a CSV against WorldQuant BRAIN.

Modernized replacement for the old WQ-Brain `main.py` batch loop.

Input CSV (default data/input.csv; see data/input.example.csv):
    code,neutralization,decay,truncation,delay,universe,region
    "rank(ts_mean(close, 20))",SUBINDUSTRY,10,0.1,1,TOP3000,USA
    ...

Run:
    ./.venv/bin/python legacy/wq_brain/batch_simulate.py [input.csv] [--workers N]

Behavior:
    - One thread per worker (default 3 = BRAIN's concurrent simulation limit).
    - Every completed simulation is appended to data/results_<timestamp>.csv
      immediately, so a Ctrl-C never loses finished work.
    - Rows that fail to start are logged (with the reason) and skipped; rerun the
      same input CSV to retry them.

Output CSV columns:
    passed, sharpe, fitness, turnover, weight, subsharpe, correlation,
    delay, region, neutralization, decay, truncation, universe, link, code
"""
from __future__ import annotations

import argparse
import csv
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


def setup_logging(csv_path: Path) -> None:
    log_path = csv_path.with_suffix(".log")
    for handler in logging.root.handlers:
        logging.root.removeHandler(handler)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_path)],
    )


def run_simulation(session, sim: dict) -> dict:
    """Simulate one expression and return a metrics dict (or an error entry)."""
    code = str(sim["code"]).strip()
    settings = {
        "instrumentType": "EQUITY",
        "region": sim.get("region", "USA"),
        "universe": sim.get("universe", "TOP3000"),
        "delay": int(sim.get("delay", 1)),
        "decay": int(sim.get("decay", 6)),
        "neutralization": str(sim.get("neutralization", "SUBINDUSTRY")).upper(),
        "truncation": float(sim.get("truncation", 0.1)),
        "pasteurization": "ON",
        "unitHandling": "VERIFY",
        "nanHandling": str(sim.get("nanHandling", "OFF")).upper(),
        "language": "FASTEXPR",
        "visualization": False,
    }

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

    # Poll until the simulation completes.
    while True:
        data = api_get(session, sim_url).json()
        if "alpha" in data:
            alpha_id = data["alpha"]
            break
        if data.get("status") in ("ERROR", "FAILED"):
            return {**sim, "error": f"simulation_error: {data.get('message', 'unknown')}"}
        progress = data.get("progress")
        if isinstance(progress, (int, float)):
            logging.info(f"[{threading.current_thread().name}] {code[:60]!r} -> {int(100 * progress)}%")
        time.sleep(10)

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

    processed: list[dict] = []
    failed: list[dict] = []
    stop = threading.Event()

    with results_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        f.flush()

        def persist(result: dict) -> None:
            if "error" in result:
                failed.append(result)
                logging.warning(f"SKIPPED {result['code'][:60]!r}: {result['error']}")
            else:
                writer.writerow(result)
                f.flush()
                processed.append(result)

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(run_simulation, session, sim) for sim in sims]
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
                print("\nInterrupted — finished results are already saved; rerun to retry the rest.")
            finally:
                if stop.is_set():
                    for fut in futures:
                        fut.cancel()
                drain_finished()

    print(f"Done. {len(processed)} results -> {results_path} | {len(failed)} skipped (see log)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
