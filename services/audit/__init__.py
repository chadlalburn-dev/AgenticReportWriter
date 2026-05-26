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

from services.audit.anchor import (
    Anchorer,
    AnchorPeriod,
    AnchorRecord,
    AnchorVerificationFailure,
    AnchorVerifier,
)
from services.audit.hash_chain import canonical_event_hash, verify_chain
from services.audit.llm_audit import AuditingLlmClient
from services.audit.merkle import compute_root, compute_root_hex, leaf_hash
from services.audit.schema import (
    AuditAction,
    AuditEvent,
    ComplianceMode,
    new_event_id,
)
from services.audit.signer import (
    LocalRsaKeypair,
    LocalRsaSigner,
    LocalRsaVerifier,
    RootSigner,
    RootVerifier,
)
from services.audit.store import (
    AuditQuery,
    AuditSink,
    AuditStore,
    HashChainViolation,
    InMemoryAuditStore,
    SqliteAuditStore,
)

# FirestoreAuditStore + KmsRootSigner are exposed lazily — their GCP SDKs are
# only installed when the [gcp] extra is present. Modules that depend on the
# audit package shouldn't have to install GCP deps.
try:
    from services.audit.firestore_store import FirestoreAuditStore
except ImportError:  # pragma: no cover - depends on local install
    FirestoreAuditStore = None  # type: ignore[assignment,misc]

try:
    from services.audit.signer import KmsRootSigner
except ImportError:  # pragma: no cover - google-cloud-kms is in [gcp] extras
    KmsRootSigner = None  # type: ignore[assignment,misc]


__all__ = [
    "AnchorPeriod",
    "AnchorRecord",
    "AnchorVerificationFailure",
    "AnchorVerifier",
    "Anchorer",
    "AuditAction",
    "AuditEvent",
    "AuditQuery",
    "AuditSink",
    "AuditStore",
    "AuditingLlmClient",
    "ComplianceMode",
    "FirestoreAuditStore",
    "HashChainViolation",
    "InMemoryAuditStore",
    "KmsRootSigner",
    "LocalRsaKeypair",
    "LocalRsaSigner",
    "LocalRsaVerifier",
    "RootSigner",
    "RootVerifier",
    "SqliteAuditStore",
    "canonical_event_hash",
    "compute_root",
    "compute_root_hex",
    "leaf_hash",
    "new_event_id",
    "verify_chain",
]
