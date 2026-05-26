"""AuditStore protocol + InMemory and SQLite implementations.

The store is INSERT-only. UPDATE and DELETE are not exposed — that's the
audit immutability requirement. To "fix" a wrong record you append a
correcting event, you don't mutate the bad one.

Stores compute and stamp the hash chain on insert: they read the latest
event for the project_id, set the new event's prev_event_hash from it,
compute this_event_hash, and write. Single-writer assumption for the
PoC; production needs optimistic concurrency on the (project_id, prev_event_hash)
pair.

An AuditSink is a thin wrapper that lets multiple consumers (the
orchestrator, the LLM client, the citation service) send events without
caring which backend is behind. It records the assigned hashes back onto
the event so downstream observers can correlate.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from services.audit.hash_chain import HashChainViolation, canonical_event_hash
from services.audit.schema import AuditAction, AuditEvent

# Re-export so callers can `from services.audit.store import HashChainViolation`
# (kept for backward-compat with the public API exported via __init__.py)
__all__ = [
    "AuditQuery",
    "AuditSink",
    "AuditStore",
    "HashChainViolation",
    "InMemoryAuditStore",
    "SqliteAuditStore",
]


@dataclass(frozen=True)
class AuditQuery:
    """Read-time query — kept narrow for PoC. Production Firestore queries
    will mirror these dimensions."""

    project_id: str | None = None
    tenant_id: str | None = None
    actor_id: str | None = None
    action: AuditAction | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int | None = None


class AuditStore(Protocol):
    """The persistence interface. INSERT-only.

    Stores must:
    - Reject duplicate event_ids
    - Compute prev_event_hash + this_event_hash on insert
    - Preserve insertion order per project_id
    - Return events in insertion order from `query`
    """

    def append(self, event: AuditEvent) -> AuditEvent:
        """Persist the event. Returns the event with prev/this hashes filled in."""
        ...

    def latest_hash(self, project_id: str) -> str | None:
        """Return the this_event_hash of the most recent event for project_id,
        or None if no events exist."""
        ...

    def query(self, query: AuditQuery) -> Iterator[AuditEvent]:
        """Iterate events matching the query, in insertion order."""
        ...

    def count(self, query: AuditQuery) -> int:
        ...


class AuditSink:
    """Convenience wrapper that emits events.

    Callers build an AuditEvent (without prev/this hashes) and pass it to
    `emit`; the sink delegates to its underlying store, which stamps the
    chain hashes. The returned event has the hashes populated, useful for
    callers that want to log/correlate.
    """

    def __init__(self, store: AuditStore) -> None:
        self._store = store

    def emit(self, event: AuditEvent) -> AuditEvent:
        return self._store.append(event)


# --- In-memory store --------------------------------------------------------


class InMemoryAuditStore(AuditStore):
    """For unit tests. Per-project chain is tracked in a simple list."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._by_project: dict[str, list[AuditEvent]] = {}
        self._seen_ids: set[str] = set()
        self._lock = threading.Lock()

    def append(self, event: AuditEvent) -> AuditEvent:
        with self._lock:
            if event.event_id in self._seen_ids:
                raise HashChainViolation(
                    f"duplicate event_id={event.event_id!r}"
                )
            prev_hash = self.latest_hash(event.project_id)
            stamped = event.model_copy(
                update={"prev_event_hash": prev_hash, "this_event_hash": None}
            )
            stamped = stamped.model_copy(
                update={"this_event_hash": canonical_event_hash(stamped, prev_hash=prev_hash)}
            )
            self._events.append(stamped)
            self._by_project.setdefault(event.project_id, []).append(stamped)
            self._seen_ids.add(event.event_id)
            return stamped

    def latest_hash(self, project_id: str) -> str | None:
        chain = self._by_project.get(project_id)
        if not chain:
            return None
        return chain[-1].this_event_hash

    def query(self, query: AuditQuery) -> Iterator[AuditEvent]:
        results = self._events
        if query.project_id is not None:
            results = [e for e in results if e.project_id == query.project_id]
        if query.tenant_id is not None:
            results = [e for e in results if e.tenant_id == query.tenant_id]
        if query.actor_id is not None:
            results = [e for e in results if e.actor_id == query.actor_id]
        if query.action is not None:
            results = [e for e in results if e.action == query.action]
        if query.since is not None:
            results = [e for e in results if e.timestamp_utc >= query.since]
        if query.until is not None:
            results = [e for e in results if e.timestamp_utc <= query.until]
        if query.limit is not None:
            results = results[: query.limit]
        return iter(results)

    def count(self, query: AuditQuery) -> int:
        return sum(1 for _ in self.query(query))


