"""Connector protocol.

Every source-system integration (Veeva, Medidata, LabVantage, SharePoint,
S3, GCS, BigQuery, local file) implements this. The canonical document is
the only thing downstream services see — they never see source-system
payload formats.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from shared.schemas import CanonicalDocument


@dataclass(frozen=True)
class ConnectorContext:
    """Per-run context passed to a connector.

    Carries enough info for the connector to attribute documents to a team
    and a run, and to find/store artifacts in the right GCS prefix.
    """

    tenant_id: str
    team_id: str
    run_id: str
    raw_storage_prefix: str = "ib-sources-raw-local"


class Connector(Protocol):
    """A source-system integration that emits CanonicalDocuments.

    `ingest` returns an iterator so connectors can stream large source
    catalogs without loading everything into memory.
    """

    connector_id: str

    def ingest(
        self, source_uri: str, context: ConnectorContext
    ) -> Iterator[tuple[CanonicalDocument, bytes]]: ...
