"""RootSigner protocol + LocalRsaSigner (testable) + KmsRootSigner (Cloud KMS).

In Validated mode, every daily Merkle root is signed by an HSM-backed
key so the root carries non-repudiation. Production should use a
Cloud KMS asymmetric key with hardware protection level — only Cloud
KMS holds the private material, only a single trusted service account
can request signatures, and every signing call lands in Cloud Audit
Logs.

For unit tests and local dev (no GCP), LocalRsaSigner generates an
in-process RSA-3072 keypair and signs with PSS. Switch to
KmsRootSigner in production — same RootSigner interface, no other
caller changes.

The verification side mirrors: LocalRsaVerifier holds the public key
(from the LocalRsaSigner), KmsRootVerifier loads the public key from
Cloud KMS by version URI.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


# -- Protocols --------------------------------------------------------------


class RootSigner(Protocol):
    """Signs a byte string (typically a 32-byte Merkle root) and returns
    a signature + the signer's identity for the audit record."""

    signer_id: str

    def sign(self, payload: bytes) -> bytes: ...

    def public_key_fingerprint(self) -> str:
        """SHA-256 (hex) of the public key in SubjectPublicKeyInfo (DER) form.

        Used as a stable identifier for verifiers to look up the key —
        rotating the key changes the fingerprint.
        """
        ...


class RootVerifier(Protocol):
    """Verifies a signature produced by a RootSigner."""

    def verify(self, signature: bytes, payload: bytes) -> bool: ...

    def public_key_fingerprint(self) -> str: ...


# -- Local RSA implementation (test + dev) ----------------------------------


@dataclass(frozen=True)
class LocalRsaKeypair:
    private_pem: bytes
    public_pem: bytes
    signer_id: str
    fingerprint: str

    @classmethod
    def generate(cls, *, signer_id: str = "local:dev-rsa-3072", key_size: int = 3072) -> "LocalRsaKeypair":
        from cryptography.hazmat.primitives import serialization  # type: ignore[import-not-found]
        from cryptography.hazmat.primitives.asymmetric import rsa  # type: ignore[import-not-found]

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
        public_key = private_key.public_key()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        public_der = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return cls(
            private_pem=private_pem,
            public_pem=public_pem,
            signer_id=signer_id,
            fingerprint=hashlib.sha256(public_der).hexdigest(),
        )


def _signature_padding():
    from cryptography.hazmat.primitives import hashes  # type: ignore[import-not-found]
    from cryptography.hazmat.primitives.asymmetric import padding  # type: ignore[import-not-found]

    return padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH
    )


class LocalRsaSigner(RootSigner):
    def __init__(self, keypair: LocalRsaKeypair) -> None:
        from cryptography.hazmat.primitives import serialization  # type: ignore[import-not-found]

        self._keypair = keypair
        self._private_key = serialization.load_pem_private_key(
            keypair.private_pem, password=None
        )

    @property
    def signer_id(self) -> str:  # type: ignore[override]
        return self._keypair.signer_id

    def sign(self, payload: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes  # type: ignore[import-not-found]

        return self._private_key.sign(payload, _signature_padding(), hashes.SHA256())

    def public_key_fingerprint(self) -> str:
        return self._keypair.fingerprint


class LocalRsaVerifier(RootVerifier):
    def __init__(self, public_pem: bytes) -> None:
        from cryptography.hazmat.primitives import serialization  # type: ignore[import-not-found]

        self._public_key = serialization.load_pem_public_key(public_pem)
        public_der = self._public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._fingerprint = hashlib.sha256(public_der).hexdigest()

    def verify(self, signature: bytes, payload: bytes) -> bool:
        from cryptography.exceptions import InvalidSignature  # type: ignore[import-not-found]
        from cryptography.hazmat.primitives import hashes  # type: ignore[import-not-found]

        try:
            self._public_key.verify(signature, payload, _signature_padding(), hashes.SHA256())
            return True
        except InvalidSignature:
            return False

    def public_key_fingerprint(self) -> str:
        return self._fingerprint


# -- Cloud KMS implementation (production) ----------------------------------


class KmsRootSigner(RootSigner):
    """Signs payloads with a Cloud KMS asymmetric key.

    Use HSM protection level + RSA_SIGN_PSS_3072_SHA256 (or stronger) for
    Validated mode. The Cloud KMS audit log captures every signing
    invocation — that log is the regulator-facing evidence that no
    out-of-band signatures occurred.

    `key_uri` shape:
        projects/{project}/locations/{loc}/keyRings/{ring}/cryptoKeys/{key}/cryptoKeyVersions/{version}

    Authentication: Application Default Credentials. Workload identity
    in Cloud Run, gcloud ADC locally. No JSON keys.
    """

    def __init__(self, key_uri: str) -> None:
        try:
            from google.cloud import kms  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "google-cloud-kms is not installed. Install the [gcp] extras "
                "before using KmsRootSigner."
            ) from exc

        self._client = kms.KeyManagementServiceClient()
        self._key_uri = key_uri
        self._cached_fingerprint: str | None = None

    @property
    def signer_id(self) -> str:  # type: ignore[override]
        return self._key_uri

    def sign(self, payload: bytes) -> bytes:
        # KMS for RSA_SIGN_PSS_*_SHA256 expects the SHA-256 digest of the
        # payload, not the payload itself.
        digest = hashlib.sha256(payload).digest()
        response = self._client.asymmetric_sign(
            request={
                "name": self._key_uri,
                "digest": {"sha256": digest},
            }
        )
        return response.signature

    def public_key_fingerprint(self) -> str:
        if self._cached_fingerprint is not None:
            return self._cached_fingerprint
        response = self._client.get_public_key(request={"name": self._key_uri})
        pem: str = response.pem  # PEM-encoded SubjectPublicKeyInfo
        # Strip header/footer + base64-decode to DER for the fingerprint.
        import base64

        body = (
            pem.replace("-----BEGIN PUBLIC KEY-----", "")
            .replace("-----END PUBLIC KEY-----", "")
            .replace("\n", "")
            .strip()
        )
        der = base64.b64decode(body)
        self._cached_fingerprint = hashlib.sha256(der).hexdigest()
        return self._cached_fingerprint
