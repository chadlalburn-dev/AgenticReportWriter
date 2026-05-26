"""Audit-trail persistence layer.

Canonical AuditEvent schema mirroring the Firestore design in the
architecture plan (docs/architecture-plan.md, section "Audit Trail Design").
Three backends implement the same AuditStore protocol:

- InMemoryAuditStore: for unit tests
- SqliteAuditStore: for the local PoC, single file per project
- FirestoreAuditStore: production (NOT YET IMPLEMENTED — placeholder
  while we're pre-GCP-provisioning)

A hash chain links each event to its predecessor (per project_id) so a
later tamper-evidence verifier can re-walk the chain and prove no event
was inserted, modified, or deleted. The Validated-mode addition is to
sign daily Merkle roots with a KMS-backed HSM key — that's a follow-up
on top of this PoC.
"""

from services.audit.hash_chain import canonical_event_hash, verify_chain
from services.audit.llm_audit import AuditingLlmClient
from services.audit.schema import (
    AuditAction,
    AuditEvent,
    ComplianceMode,
    new_event_id,
)
from services.audit.store import (
    AuditQuery,
    AuditSink,
    AuditStore,
    HashChainViolation,
    InMemoryAuditStore,
    SqliteAuditStore,
)

__all__ = [
    "AuditAction",
    "AuditEvent",
    "AuditQuery",
    "AuditSink",
    "AuditStore",
    "AuditingLlmClient",
    "ComplianceMode",
    "HashChainViolation",
    "InMemoryAuditStore",
    "SqliteAuditStore",
    "canonical_event_hash",
    "new_event_id",
    "verify_chain",
]
