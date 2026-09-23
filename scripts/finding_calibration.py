"""Which advisory screening rules actually predict a BRAIN refusal?

The validator's type/usage findings are advisory by default because the reference files
cannot prove what the platform will accept. This module replaces that guess with measured
evidence: every historic candidate that reached BRAIN is re-screened locally, and each
finding code is scored against the *real* outcome —

    accepted            ``simulations.status = 'DONE'``   BRAIN computed the request
    rejected            ``simulations.status = 'ERROR'``  BRAIN refused the request

Only the second is a rejection. An IS-gate failure (LOW_SHARPE, HIGH_TURNOVER, ...) means
BRAIN *accepted* the request and evaluated it, so it is not evidence that a local rule was
right — and a candidate the local validator itself refused was never sent, so it carries no
evidence at all. Both are excluded from the denominator.

Point-in-time safety: an expression and its settings are pre-simulation facts, so
re-screening history today cannot leak an outcome; ``settled_at`` is carried on every
observation so a replay can calibrate using only what had already settled at its own
decision time.

Nothing here changes behaviour on its own. ``refresh`` measures, ``recommended_policy``
turns a measurement into a proposed severity map, and ``approve`` is the explicit act that
makes the pipeline enforce it (``research_db`` reads the approved map for every
``queue_candidate`` call).

Approval needs more than a rejection rate. Enforcing a rule refuses candidates before BRAIN
ever sees them, so refusing work that *would* have passed costs passes, and a rule can be
perfectly accurate while buying nothing. ``approve`` therefore replays each proposed code
against the corpus (``scripts/policy_replay.py``) and keeps only the codes whose own
counterfactual shows the same passes reached for less capacity. Codes that cannot show that
are refused with their reason, and nothing is enforced without an explicit override.

CLI:
    ./.venv/bin/python scripts/finding_calibration.py --refresh
    ./.venv/bin/python scripts/finding_calibration.py --report
    ./.venv/bin/python scripts/finding_calibration.py --approve [--budget 20] [--force]
    ./.venv/bin/python scripts/finding_calibration.py --clear
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical
import research_db
import validate

CALIBRATION_VERSION = "finding-calibration-v1"
#: Samples below which a code is never promoted, however lopsided the ratio looks.
DEFAULT_MIN_SAMPLES = 5
#: Rejection share at/above which a rule is recommended for strict enforcement.
DEFAULT_STRICT_REJECT_RATE = 0.6
#: Rejection share at/below which the rule is explicitly recommended to stay advisory.
DEFAULT_KEEP_REJECT_RATE = 0.1

RECOMMEND_STRICT = "strict"
RECOMMEND_KEEP = "keep_advisory"
RECOMMEND_INSUFFICIENT = "insufficient_evidence"

#: BRAIN's own error vocabulary, reduced to opaque classes. The raw text can echo the
#: request, so only these classes are ever reported or persisted.
_ERROR_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("invalid_number_of_inputs", re.compile(r"invalid number of inputs")),
    ("incompatible_unit", re.compile(r"incompatible unit")),
    ("unsupported_event_input", re.compile(r"does not support event inputs?")),
    ("unknown_operator", re.compile(r"unknown operator")),
    ("unknown_field", re.compile(r"(unknown|invalid) (data )?field")),
    ("unsupported_operator_usage", re.compile(r"not supported|unsupported")),
)


@dataclass(frozen=True)
class FindingObservation:
    """One settled candidate: the local findings it would have produced, and the outcome.

    ``settled_at`` is a wall-clock string and is only for reporting. ``settled_clock`` is the
    monotonic ``events.id`` of the terminal event, which is what a replay must compare
    against: timestamps are second-granularity and tie constantly, so ordering outcomes by
    them would hand a replay knowledge it could not have had.
    """

    candidate_id: int
    settled_at: str | None
    codes: tuple[str, ...]
    accepted: bool
    settled_clock: int | None = None

    @property
    def brain_rejected(self) -> bool:
        return not self.accepted


def error_class(message: Any) -> str:
    text = str(message or "").lower()
    for name, pattern in _ERROR_CLASSES:
        if pattern.search(text):
            return name
    return "other_platform_error" if text else "unclassified"


def _settings(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def observations(
    db: research_db.ResearchDB,
    *,
    scope: Mapping[str, Any] | None = None,
) -> tuple[list[FindingObservation], dict[str, int]]:
    """Re-screen every candidate that actually reached BRAIN.

    Returns the observations plus counters describing what was excluded, so a report can
    show why its denominator is what it is instead of hiding dropped rows.
    """
    rows = db.query(
        """
        SELECT c.id, c.normalized_expression AS expression, c.settings_json, c.status,
               c.failure_reason, s.status AS simulation_status, s.error AS simulation_error,
               s.completed_at,
               (SELECT MIN(e.id) FROM events e
                 WHERE e.entity='simulation' AND CAST(e.entity_id AS INTEGER)=c.id
                   AND e.event='result') AS settled_event_id
        FROM candidates c JOIN simulations s ON s.canonical_key = c.canonical_key
        ORDER BY c.id
        """
    )
    scope_key = canonical.scope_hash(scope) if scope else None
    result: list[FindingObservation] = []
    excluded = {"not_settled": 0, "other_scope": 0, "local_reject": 0}
    for row in rows:
        settings = _settings(row["settings_json"])
        if scope_key is not None and canonical.scope_hash(canonical.scope_from_settings(settings)) != scope_key:
            excluded["other_scope"] += 1
            continue
        status = str(row["simulation_status"] or "")
        if status not in ("DONE", "ERROR"):
            excluded["not_settled"] += 1
            continue
        # A candidate the local gate refused never reached BRAIN, so it says nothing about
        # whether the rule predicted a platform refusal.
        if str(row["failure_reason"] or "").startswith("validation:"):
            excluded["local_reject"] += 1
            continue
        # Screened with a *neutral* policy: calibration measures the rule, never the
        # currently approved gate (otherwise approving a rule would erase its own evidence).
        report = validate.validate(str(row["expression"] or ""), settings, severity_policy={})
        result.append(FindingObservation(
            candidate_id=int(row["id"]),
            settled_at=str(row["completed_at"] or "") or None,
            codes=tuple(report.finding_codes),
            accepted=status == "DONE",
            settled_clock=(
                int(row["settled_event_id"]) if row["settled_event_id"] is not None else None
            ),
        ))
    return result, excluded


def calibrate_from(
    rows: Iterable[FindingObservation],
    *,
    as_of: str | None = None,
    as_of_clock: int | None = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    strict_threshold: float = DEFAULT_STRICT_REJECT_RATE,
    keep_threshold: float = DEFAULT_KEEP_REJECT_RATE,
    scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score every finding code against real outcomes, optionally as of a decision point.

    ``as_of`` filters by wall clock; ``as_of_clock`` filters by the monotonic event id and is
    what a replay should use, because an observation whose event id is not strictly earlier
    was not knowable yet even when its timestamp ties.
    """
    def usable_at(row: FindingObservation) -> bool:
        if as_of_clock is not None:
            # No clock means the outcome cannot be proven earlier than the decision: exclude it.
            if row.settled_clock is None or row.settled_clock >= as_of_clock:
                return False
        if as_of is not None and (row.settled_at is None or row.settled_at > as_of):
            return False
        return True

    usable = [row for row in rows if usable_at(row)]
    accepted = sum(1 for row in usable if row.accepted)
    rejected = len(usable) - accepted
    per_code: dict[str, Counter[str]] = {}
    for row in usable:
        for code in row.codes:
            per_code.setdefault(code, Counter())[("accepted" if row.accepted else "rejected")] += 1

    findings: list[dict[str, Any]] = []
    for code in sorted(per_code):
        counter = per_code[code]
        samples = counter["accepted"] + counter["rejected"]
        rate = counter["rejected"] / samples if samples else None
        if samples < int(min_samples):
            recommendation, reason = RECOMMEND_INSUFFICIENT, f"only {samples} settled sample(s)"
        elif rate >= float(strict_threshold):
            recommendation, reason = RECOMMEND_STRICT, f"{counter['rejected']}/{samples} were refused by BRAIN"
        elif rate <= float(keep_threshold):
            recommendation, reason = RECOMMEND_KEEP, f"{counter['accepted']}/{samples} were accepted by BRAIN"
        else:
            recommendation, reason = RECOMMEND_INSUFFICIENT, f"{rate:.2f} rejection share is inconclusive"
        findings.append({
            "code": code,
            "samples": samples,
            "rejections": counter["rejected"],
            "accepted": counter["accepted"],
            "reject_rate": None if rate is None else round(rate, 6),
            "recommendation": recommendation,
            "reason": reason,
        })

    return {
        "calibration_version": CALIBRATION_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": as_of,
        "as_of_clock": as_of_clock,
        "scope": dict(scope) if scope else None,
        "observations": {"total": len(usable), "accepted": accepted, "rejected": rejected},
        "thresholds": {
            "min_samples": int(min_samples),
            "strict_reject_rate": float(strict_threshold),
            "keep_reject_rate": float(keep_threshold),
        },
        "findings": findings,
        "recommended_policy": recommended_policy_from_findings(findings),
        "note": "Advisory by default; nothing is enforced until a policy is explicitly approved.",
    }


