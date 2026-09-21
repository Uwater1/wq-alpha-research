"""WQ Alpha Research Skill self-evolution helper.

Self-contained: only needs `requests`, `numpy`, and credentials supplied via
environment variables or a local untracked `credential.txt` file.

Usage:
    cd <skill-dir>
    pyenv exec python scripts/evolve_skill.py
    pyenv exec python scripts/evolve_skill.py --apply

Behavior:
    - First run (empty alpha_db.json): bulk snapshot.
    - Subsequent runs: incremental entries for new/changed alphas.
    - Without --apply: prints proposed markdown snippet, modifies nothing.
    - With --apply: appends snippet to SKILL.md Section 12 and saves alpha_db.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ALPHA_DB_PATH = SKILL_DIR / "alpha_db.json"
SKILL_PATH = SKILL_DIR / "SKILL.md"
CREDENTIAL_PATH = SKILL_DIR / "credential.txt"

API_BASE = "https://api.worldquantbrain.com"

HEADERS = {
    "Accept": "application/json;version=2.0",
    "Content-Type": "application/json",
}

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Shared pure PnL/correlation logic. The reusable pieces live in
# scripts/correlation.py; this module keeps its session-based fetchers and re-exports
# these names so the legacy reporting path and its tests keep working unchanged.
from correlation import (  # noqa: E402
    aligned_daily_returns,
    daily_returns,
    pnl_from_payload,
    recordset_columns as _recordset_columns,
    safe_corrcoef as _safe_corrcoef,
)


def _warn(message: str) -> None:
    """Surface a degraded-but-recoverable condition without aborting the run."""
    print(f"[evolve_skill] WARNING: {message}", file=sys.stderr, flush=True)


def _as_dict(value: Any) -> dict:
    """BRAIN returns null for `is`/`regular`/`settings` on some alphas; never trust the type."""
    return value if isinstance(value, dict) else {}


def alpha_code(alpha: dict) -> str:
    """Extract the FASTEXPR code, tolerating a null or plain-string `regular` field."""
    regular = alpha.get("regular")
    if regular is None:
        regular = alpha.get("expression")
    if isinstance(regular, dict):
        return regular.get("code") or ""
    return str(regular or "")


def fmt_num(value: Any, spec: str = ".2f", default: str = "n/a") -> str:
    """Format a metric that may be missing or null (alphas without IS stats exist)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value:  # NaN
        return default
    return format(value, spec)


def redact_alpha_id(alpha_id: Any) -> str:
    """Stable pseudonym: the same alpha always maps to the same token without leaking the real ID."""
    return "alpha-" + hashlib.sha256(str(alpha_id).encode("utf-8")).hexdigest()[:8]


def display_id(alpha_id: Any, sanitize: bool) -> str:
    return redact_alpha_id(alpha_id) if sanitize else str(alpha_id)


def expression_shape(expression: str) -> str:
    """Operator skeleton of an expression: keeps the reusable lesson, drops the exact formula."""
    ops: list[str] = []
    for op in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression or ""):
        if op.lower() not in ops:
            ops.append(op.lower())
    return "+".join(ops) if ops else "raw-field"


def load_credentials() -> tuple[str, str]:
    """Load BRAIN credentials without relying on committed secrets."""
    env_user = os.getenv("WQ_BRAIN_USERNAME")
    env_password = os.getenv("WQ_BRAIN_PASSWORD")
    if env_user and env_password:
        return env_user, env_password

    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))

    try:
        from credential_crypto import load_credentials_from_disk
        return load_credentials_from_disk(SKILL_DIR)
    except FileNotFoundError:
        raise FileNotFoundError(
            "BRAIN credentials not found. Set WQ_BRAIN_USERNAME/WQ_BRAIN_PASSWORD "
            'or create an untracked credential.txt (secured with credential.key).'
        )


def create_session() -> requests.Session:
    username, password = load_credentials()
    session = requests.Session()
    session.auth = HTTPBasicAuth(username, password)
    session.headers.update(HEADERS)

    try:
        resp = session.post(f"{API_BASE}/authentication", timeout=(10, 60))
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise RuntimeError(f"BRAIN auth request failed: {exc}") from exc
    if resp.status_code != 201:
        raise RuntimeError(f"BRAIN auth failed: {resp.status_code} {resp.text[:300]}")
    return session


