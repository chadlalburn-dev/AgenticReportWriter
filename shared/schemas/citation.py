"""Citation schema — every fact in a generated report carries one of these.

The locator must be precise enough that a reviewer can verify the cited claim
in the original source: PDF page + paragraph, spreadsheet cell range, SQL
query + row filter, API endpoint + parameters.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class SourceType(StrEnum):
    """The kind of source a citation points to."""

    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    SQL = "sql"
    API = "api"
    COMPUTED = "computed"


class CitationLocator(BaseModel):
    """Source-type-dependent locator.

    For files: page/paragraph (PDF/DOCX) or sheet/cell_range (XLSX).
    For DB: query_id + parameters + row_filter.
    For APIs: endpoint + parameters.

    Not all fields are populated — the renderer keys off `source_type` on the
    parent Citation to know which fields are meaningful.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # File locators
    page: int | None = None
    paragraph_index: int | None = None
    heading_trail: list[str] | None = None
    sheet: str | None = None
    cell_range: str | None = None

    # DB locators
    query_id: str | None = None
    query_parameters: dict[str, str] | None = None
    row_filter: str | None = None

    # API locators
    endpoint: str | None = None
    api_parameters: dict[str, str] | None = None


class Citation(BaseModel):
    """A single citation linking a generated claim back to its source.

    Identity: `citation_id` is generated at retrieval time; one citation can be
    referenced from multiple claims in the report. `claim_hash` lets us cache
    verification results across regenerations.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    citation_id: Annotated[str, Field(description="UUIDv7 generated at retrieval time")]
    report_instance_id: str
    source_type: SourceType
    source_uri: str = Field(description="Canonical URI for the source (GCS, source-system URL)")
    source_doc_id: str = Field(description="CanonicalDocument.doc_id")
    source_doc_version: str = Field(description="CanonicalDocument.doc_version at retrieval time")
    locator: CitationLocator
    snippet: str = Field(description="The exact text/value cited, for reviewer verification")
    retrieved_at: datetime
    retrieval_chunk_id: str | None = Field(
        default=None, description="Vector index chunk ID if retrieved via RAG"
    )