def recommended_policy_from_findings(findings: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """The codes the *rejection rate* points at.

    This is one of two views, and the weaker one. It answers "does this rule predict a BRAIN
    refusal?"; the replay verdict below answers "would enforcing it have helped?", which is the
    question an enforced gate actually asks. A code can be a poor predictor and still be worth
    enforcing (it flagged work that never paid off), and a perfect predictor can be worthless
    or harmful (it flagged work that passed). ``--approve`` requires the replay verdict, so this
    map is a shortlist of what is worth measuring, not a decision.
    """
    return {
        str(item["code"]): validate.SEVERITY_ERROR
        for item in findings
        if item.get("recommendation") == RECOMMEND_STRICT
    }


def funnel_evidence(
    db: research_db.ResearchDB,
    report: Mapping[str, Any],
    *,
    budget: int | None = None,
    scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-code replay verdict: would declining the candidates it flags have improved the funnel?

    Kept separate from the rate-based report because it is a different question, and the two
    disagree in both directions on real data.
    """
    import policy_replay  # local import: policy_replay depends on this module

    evidence: dict[str, Any] = {}
    for item in report.get("findings") or []:
        code = str(item["code"])
        result = policy_replay.evaluate_severity_policy(
            db, {code: validate.SEVERITY_ERROR}, budget=budget, scope=scope,
        )
        evidence[code] = {
            "verdict": result["verdict"],
            "reasons": result["reasons"],
            "flagged_candidates": result["flagged_candidates"],
            "baselines": result["baselines"],
            "gate": result["gate"],
        }
    supported = [code for code, item in evidence.items()
                 if item["verdict"] in policy_replay.ENFORCEABLE_VERDICTS]
    return {"by_code": evidence, "supported": sorted(supported)}


def recommended_policy(calibration: Mapping[str, Any]) -> dict[str, str]:
    return dict(calibration.get("recommended_policy") or {})


def rejections_without_findings(
    rows: Iterable[FindingObservation],
    *,
    error_classes: Mapping[int, str] | None = None,
    as_of: str | None = None,
    as_of_clock: int | None = None,
) -> dict[str, Any]:
    """Rejections no local rule predicted — the candidate list for *new* rules.

    Only sanitized error classes and counts are reported; a candidate id is a local row id
    and expressions/alpha ids never leave the database.
    """
    def visible(row: FindingObservation) -> bool:
        if as_of_clock is not None:
            return row.settled_clock is not None and row.settled_clock < as_of_clock
        return as_of is None or (row.settled_at is not None and row.settled_at <= as_of)

    unexplained = [row for row in rows if row.brain_rejected and not row.codes and visible(row)]
    classes: Counter[str] = Counter()
    for row in unexplained:
        classes[(error_classes or {}).get(row.candidate_id, "unclassified")] += 1
    return {"count": len(unexplained), "error_classes": dict(sorted(classes.items()))}


# ---------------------------------------------------------------------------
# Persistence and enforcement
# ---------------------------------------------------------------------------


def refresh(db: research_db.ResearchDB, **kwargs: Any) -> dict[str, Any]:
    """Measure, persist, and return a calibration report."""
    scope = kwargs.pop("scope", None)
    with_funnel = bool(kwargs.pop("funnel", False))
    budget = kwargs.pop("budget", None)
    rows, excluded = observations(db, scope=scope)
    errors = {
        int(row["id"]): error_class(row["simulation_error"])
        for row in db.query(
            "SELECT c.id, s.error AS simulation_error FROM candidates c "
            "JOIN simulations s ON s.canonical_key=c.canonical_key WHERE s.status='ERROR'"
        )
    }
    report = calibrate_from(rows, scope=scope, **kwargs)
    report["excluded"] = excluded
    report["unexplained_rejections"] = rejections_without_findings(rows, error_classes=errors)
    if with_funnel:
        report["funnel"] = funnel_evidence(db, report, budget=budget, scope=scope)
    report["calibration_id"] = db.record_finding_calibration(report)
    return report


def latest(db: research_db.ResearchDB) -> dict[str, Any] | None:
    return db.latest_finding_calibration()


def approve(
    db: research_db.ResearchDB,
    policy: Mapping[str, str],
    *,
    budget: int | None = None,
    require_replay: bool = True,
    force: bool = False,
    scope: Mapping[str, Any] | None = None,
    per_code: bool = True,
) -> dict[str, Any]:
    """Make a policy enforceable by the pipeline (explicit, reviewable, reversible).

    A rejection rate is not evidence that enforcing a rule is *useful*: refusing work that
    would have passed costs passes, and a rule can be perfectly accurate while buying nothing.
    So enforcement now needs the offline benchmark to agree, and only codes whose own replay
    shows the funnel improving are kept (``scripts/policy_replay.py
    evaluate_severity_policy``). Everything else is reported with the reason it was dropped,
    and nothing is enforced unless the caller overrides explicitly.

    ``force=True`` is the reviewed human escape hatch: the override is applied and reported,
    never silent.
    """
    cleaned = {str(code): str(severity) for code, severity in policy.items()}
    for code, severity in cleaned.items():
        if severity not in validate.SEVERITIES:
            raise ValueError(f"unknown severity {severity!r} for finding {code!r}")
    if not cleaned:
        return {"enforced": {}, "refused": {}, "evidence": {},
                "note": "nothing was recommended, so nothing was enforced"}

    if not require_replay or force:
        db.save_severity_policy(cleaned)
        return {
            "enforced": cleaned,
            "dropped": {},
            "evidence": {"verdict": "overridden" if force else "skipped", "codes": sorted(cleaned)},
            "note": ("replay evidence was overridden by an explicit request" if force
                     else "replay evidence was not required"),
        }

    import policy_replay  # local import: policy_replay depends on this module

    evidence: dict[str, Any] = {}
    for code in sorted(cleaned):
        evidence[code] = policy_replay.evaluate_severity_policy(
            db, {code: cleaned[code]}, budget=budget, scope=scope,
        )
    approved = {
        code for code in cleaned
        if evidence[code]["verdict"] in policy_replay.ENFORCEABLE_VERDICTS
        or not per_code  # the combined run below is what decides
    }
    if not per_code:
        combined = policy_replay.evaluate_severity_policy(db, cleaned, budget=budget, scope=scope)
        evidence["combined"] = combined
        approved = set(cleaned) if combined["verdict"] in policy_replay.ENFORCEABLE_VERDICTS else set()
    refused = {
        code: {"verdict": evidence[code]["verdict"], "reasons": evidence[code]["reasons"]}
        for code in sorted(set(cleaned) - approved)
    }
    enforced = {code: cleaned[code] for code in sorted(approved)}
    if enforced:
        db.save_severity_policy(enforced)
    return {
        "enforced": enforced,
        "refused": refused,
        "evidence": evidence,
        "note": (
            "enforcement requires the offline benchmark to show the funnel improving; "
            "nothing is enforced without it unless the caller explicitly overrides"
        ),
    }


def clear(db: research_db.ResearchDB) -> None:
    db.clear_severity_policy()


def load_severity_policy(db: research_db.ResearchDB) -> dict[str, str]:
    return db.load_severity_policy()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Calibrate advisory screening findings against BRAIN outcomes")
    parser.add_argument("--db")
    parser.add_argument("--refresh", action="store_true", help="measure and persist a calibration")
    parser.add_argument("--report", action="store_true", help="print the latest stored calibration")
    parser.add_argument("--approve", action="store_true", help="enforce the recommended severities")
    parser.add_argument("--clear", action="store_true", help="stop enforcing any approved policy")
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--strict-threshold", type=float, default=DEFAULT_STRICT_REJECT_RATE)
    parser.add_argument("--keep-threshold", type=float, default=DEFAULT_KEEP_REJECT_RATE)
    parser.add_argument("--scope", help="JSON scope filter, e.g. '{\"region\":\"USA\"}'")
    parser.add_argument("--budget", type=int,
                        help="simulation budget for the approval replay (default: 10%% of the corpus)")
    parser.add_argument("--no-replay-gate", action="store_true",
                        help="enforce the measured recommendation without replay evidence")
    parser.add_argument("--force", action="store_true",
                        help="enforce even when the replay refuses (reported as an override)")
    parser.add_argument("--code", action="append", default=[],
                        help="propose this finding code for enforcement (default: the measured recommendation)")
    parser.add_argument("--funnel", action="store_true",
                        help="also report the per-code replay verdict (does enforcing it pay?)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    scope = json.loads(args.scope) if args.scope else None
    with research_db.ResearchDB.open(args.db) as db:
        if args.clear:
            clear(db)
            print(json.dumps({"enforced": {}}, indent=2, sort_keys=True))
            return 0
        if args.refresh or args.report:
            report = refresh(
                db, scope=scope, min_samples=args.min_samples,
                strict_threshold=args.strict_threshold, keep_threshold=args.keep_threshold,
                funnel=args.funnel, budget=args.budget,
            ) if args.refresh else latest(db)
            if report is None:
                print(json.dumps({"status": "no_calibration"}, indent=2, sort_keys=True))
                return 1
        if args.approve:
            if not args.refresh and not args.report:
                report = latest(db)
                if report is None:
                    print(json.dumps({"status": "no_calibration", "action": "refused"}, indent=2, sort_keys=True))
                    return 1
            proposed = (
                {code: validate.SEVERITY_ERROR for code in args.code} if args.code
                else recommended_policy(report)
            )
            result = approve(
                db, proposed, budget=args.budget, scope=scope,
                require_replay=not args.no_replay_gate, force=args.force,
            )
            print(json.dumps({**result, "enforced": load_severity_policy(db)}, indent=2, sort_keys=True))
            return 0 if result["enforced"] or not result["refused"] else 1
        print(json.dumps({**report, "enforced": load_severity_policy(db)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
