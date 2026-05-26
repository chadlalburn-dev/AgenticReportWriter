"""Parser protocol and dispatch.

A Parser turns a CanonicalDocument + its raw bytes into a stream of
ParsedChunks with locators. Each MIME type has its own parser; the
dispatcher routes by `doc.mime_type`.

Citations require the chunks emitted here to carry enough locator detail
that a reviewer can verify the cited claim in the original source.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from typing import Protocol

from shared.schemas import CanonicalDocument, ParsedChunk


class Parser(Protocol):
    """A parser for one or more MIME types."""

    mime_types: tuple[str, ...]

    def parse(self, doc: CanonicalDocument, raw: bytes) -> Iterable[ParsedChunk]: ...


def new_chunk_id() -> str:
    """ID for a parsed chunk.

    Schema describes the intent (UUIDv7 for time-orderability); we use uuid4
    in the PoC to avoid adding a uuid7 dependency on Python 3.11. Swap to
    uuid.uuid7() when we move to Python 3.13+.
    """
    return str(uuid.uuid4())


def hash_text(text: str) -> str:
    """SHA-256 of normalized text — collapses runs of whitespace before hashing
    so trivial reformatting of the source doesn't change identity."""
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class UnsupportedMimeType(Exception):
    """Raised when no registered parser handles a document's MIME type."""


class ParserRegistry:
    """Routes a CanonicalDocument to the right Parser by MIME type."""

    def __init__(self) -> None:
        self._parsers: dict[str, Parser] = {}

    def register(self, parser: Parser) -> None:
        for mime in parser.mime_types:
            self._parsers[mime] = parser

    def parse(self, doc: CanonicalDocument, raw: bytes) -> list[ParsedChunk]:
        parser = self._parsers.get(doc.mime_type)
        if parser is None:
            raise UnsupportedMimeType(
                f"no parser registered for {doc.mime_type!r} (doc_id={doc.doc_id!r})"
            )
        return list(parser.parse(doc, raw))