def get_with_retry(session: requests.Session, url: str, retries: int = 3, **kwargs) -> requests.Response:
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=(10, 60), **kwargs)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 5))
                time.sleep(retry_after)
                continue
            return resp
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {retries} retries")


def fetch_pnl_series(session: requests.Session, alpha_id: str) -> tuple[list[str], list[float]]:
    """Fetch a cumulative PnL recordset as (dates, values), oldest record first.

    The date stamps matter: BRAIN serves a different history length per alpha, so
    correlating positionally can compare two different periods.

    Returns ([], []) when the recordset is genuinely empty, but otherwise says *why*
    on stderr: a silent empty return here used to disable the correlation check with
    nothing to show for it. Parsing is shared with scripts/correlation.py.
    """
    try:
        resp = get_with_retry(
            session,
            f"{API_BASE}/alphas/{alpha_id}/recordsets/pnl",
        )
    except Exception as exc:
        _warn(f"{alpha_id}: PnL recordset request failed: {exc}")
        return [], []
    if resp.status_code != 200:
        _warn(f"{alpha_id}: PnL recordset -> HTTP {resp.status_code}")
        return [], []
    if not resp.text.strip():
        return [], []  # BRAIN has not finished computing it yet
    try:
        data = resp.json()
    except ValueError as exc:
        _warn(f"{alpha_id}: PnL recordset was not JSON ({exc})")
        return [], []
    return pnl_from_payload(data)


def fetch_pnl(session: requests.Session, alpha_id: str) -> list[float]:
    """Cumulative PnL values only (for callers that do not need the dates)."""
    return fetch_pnl_series(session, alpha_id)[1]


def fetch_user_alphas(session: requests.Session, limit: int = 100) -> list[dict]:
    """Fetch all user alphas with pagination."""
    all_alphas: list[dict] = []
    offset = 0
    while True:
        resp = get_with_retry(
            session,
            f"{API_BASE}/users/self/alphas",
            params={"limit": limit, "offset": offset},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch alphas: {resp.status_code} {resp.text}")
        data = resp.json()
        batch = data.get("results", data.get("alphas", []))
        if not batch:
            break
        all_alphas.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.2)
    return all_alphas


def fetch_pnl_with_empty_retry(
    session: requests.Session,
    alpha_id: str,
    *,
    tries: int = 2,
    delay: float = 1.0,
) -> tuple[list[str], list[float]]:
    """Retry only empty PnL responses; successful requests pay no extra delay."""
    for attempt in range(max(1, int(tries))):
        dates, values = fetch_pnl_series(session, alpha_id)
        if values:
            return dates, values
        if attempt + 1 < max(1, int(tries)):
            time.sleep(delay)
    return [], []


def fetch_pnl_batch(
    session: requests.Session,
    alpha_ids: list[str],
    *,
    fetcher: Any = fetch_pnl_series,
    workers: int = 1,
) -> list[tuple[str, tuple[list[str], list[float]]]]:
    """Fetch independent PnL recordsets concurrently while preserving input order.

    This is deliberately a bounded thread pool: the work is HTTP I/O, not CPU, and BRAIN
    still controls simulation concurrency separately. ``workers=1`` retains the legacy
    sequential behavior for conservative environments or rate-limit troubleshooting.
    """
    ids = [str(alpha_id) for alpha_id in alpha_ids]
    if not ids:
        return []
    if int(workers) <= 1:
        return [(alpha_id, fetcher(session, alpha_id)) for alpha_id in ids]
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 8))) as executor:
        futures = [executor.submit(fetcher, session, alpha_id) for alpha_id in ids]
        return [(alpha_id, future.result()) for alpha_id, future in zip(ids, futures)]


def fetch_pnl_for_new_alpha(session: requests.Session, alpha_id: str, tries: int = 3,
                            delay: float = 5.0) -> tuple[list[str], list[float]]:
    """Fetch a fresh alpha's PnL, waiting out the window where BRAIN still computes it.

    A brand-new simulation returns an empty recordset for a while; without this retry the
    correlation check silently degraded to "no ACTIVE alpha available for comparison".
    """
    for attempt in range(tries):
        dates, pnl = fetch_pnl_series(session, alpha_id)
        if pnl:
            return dates, pnl
        if attempt < tries - 1:
            time.sleep(delay)
    _warn(f"{alpha_id}: PnL still empty after {tries} attempts; correlation left unchecked")
    return [], []


