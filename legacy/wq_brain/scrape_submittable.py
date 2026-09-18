"""Scrape your own alphas on BRAIN and filter to submission-ready ones.

Modernized replacement for the old WQ-Brain `scrape_alphas.py`.

What it does:
    - Paginates GET /users/self/alphas (status=UNSUBMITTED, stage=IS).
    - Keeps alphas where every IS check PASSED (the platform's own submit gate).
    - Records the SELF_CORRELATION value and per-alpha metrics.
    - Writes a CSV you can feed to submit_from_csv.py, sorted by sharpe desc.

Run:
    ./.venv/bin/python legacy/wq_brain/scrape_submittable.py [--min-sharpe 1.3] [--limit N]

Output: data/scrape_<timestamp>.csv with columns
    sharpe, fitness, turnover, self_corr, passed, delay, region, neutralization,
    decay, truncation, universe, link, code
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from wq_session import API_BASE, api_get, get_session

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

SCRAPE_COLUMNS = [
    "sharpe", "fitness", "turnover", "self_corr", "passed",
    "delay", "region", "neutralization", "decay", "truncation", "universe",
    "link", "code",
]


def setup_logging(log_path: Path) -> None:
    for handler in logging.root.handlers:
        logging.root.removeHandler(handler)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_path)],
    )


def fetch_all_unsubmitted(session, limit: int = 100) -> list[dict]:
    """Page through /users/self/alphas for UNSUBMITTED, IS-stage alphas."""
    results: list[dict] = []
    offset = 0
    while True:
        resp = api_get(
            session,
            f"{API_BASE}/users/self/alphas",
            params={
                "limit": limit,
                "offset": offset,
                "stage": "IS",
                "status": "UNSUBMITTED",
                "order": "-dateCreated",
            },
        )
        data = resp.json()
        batch = data.get("results", [])
        if not batch:
            break
        results.extend(batch)
        if len(batch) < limit:
            break
        count = data.get("count")
        if isinstance(count, int) and len(results) >= count:
            break
        offset += limit
    return results


def fetch_checks(session, aid: str, attempts: int = 20) -> list | None:
    """GET /alphas/{id}/check, retrying while BRAIN computes (empty or non-JSON body)."""
    for _ in range(attempts):
        try:
            resp = api_get(session, f"{API_BASE}/alphas/{aid}/check")
        except RuntimeError as exc:
            logging.warning(f"{aid}: check fetch failed: {exc}")
            return None
        if resp.content:
            try:
                return resp.json().get("is", {}).get("checks", [])
            except ValueError:
                pass  # body not ready yet
        time.sleep(2.5)
    logging.warning(f"{aid}: /check gave no result after {attempts} attempts")
    return None


def scrape_one(session, alpha: dict) -> dict | None:
    """Return a CSV row for a submission-ready alpha, or None if it should be skipped."""
    aid = alpha.get("id", "")
    checks = fetch_checks(session, aid)
    if not checks or not all(c.get("result") == "PASS" for c in checks):
        return None

    is_ = alpha.get("is", {}) or {}
    settings = alpha.get("settings", {}) or {}
    self_corr = next((c.get("value") for c in checks if c.get("name") == "SELF_CORRELATION"), "")
    code = (alpha.get("regular") or {}).get("code", "")
    sharpe = is_.get("sharpe", 0)

    return {
        "sharpe": sharpe,
        "fitness": is_.get("fitness", ""),
        "turnover": round(100 * is_["turnover"], 2) if isinstance(is_.get("turnover"), (int, float)) else "",
        "self_corr": self_corr,
        "passed": sum(c.get("result") == "PASS" for c in checks),
        "delay": settings.get("delay", ""),
        "region": settings.get("region", ""),
        "neutralization": settings.get("neutralization", ""),
        "decay": settings.get("decay", ""),
        "truncation": settings.get("truncation", ""),
        "universe": settings.get("universe", ""),
        "link": f"https://platform.worldquantbrain.com/alpha/{aid}",
        "code": code,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-sharpe", type=float, default=1.3, help="local Sharpe floor for the final CSV")
    parser.add_argument("--limit", type=int, default=0, help="cap alphas fetched (0 = all)")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"scrape_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    setup_logging(out_path.with_suffix(".log"))
    session = get_session()

    logging.info("Fetching UNSUBMITTED alphas from BRAIN...")
    alphas = fetch_all_unsubmitted(session)
    if args.limit:
        alphas = alphas[: args.limit]
    logging.info(f"Fetched {len(alphas)} UNSUBMITTED alphas; checking IS checks...")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(scrape_one, session, a): a for a in alphas}
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                logging.warning(f"{futures[future].get('id', '?')}: scrape failed: {type(exc).__name__}: {exc}")
                continue
            if row is not None:
                rows.append(row)

    if not rows:
        print("No submission-ready alphas found (all-checks-PASS). Nothing to do.")
        return 0

    rows = [r for r in rows if isinstance(r["sharpe"], (int, float)) and r["sharpe"] >= args.min_sharpe]
    rows.sort(key=lambda r: r["sharpe"], reverse=True)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SCRAPE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} submission-ready alphas to {out_path}")
    print(f"Next: ./.venv/bin/python legacy/wq_brain/submit_from_csv.py {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
