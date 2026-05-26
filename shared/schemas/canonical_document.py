"""Canonical document schema.

Every ingestion connector (Veeva, Medidata, LabVantage, SharePoint, S3, GCS, BQ, ...)
emits documents conforming to this schema. Downstream services (parsing, citation,
generation) only know this shape — they never see connector-specific payloads.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SourceSystem(StrEnum):
    """The system of record an ingested document came from."""

    GCS = "gcs"
    S3 = "s3"
    SHAREPOINT = "sharepoint"
    BIGQUERY = "bigquery"
    CLOUDSQL = "cloudsql"
    POSTGRES = "postgres"
    VEEVA = "veeva"
    MEDIDATA_RAVE = "medidata_rave"
    LABVANTAGE = "labvantage"
    LOCAL_FILE = "local_file"


class CanonicalDocument(BaseModel):
    """A document or data record retrieved from any source system.

    The fields below are sufficient to render a citation that points back
    to the source of record — `system_of_record_url` must be a deep link
    when the source system supports it, otherwise the bucket/folder URI.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str = Field(description="Stable ID within the source system (e.g., Veeva docId)")
    doc_version: str = Field(description="Source-system version; falls back to content hash")
    content_hash: str = Field(description="SHA-256 of normalized content for dedup")
    source_system: SourceSystem
    source_id: str = Field(description="Source-system-specific resource ID")
    system_of_record_url: HttpUrl | str = Field(
        description="Deep link to the resource in its source system; string if not URL-shaped"
    )
    mime_type: str = Field(description="MIME type of the original (e.g. application/pdf)")
    title: str | None = None
    author: str | None = None
    effective_date: datetime | None = Field(
        default=None,
        description="When this version of the doc became authoritative in the source system",
    )
    retrieval_timestamp: datetime
    tags: list[str] = Field(default_factory=list, description="Free-form tags (e.g. ['PK', 'nonclinical'])")
    extra: dict[str, Any] = Field(default_factory=dict, description="Source-system metadata not modeled above")

    storage_uri: str = Field(description="GCS URI of the raw payload in the landing bucket")