def load_alpha_db() -> dict[str, Any]:
    if ALPHA_DB_PATH.exists():
        return json.loads(ALPHA_DB_PATH.read_text(encoding="utf-8"))
    return {"alphas": {}, "last_update": None, "version": 1}


def save_alpha_db(db: dict[str, Any]) -> None:
    ALPHA_DB_PATH.write_text(json.dumps(db, indent=2, default=str), encoding="utf-8")


def compute_alpha_fingerprint(alpha: dict) -> dict[str, Any]:
    """Stable snapshot of an alpha that we can compare across runs."""
    is_ = _as_dict(alpha.get("is"))
    expr = alpha_code(alpha)
    settings = _as_dict(alpha.get("settings"))
    return {
        "status": alpha.get("status"),
        "expression": expr,
        "settings": settings,
        "sharpe": is_.get("sharpe"),
        "fitness": is_.get("fitness"),
        "returns": is_.get("returns"),
        "turnover": is_.get("turnover"),
        "drawdown": is_.get("drawdown"),
        "margin": is_.get("margin"),
        "long_count": is_.get("longCount"),
        "short_count": is_.get("shortCount"),
    }


def classify_alpha(expr: str) -> str:
    """Rough family classification based on expression tokens.

    Whitespace is stripped first: expressions are often written with spaces around
    operators (`operating_income / equity`), and a literal substring match silently
    filed those as "other".
    """
    compact = re.sub(r"\s+", "", str(expr or "").lower())
    tokens = []
    if any(f in compact for f in ["operating_income/equity", "oi/equity", "operating_income/sales"]):
        tokens.append("profitability")
    if any(f in compact for f in ["est_eps", "est_fcf", "est_revenue", "est_ebitda", "est_ptp"]):
        tokens.append("analyst")
    if any(f in compact for f in ["free_cash_flow", "cashflow_op", "cash_flow"]):
        tokens.append("cashflow")
    if any(f in compact for f in ["close/open", "open/close", "vwap", "returns", "volume", "high+low"]):
        tokens.append("technical")
    if any(f in compact for f in ["scl12_buzz", "scl12_sentiment", "sentiment"]):
        tokens.append("sentiment")
    if any(f in compact for f in ["equity/assets", "liabilities/assets", "sales/assets"]):
        tokens.append("quality/leverage")
    return "+".join(tokens) if tokens else "other"


def correlation_with_existing(
    new_dates: list[str], new_pnl: list[float], db: dict[str, Any], min_records: int = 50
) -> list[dict[str, Any]]:
    """Compute daily-return correlation of a new alpha against all ACTIVE alphas in DB."""
    results: list[dict[str, Any]] = []
    for old_id, old in _as_dict(db.get("alphas")).items():
        old = _as_dict(old)
        if old.get("status") != "ACTIVE" or not old.get("pnl"):
            continue
        new_ret, old_ret = aligned_daily_returns(
            new_dates, new_pnl, old.get("pnl_dates") or [], old["pnl"]
        )
        if len(new_ret) < min_records or len(new_ret) != len(old_ret):
            continue
        corr = _safe_corrcoef(new_ret, old_ret)
        if corr is None:
            continue
        results.append({"alpha_id": old_id, "corr": corr, "sharpe": old.get("sharpe"), "fitness": old.get("fitness")})
        # A perfect match is already the maximum possible absolute correlation.
        if abs(corr) >= 1.0:
            break
    results.sort(key=lambda x: abs(x["corr"]), reverse=True)
    return results


def generate_lesson(
    fp: dict[str, Any], top_corr: list[dict[str, Any]], sanitize: bool = True, pnl_available: bool = True
) -> str:
    """Generate a one-line lesson from this alpha."""
    if fp["fitness"] is None:
        metric_note = "simulation failed or data is missing"
    elif fp["fitness"] >= 1.5 and fp["turnover"] is not None and fp["turnover"] <= 0.15:
        metric_note = "high Fitness and low turnover, a strong candidate"
    elif fp["fitness"] >= 1.1 and fp["turnover"] is not None and fp["turnover"] <= 0.20:
        metric_note = "meets the basic submission threshold"
    elif fp["turnover"] is not None and fp["turnover"] > 0.35:
        metric_note = "turnover is high; increase decay or blend in more stable signals"
    else:
        metric_note = "metrics are average and need more work"

    if not pnl_available:
        # Do not blame the book when our own PnL fetch came back empty: the correlation
        # was simply never computed, and a lesson must not claim otherwise.
        corr_note = "PnL series unavailable, correlation not checked"
    elif not top_corr:
        corr_note = "no ACTIVE alpha available for comparison"
    elif abs(top_corr[0]["corr"]) >= 0.7:
        other = display_id(top_corr[0]["alpha_id"], sanitize)
        corr_note = f"highly correlated with {other} ({top_corr[0]['corr']:.2f}); switch signal clusters"
    elif abs(top_corr[0]["corr"]) >= 0.5:
        other = display_id(top_corr[0]["alpha_id"], sanitize)
        corr_note = f"moderately correlated with {other} ({top_corr[0]['corr']:.2f}); submit carefully"
    else:
        corr_note = f"low correlation with existing ACTIVE alphas ({top_corr[0]['corr']:.2f}); good diversification value"

    return f"{metric_note}；{corr_note}"


