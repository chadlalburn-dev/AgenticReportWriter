"""Local filesystem connector.

Walks a directory (or single file) on the local filesystem and emits one
CanonicalDocument per file with the raw bytes. Used for the PoC; the
production analog is GCS / S3 / SharePoint, which use the same
CanonicalDocument shape so downstream services are unchanged.
"""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from shared.schemas import CanonicalDocument, SourceSystem

from .base import Connector, ConnectorContext


# Augment Python's MIME registry with the modern OOXML names — Windows often
# returns the older application/msword variant for .docx, which is wrong.
mimetypes.add_type(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"
)
mimetypes.add_type(
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"
)


class LocalFileConnector(Connector):
    connector_id = "local_file"

    def ingest(
        self, source_uri: str, context: ConnectorContext
    ) -> Iterator[tuple[CanonicalDocument, bytes]]:
        root = Path(source_uri)
        if not root.exists():
            raise FileNotFoundError(f"source_uri does not exist: {source_uri}")

        targets = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in targets:
            if not path.is_file():
                continue
            raw = path.read_bytes()
            mime, _ = mimetypes.guess_type(path.name)
            if mime is None:
                # Skip files we can't classify; the parsing service would
                # reject them anyway.
                continue
            content_hash = hashlib.sha256(raw).hexdigest()
            doc = CanonicalDocument(
                doc_id=f"local::{path.resolve().as_posix()}",
                doc_version=content_hash[:12],
                content_hash=content_hash,
                source_system=SourceSystem.LOCAL_FILE,
                source_id=path.resolve().as_posix(),
                system_of_record_url=f"file:///{path.resolve().as_posix()}",
                mime_type=mime,
                title=path.stem,
                retrieval_timestamp=datetime.now(timezone.utc),
                tags=_infer_tags_from_path(path),
                storage_uri=f"{context.raw_storage_prefix}://{context.run_id}/{path.name}",
            )
            yield doc, raw


def _infer_tags_from_path(path: Path) -> list[str]:
    """Cheap heuristic: use parent directory names as tags.

    Example: samples/nonclinical/pk/study42.pdf -> ["nonclinical", "pk"].
    The full ingestion pipeline overwrites this from a metadata sidecar
    file or the source system's own tagging.
    """
    return [p.name.lower() for p in path.parents if p.name and p.name != path.anchor.rstrip("\\/")]
