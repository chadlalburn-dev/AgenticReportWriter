"""FirestoreAuditStore — production swap-in for SqliteAuditStore.

Same AuditStore protocol, same hash-chain semantics, Firestore as the
backing store. The orchestrator and CLI demo don't change; you swap
SqliteAuditStore for FirestoreAuditStore at startup and everything else
just works.

Authentication: Application Default Credentials. In Cloud Run, workload
identity binds the service account automatically. Locally, run
`gcloud auth application-default login`. NEVER use a service-account
JSON key file in this project.

Concurrency: writes use a Firestore transaction so two concurrent
appenders to the same project_id chain don't race. The transaction
reads the latest event for the project, computes the new event's
hashes against that prev_event_hash, then writes — Firestore retries
the transaction if a competing write landed between read and write.

## Document layout

Collection: `audit_events`
  Document key: event_id (UUID)
  Fields: all AuditEvent fields plus
    - `_project_chain` (string): copy of project_id, used as the
      partition key for chain queries
    - `_insertion_order` (int): monotonically increasing per project

A small helper collection `audit_chain_state` keeps the next
insertion_order per project_id so we don't need to query for MAX on
every write.

## Testing path (not done in this commit)

Run `gcloud emulators firestore start --host-port=localhost:8086` and
set FIRESTORE_EMULATOR_HOST=localhost:8086 before running the
firestore-tagged tests. Tests skip when the env var is not set.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from services.audit.hash_chain import HashChainViolation, canonical_event_hash
from services.audit.schema import AuditAction, AuditEvent
from services.audit.store import AuditQuery, AuditStore

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from google.cloud import firestore  # type: ignore[import-not-found]


_EVENTS_COLLECTION = "audit_events"
_CHAIN_STATE_COLLECTION = "audit_chain_state"
# Internal helper fields stored on each event document. Prefixed `_` so the
# Firestore web UI shows them at the bottom and so they don't collide with
# canonical AuditEvent attribute names.
_PROJECT_CHAIN_FIELD = "_project_chain"
_INSERTION_ORDER_FIELD = "_insertion_order"


class FirestoreAuditStore(AuditStore):
    def __init__(
        self,
        *,
        project_id: str,
        database_id: str = "(default)",
        events_collection: str = _EVENTS_COLLECTION,
        chain_state_collection: str = _CHAIN_STATE_COLLECTION,
    ) -> None:
        """Connect to Firestore via Application Default Credentials.

        Args:
            project_id: GCP project hosting the Firestore database.
            database_id: Firestore database name; `(default)` is the
                single-database mode used by most projects.
        """
        try:
            from google.cloud import firestore  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "google-cloud-firestore is not installed. "
                "Run `pip install google-cloud-firestore>=2.16` (or install the "
                "project's [gcp] extras) before using FirestoreAuditStore."
            ) from exc

        # If the emulator host is set, the SDK picks it up automatically.
        self._emulator_host = os.environ.get("FIRESTORE_EMULATOR_HOST")
        self._client = firestore.Client(project=project_id, database=database_id)
        self._events_collection = events_collection
        self._chain_state_collection = chain_state_collection

    # -- AuditStore protocol ------------------------------------------------

    def append(self, event: AuditEvent) -> AuditEvent:
        events_ref = self._client.collection(self._events_collection)
        state_ref = self._client.collection(self._chain_state_collection).document(
            event.project_id
        )
        event_doc_ref = events_ref.document(event.event_id)

        # The transaction below is a function decorated with @transactional, so
        # define it inside append to capture self/event without globals.
        from google.cloud.firestore_v1 import transactional  # type: ignore[import-not-found]

        @transactional
        def _txn(transaction):  # type: ignore[no-untyped-def]
            existing = event_doc_ref.get(transaction=transaction)
            if existing.exists:
                raise HashChainViolation(
                    f"duplicate event_id={event.event_id!r}"
                )
            state_snapshot = state_ref.get(transaction=transaction)
            prev_hash: str | None = None
            insertion_order = 1
            if state_snapshot.exists:
                data = state_snapshot.to_dict() or {}
                prev_hash = data.get("latest_hash")
                insertion_order = int(data.get("next_insertion_order", 1))

            stamped = event.model_copy(
                update={"prev_event_hash": prev_hash, "this_event_hash": None}
            )
            stamped = stamped.model_copy(
                update={
                    "this_event_hash": canonical_event_hash(stamped, prev_hash=prev_hash)
                }
            )

            doc_body = _event_to_doc(stamped, insertion_order=insertion_order)
            transaction.set(event_doc_ref, doc_body)
            transaction.set(
                state_ref,
                {
                    "project_id": stamped.project_id,
                    "latest_hash": stamped.this_event_hash,
                    "next_insertion_order": insertion_order + 1,
                    "updated_at": stamped.timestamp_utc,
                },
                merge=True,
            )
            return stamped

        transaction = self._client.transaction()
        result: AuditEvent = _txn(transaction)
        return result

    def latest_hash(self, project_id: str) -> str | None:
        state = (
            self._client.collection(self._chain_state_collection)
            .document(project_id)
            .get()
        )
        if not state.exists:
            return None
        return (state.to_dict() or {}).get("latest_hash")

    def query(self, query: AuditQuery) -> Iterator[AuditEvent]:
        ref: Any = self._client.collection(self._events_collection)
        if query.project_id is not None:
            ref = ref.where(_PROJECT_CHAIN_FIELD, "==", query.project_id)
        if query.tenant_id is not None:
            ref = ref.where("tenant_id", "==", query.tenant_id)
        if query.actor_id is not None:
            ref = ref.where("actor_id", "==", query.actor_id)
        if query.action is not None:
            ref = ref.where("action", "==", query.action.value)
        if query.since is not None:
            ref = ref.where("timestamp_utc", ">=", query.since)
        if query.until is not None:
            ref = ref.where("timestamp_utc", "<=", query.until)

        # Order by insertion_order when scoped to a project (the chain).
        # Across-project queries fall back to timestamp ordering — Firestore
        # composite indexes are required for some of these combinations; the
        # SDK surfaces a helpful error message with an index-creation link the
        # first time you run an unsupported query.
        if query.project_id is not None:
            ref = ref.order_by(_INSERTION_ORDER_FIELD)
        else:
            ref = ref.order_by("timestamp_utc")
        if query.limit is not None:
            ref = ref.limit(int(query.limit))
        for snapshot in ref.stream():
            yield _doc_to_event(snapshot.to_dict() or {})

    def count(self, query: AuditQuery) -> int:
        # Firestore supports COUNT aggregation natively (count()), but it's
        # available only on more recent SDK versions. Fall back to streaming
        # IDs and counting client-side for portability across SDK versions.
        return sum(1 for _ in self.query(query))


# -- Serialization helpers --------------------------------------------------


def _event_to_doc(event: AuditEvent, *, insertion_order: int) -> dict[str, Any]:
    """Serialize an AuditEvent into a Firestore document body.

    timestamp_utc is stored as a Firestore Timestamp (the SDK auto-converts
    datetime objects); everything else is plain JSON-compatible types.
    """
    body = event.model_dump(mode="python")
    # Replace the StrEnum with its value for round-trip safety.
    body["action"] = event.action.value
    body[_PROJECT_CHAIN_FIELD] = event.project_id
    body[_INSERTION_ORDER_FIELD] = insertion_order
    return body


def _doc_to_event(doc: dict[str, Any]) -> AuditEvent:
    payload = {k: v for k, v in doc.items() if not k.startswith("_")}
    if "action" in payload and isinstance(payload["action"], str):
        payload["action"] = AuditAction(payload["action"])
    # Firestore returns DatetimeWithNanoseconds — pydantic accepts datetimes.
    ts = payload.get("timestamp_utc")
    if isinstance(ts, str):
        payload["timestamp_utc"] = datetime.fromisoformat(ts)
    return AuditEvent.model_validate(payload)
