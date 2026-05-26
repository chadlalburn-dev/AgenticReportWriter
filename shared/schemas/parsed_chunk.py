"""Parsed chunk schema.

A `ParsedChunk` is a piece of text extracted from a CanonicalDocument with
enough source metadata to render a precise citation. The chunker is
structure-aware: PDF chunks know their page + paragraph index, DOCX chunks
know their heading trail, XLSX chunks know their sheet + cell range.

Retrieval (vector or keyword) returns these chunks; the generation
orchestrator passes them to the LLM with a citation_id binding so every
claim carries a back-link to the chunk it was derived from.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ChunkKind(StrEnum):
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE_ROW = "table_row"
    LIST_ITEM = "list_item"
    SHEET_REGION = "sheet_region"
    PAGE_TEXT = "page_text"


class PdfLocator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    page: int = Field(ge=1)
    paragraph_index: int | None = Field(default=None, ge=0)


class DocxLocator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    heading_trail: list[str] = Field(default_factory=list)
    paragraph_index: int = Field(ge=0)


class XlsxLocator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sheet: str
    cell_range: str = Field(description="A1-style cell range, e.g. 'A1:D12'")


ChunkLocator = Annotated[PdfLocator | DocxLocator | XlsxLocator, Field(discriminator=None)]


class ParsedChunk(BaseModel):
    """A piece of text extracted from a source document, with locator.

    `chunk_id` is the join key for retrieval ↔ citation. The locator is one of
    the typed locators above, picked based on the source MIME type.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(description="UUIDv7")
    source_doc_id: str
    source_doc_version: str
    kind: ChunkKind
    text: str
    text_hash: str = Field(description="SHA-256 of normalized text")
    char_count: int = Field(ge=0)
    locator: PdfLocator | DocxLocator | XlsxLocator
    tags: list[str] = Field(default_factory=list, description="Inherited from source document")