# --- SQLite store -----------------------------------------------------------


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_events (
    event_id            TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    mode                TEXT NOT NULL,
    actor_id            TEXT NOT NULL,
    actor_role          TEXT,
    actor_auth_method   TEXT,
    action              TEXT NOT NULL,
    target_type         TEXT NOT NULL,
    target_id           TEXT NOT NULL,
    target_version      TEXT,
    timestamp_utc       TEXT NOT NULL,
    client_ip           TEXT,
    user_agent          TEXT,
    reason              TEXT,
    before_hash         TEXT,
    after_hash          TEXT,
    payload_ref         TEXT,
    prev_event_hash     TEXT,
    this_event_hash     TEXT NOT NULL,
    signature           TEXT,
    notes_json          TEXT NOT NULL DEFAULT '[]',
    extra_json          TEXT NOT NULL DEFAULT '{}',
    insertion_order     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_project_order
    ON audit_events(project_id, insertion_order);
CREATE INDEX IF NOT EXISTS idx_audit_action_ts
    ON audit_events(action, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_audit_actor_ts
    ON audit_events(actor_id, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_audit_project_ts
    ON audit_events(project_id, timestamp_utc);
"""


_COLUMNS = (
    "event_id",
    "tenant_id",
    "project_id",
    "mode",
    "actor_id",
    "actor_role",
    "actor_auth_method",
    "action",
    "target_type",
    "target_id",
    "target_version",
    "timestamp_utc",
    "client_ip",
    "user_agent",
    "reason",
    "before_hash",
    "after_hash",
    "payload_ref",
    "prev_event_hash",
    "this_event_hash",
    "signature",
    "notes_json",
    "extra_json",
    "insertion_order",
)


def _event_to_row(event: AuditEvent, insertion_order: int) -> tuple[object, ...]:
    return (
        event.event_id,
        event.tenant_id,
        event.project_id,
        event.mode,
        event.actor_id,
        event.actor_role,
        event.actor_auth_method,
        event.action.value,
        event.target_type,
        event.target_id,
        event.target_version,
        event.timestamp_utc.isoformat(),
        event.client_ip,
        event.user_agent,
        event.reason,
        event.before_hash,
        event.after_hash,
        event.payload_ref,
        event.prev_event_hash,
        event.this_event_hash,
        event.signature,
        json.dumps(event.notes, ensure_ascii=False),
        json.dumps(event.extra, ensure_ascii=False, default=str),
        insertion_order,
    )


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        event_id=row["event_id"],
        tenant_id=row["tenant_id"],
        project_id=row["project_id"],
        mode=row["mode"],
        actor_id=row["actor_id"],
        actor_role=row["actor_role"],
        actor_auth_method=row["actor_auth_method"],
        action=AuditAction(row["action"]),
        target_type=row["target_type"],
        target_id=row["target_id"],
        target_version=row["target_version"],
        timestamp_utc=datetime.fromisoformat(row["timestamp_utc"]),
        client_ip=row["client_ip"],
        user_agent=row["user_agent"],
        reason=row["reason"],
        before_hash=row["before_hash"],
        after_hash=row["after_hash"],
        payload_ref=row["payload_ref"],
        prev_event_hash=row["prev_event_hash"],
        this_event_hash=row["this_event_hash"],
        signature=row["signature"],
        notes=json.loads(row["notes_json"]),
        extra=json.loads(row["extra_json"]),
    )


class SqliteAuditStore(AuditStore):
    """SQLite-backed AuditStore — one file per environment.

    Schema mirrors the Firestore document shape one-to-one. Migrating to
    Firestore in production swaps this implementation; the AuditStore
    protocol stays the same.

    Single-writer assumed for the PoC. Multi-writer correctness needs
    optimistic concurrency on (project_id, latest hash) which is more
    natural in Firestore's transactions than SQLite.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA_SQL)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteAuditStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def append(self, event: AuditEvent) -> AuditEvent:
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT 1 FROM audit_events WHERE event_id = ? LIMIT 1",
                (event.event_id,),
            ).fetchone()
            if existing:
                raise HashChainViolation(
                    f"duplicate event_id={event.event_id!r}"
                )
            prev_hash = self.latest_hash(event.project_id)
            stamped = event.model_copy(
                update={"prev_event_hash": prev_hash, "this_event_hash": None}
            )
            stamped = stamped.model_copy(
                update={"this_event_hash": canonical_event_hash(stamped, prev_hash=prev_hash)}
            )
            next_order_row = self._conn.execute(
                "SELECT COALESCE(MAX(insertion_order), 0) + 1 FROM audit_events"
            ).fetchone()
            insertion_order = int(next_order_row[0])
            placeholders = ",".join(["?"] * len(_COLUMNS))
            self._conn.execute(
                f"INSERT INTO audit_events ({','.join(_COLUMNS)}) VALUES ({placeholders})",
                _event_to_row(stamped, insertion_order),
            )
            return stamped

    def latest_hash(self, project_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT this_event_hash FROM audit_events "
            "WHERE project_id = ? ORDER BY insertion_order DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return row[0] if row else None

    def query(self, query: AuditQuery) -> Iterator[AuditEvent]:
        sql, params = self._build_query_sql(query)
        cursor = self._conn.execute(sql, params)
        for row in cursor:
            yield _row_to_event(row)

    def count(self, query: AuditQuery) -> int:
        # Replace SELECT * with COUNT(*) but keep the same WHERE
        sql, params = self._build_query_sql(query, count_only=True)
        row = self._conn.execute(sql, params).fetchone()
        return int(row[0])

    def _build_query_sql(
        self, q: AuditQuery, *, count_only: bool = False
    ) -> tuple[str, tuple[object, ...]]:
        select = "COUNT(*)" if count_only else "*"
        conditions: list[str] = []
        params: list[object] = []
        if q.project_id is not None:
            conditions.append("project_id = ?")
            params.append(q.project_id)
        if q.tenant_id is not None:
            conditions.append("tenant_id = ?")
            params.append(q.tenant_id)
        if q.actor_id is not None:
            conditions.append("actor_id = ?")
            params.append(q.actor_id)
        if q.action is not None:
            conditions.append("action = ?")
            params.append(q.action.value)
        if q.since is not None:
            conditions.append("timestamp_utc >= ?")
            params.append(q.since.isoformat())
        if q.until is not None:
            conditions.append("timestamp_utc <= ?")
            params.append(q.until.isoformat())
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        order = "" if count_only else " ORDER BY insertion_order ASC"
        limit = ""
        if not count_only and q.limit is not None:
            limit = f" LIMIT {int(q.limit)}"
        return (f"SELECT {select} FROM audit_events{where}{order}{limit}", tuple(params))