def truncate_expr(expr: str, max_len: int = 120) -> str:
    expr = expr or ""
    lines = expr.strip().splitlines()
    first = lines[0].strip() if lines else ""
    if len(first) > max_len:
        first = first[: max_len - 3] + "..."
    return first


def _metric(alpha: dict, key: str) -> float | None:
    """Numeric IS metric of an alpha, or None when absent (never raises)."""
    value = _as_dict(alpha.get("is")).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def build_bulk_summary(
    alphas: list[dict],
    active_correlations: dict[str, list[dict[str, Any]]],
    sanitize: bool = True,
) -> str:
    """Build a compact summary for the first (bulk) run."""
    from collections import Counter

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = len(alphas)
    active = [a for a in alphas if a.get("status") == "ACTIVE"]
    unsubmitted = [a for a in alphas if a.get("status") != "ACTIVE"]
    families = Counter(classify_alpha(alpha_code(a)) for a in alphas)

    top_active = sorted(active, key=lambda a: (_metric(a, "fitness") or 0), reverse=True)[:5]
    failures = [a for a in alphas if _metric(a, "fitness") is not None and _metric(a, "fitness") < 0.5]
    high_to = [a for a in alphas if _metric(a, "turnover") is not None and _metric(a, "turnover") > 0.50]

    lines = [
        f"\n### {now} — Bulk Initialization Snapshot\n",
        f"- Total alphas: {total} | ACTIVE: {len(active)} | non-ACTIVE: {len(unsubmitted)}",
        f"- Signal-cluster distribution: {dict(families.most_common(8))}",
        "",
        "**Top 5 ACTIVE alphas by Fitness**:",
    ]
    for a in top_active:
        code = alpha_code(a)
        expr = expression_shape(code) if sanitize else truncate_expr(code)
        lines.append(
            f"- `{display_id(a.get('id', '?'), sanitize)}` ({classify_alpha(code)}): "
            f"Sharpe={fmt_num(_metric(a, 'sharpe'))}, Fitness={fmt_num(_metric(a, 'fitness'))}, "
            f"TO={fmt_num(_metric(a, 'turnover'), '.3f')} — `{expr}`"
        )

    if active_correlations:
        high_corr_pairs = []
        ids = sorted(active_correlations.keys())
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                corr = next((c["corr"] for c in active_correlations[a] if c["alpha_id"] == b), None)
                if corr is None:
                    corr = next((c["corr"] for c in active_correlations[b] if c["alpha_id"] == a), 0.0)
                if abs(corr) >= 0.7:
                    high_corr_pairs.append((a, b, corr))
        if high_corr_pairs:
            lines.extend(["", "**High-correlation ACTIVE daily-return pairs (>= 0.7)**:"])
            for a, b, c in high_corr_pairs[:10]:
                lines.append(f"- `{display_id(a, sanitize)}` vs `{display_id(b, sanitize)}`: {c:.3f}")
        else:
            lines.extend(["", "**High-correlation ACTIVE daily-return pairs**: none >= 0.7 (or insufficient PnL)"])

    if failures:
        lines.extend(["", f"**Clear failures (Fitness < 0.5, {len(failures)} total)**:"])
        families_fail = Counter(classify_alpha(alpha_code(a)) for a in failures)
        lines.append(f"- Cluster distribution: {dict(families_fail.most_common(5))}")

    if high_to:
        lines.extend(["", f"**High turnover (TO > 50%, {len(high_to)} total)**:"])
        families_to = Counter(classify_alpha(alpha_code(a)) for a in high_to)
        lines.append(f"- Cluster distribution: {dict(families_to.most_common(5))}")

    lines.append("\n---\n")
    return "\n".join(lines)


