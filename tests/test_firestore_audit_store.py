"""Tests for FirestoreAuditStore.

Two layers:
1. Serialization round-trip (unit, no Firestore needed) — verifies the
   AuditEvent <-> Firestore document conversion is lossless.
2. End-to-end contract tests against the Firestore Emulator — skipped
   unless FIRESTORE_EMULATOR_HOST is set. To enable locally:

       gcloud emulators firestore start --host-port=localhost:8086
       export FIRESTORE_EMULATOR_HOST=localhost:8086   # (or set FIRESTORE_EMULATOR_HOST=localhost:8086 on Windows)
       pytest tests/test_firestore_audit_store.py
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from services.audit import (
    AuditAction,
    AuditEvent,
    AuditQuery,
    FirestoreAuditStore,
    HashChainViolation,
    verify_chain,
)


def _event(
    *,
    action: AuditAction = AuditAction.SOURCE_INGESTED,
    project_id: str = "pytest-project",
    actor_id: str = "system:test",
    notes: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> AuditEvent:
    return AuditEvent(
        action=action,
        tenant_id="gsk",
        project_id=project_id,
        mode="rd",
        actor_id=actor_id,
        target_type="document",
        target_id=str(uuid.uuid4()),
        timestamp_utc=datetime.now(timezone.utc),
        notes=notes or [],
        extra=extra or {},
    )


# --- Serialization (unit) --------------------------------------------------


def test_event_to_doc_includes_chain_helpers() -> None:
    from services.audit.firestore_store import _event_to_doc

    event = _event(notes=["one"], extra={"input_tokens": 42})
    doc = _event_to_doc(event, insertion_order=7)
    assert doc["action"] == "source_ingested"
    assert doc["_project_chain"] == event.project_id
    assert doc["_insertion_order"] == 7
    assert doc["notes"] == ["one"]
    assert doc["extra"]["input_tokens"] == 42


def test_doc_to_event_round_trips() -> None:
    from services.audit.firestore_store import _doc_to_event, _event_to_doc

    event = _event(extra={"input_tokens": 42})
    doc = _event_to_doc(event, insertion_order=1)
    reloaded = _doc_to_event(doc)
    assert reloaded.event_id == event.event_id
    assert reloaded.action == event.action
    assert reloaded.tenant_id == event.tenant_id
    assert reloaded.actor_id == event.actor_id


def test_doc_to_event_handles_string_action() -> None:
    """Defensive: if a document is loaded with action as a plain string
    (e.g. mid-migration), we must still parse it."""
    from services.audit.firestore_store import _doc_to_event

    base_event = _event()
    payload = base_event.model_dump(mode="python")
    payload["action"] = "source_ingested"  # already a string in this dump
    reloaded = _doc_to_event(payload)
    assert reloaded.action == AuditAction.SOURCE_INGESTED


# --- Emulator-backed contract tests ----------------------------------------


_HAS_EMULATOR = bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))

skip_no_emulator = pytest.mark.skipif(
    not _HAS_EMULATOR,
    reason=(
        "Set FIRESTORE_EMULATOR_HOST and start `gcloud emulators firestore "
        "start` to run these tests."
    ),
)


@pytest.fixture
def isolated_store() -> FirestoreAuditStore:
    """A FirestoreAuditStore writing to unique collections per test so
    parallel test runs don't collide in the emulator."""
    assert FirestoreAuditStore is not None
    suffix = uuid.uuid4().hex[:8]
    return FirestoreAuditStore(
        project_id="audit-store-test",
        events_collection=f"audit_events_{suffix}",
        chain_state_collection=f"audit_chain_state_{suffix}",
    )


@skip_no_emulator
def test_firestore_append_links_chain(isolated_store: FirestoreAuditStore) -> None:
    e1 = isolated_store.append(_event(project_id="p1"))
    e2 = isolated_store.append(_event(project_id="p1"))
    e3 = isolated_store.append(_event(project_id="p1"))

    assert e1.prev_event_hash is None
    assert e2.prev_event_hash == e1.this_event_hash
    assert e3.prev_event_hash == e2.this_event_hash
    chain = list(isolated_store.query(AuditQuery(project_id="p1")))
    assert verify_chain(chain) == 3


@skip_no_emulator
def test_firestore_chains_are_per_project(
    isolated_store: FirestoreAuditStore,
) -> None:
    a = isolated_store.append(_event(project_id="proj-A"))
    b = isolated_store.append(_event(project_id="proj-B"))
    a2 = isolated_store.append(_event(project_id="proj-A"))

    assert a.prev_event_hash is None
    assert b.prev_event_hash is None
    assert a2.prev_event_hash == a.this_event_hash


@skip_no_emulator
def test_firestore_rejects_duplicate_event_id(
    isolated_store: FirestoreAuditStore,
) -> None:
    event = _event(project_id="dup-test")
    isolated_store.append(event)
    with pytest.raises(HashChainViolation):
        isolated_store.append(event)


@skip_no_emulator
def test_firestore_latest_hash_matches_chain_tail(
    isolated_store: FirestoreAuditStore,
) -> None:
    events = [
        isolated_store.append(_event(project_id="lh-test")) for _ in range(5)
    ]
    assert isolated_store.latest_hash("lh-test") == events[-1].this_event_hash


@skip_no_emulator
def test_firestore_query_filters_by_action(
    isolated_store: FirestoreAuditStore,
) -> None:
    isolated_store.append(_event(action=AuditAction.SOURCE_INGESTED))
    isolated_store.append(_event(action=AuditAction.LLM_CALL))
    isolated_store.append(_event(action=AuditAction.LLM_CALL))

    llm = list(isolated_store.query(AuditQuery(action=AuditAction.LLM_CALL)))
    assert len(llm) == 2
