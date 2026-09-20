"""Safe mutations for the tracked skill file.

The manager is intentionally a small shell-independent API: any agent can call it, and
all writes use an expected SHA, an atomic replace, a content-addressed backup, and a
SQLite mutation ledger. Only PUBLIC/SANITIZED rules may be rendered into SKILL.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import research_db

SECRET_RE = re.compile(
    r"(?i)(wq[_-]?brain[_-]?(?:username|password)|credential\.(?:txt|key)|password\s*=|secret\s*=|authorization\s*:|/alphas/[A-Za-z0-9_-]{6,})"
)
TRACKED_PRIVACY = frozenset({"PUBLIC", "SANITIZED"})


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_skill(path: str | Path) -> tuple[str, str]:
    file_path = Path(path)
    content = file_path.read_text(encoding="utf-8")
    return content, sha256_text(content)


def _assert_public(rule: Mapping[str, Any]) -> None:
    privacy = str(rule.get("privacy_class", "")).upper()
    if privacy not in TRACKED_PRIVACY:
        raise ValueError("only PUBLIC or SANITIZED rules may enter tracked skill text")
    text = json.dumps(dict(rule), sort_keys=True, default=str)
    if SECRET_RE.search(text):
        raise ValueError("rule contains private identifiers, credentials, or account-linked data")


def _assert_snippet_public(snippet: str, privacy_class: str) -> None:
    if str(privacy_class).upper() not in TRACKED_PRIVACY:
        raise ValueError("only PUBLIC or SANITIZED snippets may enter tracked skill text")
    if SECRET_RE.search(snippet):
        raise ValueError("snippet contains private identifiers, credentials, or account-linked data")


def render_rule(rule: Mapping[str, Any]) -> str:
    """Render only general rule prose, never raw evidence payloads."""
    _assert_public(rule)
    title = str(rule.get("title", "")).strip()
    body = str(rule.get("body", "")).strip()
    scope = rule.get("scope") or {}
    scope_text = ", ".join(f"{key}={value}" for key, value in sorted(scope.items()))
    return f"\n### {title}\n\n{body}\n\n_Scope: {scope_text or 'general'}._\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _record_mutation(
    db: research_db.ResearchDB,
    *,
    operation: str,
    actor: str,
    expected_sha: str | None,
    before_sha: str,
    after_sha: str,
    backup_sha: str,
    backup_path: Path,
    rule_id: int | None,
    privacy_class: str,
) -> dict[str, Any]:
    version = int(db.get_meta("skill_version") or 0) + 1
    db.set_meta("skill_version", str(version))
    mutation_id = db.record_skill_mutation(
        operation=operation, actor=actor, expected_sha=expected_sha,
        before_sha=before_sha, after_sha=after_sha, backup_sha=backup_sha,
        backup_path=str(backup_path), rule_id=rule_id,
        privacy_class=privacy_class, version=version,
    )
    return {"mutation_id": mutation_id, "before_sha": before_sha, "after_sha": after_sha,
            "backup_sha": backup_sha, "version": version}


def _prepare_mutation(path: Path, expected_sha: str | None, backup_dir: str | Path | None) -> tuple[str, str, Path]:
    before, actual_sha = read_skill(path)
    if expected_sha is not None and expected_sha != actual_sha:
        raise ValueError(f"skill SHA mismatch: expected {expected_sha}, current {actual_sha}")
    backup_root = Path(backup_dir) if backup_dir else path.parent / ".skill-history"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / f"{actual_sha}.md"
    if not backup_path.exists():
        _atomic_write(backup_path, before)
    return before, actual_sha, backup_path


def _write_compare_and_swap(path: Path, before_sha: str, after: str) -> str:
    _, latest_sha = read_skill(path)
    if latest_sha != before_sha:
        raise ValueError("skill changed during mutation; retry with a fresh expected SHA")
    _atomic_write(path, after)
    return sha256_text(after)


def apply_snippet(
    db: research_db.ResearchDB,
    skill_path: str | Path,
    snippet: str,
    *,
    expected_sha: str | None = None,
    actor: str = "agent",
    backup_dir: str | Path | None = None,
    privacy_class: str = "SANITIZED",
) -> dict[str, Any]:
    """Append reviewed self-evolution prose through the guarded mutation path."""
    _assert_snippet_public(snippet, privacy_class)
    path = Path(skill_path)
    before, before_sha, backup_path = _prepare_mutation(path, expected_sha, backup_dir)
    after = before + ("\n" if before and not before.endswith("\n") else "") + snippet + "\n"
    after_sha = _write_compare_and_swap(path, before_sha, after)
    return _record_mutation(
        db, operation="skill.apply_snippet", actor=actor, expected_sha=expected_sha,
        before_sha=before_sha, after_sha=after_sha, backup_sha=before_sha,
        backup_path=backup_path, rule_id=None, privacy_class=str(privacy_class).upper(),
    )


def apply_rule(
    db: research_db.ResearchDB,
    skill_path: str | Path,
    rule: Mapping[str, Any],
    *,
    expected_sha: str | None = None,
    actor: str = "agent",
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Append an evaluated rule with compare-and-swap and a content-addressed backup."""
    _assert_public(rule)
    path = Path(skill_path)
    before, actual_sha, backup_path = _prepare_mutation(path, expected_sha, backup_dir)
    rendered = render_rule(rule)
    after = before + ("\n" if before and not before.endswith("\n") else "") + rendered
    after_sha = _write_compare_and_swap(path, actual_sha, after)
    return _record_mutation(
        db, operation="skill.apply", actor=actor, expected_sha=expected_sha,
        before_sha=actual_sha, after_sha=after_sha, backup_sha=actual_sha,
        backup_path=backup_path, rule_id=int(rule["id"]) if rule.get("id") else None,
        privacy_class=str(rule["privacy_class"]).upper(),
    )


def rollback(db: research_db.ResearchDB, skill_path: str | Path, backup_sha: str,
             *, expected_sha: str | None = None, actor: str = "agent",
             backup_dir: str | Path | None = None) -> dict[str, Any]:
    """Restore a content-addressed backup, also guarded and ledgered."""
    path = Path(skill_path)
    _, actual_sha = read_skill(path)
    if expected_sha is not None and expected_sha != actual_sha:
        raise ValueError(f"skill SHA mismatch: expected {expected_sha}, current {actual_sha}")
    backup_path = (Path(backup_dir) if backup_dir else path.parent / ".skill-history") / f"{backup_sha}.md"
    if not backup_path.exists():
        raise FileNotFoundError(f"skill backup not found: {backup_sha}")
    restored = backup_path.read_text(encoding="utf-8")
    restored_sha = sha256_text(restored)
    _write_compare_and_swap(path, actual_sha, restored)
    return _record_mutation(
        db, operation="skill.rollback", actor=actor, expected_sha=expected_sha,
        before_sha=actual_sha, after_sha=restored_sha, backup_sha=backup_sha,
        backup_path=backup_path, rule_id=None, privacy_class="SANITIZED",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db")
    parser.add_argument("--skill", default=str(SCRIPT_DIR.parent / "SKILL.md"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show knowledge and mutation status")
    recall = sub.add_parser("recall", help="search evaluated rules")
    recall.add_argument("query")
    args = parser.parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        if args.command == "status":
            print(json.dumps({"skill_sha": read_skill(args.skill)[1], **db.knowledge_status()}, indent=2, sort_keys=True))
        else:
            print(json.dumps(db.recall_knowledge(args.query), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