def build_incremental_report(entries: list[dict[str, Any]], sanitize: bool = True) -> str:
    """Build per-alpha markdown for incremental updates."""
    if not entries:
        return ""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"\n### {now}\n"]
    for e in entries:
        aid = display_id(e.get("alpha_id", "?"), sanitize)
        if e.get("event") == "status_or_metric_changed":
            lines.append(
                f"- **{aid}** status changed: {e.get('old_status')} -> {e.get('new_status')}; "
                f"Sharpe={fmt_num(e.get('sharpe'))}, Fitness={fmt_num(e.get('fitness'))}, "
                f"TO={fmt_num(e.get('turnover'), '.3f')}. {e.get('lesson', '')}"
            )
            continue
        lines.append(
            f"- **{aid}** ({e.get('status')}, {e.get('family')}): "
            f"Sharpe={fmt_num(e.get('sharpe'))}, Fitness={fmt_num(e.get('fitness'))}, "
            f"TO={fmt_num(e.get('turnover'), '.3f')}, DD={fmt_num(e.get('drawdown'), '.3f')}. {e.get('lesson', '')}"
        )
        if e.get("top_corr"):
            corr_strs = [
                f"{display_id(c.get('alpha_id'), sanitize)}({c.get('corr', 0):+.2f})" for c in e["top_corr"]
            ]
            lines.append(f"  - Correlation: {', '.join(corr_strs)}")
        expr = e.get("expression") or ""
        shape = expression_shape(expr) if sanitize else truncate_expr(expr)
        if shape:
            lines.append(f"  - Expression: `{shape}`")
    lines.append("\n---\n")
    return "\n".join(lines)


