"""Tests for Validated-mode Merkle root anchoring + KMS-style signing.

The KMS-backed signer (KmsRootSigner) is code-ready but not exercised
here — that requires a real Cloud KMS key. The local RSA signer is
fully exercised end-to-end: keypair generation, signing, verification,
and tamper detection.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.audit import (
    AnchorPeriod,
    AnchorRecord,
    AnchorVerificationFailure,
    AnchorVerifier,
    Anchorer,
    AuditAction,
    AuditEvent,
    AuditQuery,
    InMemoryAuditStore,
    LocalRsaKeypair,
    LocalRsaSigner,
    LocalRsaVerifier,
    SqliteAuditStore,
    compute_root,
    compute_root_hex,
    leaf_hash,
)


# --- Merkle tree -----------------------------------------------------------


def test_merkle_root_known_one_leaf() -> None:
    """One leaf: the root is its leaf hash."""
    h = "a" * 64  # any 64-hex string
    expected = leaf_hash(h)
    assert compute_root([h]) == expected


def test_merkle_root_known_two_leaves() -> None:
    """Two leaves: root = node(leaf(h1), leaf(h2))."""
    h1, h2 = "11" * 32, "22" * 32
    import hashlib

    expected = hashlib.sha256(b"node:" + leaf_hash(h1) + leaf_hash(h2)).digest()
    assert compute_root([h1, h2]) == expected


def test_merkle_root_is_deterministic_and_order_sensitive() -> None:
    a = ["aa" * 32, "bb" * 32, "cc" * 32]
    b = ["aa" * 32, "bb" * 32, "cc" * 32]
    assert compute_root(a) == compute_root(b)
    # Reordering changes the root
    c = ["cc" * 32, "bb" * 32, "aa" * 32]
    assert compute_root(c) != compute_root(a)


def test_merkle_root_empty_raises() -> None:
    with pytest.raises(ValueError):
        compute_root([])


def test_merkle_root_hex_matches_bytes() -> None:
    h = ["11" * 32, "22" * 32, "33" * 32]
    assert bytes.fromhex(compute_root_hex(h)) == compute_root(h)


# --- LocalRsaSigner / Verifier --------------------------------------------


@pytest.fixture(scope="module")
def keypair() -> LocalRsaKeypair:
    return LocalRsaKeypair.generate(signer_id="local:test-rsa-3072")


def test_signer_signature_verifies(keypair: LocalRsaKeypair) -> None:
    signer = LocalRsaSigner(keypair)
    verifier = LocalRsaVerifier(keypair.public_pem)
    payload = b"hello world"
    signature = signer.sign(payload)
    assert verifier.verify(signature, payload)


def test_signer_rejects_wrong_payload(keypair: LocalRsaKeypair) -> None:
    signer = LocalRsaSigner(keypair)
    verifier = LocalRsaVerifier(keypair.public_pem)
    signature = signer.sign(b"original")
    assert not verifier.verify(signature, b"tampered")


def test_signer_fingerprints_match_keypair(keypair: LocalRsaKeypair) -> None:
    signer = LocalRsaSigner(keypair)
    verifier = LocalRsaVerifier(keypair.public_pem)
    assert signer.public_key_fingerprint() == verifier.public_key_fingerprint()
    assert signer.public_key_fingerprint() == keypair.fingerprint


def test_two_keypairs_have_different_fingerprints() -> None:
    kp1 = LocalRsaKeypair.generate()
    kp2 = LocalRsaKeypair.generate()
    assert kp1.fingerprint != kp2.fingerprint


# --- Anchorer + AnchorVerifier --------------------------------------------


def _event(
    *,
    project_id: str = "anchor-test",
    actor_id: str = "system:test",
    action: AuditAction = AuditAction.SOURCE_INGESTED,
    when: datetime | None = None,
) -> AuditEvent:
    return AuditEvent(
        action=action,
        tenant_id="gsk",
        project_id=project_id,
        mode="part11",
        actor_id=actor_id,
        target_type="document",
        target_id="doc-x",
        timestamp_utc=when or datetime.now(timezone.utc),
    )


@pytest.fixture
def populated_store() -> InMemoryAuditStore:
    store = InMemoryAuditStore()
    base = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    for i in range(7):
        store.append(_event(when=base + timedelta(minutes=i)))
    return store


def test_anchor_produces_record_with_valid_signature(
    populated_store: InMemoryAuditStore, keypair: LocalRsaKeypair
) -> None:
    signer = LocalRsaSigner(keypair)
    anchorer = Anchorer(store=populated_store, signer=signer)
    period = AnchorPeriod(
        project_id="anchor-test",
        period_start=datetime(2026, 5, 26, 11, 0, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 26, 13, 0, tzinfo=timezone.utc),
    )
    anchor = anchorer.anchor(period)
    assert anchor.event_count == 7
    assert anchor.signer_id == "local:test-rsa-3072"
    assert anchor.public_key_fingerprint == keypair.fingerprint
    # Round-trip verification
    verifier = AnchorVerifier(LocalRsaVerifier(keypair.public_pem))
    events = list(
        populated_store.query(
            AuditQuery(
                project_id="anchor-test",
                since=period.period_start,
                until=period.period_end,
            )
        )
    )
    verifier.verify(anchor, events)


def test_anchor_raises_on_empty_period(
    populated_store: InMemoryAuditStore, keypair: LocalRsaKeypair
) -> None:
    anchorer = Anchorer(store=populated_store, signer=LocalRsaSigner(keypair))
    with pytest.raises(ValueError, match="no events"):
        anchorer.anchor(
            AnchorPeriod(
                project_id="nonexistent",
                period_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
                period_end=datetime(2020, 1, 2, tzinfo=timezone.utc),
            )
        )


def test_verifier_detects_wrong_fingerprint(
    populated_store: InMemoryAuditStore, keypair: LocalRsaKeypair
) -> None:
    signer = LocalRsaSigner(keypair)
    anchor = Anchorer(store=populated_store, signer=signer).anchor(
        AnchorPeriod(
            project_id="anchor-test",
            period_start=datetime(2026, 5, 26, 11, 0, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 26, 13, 0, tzinfo=timezone.utc),
        )
    )
    different_kp = LocalRsaKeypair.generate()
    verifier = AnchorVerifier(LocalRsaVerifier(different_kp.public_pem))
    events = list(populated_store.query(AuditQuery(project_id="anchor-test")))
    with pytest.raises(AnchorVerificationFailure) as exc:
        verifier.verify(anchor, events)
    assert exc.value.reason == "fingerprint_mismatch"


def test_verifier_detects_modified_merkle_root(
    populated_store: InMemoryAuditStore, keypair: LocalRsaKeypair
) -> None:
    signer = LocalRsaSigner(keypair)
    anchor = Anchorer(store=populated_store, signer=signer).anchor(
        AnchorPeriod(
            project_id="anchor-test",
            period_start=datetime(2026, 5, 26, 11, 0, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 26, 13, 0, tzinfo=timezone.utc),
        )
    )
    bogus = anchor.model_copy(update={"merkle_root_hex": "0" * 64})
    events = list(populated_store.query(AuditQuery(project_id="anchor-test")))
    verifier = AnchorVerifier(LocalRsaVerifier(keypair.public_pem))
    with pytest.raises(AnchorVerificationFailure) as exc:
        verifier.verify(bogus, events)
    # Either the root mismatch is caught, or the signature check fails first —
    # both are valid tamper-detection outcomes.
    assert exc.value.reason in ("merkle_root_mismatch", "signature_invalid")


def test_verifier_detects_event_count_mismatch(
    populated_store: InMemoryAuditStore, keypair: LocalRsaKeypair
) -> None:
    signer = LocalRsaSigner(keypair)
    anchor = Anchorer(store=populated_store, signer=signer).anchor(
        AnchorPeriod(
            project_id="anchor-test",
            period_start=datetime(2026, 5, 26, 11, 0, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 26, 13, 0, tzinfo=timezone.utc),
        )
    )
    events = list(populated_store.query(AuditQuery(project_id="anchor-test")))
    verifier = AnchorVerifier(LocalRsaVerifier(keypair.public_pem))
    with pytest.raises(AnchorVerificationFailure) as exc:
        verifier.verify(anchor, events[:-1])  # drop the last event
    assert exc.value.reason == "event_set_mismatch"


def test_anchor_persists_to_sqlite_and_reloads(
    tmp_path: Path, keypair: LocalRsaKeypair
) -> None:
    """End-to-end against the real SqliteAuditStore + anchor JSON on disk."""
    db = tmp_path / "audit.sqlite"
    with SqliteAuditStore(db) as store:
        base = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
        for i in range(5):
            store.append(_event(when=base + timedelta(minutes=i)))
        anchor = Anchorer(store=store, signer=LocalRsaSigner(keypair)).anchor(
            AnchorPeriod(
                project_id="anchor-test",
                period_start=datetime(2026, 5, 26, 11, 0, tzinfo=timezone.utc),
                period_end=datetime(2026, 5, 26, 13, 0, tzinfo=timezone.utc),
            )
        )
        events = list(store.query(AuditQuery(project_id="anchor-test")))

    # Persist + reload the anchor record as JSON
    anchor_path = tmp_path / "anchor.json"
    anchor_path.write_text(anchor.model_dump_json(indent=2), encoding="utf-8")
    reloaded = AnchorRecord.model_validate_json(anchor_path.read_text(encoding="utf-8"))
    AnchorVerifier(LocalRsaVerifier(keypair.public_pem)).verify(reloaded, events)


def test_anchor_breaks_after_sqlite_tamper(
    tmp_path: Path, keypair: LocalRsaKeypair
) -> None:
    """End-to-end: anchor a chain, mutate a row out-of-band, re-verify
    against the anchored events — the chain check inside the verifier
    should catch the tamper."""
    db = tmp_path / "audit.sqlite"
    with SqliteAuditStore(db) as store:
        base = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
        for i in range(5):
            store.append(_event(when=base + timedelta(minutes=i)))
        period = AnchorPeriod(
            project_id="anchor-test",
            period_start=datetime(2026, 5, 26, 11, 0, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 26, 13, 0, tzinfo=timezone.utc),
        )
        anchor = Anchorer(store=store, signer=LocalRsaSigner(keypair)).anchor(period)

    # Mutate one row's notes via raw SQL.
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE audit_events SET notes_json = ? "
        "WHERE insertion_order = (SELECT MIN(insertion_order) FROM audit_events)",
        (json.dumps(["TAMPERED"]),),
    )
    conn.commit()
    conn.close()

    with SqliteAuditStore(db) as store:
        events = list(store.query(AuditQuery(project_id="anchor-test")))
        verifier = AnchorVerifier(LocalRsaVerifier(keypair.public_pem))
        with pytest.raises(AnchorVerificationFailure) as exc:
            verifier.verify(anchor, events)
        assert exc.value.reason in ("chain_broken", "merkle_root_mismatch")


# --- KmsRootSigner code-readiness check -----------------------------------


def test_kms_signer_class_imports_lazily() -> None:
    """KmsRootSigner is importable through the audit package when
    google-cloud-kms is installed; otherwise it's None. This test
    just confirms the symbol is exposed."""
    from services.audit import KmsRootSigner

    # Either real class (if google-cloud-kms is installed) or None.
    # Importing here just verifies the lazy-import wiring isn't broken.
    assert KmsRootSigner is None or isinstance(KmsRootSigner, type)
