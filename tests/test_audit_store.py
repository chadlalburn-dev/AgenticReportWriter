"""Tests for the canonical AuditEvent schema, hash chain, and stores."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.audit import (
    AuditAction,
    AuditEvent,
    AuditQuery,
    AuditSink,
    HashChainViolation,
    InMemoryAuditStore,
    SqliteAuditStore,
    canonical_event_hash,
    verify_chain,
)


def _event(
    *,
    action: AuditAction = AuditAction.SOURCE_INGESTED,
    project_id: str = "p1",
    actor_id: str = "system:test",
    tenant_id: str = "gsk",
    mode: str = "rd",
    target_type: str = "document",
    target_id: str = "doc-1",
    ts: datetime | None = None,
    **kwargs: object,
) -> AuditEvent:
    return AuditEvent(
        action=action,
        project_id=project_id,
        actor_id=actor_id,
        tenant_id=tenant_id,
        mode=mode,  # type: ignore[arg-type]
        target_type=target_type,
        target_id=target_id,
        timestamp_utc=ts or datetime.now(timezone.utc),
        **kwargs,  # type: ignore[arg-type]
    )


# --- Schema basics ---------------------------------------------------------


def test_audit_event_is_frozen() -> None:
    event = _event()
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises FrozenInstanceError-like
        event.actor_id = "different"  # type: ignore[misc]


def test_audit_event_serializes_to_json() -> None:
    event = _event(notes=["one", "two"], extra={"model_version": "claude-sonnet-4-6@xx"})
    j = event.model_dump_json()
    parsed = json.loads(j)
    assert parsed["action"] == "source_ingested"
    assert parsed["notes"] == ["one", "two"]
    assert parsed["extra"]["model_version"] == "claude-sonnet-4-6@xx"


# --- Hash chain ------------------------------------------------------------


def test_canonical_hash_is_deterministic() -> None:
    e1 = _event(ts=datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc))
    h1 = canonical_event_hash(e1, prev_hash=None)
    h2 = canonical_event_hash(e1, prev_hash=None)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_canonical_hash_changes_with_prev() -> None:
    e1 = _event(ts=datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc))
    h_no_prev = canonical_event_hash(e1, prev_hash=None)
    h_with_prev = canonical_event_hash(e1, prev_hash="abc")
    assert h_no_prev != h_with_prev


def test_canonical_hash_excludes_self_hash_field() -> None:
    """Setting this_event_hash on the event should not affect canonical_event_hash —
    otherwise computing the hash would be circular."""
    base = _event()
    h_base = canonical_event_hash(base, prev_hash=None)
    with_self = base.model_copy(update={"this_event_hash": "anything"})
    h_with_self = canonical_event_hash(with_self, prev_hash=None)
    assert h_base == h_with_self


# --- InMemoryAuditStore ----------------------------------------------------


def test_inmemory_append_links_chain() -> None:
    store = InMemoryAuditStore()
    e1 = store.append(_event(project_id="p1"))
    e2 = store.append(_event(project_id="p1"))
    e3 = store.append(_event(project_id="p1"))

    assert e1.prev_event_hash is None
    assert e2.prev_event_hash == e1.this_event_hash
    assert e3.prev_event_hash == e2.this_event_hash
    assert verify_chain([e1, e2, e3]) == 3


def test_inmemory_chains_are_per_project() -> None:
    store = InMemoryAuditStore()
    a1 = store.append(_event(project_id="p1"))
    b1 = store.append(_event(project_id="p2"))
    a2 = store.append(_event(project_id="p1"))
    assert a1.prev_event_hash is None
    assert b1.prev_event_hash is None
    assert a2.prev_event_hash == a1.this_event_hash


def test_inmemory_rejects_duplicate_event_id() -> None:
    store = InMemoryAuditStore()
    e = _event()
    store.append(e)
    with pytest.raises(HashChainViolation):
        store.append(e)


def test_inmemory_query_filters() -> None:
    store = InMemoryAuditStore()
    store.append(_event(action=AuditAction.SOURCE_INGESTED))
    store.append(_event(action=AuditAction.LLM_CALL))
    store.append(_event(action=AuditAction.LLM_CALL))

    llm_calls = list(store.query(AuditQuery(action=AuditAction.LLM_CALL)))
    assert len(llm_calls) == 2

    by_actor = list(store.query(AuditQuery(actor_id="system:test")))
    assert len(by_actor) == 3


def test_inmemory_query_time_window() -> None:
    store = InMemoryAuditStore()
    base = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    store.append(_event(ts=base))
    store.append(_event(ts=base + timedelta(hours=1)))
    store.append(_event(ts=base + timedelta(hours=2)))

    middle = list(
        store.query(
            AuditQuery(
                since=base + timedelta(minutes=30),
                until=base + timedelta(hours=1, minutes=30),
            )
        )
    )
    assert len(middle) == 1


# --- SqliteAuditStore ------------------------------------------------------


def test_sqlite_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "audit.sqlite"
    with SqliteAuditStore(db) as store:
        e1 = store.append(_event(project_id="p1"))
        e2 = store.append(_event(project_id="p1"))
        assert e2.prev_event_hash == e1.this_event_hash

    with SqliteAuditStore(db) as store:
        chain = list(store.query(AuditQuery(project_id="p1")))
        assert len(chain) == 2
        assert verify_chain(chain) == 2
        # Latest hash matches across reopens
        assert store.latest_hash("p1") == chain[-1].this_event_hash


def test_sqlite_in_memory_db_works() -> None:
    """:memory: lets unit tests use the SQLite path without disk IO."""
    with SqliteAuditStore(":memory:") as store:
        e1 = store.append(_event(project_id="p1"))
        e2 = store.append(_event(project_id="p1"))
        assert verify_chain([e1, e2]) == 2


def test_sqlite_query_by_action(tmp_path: Path) -> None:
    with SqliteAuditStore(tmp_path / "audit.sqlite") as store:
        store.append(_event(action=AuditAction.SOURCE_INGESTED))
        store.append(_event(action=AuditAction.LLM_CALL))
        store.append(_event(action=AuditAction.LLM_CALL))
        store.append(_event(action=AuditAction.SIGNATURE_APPLIED))

        n = store.count(AuditQuery(action=AuditAction.LLM_CALL))
        assert n == 2
        n_total = store.count(AuditQuery())
        assert n_total == 4


def test_sqlite_chain_per_project(tmp_path: Path) -> None:
    with SqliteAuditStore(tmp_path / "audit.sqlite") as store:
        a1 = store.append(_event(project_id="p1"))
        b1 = store.append(_event(project_id="p2"))
        a2 = store.append(_event(project_id="p1"))
        b2 = store.append(_event(project_id="p2"))

        p1_chain = list(store.query(AuditQuery(project_id="p1")))
        p2_chain = list(store.query(AuditQuery(project_id="p2")))
        assert [e.event_id for e in p1_chain] == [a1.event_id, a2.event_id]
        assert [e.event_id for e in p2_chain] == [b1.event_id, b2.event_id]
        assert verify_chain(p1_chain) == 2
        assert verify_chain(p2_chain) == 2


def test_sqlite_rejects_duplicate_event_id(tmp_path: Path) -> None:
    with SqliteAuditStore(tmp_path / "audit.sqlite") as store:
        e = _event()
        store.append(e)
        with pytest.raises(HashChainViolation):
            store.append(e)


def test_sqlite_tamper_detection(tmp_path: Path) -> None:
    """If someone mutates a row out-of-band, verify_chain catches it."""
    db = tmp_path / "audit.sqlite"
    with SqliteAuditStore(db) as store:
        store.append(_event(project_id="p1", notes=["original"]))
        store.append(_event(project_id="p1"))
        store.append(_event(project_id="p1"))

    # Mutate the middle event directly via raw SQL.
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE audit_events SET notes_json = ? "
        "WHERE insertion_order = (SELECT MIN(insertion_order) FROM audit_events)",
        (json.dumps(["TAMPERED"]),),
    )
    conn.commit()
    conn.close()

    with SqliteAuditStore(db) as store:
        chain = list(store.query(AuditQuery(project_id="p1")))
        with pytest.raises(HashChainViolation):
            verify_chain(chain)


# --- AuditSink ------------------------------------------------------------


def test_audit_sink_returns_stamped_event() -> None:
    store = InMemoryAuditStore()
    sink = AuditSink(store)
    event = _event()
    stamped = sink.emit(event)
    assert stamped.this_event_hash is not None
    assert stamped.event_id == event.event_id
    assert stamped.prev_event_hash is None  # first in chain


def test_audit_sink_chains_through_multiple_emits() -> None:
    sink = AuditSink(InMemoryAuditStore())
    e1 = sink.emit(_event(project_id="p1"))
    e2 = sink.emit(_event(project_id="p1"))
    e3 = sink.emit(_event(project_id="p1"))
    assert verify_chain([e1, e2, e3]) == 3