def append_to_skill(snippet: str, *, expected_sha: str | None = None) -> dict[str, Any]:
    """Append reviewed sanitized prose through the shared guarded skill manager."""
    if not SKILL_PATH.exists():
        raise FileNotFoundError(f"SKILL.md not found at {SKILL_PATH}")
    from skill_manager import apply_snippet, read_skill
    from research_db import ResearchDB

    _, actual_sha = read_skill(SKILL_PATH)
    with ResearchDB.open() as knowledge_db:
        return apply_snippet(
            knowledge_db, SKILL_PATH, snippet,
            expected_sha=expected_sha or actual_sha,
            actor="evolve_skill",
            privacy_class="SANITIZED",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Evolve WQ Alpha Research SKILL with new empirical data.")
    parser.add_argument("--apply", action="store_true", help="Automatically append the generated snippet to SKILL.md")
    parser.add_argument(
        "--pnl-workers", type=int, default=4,
        help="bounded concurrent PnL fetches for self-evolution (1 disables parallel I/O)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Write real alpha IDs and expressions into SKILL.md (local-only; breaks the sanitized-record policy)",
    )
    args = parser.parse_args()
    sanitize = not args.raw
    if args.raw:
        print("WARNING: --raw writes real alpha IDs/expressions into SKILL.md; never publish that file.", flush=True)

    session = create_session()
    print("auth ok", flush=True)

    db = load_alpha_db()
    known_ids = set(db.get("alphas", {}).keys())
    is_first_run = len(known_ids) == 0

    all_alphas = fetch_user_alphas(session)
    print(f"fetched {len(all_alphas)} alphas, known={len(known_ids)}", flush=True)

    new_alphas: list[dict] = []
    changed_alphas: list[tuple[dict, dict]] = []

    for alpha in all_alphas:
        aid = alpha.get("id")
        if not aid:
            continue
        fp = compute_alpha_fingerprint(alpha)

        if aid not in known_ids:
            new_alphas.append(alpha)
        else:
            old = db["alphas"][aid]
            if old.get("status") != fp["status"] or old.get("sharpe") != fp["sharpe"]:
                changed_alphas.append((old, {**fp, "alpha_id": aid}))

    # ------------------------------------------------------------------
    # Preview mode: compute snippet without mutating DB
    # ------------------------------------------------------------------
    if is_first_run:
        print("first run: building bulk snapshot...", flush=True)
        active_alphas = [a for a in all_alphas if a.get("status") == "ACTIVE"]
        active_correlations: dict[str, list[dict[str, Any]]] = {}

        preview_db = {"alphas": {}}
        alpha_rows = [(alpha, compute_alpha_fingerprint(alpha)) for alpha in all_alphas if alpha.get("id")]
        fetched = fetch_pnl_batch(
            session, [alpha["id"] for alpha, _ in alpha_rows],
            fetcher=fetch_pnl_with_empty_retry, workers=args.pnl_workers,
        )
        for idx, ((aid, (dates, pnl)), (alpha, fp)) in enumerate(zip(fetched, alpha_rows), start=1):
            preview_db["alphas"][aid] = {**fp, "pnl": pnl, "pnl_dates": dates}
            if idx % 10 == 0 or idx == 1:
                print(f"  fetched {idx}/{len(alpha_rows)} PnLs", flush=True)
            if args.pnl_workers <= 1:
                time.sleep(0.3)

        active_ids = [a.get("id") for a in active_alphas if a.get("id")]
        for aid in active_ids:
            entry = _as_dict(preview_db["alphas"].get(aid))
            active_correlations[aid] = correlation_with_existing(
                entry.get("pnl_dates", []), entry.get("pnl", []), preview_db
            )

        snippet = build_bulk_summary(all_alphas, active_correlations, sanitize=sanitize)

        if args.apply:
            db["alphas"] = preview_db["alphas"]
    else:
        # ------------------------------------------------------------------
        # Incremental run: per-alpha entries for new/changed only
        # ------------------------------------------------------------------
        print(f"incremental: {len(new_alphas)} new, {len(changed_alphas)} changed", flush=True)
        entries: list[dict[str, Any]] = []

        fetched = fetch_pnl_batch(
            session, [alpha.get("id") for alpha in new_alphas if alpha.get("id")],
            fetcher=fetch_pnl_for_new_alpha, workers=args.pnl_workers,
        )
        for alpha, (aid, (dates, pnl)) in zip(new_alphas, fetched):
            fp = compute_alpha_fingerprint(alpha)
            top_corr = correlation_with_existing(dates, pnl, db)
            lesson = generate_lesson(fp, top_corr, sanitize=sanitize, pnl_available=bool(pnl))
            entries.append(
                {
                    "alpha_id": aid,
                    "status": fp["status"],
                    "family": classify_alpha(fp["expression"]),
                    "sharpe": fp["sharpe"],
                    "fitness": fp["fitness"],
                    "turnover": fp["turnover"],
                    "drawdown": fp["drawdown"],
                    "expression": fp["expression"],
                    "top_corr": top_corr[:3],
                    "lesson": lesson,
                }
            )
            if args.apply:
                db["alphas"][aid] = {**fp, "pnl": pnl, "pnl_dates": dates}
            print(f"  new: {aid} | sharpe={fp['sharpe']} | fitness={fp['fitness']} | to={fp['turnover']}")
            if args.pnl_workers <= 1:
                time.sleep(0.3)

        for old, new in changed_alphas:
            aid = new["alpha_id"]
            previous = _as_dict(_as_dict(db.get("alphas")).get(aid))
            if "pnl" in previous:
                new["pnl"] = previous["pnl"]
                new["pnl_dates"] = previous.get("pnl_dates", [])
            entries.append(
                {
                    "alpha_id": aid,
                    "event": "status_or_metric_changed",
                    "old_status": old.get("status"),
                    "new_status": new["status"],
                    "sharpe": new["sharpe"],
                    "fitness": new["fitness"],
                    "turnover": new["turnover"],
                    "lesson": f"status changed from {old.get('status')} to {new['status']}",
                }
            )
            if args.apply:
                db["alphas"][aid] = new
            print(f"  changed: {aid} | {old.get('status')} -> {new['status']}")

        snippet = build_incremental_report(entries, sanitize=sanitize)

    if not snippet.strip():
        print("\nNo new empirical findings to record.")
        return 0

    print("\n" + "=" * 60)
    print("PROPOSED SKILL.md APPEND SNIPPET")
    print("=" * 60)
    print(snippet)
    print("=" * 60)

    if args.apply:
        from skill_manager import read_skill

        _, expected_sha = read_skill(SKILL_PATH)
        mutation = append_to_skill(snippet, expected_sha=expected_sha)
        db["last_update"] = datetime.now(timezone.utc).isoformat()
        save_alpha_db(db)
        print(f"\nAppended to {SKILL_PATH} via skill manager (mutation {mutation['mutation_id']}, version {mutation['version']})")
        print(f"alpha_db.json updated: {len(db['alphas'])} alphas tracked.")
    else:
        print("\nDry-run: SKILL.md and alpha_db.json were NOT modified. Use --apply to commit.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
