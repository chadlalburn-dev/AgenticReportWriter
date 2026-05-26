"""Anchorer + AnchorRecord — periodically commit a signed Merkle root
over the audit chain.

The Anchorer walks an AuditStore's chain for a (project_id, period)
window, verifies the chain, computes the Merkle root, signs it with a
RootSigner, and produces an AnchorRecord. The record is small (a few
hundred bytes); persist it next to the audit chain so verifiers can
fetch it cheaply.

Verification:
- A regulator/QA can re-read the events for the period, re-walk the
  chain, recompute the Merkle root, and verify the signature against
  the signer's public key. Any mutation between events breaks the
  hash chain; any mutation of the root or the signature breaks the
  signature check.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.audit.hash_chain import verify_chain
from services.audit.merkle import compute_root_hex
from services.audit.schema import AuditEvent
from services.audit.signer import RootSigner, RootVerifier
from services.audit.store import AuditQuery, AuditStore


class AnchorRecord(BaseModel):
    """A signed commitment that the chain held a specific set of events
    for (project_id, period_start..period_end)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    anchor_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    period_start: datetime
    period_end: datetime
    event_count: int
    first_event_id: str
    last_event_id: str
    last_event_chain_hash: str
    merkle_root_hex: str
    signer_id: str
    signature_b64: str
    public_key_fingerprint: str
    signed_at: datetime
    notes: list[str] = Field(default_factory=list)


class AnchorVerificationFailure(Exception):
    """Raised when re-verifying an AnchorRecord against the actual events
    detects tampering, signature mismatch, or chain breakage."""

    def __init__(self, reason: Literal[
        "chain_broken", "merkle_root_mismatch", "signature_invalid",
        "event_set_mismatch", "fingerprint_mismatch",
    ], detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class AnchorPeriod:
    project_id: str
    period_start: datetime
    period_end: datetime


class Anchorer:
    """Computes and signs a single AnchorRecord for a period."""

    def __init__(self, *, store: AuditStore, signer: RootSigner) -> None:
        self._store = store
        self._signer = signer

    def anchor(self, period: AnchorPeriod, *, notes: list[str] | None = None) -> AnchorRecord:
        events = list(
            self._store.query(
                AuditQuery(
                    project_id=period.project_id,
                    since=period.period_start,
                    until=period.period_end,
                )
            )
        )
        if not events:
            raise ValueError(
                f"no events found for project_id={period.project_id!r} "
                f"in period [{period.period_start}, {period.period_end}]"
            )

        # Re-verify the chain first — anchoring a broken chain hides
        # nothing, so fail loudly.
        verify_chain(events)

        chain_hashes = [e.this_event_hash or "" for e in events]
        assert all(chain_hashes), "verify_chain should have rejected empty this_event_hash"
        root_hex = compute_root_hex(chain_hashes)
        signature = self._signer.sign(bytes.fromhex(root_hex))

        return AnchorRecord(
            project_id=period.project_id,
            period_start=period.period_start,
            period_end=period.period_end,
            event_count=len(events),
            first_event_id=events[0].event_id,
            last_event_id=events[-1].event_id,
            last_event_chain_hash=chain_hashes[-1],
            merkle_root_hex=root_hex,
            signer_id=self._signer.signer_id,
            signature_b64=base64.b64encode(signature).decode("ascii"),
            public_key_fingerprint=self._signer.public_key_fingerprint(),
            signed_at=datetime.now(timezone.utc),
            notes=notes or [],
        )


class AnchorVerifier:
    """Verifies an AnchorRecord against the actual events.

    The verifier holds a RootVerifier with the trusted public key. In
    production, fetch the public key from Cloud KMS by signer_id; in
    tests, supply LocalRsaVerifier with the public PEM that was
    paired with the LocalRsaSigner.
    """

    def __init__(self, root_verifier: RootVerifier) -> None:
        self._root_verifier = root_verifier

    def verify(self, anchor: AnchorRecord, events: list[AuditEvent]) -> None:
        if anchor.public_key_fingerprint != self._root_verifier.public_key_fingerprint():
            raise AnchorVerificationFailure(
                "fingerprint_mismatch",
                f"anchor signed by {anchor.public_key_fingerprint!r} but "
                f"verifier holds {self._root_verifier.public_key_fingerprint()!r}",
            )
        if len(events) != anchor.event_count:
            raise AnchorVerificationFailure(
                "event_set_mismatch",
                f"anchor expects {anchor.event_count} events, got {len(events)}",
            )
        try:
            verify_chain(events)
        except Exception as exc:
            raise AnchorVerificationFailure("chain_broken", str(exc)) from exc

        chain_hashes = [e.this_event_hash or "" for e in events]
        recomputed = compute_root_hex(chain_hashes)
        if recomputed != anchor.merkle_root_hex:
            raise AnchorVerificationFailure(
                "merkle_root_mismatch",
                f"anchor.merkle_root_hex={anchor.merkle_root_hex!r} but "
                f"recomputed {recomputed!r}",
            )
        signature = base64.b64decode(anchor.signature_b64)
        ok = self._root_verifier.verify(signature, bytes.fromhex(anchor.merkle_root_hex))
        if not ok:
            raise AnchorVerificationFailure(
                "signature_invalid", "signature did not verify against the trusted key"
            )
