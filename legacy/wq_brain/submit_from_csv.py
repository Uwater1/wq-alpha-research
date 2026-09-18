"""Submit alphas from a scrape CSV, highest Sharpe first, stopping at the first PASS.

Modernized replacement for the old WQ-Brain `submit_alphas.py`.

Fixes vs the original:
    - The old script's CSV fieldnames dropped the 'sharpe' column (typo in
      scrape_alphas.py), so sort order silently degraded to 'after' — the new
      scrape_submittable.py always writes 'sharpe'.
    - Uses the shared session with 429 backoff and typed auth errors instead of
      bare excepts that retried forever.

Run:
    ./.venv/bin/python legacy/wq_brain/submit_from_csv.py data/scrape_YYYYMMDD_HHMMSS.csv

Behavior:
    - Submits in CSV order (sharpe desc by default).
    - After each POST /alphas/{id}/submit, polls the submit endpoint until
      SELF_CORRELATION resolves (PASS keeps going, FAIL moves to the next alpha).
    - Stops after the first alpha whose SELF_CORRELATION PASSES.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

from wq_session import API_BASE, api_get, api_post, get_session

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"


def setup_logging(log_path: Path) -> None:
    for handler in logging.root.handlers:
        logging.root.removeHandler(handler)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_path)],
    )


def submit_one(session, aid: str) -> bool | None:
    """Attempt submission; return True if SELF_CORRELATION PASSes, False if FAIL, None if skipped."""
    try:
        api_post(session, f"{API_BASE}/alphas/{aid}/submit")
    except RuntimeError as exc:
        # 404-style responses usually mean "already submitted" — treat as skip.
        if "404" in str(exc):
            logging.info(f"{aid}: already submitted (404) — skipping")
            return None
        raise
    logging.info(f"Submitted request for https://platform.worldquantbrain.com/alpha/{aid}; polling SELF_CORRELATION...")

    for _ in range(60):
        time.sleep(5)
        resp = api_get(session, f"{API_BASE}/alphas/{aid}/submit")
        if not resp.content:
            continue
        try:
            checks = resp.json().get("is", {}).get("checks", [])
        except ValueError:
            continue
        sc = next((c for c in checks if c.get("name") == "SELF_CORRELATION"), None)
        if sc is None:
            continue
        logging.info(f"{aid}: SELF_CORRELATION -> {sc}")
        return sc.get("result") == "PASS"
    logging.warning(f"{aid}: SELF_CORRELATION did not resolve in time — moving on")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv_path", help="CSV from scrape_submittable.py")
    parser.add_argument("--max-submits", type=int, default=0, help="stop after N successful submissions (0 = first PASS only)")
    args = parser.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        parser.error(f"CSV not found: {csv_path}")

    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("CSV has no rows — run scrape_submittable.py first.")
        return 1

    setup_logging(DATA_DIR / f"submit_{time.strftime('%Y%m%d_%H%M%S')}.log")
    session = get_session()

    successes = 0
    for row in rows:
        aid = str(row.get("link", "")).rstrip("/").split("/")[-1]
        if not aid:
            logging.warning(f"Row without link, skipping: {row.get('code', '')[:50]}")
            continue
        outcome = submit_one(session, aid)
        if outcome is True:
            successes += 1
            logging.info(f"SUCCESS: {aid} submitted and correlation check passed.")
            if not args.max_submits or successes >= args.max_submits:
                break
        elif outcome is None:
            continue
        # outcome False -> SELF_CORRELATION FAIL, try the next alpha
    print(f"Done. {successes} successful submission(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
