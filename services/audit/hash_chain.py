"""Hash-chain tamper evidence.

For every event, we compute `this_event_hash = sha256(prev_event_hash +
canonical_json(event_with_no_hashes))`. The chain is per project_id —
events for different projects are independent chains.

A separate `verify_chain` walks a sequence of events and re-derives each
hash, raising on the first mismatch. The Validated-mode addition signs
the daily root hash with Cloud KMS HSM; that's layered on top.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from services.audit.schema import AuditEvent


# Fields excluded when computing this_event_hash — they're computed FROM
# the event, so including them in the hash input would be circular.
_EXCLUDED_FROM_HASH = frozenset({"this_event_hash", "signature"})


def canonical_event_hash(event: AuditEvent, *, prev_hash: str | None) -> str:
    """Deterministic SHA-256 over (prev_hash || canonical-JSON of event)."""
    dumped = event.model_dump(mode="json", exclude=_EXCLUDED_FROM_HASH)
    # prev_event_hash is part of the input; the store sets it on the event
    # before calling this function so the chain link is in the hash domain.
    payload = json.dumps(dumped, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    h = hashlib.sha256()
    if prev_hash:
        h.update(prev_hash.encode("utf-8"))
    h.update(payload.encode("utf-8"))
    return h.hexdigest()


class HashChainViolation(Exception):
    """Raised when an event's recomputed hash doesn't match the stored value,
    or its prev_event_hash doesn't match the prior event's this_event_hash."""


def verify_chain(events: Iterable[AuditEvent]) -> int:
    """Walk a sequence of events (insertion order) and verify the chain.

    Returns the number of events checked. Raises HashChainViolation on the
    first inconsistency, with enough context to identify which event broke.

    Caller's responsibility: pass events in insertion order, scoped to a
    single project_id (different projects have independent chains).
    """
    count = 0
    prev_hash: str | None = None
    for event in events:
        if event.prev_event_hash != prev_hash:
            raise HashChainViolation(
                f"event {event.event_id!r}: prev_event_hash={event.prev_event_hash!r} "
                f"but actual previous chain hash was {prev_hash!r}"
            )
        expected = canonical_event_hash(event, prev_hash=prev_hash)
        if event.this_event_hash != expected:
            raise HashChainViolation(
                f"event {event.event_id!r}: stored this_event_hash="
                f"{event.this_event_hash!r} but recomputed {expected!r}"
            )
        prev_hash = event.this_event_hash
        count += 1
    return count
