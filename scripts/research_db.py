"""Local research state store: ``research.db`` (TODO P0) + simulation cache (TODO P1).

One SQLite file is the single source of truth for the pipeline:

    candidates     lifecycle + priority + metrics of one canonical research idea
    simulations    reusable simulation cache, one row per canonical request
    submissions    submission queue state, leased like simulations
    active_alphas  ACTIVE portfolio snapshot + correlation bookkeeping
    events         append-only audit trail of every state transition

State machine (TODO P0):

    GENERATED -> VALIDATED -> QUEUED -> SIMULATING -> SIMULATED -> IS_PASS
    -> CORR_PASS -> SUBMISSION_READY -> SUBMITTING -> ACTIVE / REJECTED / RETRY

``candidates`` and ``simulations`` are 1:1 on ``canonical_key`` by design:
``candidates`` answers "what should we work on and why", ``simulations`` answers
"has BRAIN already computed this exact request". A cache hit therefore replays
metrics without ever calling BRAIN.

Dedup policy before simulating (TODO P1):

    cached DONE            -> action "cache_hit"       (reuse, no BRAIN call)
    QUEUED / RUNNING       -> action "in_flight"       (another process owns it)
    REJECTED / ACTIVE      -> action "skipped_final"   (do not re-spend capacity)
    RETRY                  -> action "requeued"        (attempt again)
    otherwise              -> action "queued"

Everything is written inside short ``BEGIN IMMEDIATE`` transactions, so any
process can stop or restart without losing finished work and without two workers
claiming the same row. WAL mode keeps readers (``status``) from blocking writers.

Usage:
    from research_db import ResearchDB

    db = ResearchDB.open()                     # <repo root>/research.db
    outcome = db.queue_candidate("rank(close)", {"region": "USA"})
    claimed = db.claim_simulation(worker_id="batch-1", lease_seconds=1800)

CLI:
    ./.venv/bin/python scripts/research_db.py init
    ./.venv/bin/python scripts/research_db.py status
    ./.venv/bin/python scripts/research_db.py queue legacy/wq_brain/data/input.csv
    ./.venv/bin/python scripts/research_db.py cache --expression "rank(close)"
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical  # noqa: E402  (scripts/ is on sys.path above)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "research.db"
DB_ENV_VAR = "WQ_RESEARCH_DB"

SCHEMA_VERSION = 1

# IS gates from SKILL.md Section 5, used only when BRAIN's own IS checks are absent.
IS_THRESHOLDS = {"sharpe": 1.25, "fitness": 1.1, "turnover_min": 0.01, "turnover_max": 0.20}

CANDIDATE_STATUSES: tuple[str, ...] = (
    "GENERATED",
    "VALIDATED",
    "QUEUED",
    "SIMULATING",
    "SIMULATED",
    "IS_PASS",
    "CORR_PASS",
    "SUBMISSION_READY",
    "SUBMITTING",
    "ACTIVE",
    "RETRY",
    "REJECTED",
)

# Allowed one-step transitions. Multi-step moves (SIMULATING -> IS_PASS through
# SIMULATED, for example) are resolved by RESEARCH_DB.transition_path().
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "GENERATED": frozenset({"VALIDATED", "REJECTED"}),
    "VALIDATED": frozenset({"QUEUED", "REJECTED"}),
    "QUEUED": frozenset({"SIMULATING", "SIMULATED", "RETRY", "REJECTED"}),
    # SIMULATING -> QUEUED covers a lease that was taken but never submitted (429/timeout).
    "SIMULATING": frozenset({"SIMULATED", "RETRY", "REJECTED", "QUEUED"}),
    "SIMULATED": frozenset({"IS_PASS", "REJECTED"}),
    "IS_PASS": frozenset({"CORR_PASS", "REJECTED"}),
    "CORR_PASS": frozenset({"SUBMISSION_READY", "REJECTED"}),
    "SUBMISSION_READY": frozenset({"SUBMITTING", "RETRY", "REJECTED"}),
    "SUBMITTING": frozenset({"ACTIVE", "REJECTED", "RETRY"}),
    "RETRY": frozenset({"QUEUED", "SUBMISSION_READY", "REJECTED"}),
    "ACTIVE": frozenset(),
    "REJECTED": frozenset(),
}

SIMULATION_STATUSES: tuple[str, ...] = ("QUEUED", "RUNNING", "DONE", "ERROR")

SUBMISSION_STATUSES: tuple[str, ...] = (
    "READY",
    "SUBMITTING",
    "CHECK_PENDING",
    "ACTIVE",
    "SELF_CORR_FAIL",
    "PLATFORM_REJECTED",
    "RETRY",
)

# Terminal submission states: an expired lease on these is not "recoverable work".
FINAL_SUBMISSION_STATUSES = frozenset({"ACTIVE", "SELF_CORR_FAIL", "PLATFORM_REJECTED"})

# CSV columns that describe simulation settings (mirrors batch_simulate.build_settings).
SETTINGS_COLUMNS: tuple[str, ...] = (
    "instrumentType",
    "region",
    "universe",
    "delay",
    "decay",
    "neutralization",
    "truncation",
    "pasteurization",
    "unitHandling",
    "nanHandling",
    "language",
)

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidates (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_key         TEXT NOT NULL UNIQUE,
        expression            TEXT NOT NULL,
        normalized_expression TEXT NOT NULL,
        expression_hash       TEXT NOT NULL,
        settings_hash         TEXT NOT NULL,
        settings_json         TEXT NOT NULL,
        skeleton_hash         TEXT,
        status                TEXT NOT NULL,
        priority              REAL NOT NULL DEFAULT 0,
        expected_quality      REAL,
        novelty_score         REAL,
        failure_risk          REAL,
        signal_family         TEXT,
        source                TEXT,
        parent_id             INTEGER REFERENCES candidates(id),
        generation            INTEGER NOT NULL DEFAULT 0,
        mutation_type         TEXT,
        near_duplicate_of     INTEGER REFERENCES candidates(id),
        brain_alpha_id        TEXT,
        simulation_id         TEXT,
        sharpe                REAL,
        fitness               REAL,
        turnover              REAL,
        drawdown              REAL,
        self_corr             REAL,
        is_pass               INTEGER,
        failure_reason        TEXT,
        attempt_count         INTEGER NOT NULL DEFAULT 0,
        next_attempt_at       TEXT,
        worker_id             TEXT,
        lease_until           TEXT,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_candidates_queue ON candidates(status, priority DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_skeleton ON candidates(skeleton_hash)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_brain_alpha ON candidates(brain_alpha_id)",
    """
    CREATE TABLE IF NOT EXISTS simulations (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_key         TEXT NOT NULL UNIQUE,
        candidate_id          INTEGER REFERENCES candidates(id),
        expression            TEXT NOT NULL,
        normalized_expression TEXT NOT NULL,
        settings_json         TEXT NOT NULL,
        status                TEXT NOT NULL,
        brain_alpha_id        TEXT,
        simulation_id         TEXT,
        sharpe                REAL,
        fitness               REAL,
        turnover              REAL,
        drawdown              REAL,
        is_pass               INTEGER,
        checks_json           TEXT,
        error                 TEXT,
        attempts              INTEGER NOT NULL DEFAULT 0,
        worker_id             TEXT,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL,
        completed_at          TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_simulations_status ON simulations(status)",
    """
    CREATE TABLE IF NOT EXISTS submissions (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id     INTEGER NOT NULL REFERENCES candidates(id),
        brain_alpha_id   TEXT,
        status           TEXT NOT NULL,
        priority         REAL NOT NULL DEFAULT 0,
        attempt          INTEGER NOT NULL DEFAULT 0,
        max_corr         REAL,
        max_corr_alpha_id TEXT,
        worker_id        TEXT,
        lease_until      TEXT,
        next_attempt_at  TEXT,
        message          TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_submissions_queue ON submissions(status, priority DESC, id)",
    """
    CREATE TABLE IF NOT EXISTS active_alphas (
        brain_alpha_id  TEXT PRIMARY KEY,
        canonical_key   TEXT,
        expression_hash TEXT,
        settings_hash   TEXT,
        sharpe          REAL,
        fitness         REAL,
        turnover        REAL,
        corr_checked_at TEXT,
        pnl_ref         TEXT,
        first_seen_at   TEXT NOT NULL,
        last_seen_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at   TEXT NOT NULL,
        entity       TEXT NOT NULL,
        entity_id    TEXT,
        event        TEXT NOT NULL,
        from_status  TEXT,
        to_status    TEXT,
        payload_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity, entity_id)",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """UTC timestamp; fixed width so lease comparisons can use string ordering."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plus_seconds_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def is_expired(timestamp: str | None, reference: str | None = None) -> bool:
    """True when a lease/deadline timestamp is missing or in the past."""
    if not timestamp:
        return True
    return timestamp <= (reference or now_iso())


def settings_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build canonical settings from a CSV row (blank/absent cells fall back to defaults)."""
    return canonical.normalize_settings({column: row[column] for column in SETTINGS_COLUMNS if column in row})


def transition_path(current: str, target: str, max_depth: int = 3) -> list[str] | None:
    """Shortest allowed status path from current to target (excluding current), or None."""
    if current == target:
        return []
    frontier: list[tuple[str, list[str]]] = [(current, [])]
    seen = {current}
    for _ in range(max_depth):
        next_frontier: list[tuple[str, list[str]]] = []
        for status, path in frontier:
            for candidate in sorted(ALLOWED_TRANSITIONS.get(status, frozenset())):
                if candidate == target:
                    return path + [candidate]
                if candidate not in seen:
                    seen.add(candidate)
                    next_frontier.append((candidate, path + [candidate]))
        frontier = next_frontier
        if not frontier:
            break
    return None


def is_gate(
    metrics: Mapping[str, Any],
    checks: Iterable[Mapping[str, Any]] | None = None,
    thresholds: Mapping[str, float] | None = None,
) -> tuple[bool, str]:
    """IS gate: BRAIN's own check results when available, otherwise local thresholds.

    ``turnover`` is a fraction (0.05 = 5%), matching BRAIN's IS payload.
    """
    limits = {**IS_THRESHOLDS, **(thresholds or {})}
    check_list = [c for c in (checks or []) if isinstance(c, Mapping)]
    failed = [str(c.get("name")) for c in check_list if str(c.get("result", "")).upper() == "FAIL"]
    if failed:
        return False, "IS check failed: " + ",".join(sorted(failed))
    if check_list:
        return True, ""

    sharpe = metrics.get("sharpe")
    fitness = metrics.get("fitness")
    turnover = metrics.get("turnover")
    reasons: list[str] = []
    if not isinstance(sharpe, (int, float)) or sharpe < limits["sharpe"]:
        reasons.append(f"sharpe<{limits['sharpe']}")
    if not isinstance(fitness, (int, float)) or fitness < limits["fitness"]:
        reasons.append(f"fitness<{limits['fitness']}")
    if not isinstance(turnover, (int, float)):
        reasons.append("turnover missing")
    elif not (limits["turnover_min"] <= turnover <= limits["turnover_max"]):
        reasons.append(f"turnover outside {limits['turnover_min']}-{limits['turnover_max']}")
    return (not reasons), "; ".join(reasons)


@dataclass
class QueueOutcome:
    """Result of asking the store whether a candidate still needs BRAIN capacity."""

    action: str  # cache_hit | in_flight | skipped_final | queued | requeued
    canonical_key: str
    candidate_id: int | None = None
    status: str | None = None
    cached: dict[str, Any] | None = None

    @property
    def needs_simulation(self) -> bool:
        return self.action in ("queued", "requeued")


@dataclass
class StatusReport:
    """Aggregated counters for the observability CLI (TODO P13 groundwork)."""

    candidates: dict[str, int] = field(default_factory=dict)
    simulations: dict[str, int] = field(default_factory=dict)
    submissions: dict[str, int] = field(default_factory=dict)
    cache_hits: int = 0
    active_alphas: int = 0
    events: int = 0
    generated_per_hour: float = 0.0
    simulated_per_hour: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "simulations": self.simulations,
            "submissions": self.submissions,
            "cache_hits": self.cache_hits,
            "active_alphas": self.active_alphas,
            "events": self.events,
            "generated_per_hour": self.generated_per_hour,
            "simulated_per_hour": self.simulated_per_hour,
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ResearchDB:
    """Thin, explicit SQLite wrapper — one connection per process."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH, *, timeout: float = 30.0) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(str(self.path), timeout=timeout, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path | None = None) -> "ResearchDB":
        """Open (creating if needed) the store; ``WQ_RESEARCH_DB`` overrides the default."""
        return cls(resolve_db_path(path))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ResearchDB":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        with self._tx() as conn:
            for statement in SCHEMA:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """IMMEDIATE transaction: serializes writers across processes (WAL readers stay open)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- events ------------------------------------------------------------

    def log_event(
        self,
        entity: str,
        entity_id: Any,
        event: str,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
        payload: Mapping[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Append to the audit trail; safe to call inside an open transaction."""
        sql = (
            "INSERT INTO events(created_at, entity, entity_id, event, from_status, to_status, payload_json) "
            "VALUES(?,?,?,?,?,?,?)"
        )
        params = (
            now_iso(),
            entity,
            None if entity_id is None else str(entity_id),
            event,
            from_status,
            to_status,
            json.dumps(payload, sort_keys=True, default=str) if payload else None,
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self._tx() as owned:
                owned.execute(sql, params)

    # -- candidates --------------------------------------------------------

    def queue_candidate(
        self,
        expression: str,
        settings: Mapping[str, Any] | None = None,
        *,
        source: str = "",
        signal_family: str | None = None,
        priority: float = 0.0,
        parent_id: int | None = None,
        generation: int = 0,
        mutation_type: str | None = None,
        requeue: bool = False,
    ) -> QueueOutcome:
        """Register a candidate and decide whether it still needs BRAIN capacity."""
        normalized_expression = canonical.normalize_expression(expression)
        normalized_settings = canonical.normalize_settings(settings)
        key = canonical.canonical_key(normalized_expression, normalized_settings)
        settings_json = json.dumps(normalized_settings, sort_keys=True)
        timestamp = now_iso()

        with self._tx() as conn:
            simulation = conn.execute("SELECT * FROM simulations WHERE canonical_key=?", (key,)).fetchone()
            candidate = conn.execute("SELECT * FROM candidates WHERE canonical_key=?", (key,)).fetchone()

            if simulation is not None and simulation["status"] == "DONE":
                candidate_id = candidate["id"] if candidate else self._insert_candidate(
                    conn,
                    key,
                    expression,
                    normalized_expression,
                    normalized_settings,
                    settings_json,
                    timestamp,
                    source=source,
                    signal_family=signal_family,
                    priority=priority,
                    parent_id=parent_id,
                    generation=generation,
                    mutation_type=mutation_type,
                    status="SIMULATED",
                )
                self.log_event(
                    "candidate", candidate_id, "cache_hit", to_status=candidate["status"] if candidate else "SIMULATED",
                    payload={"canonical_key": key}, conn=conn,
                )
                return QueueOutcome("cache_hit", key, candidate_id, candidate["status"] if candidate else "SIMULATED",
                                    cached=self._cached_result(simulation))

            if simulation is not None and simulation["status"] in ("QUEUED", "RUNNING"):
                if simulation["status"] == "RUNNING" and is_expired(candidate["lease_until"] if candidate else None):
                    return self._requeue(conn, candidate, simulation, timestamp, reason="stale_lease")
                return QueueOutcome("in_flight", key, candidate["id"] if candidate else None,
                                    candidate["status"] if candidate else simulation["status"])

            if candidate is not None and candidate["status"] in ("REJECTED", "ACTIVE", "SUBMITTING"):
                return QueueOutcome("skipped_final", key, candidate["id"], candidate["status"])

            if candidate is not None and candidate["status"] == "RETRY" and not requeue \
                    and not is_expired(candidate["next_attempt_at"], timestamp):
                return QueueOutcome("in_flight", key, candidate["id"], candidate["status"])

            if candidate is None:
                candidate_id = self._insert_candidate(
                    conn,
                    key,
                    expression,
                    normalized_expression,
                    normalized_settings,
                    settings_json,
                    timestamp,
                    source=source,
                    signal_family=signal_family,
                    priority=priority,
                    parent_id=parent_id,
                    generation=generation,
                    mutation_type=mutation_type,
                    status="QUEUED",
                )
                from_status = None
            else:
                candidate_id = candidate["id"]
                from_status = candidate["status"]
                self._set_status(conn, candidate, "QUEUED", timestamp)
                if priority:
                    conn.execute("UPDATE candidates SET priority=? WHERE id=?", (priority, candidate_id))

            self._upsert_simulation(
                conn, key, candidate_id, expression, normalized_expression, settings_json, "QUEUED", timestamp
            )
            self.log_event(
                "candidate", candidate_id, "queued", from_status=from_status, to_status="QUEUED",
                payload={"source": source, "canonical_key": key}, conn=conn,
            )
            action = "requeued" if from_status == "RETRY" or requeue else "queued"
            return QueueOutcome(action, key, candidate_id, "QUEUED")

    def _insert_candidate(
        self,
        conn: sqlite3.Connection,
        key: str,
        expression: str,
        normalized_expression: str,
        normalized_settings: Mapping[str, Any],
        settings_json: str,
        timestamp: str,
        *,
        source: str,
        signal_family: str | None,
        priority: float,
        parent_id: int | None,
        generation: int,
        mutation_type: str | None,
        status: str,
    ) -> int:
        skeleton_hash = canonical.skeleton_hash(normalized_expression)
        duplicate = conn.execute(
            "SELECT id FROM candidates WHERE skeleton_hash=? AND canonical_key<>? ORDER BY id LIMIT 1",
            (skeleton_hash, key),
        ).fetchone()
        cursor = conn.execute(
            """
            INSERT INTO candidates(
                canonical_key, expression, normalized_expression, expression_hash, settings_hash,
                settings_json, skeleton_hash, status, priority, signal_family, source, parent_id,
                generation, mutation_type, near_duplicate_of, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key, expression, normalized_expression, canonical.expression_hash(normalized_expression),
                canonical.settings_hash(normalized_settings), settings_json, skeleton_hash, status, priority,
                signal_family, source, parent_id, generation, mutation_type,
                duplicate["id"] if duplicate else None, timestamp, timestamp,
            ),
        )
        candidate_id = int(cursor.lastrowid)
        if duplicate is not None:
            self.log_event(
                "candidate", candidate_id, "near_duplicate", to_status=status,
                payload={"near_duplicate_of": duplicate["id"]}, conn=conn,
            )
        return candidate_id

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        return dict(row) if row else None

    def find_candidate(self, expression: str, settings: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        key = canonical.canonical_key(expression, settings)
        row = self._conn.execute("SELECT * FROM candidates WHERE canonical_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def set_status(
        self,
        candidate_id: int,
        status: str,
        *,
        reason: str | None = None,
        force: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Move a candidate to ``status`` along an allowed path; returns the previous status."""
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"unknown status: {status}")
        owned = conn is None
        conn = conn or self._conn
        if owned:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if row is None:
                raise KeyError(f"candidate {candidate_id} not found")
            previous = row["status"]
            self._set_status(conn, row, status, now_iso(), reason=reason, force=force)
            if owned:
                conn.execute("COMMIT")
        except BaseException:
            if owned:
                conn.execute("ROLLBACK")
            raise
        return previous

    def _set_status(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        status: str,
        timestamp: str,
        *,
        reason: str | None = None,
        force: bool = False,
    ) -> None:
        """Internal transition; validates the state machine unless ``force`` is set."""
        if not force and transition_path(row["status"], status) is None:
            raise ValueError(f"illegal transition {row['status']} -> {status} for candidate {row['id']}")
        updates: list[str] = ["status=?", "updated_at=?"]
        params: list[Any] = [status, timestamp]
        if reason is not None:
            updates.append("failure_reason=?")
            params.append(reason)
        if status in ("ACTIVE", "REJECTED"):
            updates.extend(["worker_id=NULL", "lease_until=NULL"])
        params.append(row["id"])
        conn.execute(f"UPDATE candidates SET {', '.join(updates)} WHERE id=?", params)
        self.log_event(
            "candidate", row["id"], "status", from_status=row["status"], to_status=status,
            payload={"reason": reason} if reason else None, conn=conn,
        )

    def _requeue(
        self,
        conn: sqlite3.Connection,
        candidate: sqlite3.Row | None,
        simulation: sqlite3.Row | None,
        timestamp: str,
        *,
        reason: str,
    ) -> QueueOutcome:
        if candidate is None:
            return QueueOutcome("in_flight", simulation["canonical_key"] if simulation else "", None, None)
        self._set_status(conn, candidate, "QUEUED", timestamp, reason=None)
        conn.execute("UPDATE candidates SET worker_id=NULL, lease_until=NULL WHERE id=?", (candidate["id"],))
        if simulation is not None:
            conn.execute(
                "UPDATE simulations SET status='QUEUED', worker_id=NULL, updated_at=? WHERE canonical_key=?",
                (timestamp, candidate["canonical_key"]),
            )
        self.log_event(
            "candidate", candidate["id"], "lease_recovered", from_status=candidate["status"], to_status="QUEUED",
            payload={"reason": reason}, conn=conn,
        )
        return QueueOutcome("requeued", candidate["canonical_key"], candidate["id"], "QUEUED")

    # -- simulation cache and results --------------------------------------

    def cache_lookup(self, expression: str, settings: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        """Cached metrics for an exact completed request, or None."""
        key = canonical.canonical_key(expression, settings)
        row = self._conn.execute(
            "SELECT * FROM simulations WHERE canonical_key=? AND status='DONE'", (key,)
        ).fetchone()
        return self._cached_result(row) if row else None

    @staticmethod
    def _cached_result(simulation: sqlite3.Row) -> dict[str, Any]:
        payload = dict(simulation)
        payload["checks"] = json.loads(payload["checks_json"]) if payload.get("checks_json") else []
        return payload

    def _upsert_simulation(
        self,
        conn: sqlite3.Connection,
        key: str,
        candidate_id: int | None,
        expression: str,
        normalized_expression: str,
        settings_json: str,
        status: str,
        timestamp: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO simulations(
                canonical_key, candidate_id, expression, normalized_expression, settings_json,
                status, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(canonical_key) DO UPDATE SET
                candidate_id=COALESCE(excluded.candidate_id, simulations.candidate_id),
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (key, candidate_id, expression, normalized_expression, settings_json, status, timestamp, timestamp),
        )

    def claim_simulation(
        self, worker_id: str, lease_seconds: float = 1800.0, candidate_id: int | None = None
    ) -> dict[str, Any] | None:
        """Atomically lease a QUEUED candidate (a specific one, else highest priority)."""
        timestamp = now_iso()
        with self._tx() as conn:
            if candidate_id is None:
                row = conn.execute(
                    """
                    SELECT * FROM candidates
                    WHERE status='QUEUED' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY priority DESC, id ASC LIMIT 1
                    """,
                    (timestamp,),
                ).fetchone()
            else:
                row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
                if row is not None and (
                    row["status"] != "QUEUED"
                    or (row["next_attempt_at"] is not None and row["next_attempt_at"] > timestamp)
                ):
                    row = None
            if row is None:
                return None
            attempt = int(row["attempt_count"]) + 1
            cursor = conn.execute(
                """
                UPDATE candidates SET status='SIMULATING', worker_id=?, lease_until=?, attempt_count=?,
                       next_attempt_at=NULL, updated_at=?
                WHERE id=? AND status='QUEUED'
                """,
                (worker_id, plus_seconds_iso(lease_seconds), attempt, timestamp, row["id"]),
            )
            if cursor.rowcount != 1:  # lost the race to another worker
                return None
            conn.execute(
                "UPDATE simulations SET status='RUNNING', attempts=?, worker_id=?, updated_at=? WHERE canonical_key=?",
                (attempt, worker_id, timestamp, row["canonical_key"]),
            )
            self.log_event(
                "candidate", row["id"], "simulation_claimed", from_status="QUEUED", to_status="SIMULATING",
                payload={"worker_id": worker_id, "attempt": attempt}, conn=conn,
            )
            return self.get_candidate(int(row["id"]))

    def mark_simulation_started(
        self,
        candidate_id: int,
        simulation_id: str,
        *,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> None:
        """Remember the BRAIN simulation id so a restarted worker can keep polling it."""
        timestamp = now_iso()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if row is None:
                raise KeyError(f"candidate {candidate_id} not found")
            updates = ["simulation_id=?", "updated_at=?"]
            params: list[Any] = [simulation_id, timestamp]
            if worker_id is not None:
                updates.append("worker_id=?")
                params.append(worker_id)
            if lease_seconds is not None:
                updates.append("lease_until=?")
                params.append(plus_seconds_iso(lease_seconds))
            params.append(candidate_id)
            conn.execute(f"UPDATE candidates SET {', '.join(updates)} WHERE id=?", params)
            conn.execute(
                "UPDATE simulations SET simulation_id=?, status='RUNNING', worker_id=COALESCE(?, worker_id), updated_at=? "
                "WHERE canonical_key=?",
                (simulation_id, worker_id, timestamp, row["canonical_key"]),
            )
            self.log_event("candidate", candidate_id, "simulation_started", to_status="SIMULATING",
                           payload={"simulation_id": simulation_id}, conn=conn)

    def release_claim(self, candidate_id: int, reason: str = "released") -> None:
        """Give a claimed-but-not-submitted candidate back to the queue (429, interrupt)."""
        timestamp = now_iso()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if row is None or row["status"] != "SIMULATING":
                return
            conn.execute(
                "UPDATE candidates SET worker_id=NULL, lease_until=NULL, updated_at=? WHERE id=?",
                (timestamp, candidate_id),
            )
            self._set_status(conn, row, "QUEUED", timestamp)
            conn.execute(
                "UPDATE simulations SET status='QUEUED', worker_id=NULL, updated_at=? WHERE canonical_key=?",
                (timestamp, row["canonical_key"]),
            )
            self.log_event("candidate", candidate_id, "claim_released", from_status="SIMULATING",
                           to_status="QUEUED", payload={"reason": reason}, conn=conn)

    def requeue_due_retries(self, max_attempts: int = 3) -> list[int]:
        """Move RETRY candidates whose backoff window elapsed back to QUEUED.

        Turns a transient BRAIN failure into work the scheduler picks up again, while
        a candidate that keeps failing stops consuming slots (TODO P2 retry policy).
        """
        timestamp = now_iso()
        requeued: list[int] = []
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT c.* FROM candidates c JOIN simulations s ON s.canonical_key = c.canonical_key
                WHERE c.status='RETRY' AND (c.next_attempt_at IS NULL OR c.next_attempt_at <= ?)
                      AND s.status <> 'DONE' AND c.attempt_count < ?
                ORDER BY c.id
                """,
                (timestamp, max_attempts),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE candidates SET worker_id=NULL, lease_until=NULL, updated_at=? WHERE id=?",
                    (timestamp, row["id"]),
                )
                self._set_status(conn, row, "QUEUED", timestamp)
                conn.execute(
                    "UPDATE simulations SET status='QUEUED', updated_at=? WHERE canonical_key=?",
                    (timestamp, row["canonical_key"]),
                )
                requeued.append(int(row["id"]))
        return requeued

    def list_queued(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Candidates waiting for a simulation slot, priority-first."""
        sql = "SELECT * FROM candidates WHERE status='QUEUED' ORDER BY priority DESC, id ASC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [dict(row) for row in self._conn.execute(sql)]

    def running_simulations(self) -> list[dict[str, Any]]:
        """Leased simulations, with the BRAIN handle needed to resume polling them."""
        rows = self._conn.execute(
            """
            SELECT c.id, c.canonical_key, c.expression, c.normalized_expression, c.simulation_id,
                   c.worker_id, c.lease_until, c.attempt_count, c.settings_json, c.priority
            FROM candidates c JOIN simulations s ON s.canonical_key = c.canonical_key
            WHERE c.status='SIMULATING' AND s.simulation_id IS NOT NULL
            ORDER BY c.id
            """
        )
        return [dict(row) for row in rows]

    def record_ranking(
        self,
        candidate_id: int,
        components: Mapping[str, float],
        priority: float,
        reasons: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist the ranking components (TODO P10), not only the final score."""
        timestamp = now_iso()
        with self._tx() as conn:
            conn.execute(
                "UPDATE candidates SET priority=?, expected_quality=?, novelty_score=?, failure_risk=?, updated_at=? "
                "WHERE id=?",
                (
                    priority,
                    components.get("expected_quality"),
                    components.get("novelty"),
                    components.get("failure_risk"),
                    timestamp,
                    candidate_id,
                ),
            )
            self.log_event("candidate", candidate_id, "ranked", payload={**components, "priority": priority,
                                                                          "reasons": reasons or {}}, conn=conn)

    def record_simulation_result(
        self,
        *,
        candidate_id: int | None = None,
        canonical_key: str | None = None,
        status: str = "DONE",
        metrics: Mapping[str, Any] | None = None,
        checks: Iterable[Mapping[str, Any]] | None = None,
        brain_alpha_id: str | None = None,
        simulation_id: str | None = None,
        error: str | None = None,
        retry_delay_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """Persist one finished simulation; DONE results are cached and IS-gated.

        Called the moment a result lands so an interrupt cannot lose it (TODO P0).
        """
        if status not in SIMULATION_STATUSES:
            raise ValueError(f"unknown simulation status: {status}")
        metrics = dict(metrics or {})
        check_list = [c for c in (checks or []) if isinstance(c, Mapping)]
        timestamp = now_iso()
        if candidate_id is None and canonical_key is None:
            raise ValueError("record_simulation_result needs candidate_id or canonical_key")

        with self._tx() as conn:
            row = None
            if candidate_id is not None:
                row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if row is None and canonical_key is not None:
                row = conn.execute("SELECT * FROM candidates WHERE canonical_key=?", (canonical_key,)).fetchone()
            if row is None:
                raise KeyError(f"candidate not found (id={candidate_id}, key={canonical_key})")
            key = row["canonical_key"]

            passed, reason = (False, error or "simulation error")
            if status == "DONE":
                passed, reason = is_gate(metrics, check_list)

            conn.execute(
                """
                UPDATE simulations SET status=?, brain_alpha_id=?, simulation_id=?, sharpe=?, fitness=?,
                       turnover=?, drawdown=?, is_pass=?, checks_json=?, error=?, updated_at=?,
                       completed_at=CASE WHEN ?='DONE' THEN ? ELSE completed_at END
                WHERE canonical_key=?
                """,
                (
                    status, brain_alpha_id, simulation_id, metrics.get("sharpe"), metrics.get("fitness"),
                    metrics.get("turnover"), metrics.get("drawdown"), int(passed) if status == "DONE" else None,
                    json.dumps(check_list) if check_list else None, error, timestamp, status, timestamp, key,
                ),
            )
            if status == "DONE":
                conn.execute(
                    """
                    UPDATE candidates SET brain_alpha_id=?, simulation_id=?, sharpe=?, fitness=?, turnover=?,
                           drawdown=?, is_pass=?, attempt_count=?, worker_id=NULL, lease_until=NULL,
                           next_attempt_at=NULL, updated_at=? WHERE id=?
                    """,
                    (
                        brain_alpha_id, simulation_id, metrics.get("sharpe"), metrics.get("fitness"),
                        metrics.get("turnover"), metrics.get("drawdown"), int(passed),
                        max(int(row["attempt_count"]), 1), timestamp, row["id"],
                    ),
                )
                self._set_status(conn, row, "IS_PASS" if passed else "REJECTED", timestamp, reason=reason or None)
            else:
                conn.execute(
                    """
                    UPDATE candidates SET failure_reason=?, worker_id=NULL, lease_until=NULL,
                           next_attempt_at=?, updated_at=? WHERE id=?
                    """,
                    (error, plus_seconds_iso(retry_delay_seconds) if retry_delay_seconds else None, timestamp, row["id"]),
                )
                self._set_status(conn, row, "RETRY", timestamp, reason=error)

            self.log_event(
                "simulation", row["id"], "result", from_status="RUNNING", to_status=status,
                payload={
                    "is_pass": bool(passed) if status == "DONE" else None,
                    "reason": reason or None,
                    "sharpe": metrics.get("sharpe"),
                    "fitness": metrics.get("fitness"),
                    "turnover": metrics.get("turnover"),
                    "brain_alpha_id": brain_alpha_id,
                },
                conn=conn,
            )
            return self.get_candidate(int(row["id"]))

    def recover_expired_leases(self, lease_seconds: float | None = None) -> dict[str, int]:
        """Reclaim work left behind by a killed process (TODO P0/P8).

        An expired SIMULATING/SUBMITTING lease becomes RETRY, never an automatic
        resubmission: BRAIN state must be reconciled first (TODO P8).
        """
        reference = now_iso()
        recovered = {"simulations": 0, "submissions": 0}
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE status='SIMULATING' AND (lease_until IS NULL OR lease_until <= ?)",
                (reference,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE candidates SET next_attempt_at=?, worker_id=NULL, lease_until=NULL, updated_at=? WHERE id=?",
                    (reference if lease_seconds is None else plus_seconds_iso(lease_seconds), reference, row["id"]),
                )
                self._set_status(conn, row, "RETRY", reference, reason="lease_expired")
                conn.execute(
                    "UPDATE simulations SET status='ERROR', error='lease_expired', updated_at=? WHERE canonical_key=?",
                    (reference, row["canonical_key"]),
                )
                recovered["simulations"] += 1

            submissions = conn.execute(
                "SELECT * FROM submissions WHERE status='SUBMITTING' AND (lease_until IS NULL OR lease_until <= ?)",
                (reference,),
            ).fetchall()
            for submission in submissions:
                conn.execute(
                    "UPDATE submissions SET status='RETRY', worker_id=NULL, lease_until=NULL, "
                    "message='lease_expired', updated_at=? WHERE id=?",
                    (reference, submission["id"]),
                )
                self.log_event(
                    "submission", submission["id"], "lease_expired", from_status="SUBMITTING", to_status="RETRY",
                    conn=conn,
                )
                recovered["submissions"] += 1
        return recovered

    # -- submissions (TODO P6/P8 build on these primitives) ----------------

    def enqueue_submission(self, candidate_id: int, *, priority: float | None = None) -> int:
        """Queue a candidate for submission; reuses the open row instead of duplicating it."""
        timestamp = now_iso()
        with self._tx() as conn:
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if candidate is None:
                raise KeyError(f"candidate {candidate_id} not found")
            if candidate["status"] != "SUBMISSION_READY":
                self._set_status(conn, candidate, "SUBMISSION_READY", timestamp)
            open_row = conn.execute(
                "SELECT id FROM submissions WHERE candidate_id=? AND status NOT IN ('ACTIVE','SELF_CORR_FAIL','PLATFORM_REJECTED') "
                "ORDER BY id DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
            if open_row is not None:
                conn.execute(
                    "UPDATE submissions SET status='READY', priority=?, updated_at=? WHERE id=?",
                    (candidate["priority"] if priority is None else priority, timestamp, open_row["id"]),
                )
                return int(open_row["id"])
            cursor = conn.execute(
                """
                INSERT INTO submissions(candidate_id, brain_alpha_id, status, priority, created_at, updated_at)
                VALUES(?,?, 'READY', ?, ?, ?)
                """,
                (candidate_id, candidate["brain_alpha_id"], candidate["priority"] if priority is None else priority,
                 timestamp, timestamp),
            )
            submission_id = int(cursor.lastrowid)
            self.log_event("submission", submission_id, "ready", to_status="READY",
                           payload={"candidate_id": candidate_id}, conn=conn)
            return submission_id

    def claim_submission(self, worker_id: str, lease_seconds: float = 1800.0) -> dict[str, Any] | None:
        """Lease the highest-priority READY submission (the P8 anti-duplicate lock)."""
        timestamp = now_iso()
        with self._tx() as conn:
            row = conn.execute(
                """
                SELECT s.*, c.canonical_key, c.brain_alpha_id AS candidate_alpha_id
                FROM submissions s JOIN candidates c ON c.id = s.candidate_id
                WHERE s.status='READY' AND (s.next_attempt_at IS NULL OR s.next_attempt_at <= ?)
                ORDER BY s.priority DESC, s.id ASC LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                "UPDATE submissions SET status='SUBMITTING', worker_id=?, lease_until=?, attempt=attempt+1, "
                "updated_at=? WHERE id=? AND status='READY'",
                (worker_id, plus_seconds_iso(lease_seconds), timestamp, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            self.log_event(
                "submission", row["id"], "claimed", from_status="READY", to_status="SUBMITTING",
                payload={"worker_id": worker_id}, conn=conn,
            )
            return dict(conn.execute("SELECT * FROM submissions WHERE id=?", (row["id"],)).fetchone())

    def finish_submission(
        self,
        submission_id: int,
        status: str,
        *,
        message: str | None = None,
        max_corr: float | None = None,
        max_corr_alpha_id: str | None = None,
        brain_alpha_id: str | None = None,
    ) -> None:
        """Record a submission outcome and mirror terminal states onto the candidate."""
        if status not in SUBMISSION_STATUSES:
            raise ValueError(f"unknown submission status: {status}")
        timestamp = now_iso()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            if row is None:
                raise KeyError(f"submission {submission_id} not found")
            conn.execute(
                """
                UPDATE submissions SET status=?, message=?, max_corr=COALESCE(?, max_corr),
                       max_corr_alpha_id=COALESCE(?, max_corr_alpha_id), brain_alpha_id=COALESCE(?, brain_alpha_id),
                       worker_id=NULL, lease_until=NULL, updated_at=? WHERE id=?
                """,
                (status, message, max_corr, max_corr_alpha_id, brain_alpha_id, timestamp, submission_id),
            )
            self.log_event("submission", submission_id, "result", from_status=row["status"], to_status=status,
                           payload={"message": message, "max_corr": max_corr}, conn=conn)

            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
            if candidate is None:
                return
            target = {
                "ACTIVE": "ACTIVE",
                "SELF_CORR_FAIL": "REJECTED",
                "PLATFORM_REJECTED": "REJECTED",
                "RETRY": "RETRY",
                "READY": "SUBMISSION_READY",
                "CHECK_PENDING": "SUBMITTING",
            }.get(status)
            if target is None or candidate["status"] == target:
                return
            if transition_path(candidate["status"], target) is None:
                return
            self._set_status(conn, candidate, target, timestamp, reason=message)
            if status == "ACTIVE" and brain_alpha_id:
                self.upsert_active_alpha(brain_alpha_id, conn=conn)

    # -- ACTIVE portfolio snapshot (TODO P9) -------------------------------

    def upsert_active_alpha(
        self,
        brain_alpha_id: str,
        *,
        expression: str | None = None,
        settings: Mapping[str, Any] | None = None,
        sharpe: float | None = None,
        fitness: float | None = None,
        turnover: float | None = None,
        pnl_ref: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Track an ACTIVE alpha so correlation re-checks know when the set changed."""
        timestamp = now_iso()
        owned = conn is None
        conn = conn or self._conn
        if owned:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO active_alphas(brain_alpha_id, canonical_key, expression_hash, settings_hash, sharpe,
                       fitness, turnover, pnl_ref, first_seen_at, last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(brain_alpha_id) DO UPDATE SET
                    canonical_key=COALESCE(excluded.canonical_key, active_alphas.canonical_key),
                    sharpe=COALESCE(excluded.sharpe, active_alphas.sharpe),
                    fitness=COALESCE(excluded.fitness, active_alphas.fitness),
                    turnover=COALESCE(excluded.turnover, active_alphas.turnover),
                    pnl_ref=COALESCE(excluded.pnl_ref, active_alphas.pnl_ref),
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    brain_alpha_id,
                    canonical.canonical_key(expression, settings) if expression else None,
                    canonical.expression_hash(expression) if expression else None,
                    canonical.settings_hash(settings) if expression else None,
                    sharpe, fitness, turnover, pnl_ref, timestamp, timestamp,
                ),
            )
            if owned:
                conn.execute("COMMIT")
        except BaseException:
            if owned:
                conn.execute("ROLLBACK")
            raise

    def active_alpha_ids(self) -> list[str]:
        return [row["brain_alpha_id"] for row in self._conn.execute(
            "SELECT brain_alpha_id FROM active_alphas ORDER BY brain_alpha_id"
        )]

    def record_correlation_check(self, candidate_id: int, max_corr: float, max_corr_alpha_id: str | None) -> None:
        """Store the local self-correlation result for a candidate (TODO P9)."""
        timestamp = now_iso()
        with self._tx() as conn:
            conn.execute(
                "UPDATE candidates SET self_corr=?, updated_at=? WHERE id=?", (max_corr, timestamp, candidate_id)
            )
            self.log_event("candidate", candidate_id, "correlation_checked",
                           payload={"max_corr": max_corr, "max_corr_alpha_id": max_corr_alpha_id}, conn=conn)

    # -- meta (capabilities, schema facts) ---------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # -- generic read access -----------------------------------------------

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        """Read-only helper for aggregations (ranking, observability)."""
        return [dict(row) for row in self._conn.execute(sql, tuple(params))]

    # -- observability -----------------------------------------------------

    def counts(self, table: str, column: str = "status") -> dict[str, int]:
        if table not in ("candidates", "simulations", "submissions"):
            raise ValueError(f"unknown table: {table}")
        if column not in ("status",):
            raise ValueError(f"unknown column: {column}")
        rows = self._conn.execute(f"SELECT {column} AS value, COUNT(*) AS n FROM {table} GROUP BY {column}")
        return {str(row["value"]): int(row["n"]) for row in rows}

    def stats(self, *, window_hours: float = 24.0) -> StatusReport:
        """Throughput snapshot (TODO P13): counts plus generated/simulated per hour."""
        since = plus_seconds_iso(-3600.0 * window_hours)
        report = StatusReport(
            candidates=self.counts("candidates"),
            simulations=self.counts("simulations"),
            submissions=self.counts("submissions"),
            cache_hits=int(self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE event='cache_hit'"
            ).fetchone()["n"]),
            active_alphas=int(self._conn.execute("SELECT COUNT(*) AS n FROM active_alphas").fetchone()["n"]),
            events=int(self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]),
        )
        hours = max(window_hours, 1e-6)
        report.generated_per_hour = round(self._conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE created_at >= ?", (since,)
        ).fetchone()["n"] / hours, 3)
        report.simulated_per_hour = round(self._conn.execute(
            "SELECT COUNT(*) AS n FROM simulations WHERE completed_at IS NOT NULL AND completed_at >= ?", (since,)
        ).fetchone()["n"] / hours, 3)
        return report


def resolve_db_path(path: str | Path | None = None) -> Path:
    """Explicit path > ``WQ_RESEARCH_DB`` > <repo root>/research.db."""
    if path:
        return Path(path)
    return Path(os.environ.get(DB_ENV_VAR) or DEFAULT_DB_PATH)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_rows(input_path: Path) -> list[dict[str, str]]:
    with input_path.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if (row.get("code") or "").strip()]


def cmd_init(db: ResearchDB, _args: argparse.Namespace) -> int:
    print(f"research.db ready at {db.path} (schema v{SCHEMA_VERSION})")
    return 0


def cmd_status(db: ResearchDB, _args: argparse.Namespace) -> int:
    report = db.stats()
    payload = {"db": str(db.path), **report.as_dict()}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_queue(db: ResearchDB, args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"input CSV not found: {input_path}", file=sys.stderr)
        return 1
    outcomes: dict[str, int] = {}
    for row in _load_rows(input_path):
        try:
            settings = settings_from_row(row)
        except ValueError as exc:
            outcomes["bad_settings"] = outcomes.get("bad_settings", 0) + 1
            print(f"SKIPPED {row['code'][:60]!r}: {exc}", file=sys.stderr)
            continue
        outcome = db.queue_candidate(
            row["code"],
            settings,
            source=row.get("source") or input_path.name,
            signal_family=row.get("signal_family") or None,
            priority=float(row.get("priority") or 0.0),
        )
        outcomes[outcome.action] = outcomes.get(outcome.action, 0) + 1
    print(json.dumps({"input": str(input_path), "outcomes": outcomes}, indent=2, sort_keys=True))
    return 0


def cmd_cache(db: ResearchDB, args: argparse.Namespace) -> int:
    cached = db.cache_lookup(args.expression, settings_from_row(vars(args)))
    print(json.dumps(cached or {}, indent=2, sort_keys=True, default=str))
    return 0 if cached else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help=f"path to research.db (default: ${DB_ENV_VAR} or <repo root>/research.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create/upgrade the schema").set_defaults(func=cmd_init)
    sub.add_parser("status", help="print queue/cache counters as JSON").set_defaults(func=cmd_status)

    queue = sub.add_parser("queue", help="normalize + dedup + queue candidates from a CSV")
    queue.add_argument("input", help="CSV with a code column and optional settings columns")
    queue.set_defaults(func=cmd_queue)

    cache = sub.add_parser("cache", help="print the cached result for an exact request (exit 1 on miss)")
    cache.add_argument("expression")
    for column in SETTINGS_COLUMNS:
        cache.add_argument(f"--{column}")
    cache.set_defaults(func=cmd_cache)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with ResearchDB.open(args.db) as db:
        return int(args.func(db, args))


if __name__ == "__main__":
    sys.exit(main())
