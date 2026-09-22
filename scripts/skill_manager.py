"""Safe mutations for the tracked skill file.

The manager is intentionally a small shell-independent API: any agent can call it, and
all writes use an expected SHA, an atomic replace, a content-addressed backup, and a
SQLite mutation ledger. Only PUBLIC/SANITIZED rules may be rendered into SKILL.md.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
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
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
#: States a DB rule must be in before its prose may enter the tracked skill file.
EVALUATED_RULE_STATES = frozenset({"active", "pinned"})
#: Actors allowed to write arbitrary (non-rule) prose. Autonomous agents must go
#: through :func:`apply_evaluated_rule` with a DB rule id + expected version.
MANUAL_SNIPPET_ACTORS = frozenset({"user"})


@contextlib.contextmanager
def _skill_lock(skill_path: str | Path, *, timeout: float = 30.0):
    """Inter-process mutex spanning the whole mutation critical section.

    ``fcntl.flock`` on a sidecar ``<skill>.lock`` file serializes writers across
    processes so two workers holding the same stale expected SHA cannot both
    commit. Falls back to an atomic lock-dir spin when ``fcntl`` is unavailable.
    """
    path = Path(skill_path)
    lock_path = path.parent / (path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl  # Unix-only; the current deployment environment provides it.

        handle = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for skill lock: {lock_path}")
                    time.sleep(0.02)
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                os.close(handle)
        return
    except ImportError:
        pass
    # Portable fallback: atomic lock-dir acquisition.
    lock_dir = path.parent / (path.name + ".lockdir")
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for skill lock: {lock_dir}")
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            os.rmdir(lock_dir)
        except OSError:
            pass


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
    outcome = db.record_skill_mutation_atomic(
        operation=operation, actor=actor, expected_sha=expected_sha,
        before_sha=before_sha, after_sha=after_sha, backup_sha=backup_sha,
        backup_path=str(backup_path), rule_id=rule_id,
        privacy_class=str(privacy_class).upper(),
    )
    return {"mutation_id": outcome["mutation_id"], "before_sha": before_sha, "after_sha": after_sha,
            "backup_sha": backup_sha, "version": outcome["version"]}


def _commit_locked(
    db: research_db.ResearchDB,
    path: Path,
    *,
    operation: str,
    actor: str,
    expected_sha: str | None,
    before: str,
    before_sha: str,
    backup_path: Path,
    after: str,
    rule_id: int | None,
    privacy_class: str,
) -> dict[str, Any]:
    """Write ``after`` and ledger it; restore ``before`` when DB persistence fails.

    Must be called with :func:`_skill_lock` held. The file replace happens first,
    then version+ledger persist in one SQLite transaction. When persistence fails
    the captured before-state is restored before the lock is released so the file
    and ledger cannot disagree about the current mutation.
    """
    _atomic_write(path, after)
    after_sha = sha256_text(after)
    try:
        return _record_mutation(
            db, operation=operation, actor=actor, expected_sha=expected_sha,
            before_sha=before_sha, after_sha=after_sha, backup_sha=before_sha,
            backup_path=backup_path, rule_id=rule_id, privacy_class=privacy_class,
        )
    except BaseException:
        try:
            _atomic_write(path, before)
        except BaseException:
            pass
        raise


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


def _load_evaluated_rule(
    db: research_db.ResearchDB, rule_id: int, *, expected_rule_version: int | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    """Load a DB rule and prove it may enter tracked skill text.

    Requires existence, expected version match, ``active``/``pinned`` state,
    a passing evaluation, and PUBLIC/SANITIZED privacy. The rendered prose comes
    from this DB row — never from a caller-supplied mapping.
    """
    connection = conn if conn is not None else db._conn
    row = connection.execute("SELECT * FROM knowledge_rules WHERE id=?", (int(rule_id),)).fetchone()
    if row is None:
        raise KeyError(f"rule {rule_id} not found")
    rule = dict(row)
    if expected_rule_version is not None and int(rule["version"]) != int(expected_rule_version):
        raise ValueError(
            f"rule version mismatch: expected {expected_rule_version}, current {rule['version']}"
        )
    if str(rule["state"]) not in EVALUATED_RULE_STATES:
        raise ValueError(
            f"rule {rule_id} is '{rule['state']}': only evaluated active/pinned rules may enter tracked skill text"
        )
    try:
        evaluation = json.loads(rule.get("evaluation_json") or "{}")
    except ValueError:
        evaluation = {}
    if not evaluation.get("passed"):
        raise ValueError(f"rule {rule_id} has not passed its evaluation gate")
    candidate = {"privacy_class": rule.get("privacy_class"), "title": rule.get("title"),
                 "body": rule.get("body"), "scope": rule.get("scope_json")}
    _assert_public({"privacy_class": rule.get("privacy_class"),
                    "title": rule.get("title"), "body": rule.get("body")})
    try:
        scope = json.loads(rule.get("scope_json") or "{}")
    except ValueError:
        scope = {}
    return {"id": int(rule["id"]), "title": str(rule.get("title") or ""),
            "body": str(rule.get("body") or ""), "scope": scope if isinstance(scope, dict) else {},
            "privacy_class": str(rule.get("privacy_class") or "").upper(),
            "version": int(rule["version"]), "state": str(rule["state"])}


def _commit_evaluated_rule_locked(
    db: research_db.ResearchDB,
    path: Path,
    *,
    rule_id: int,
    expected_rule_version: int | None,
    expected_sha: str | None,
    actor: str,
    backup_dir: str | Path | None,
) -> dict[str, Any]:
    """Validate rule + write skill + ledger under one SQLite IMMEDIATE transaction.

    The skill file lock is held by the caller. BEGIN IMMEDIATE prevents a concurrent
    rule transition/version change after validation but before the mutation ledger
    commits. If file or DB persistence fails, the captured file state is restored.
    """
    before, before_sha, backup_path = _prepare_mutation(path, expected_sha, backup_dir)
    file_written = False
    try:
        with db._tx() as conn:
            evaluated = _load_evaluated_rule(
                db, int(rule_id), expected_rule_version=expected_rule_version, conn=conn,
            )
            rendered = render_rule(evaluated)
            after = before + ("\n" if before and not before.endswith("\n") else "") + rendered
            after_sha = sha256_text(after)
            _atomic_write(path, after)
            file_written = True
            outcome = db._record_skill_mutation_in_tx(
                conn,
                operation="skill.apply",
                actor=actor,
                expected_sha=expected_sha,
                before_sha=before_sha,
                after_sha=after_sha,
                backup_sha=before_sha,
                backup_path=str(backup_path),
                rule_id=evaluated["id"],
                privacy_class=evaluated["privacy_class"],
            )
        return {
            "mutation_id": outcome["mutation_id"],
            "before_sha": before_sha,
            "after_sha": after_sha,
            "backup_sha": before_sha,
            "version": outcome["version"],
        }
    except BaseException:
        if file_written:
            try:
                _atomic_write(path, before)
            except BaseException:
                pass
        raise


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
    """Append manually reviewed prose. Explicitly user-only.

    Autonomous agents must use :func:`apply_evaluated_rule` with a DB rule id;
    arbitrary Markdown can never become durable guidance on its own.
    """
    if str(actor) not in MANUAL_SNIPPET_ACTORS:
        raise PermissionError(
            "apply_snippet is manual/user-only: autonomous writes must use apply_evaluated_rule(rule_id)"
        )
    _assert_snippet_public(snippet, privacy_class)
    path = Path(skill_path)
    with _skill_lock(path):
        before, before_sha, backup_path = _prepare_mutation(path, expected_sha, backup_dir)
        after = before + ("\n" if before and not before.endswith("\n") else "") + snippet + "\n"
        return _commit_locked(
            db, path, operation="skill.apply_snippet", actor=actor, expected_sha=expected_sha,
            before=before, before_sha=before_sha, backup_path=backup_path, after=after,
            rule_id=None, privacy_class=privacy_class,
        )


def apply_rule(
    db: research_db.ResearchDB,
    skill_path: str | Path,
    rule: Mapping[str, Any],
    *,
    expected_sha: str | None = None,
    actor: str = "agent",
    backup_dir: str | Path | None = None,
    expected_rule_version: int | None = None,
) -> dict[str, Any]:
    """Append a DB-backed evaluated rule, or manual user prose without a DB id."""
    path = Path(skill_path)
    rule_id = rule.get("id")
    if rule_id is not None and str(actor) not in MANUAL_SNIPPET_ACTORS:
        if expected_rule_version is None:
            raise ValueError("autonomous rule application requires expected_rule_version")
        if expected_sha is None:
            raise ValueError("autonomous rule application requires expected_sha")
    with _skill_lock(path):
        if rule_id is not None:
            return _commit_evaluated_rule_locked(
                db, path, rule_id=int(rule_id),
                expected_rule_version=expected_rule_version,
                expected_sha=expected_sha, actor=actor, backup_dir=backup_dir,
            )
        if str(actor) not in MANUAL_SNIPPET_ACTORS:
            raise PermissionError(
                "rule mappings without a DB id are manual/user-only: "
                "autonomous writes must use apply_evaluated_rule(rule_id)"
            )
        _assert_public(rule)
        before, actual_sha, backup_path = _prepare_mutation(path, expected_sha, backup_dir)
        rendered = render_rule(rule)
        after = before + ("\n" if before and not before.endswith("\n") else "") + rendered
        return _commit_locked(
            db, path, operation="skill.apply", actor=actor, expected_sha=expected_sha,
            before=before, before_sha=actual_sha, backup_path=backup_path, after=after,
            rule_id=None, privacy_class=str(rule["privacy_class"]).upper(),
        )


def apply_evaluated_rule(
    db: research_db.ResearchDB,
    skill_path: str | Path,
    rule_id: int,
    *,
    expected_rule_version: int | None = None,
    expected_sha: str | None = None,
    actor: str = "agent",
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Autonomous mutation path with mandatory rule-version and file-SHA CAS."""
    if expected_sha is None:
        raise ValueError("autonomous rule application requires expected_sha")
    path = Path(skill_path)
    with _skill_lock(path):
        # Validate lifecycle before the optimistic-concurrency requirement so callers
        # receive the actionable rule-state error for a proposed rule.
        if expected_rule_version is None:
            rule = db.get_rule(int(rule_id))
            if rule is None:
                raise KeyError(f"rule {rule_id} not found")
            if str(rule["state"]) not in EVALUATED_RULE_STATES:
                raise ValueError(
                    f"rule {rule_id} is '{rule['state']}': only evaluated active/pinned rules may enter tracked skill text"
                )
            raise ValueError("autonomous rule application requires expected_rule_version")
        return _commit_evaluated_rule_locked(
            db, path, rule_id=int(rule_id),
            expected_rule_version=expected_rule_version,
            expected_sha=expected_sha, actor=actor, backup_dir=backup_dir,
        )


