"""Submit alphas from a scrape CSV, highest Sharpe first, stopping at the first PASS.

Modernized replacement for the old WQ-Brain `submit_alphas.py`.

Fixes vs the original:
    - The old script's CSV fieldnames dropped the 'sharpe' column (typo in
      scrape_alphas.py), so sort order silently degraded to 'after' — the new
      scrape_submittable.py always writes 'sharpe'.
    - Uses the shared session with 429 backoff and typed auth errors instead of
      bare excepts that retried forever.
    - A SELF_CORRELATION result is no longer treated as success on its own: the
      alpha status is re-read afterwards, and a failed status request no longer
      aborts the whole batch.

Run:
    ./.venv/bin/python legacy/wq_brain/submit_from_csv.py data/scrape_YYYYMMDD_HHMMSS.csv

Behavior:
    - Submits in CSV order (sharpe desc by default).
    - After each POST /alphas/{id}/submit, polls the submit endpoint until
      SELF_CORRELATION resolves (PASS keeps going, FAIL moves to the next alpha).
    - Confirms the alpha actually reached ACTIVE (a 201 is not proof of a live alpha).
    - Stops after the first confirmed submission.
    - Writes an account-linked JSON record to ./batch_submit_results.json (git-ignored).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from wq_session import API_BASE, api_get, api_post, get_session

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
REPO_ROOT = SCRIPT_DIR.parents[1]
RESULTS_PATH = REPO_ROOT / "batch_submit_results.json"


def setup_logging(log_path: Path) -> None:
    for handler in logging.root.handlers:
        logging.root.removeHandler(handler)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_path)],
    )


def poll_self_correlation(session, aid: str, attempts: int = 60, interval: float = 5.0) -> bool | None:
    """Poll the submit endpoint until SELF_CORRELATION resolves.

    Returns True on PASS, False on FAIL, None if it never resolved. A single failed
    poll request is retried rather than treated as "unresolved": BRAIN evaluates the
    correlation check server-side for minutes after the submit request.
    """
    for _ in range(attempts):
        time.sleep(interval)
        try:
            resp = api_get(session, f"{API_BASE}/alphas/{aid}/submit")
        except RuntimeError as exc:
            logging.warning(f"{aid}: submit poll failed, still waiting: {exc}")
            continue
        if not resp.content:
            continue
        try:
            checks = (resp.json().get("is") or {}).get("checks", [])
        except ValueError:
            continue
        sc = next((c for c in checks if c.get("name") == "SELF_CORRELATION"), None)
        if sc is None:
            continue
        logging.info(f"{aid}: SELF_CORRELATION -> {sc}")
        return sc.get("result") == "PASS"
    logging.warning(f"{aid}: SELF_CORRELATION did not resolve in time — moving on")
    return None


def confirm_active(session, aid: str, attempts: int = 12, interval: float = 5.0) -> str | None:
    """Re-read the alpha until it reports ACTIVE; return the last status seen."""
    last_status: str | None = None
    for _ in range(attempts):
        try:
            alpha = api_get(session, f"{API_BASE}/alphas/{aid}").json()
        except RuntimeError as exc:
            logging.warning(f"{aid}: status check failed: {exc}")
            return last_status
        last_status = alpha.get("status")
        if last_status == "ACTIVE":
            return "ACTIVE"
        time.sleep(interval)
    return last_status


def submit_one(session, aid: str) -> dict:
    """Attempt submission and report the outcome as a record."""
    try:
        api_post(session, f"{API_BASE}/alphas/{aid}/submit")
    except RuntimeError as exc:
        detail = str(exc)
        if "404" in detail:
            # 404-style responses usually mean "already submitted" — treat as skip.
            logging.info(f"{aid}: already submitted (404) — skipping")
            return {"alpha_id": aid, "outcome": "skipped", "detail": "already submitted (404)"}
        if "403" in detail or "409" in detail:
            # 403/409 means a previous submit request for this alpha is still being
            # evaluated (SELF_CORRELATION can stay PENDING for minutes). Keep waiting
            # instead of discarding an alpha that may well become ACTIVE.
            logging.info(f"{aid}: submit request already in progress — waiting for the checks")
        else:
            logging.warning(f"{aid}: submit failed: {detail}")
            return {"alpha_id": aid, "outcome": "error", "detail": detail[:200]}
    else:
        logging.info(
            f"Submitted request for https://platform.worldquantbrain.com/alpha/{aid}; "
            "polling SELF_CORRELATION..."
        )

    outcome = poll_self_correlation(session, aid)
    if outcome is False:
        return {"alpha_id": aid, "outcome": "correlation_fail", "detail": "SELF_CORRELATION FAIL"}

    status = confirm_active(session, aid)
    if status == "ACTIVE":
        return {"alpha_id": aid, "outcome": "submitted", "status": status}
    if outcome is None:
        # Still pending server-side: rerun later, the alpha is already queued.
        return {
            "alpha_id": aid,
            "outcome": "unresolved",
            "status": status,
            "detail": "SELF_CORRELATION still pending; rerun to confirm",
        }
    # SELF_CORRELATION PASSed but the alpha is not live yet (review, or a duplicate
    # of an existing signal). Report it honestly instead of claiming success.
    logging.warning(f"{aid}: SELF_CORRELATION PASS but status={status} — not confirmed ACTIVE")
    return {"alpha_id": aid, "outcome": "not_active", "status": status}


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

    submitted = 0
    results: list[dict] = []
    for row in rows:
        aid = str(row.get("link", "")).rstrip("/").split("/")[-1]
        if not aid:
            logging.warning(f"Row without link, skipping: {row.get('code', '')[:50]}")
            continue
        record = submit_one(session, aid)
        results.append(record)
        if record["outcome"] == "submitted":
            submitted += 1
            logging.info(f"SUCCESS: {aid} is ACTIVE.")
            if not args.max_submits or submitted >= args.max_submits:
                break
        elif record["outcome"] in ("skipped", "error"):
            continue
        # correlation_fail / unresolved / not_active -> try the next alpha

    RESULTS_PATH.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source_csv": str(csv_path),
                "confirmed_active": submitted,
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Done. {submitted} confirmed ACTIVE submission(s).")
    print(f"Record written to {RESULTS_PATH} (git-ignored — do not publish).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
