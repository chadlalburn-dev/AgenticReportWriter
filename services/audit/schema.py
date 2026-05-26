"""Canonical AuditEvent + AuditAction enum.

The schema is a deliberate superset of what any single phase emits — the
audit ledger is the join point across templates, ingestion, generation,
review, and signing, so any one field may be irrelevant to a given action
but must exist on the record.

All fields except action/target/actor/timestamp can be omitted via None,
but `extra` is the right place for action-specific structured data
(e.g., LLM_CALL records put model_version, temperature, retrieval_ids
there).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AuditAction(StrEnum):
    """Every observable transition in the system gets one of these.

    Keep this list narrow — adding a new action is a schema change. If you
    find yourself wanting to add a value, ask whether the existing action
    + a more specific `extra` would do.
    """

    # Template lifecycle
    TEMPLATE_CREATED = "template_created"
    TEMPLATE_APPROVED = "template_approved"
    TEMPLATE_RETIRED = "template_retired"

    # Source ingestion / parsing
    SOURCE_INGESTED = "source_ingested"
    SOURCE_DEIDENTIFIED = "source_deidentified"

    # Generation pipeline
    GENERATION_REQUESTED = "generation_requested"
    GENERATION_PLAN_COMPLETED = "generation_plan_completed"
    GENERATION_SECTION_FILLED = "generation_section_filled"
    GENERATION_SECTION_CRITIQUED = "generation_section_critiqued"
    GENERATION_COMPLETED = "generation_completed"

    # LLM call (always logged in GxP-aware and Validated modes)
    LLM_CALL = "llm_call"

    # Citation lifecycle
    CITATION_CREATED = "citation_created"

    # Review workflow
    SECTION_EDITED = "section_edited"
    REVIEWER_COMMENTED = "reviewer_commented"
    ATTESTATION_RECORDED = "attestation_recorded"

    # Signature & lock
    SIGNATURE_APPLIED = "signature_applied"
    MODE_LOCKED = "mode_locked"

    # Export
    EXPORT_PERFORMED = "export_performed"


ComplianceMode = Literal["rd", "gxp", "part11"]


def new_event_id() -> str:
    """Stable identifier for an event. Schema describes the intent
    (UUIDv7 in production for time-orderability + insertion-order
    durability); we use uuid4 in the PoC to avoid a uuid7 dependency
    on Python 3.11. Switch to uuid.uuid7() when we move to 3.13+."""
    return str(uuid.uuid4())


class AuditEvent(BaseModel):
    """A single canonical audit record.

    Mirrors the Firestore document shape from the architecture plan.
    Immutable once persisted — stores must reject UPDATE/DELETE.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Identity
    event_id: str = Field(default_factory=new_event_id)

    # Multi-tenancy + project scope
    tenant_id: str
    project_id: str

    # Compliance mode at the time of the event (drives retention + hashing)
    mode: ComplianceMode

    # Who did it (in service contexts this is the service account)
    actor_id: str
    actor_role: str | None = None
    actor_auth_method: str | None = Field(
        default=None,
        description=(
            "How the actor was authenticated. For Validated-mode signatures "
            "this records the two ID components verified (e.g., "
            "'sso+password+webauthn')."
        ),
    )

    # What happened
    action: AuditAction
    target_type: str = Field(description="e.g. 'template', 'report_instance', 'section', 'citation'")
    target_id: str
    target_version: str | None = None

    # When + where
    timestamp_utc: datetime
    client_ip: str | None = None
    user_agent: str | None = None

    # Validated-mode signature support
    reason: str | None = Field(
        default=None,
        description=(
            "Required for SIGNATURE_APPLIED in Validated mode (controlled "
            "vocabulary: Authored, Reviewed, QA-Approved, Released)."
        ),
    )

    # State hashes (for change-tracking on SECTION_EDITED, etc.)
    before_hash: str | None = None
    after_hash: str | None = None

    # Large blobs (LLM call payloads, parsed chunks) live in GCS; this is
    # the URI pointer.
    payload_ref: str | None = None

    # Hash chain — populated by the store at write time
    prev_event_hash: str | None = None
    this_event_hash: str | None = None

    # KMS-signed (Validated mode only) — placeholder field, signing wire-up
    # is a follow-up tied to a Cloud KMS HSM key.
    signature: str | None = None

    # Free-form notes (critique issues, deviation comments)
    notes: list[str] = Field(default_factory=list)

    # Action-specific structured data. Keep values JSON-serializable.
    extra: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
