"""Agent-agnostic knowledge and skill command line interface.

All commands are offline and operate on the local research.db. Raw private evidence stays
in the ignored database; the tracked skill manager accepts only PUBLIC/SANITIZED rules.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import research_db
import skill_manager


def _json_object(raw: str, name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db")
    parser.add_argument("--skill", default=str(SCRIPT_DIR.parent / "SKILL.md"))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")
    recall = sub.add_parser("recall")
    recall.add_argument("query")
    recall.add_argument("--scope", default="{}")
    recall.add_argument("--max-privacy", default="PRIVATE")
    recall.add_argument("--limit", type=int, default=20)
    recall.add_argument("--state", action="append", default=None,
                        help="repeatable durable state filter (default: active,pinned)")
    recall.add_argument("--include-proposed", action="store_true",
                        help="also surface unvalidated proposed hypotheses")

    observations = sub.add_parser("observations", help="list scoped observations")
    observations.add_argument("--subject-type")
    observations.add_argument("--subject-key")
    observations.add_argument("--claim")
    observations.add_argument("--privacy")
    observations.add_argument("--lifecycle", default="active")

    show_rule = sub.add_parser("rule", help="inspect a rule with its evidence")
    show_rule.add_argument("rule_id", type=int)
    show_rule.add_argument("--include-non-active", action="store_true")

    attach = sub.add_parser("attach", help="attach evidence to a rule proposal")
    attach.add_argument("rule_id", type=int)
    attach.add_argument("observation_id", type=int)
    attach.add_argument("--polarity", default="support", choices=["support", "contradiction"])

    observe = sub.add_parser("observe")
    observe.add_argument("--subject-type", required=True)
    observe.add_argument("--subject-key", required=True)
    observe.add_argument("--claim", required=True)
    observe.add_argument("--value", required=True, help="JSON value")
    observe.add_argument("--scope", default="{}")
    observe.add_argument("--evidence-group", required=True)
    observe.add_argument("--provenance", default="{}")
    observe.add_argument("--privacy", default="PRIVATE")
    observe.add_argument("--candidate-id", type=int)
    observe.add_argument("--simulation-id")
    observe.add_argument("--submission-id", type=int)

    propose = sub.add_parser("propose")
    propose.add_argument("--title", required=True)
    propose.add_argument("--body", required=True)
    propose.add_argument("--scope", default="{}")
    propose.add_argument("--provenance", default="{}")
    propose.add_argument("--privacy", default="SANITIZED")
    propose.add_argument("--owner", default="agent")
    propose.add_argument("--evidence", action="append", default=[], metavar="OBS_ID:POLARITY")

    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("rule_id", type=int)
    evaluate.add_argument("--min-support", type=int, default=2)
    evaluate.add_argument("--min-groups", type=int, default=2)

    transition = sub.add_parser("transition")
    transition.add_argument("rule_id", type=int)
    transition.add_argument("state", choices=sorted(research_db.KNOWLEDGE_RULE_STATES))
    transition.add_argument("--expected-version", type=int)
    transition.add_argument("--force", action="store_true")

    promote = sub.add_parser("promote", help="alias for transition <id> active")
    promote.add_argument("rule_id", type=int)
    promote.add_argument("--expected-version", type=int)

    reject = sub.add_parser("reject", help="alias for transition <id> rejected")
    reject.add_argument("rule_id", type=int)
    reject.add_argument("--expected-version", type=int)
    reject.add_argument("--force", action="store_true")

    sub.add_parser("materialize", help="materialize new event observations (idempotent)")
    review = sub.add_parser("review", help="run the idempotent review worker (no auto-promotion)")
    review.add_argument("--min-support", type=int, default=2)
    review.add_argument("--min-groups", type=int, default=2)

    skill_validate = sub.add_parser("skill-validate", help="verify skill file SHA and ledger consistency")
    skill_validate.add_argument("--expected-sha")

    skill_diff = sub.add_parser("skill-diff", help="show ledgered mutations (audit trail)")
    skill_diff.add_argument("--limit", type=int, default=20)

    skill_apply = sub.add_parser("skill-apply-rule", help="render an evaluated rule into the tracked skill")
    skill_apply.add_argument("rule_id", type=int)
    skill_apply.add_argument("--expected-rule-version", type=int)
    skill_apply.add_argument("--expected-sha")

    skill_history = sub.add_parser("skill-history", help="alias for skill-diff")
    skill_history.add_argument("--limit", type=int, default=20)

    skill_rollback = sub.add_parser("skill-rollback", help="restore a content-addressed skill backup")
    skill_rollback.add_argument("backup_sha")
    skill_rollback.add_argument("--expected-sha")

    rules = sub.add_parser("rules")
    rules.add_argument("--state")
    rules.add_argument("--privacy")

    args = parser.parse_args(argv)
    try:
        with research_db.ResearchDB.open(args.db) as db:
            if args.command == "status":
                _emit({"skill_sha": skill_manager.read_skill(args.skill)[1], **db.knowledge_status()})
            elif args.command == "recall":
                _emit(db.recall_knowledge(args.query, scope=_json_object(args.scope, "scope"),
                                          states=args.state or ("active", "pinned"),
                                          include_proposed=args.include_proposed,
                                          max_privacy=args.max_privacy, limit=args.limit))
            elif args.command == "observations":
                _emit(db.observations(subject_type=args.subject_type, subject_key=args.subject_key,
                                      claim=args.claim, privacy_class=args.privacy,
                                      lifecycle_state=args.lifecycle))
            elif args.command == "rule":
                rule = db.get_rule(args.rule_id)
                if rule is None:
                    raise KeyError(f"rule {args.rule_id} not found")
                if not args.include_non_active:
                    rule = dict(rule)
                    rule["evidence"] = [item for item in rule["evidence"]
                                        if str(item.get("lifecycle_state") or "active") == "active"]
                _emit(rule)
            elif args.command == "attach":
                db.attach_rule_evidence(args.rule_id, args.observation_id, polarity=args.polarity)
                _emit({"rule_id": args.rule_id, "observation_id": args.observation_id,
                       "polarity": args.polarity})
            elif args.command == "observe":
                observation_id = db.record_observation(
                    subject_type=args.subject_type, subject_key=args.subject_key, claim=args.claim,
                    value=json.loads(args.value), scope=_json_object(args.scope, "scope"),
                    evidence_group=args.evidence_group, provenance=_json_object(args.provenance, "provenance"),
                    candidate_id=args.candidate_id, simulation_id=args.simulation_id,
                    submission_id=args.submission_id, privacy_class=args.privacy,
                )
                _emit({"observation_id": observation_id})
            elif args.command == "propose":
                evidence = []
                for raw in args.evidence:
                    try:
                        observation_id, polarity = raw.split(":", 1)
                        evidence.append((int(observation_id), polarity))
                    except ValueError as exc:
                        raise ValueError("--evidence must be OBS_ID: support|contradiction") from exc
                rule_id = db.propose_rule(
                    title=args.title, body=args.body, scope=_json_object(args.scope, "scope"),
                    provenance=_json_object(args.provenance, "provenance"), evidence=evidence,
                    privacy_class=args.privacy, owner=args.owner,
                )
                _emit({"rule_id": rule_id, "state": "proposed"})
            elif args.command == "evaluate":
                _emit(db.evaluate_rule(args.rule_id, min_support=args.min_support, min_independent_groups=args.min_groups))
            elif args.command == "transition":
                _emit(db.transition_rule(args.rule_id, args.state, expected_version=args.expected_version,
                                         force=args.force))
            elif args.command == "promote":
                _emit(db.transition_rule(args.rule_id, "active", expected_version=args.expected_version))
            elif args.command == "reject":
                _emit(db.transition_rule(args.rule_id, "rejected", expected_version=args.expected_version,
                                         force=args.force))
            elif args.command == "materialize":
                _emit({"simulation": db.materialize_event_observations(),
                       "submission": db.materialize_submission_observations()})
            elif args.command == "review":
                _emit(db.review_knowledge(min_support=args.min_support, min_groups=args.min_groups))
            elif args.command in ("skill-diff", "skill-history"):
                _emit(db.query("SELECT * FROM skill_mutations ORDER BY id DESC LIMIT ?",
                               (int(args.limit),)))
            elif args.command == "skill-validate":
                content, sha = skill_manager.read_skill(args.skill)
                mutations = db.query("SELECT * FROM skill_mutations ORDER BY id DESC LIMIT 1")
                _emit({"skill_sha": sha, "expected_sha": args.expected_sha,
                       "matches": args.expected_sha in (None, sha),
                       "latest_mutation": mutations[0] if mutations else None,
                       "chars": len(content)})
            elif args.command == "skill-apply-rule":
                _emit(skill_manager.apply_evaluated_rule(
                    db, args.skill, args.rule_id,
                    expected_rule_version=args.expected_rule_version,
                    expected_sha=args.expected_sha))
            elif args.command == "skill-rollback":
                _emit(skill_manager.rollback(db, args.skill, args.backup_sha,
                                             expected_sha=args.expected_sha))
            elif args.command == "rules":
                _emit(db.list_rules(state=args.state, privacy_class=args.privacy))
    except (ValueError, KeyError, PermissionError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
