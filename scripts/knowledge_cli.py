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
                                          max_privacy=args.max_privacy, limit=args.limit))
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
            elif args.command == "rules":
                _emit(db.list_rules(state=args.state, privacy_class=args.privacy))
    except (ValueError, KeyError, PermissionError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