def rollback(db: research_db.ResearchDB, skill_path: str | Path, backup_sha: str,
             *, expected_sha: str | None = None, actor: str = "agent",
             backup_dir: str | Path | None = None) -> dict[str, Any]:
    """Restore a verified content-addressed backup, capturing the state being left."""
    backup_sha = str(backup_sha)
    if not SHA256_HEX_RE.fullmatch(backup_sha):
        raise ValueError("backup_sha must be a lowercase 64-character SHA-256 digest")
    path = Path(skill_path)
    backup_root = (Path(backup_dir) if backup_dir else path.parent / ".skill-history")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_root = backup_root.resolve()
    with _skill_lock(path):
        current, actual_sha = read_skill(path)
        if expected_sha is not None and expected_sha != actual_sha:
            raise ValueError(f"skill SHA mismatch: expected {expected_sha}, current {actual_sha}")
        current_backup = (backup_root / f"{actual_sha}.md").resolve()
        if current_backup.parent != backup_root:
            raise ValueError("current backup path escaped the skill history directory")
        if not current_backup.exists():
            _atomic_write(current_backup, current)

        backup_path = (backup_root / f"{backup_sha}.md").resolve()
        if backup_path.parent != backup_root:
            raise ValueError("backup path escaped the skill history directory")
        if not backup_path.exists():
            raise FileNotFoundError(f"skill backup not found: {backup_sha}")
        restored = backup_path.read_text(encoding="utf-8")
        restored_sha = sha256_text(restored)
        if restored_sha != backup_sha:
            raise ValueError(
                f"skill backup hash mismatch: requested {backup_sha}, content hashes to {restored_sha}"
            )
        _atomic_write(path, restored)
        try:
            return _record_mutation(
                db, operation="skill.rollback", actor=actor, expected_sha=expected_sha,
                before_sha=actual_sha, after_sha=restored_sha, backup_sha=backup_sha,
                backup_path=backup_path, rule_id=None, privacy_class="SANITIZED",
            )
        except BaseException:
            try:
                _atomic_write(path, current)
            except BaseException:
                pass
            raise


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
