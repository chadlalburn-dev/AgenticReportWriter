"""PDF parser.

Uses pypdf to extract page-level text, then splits into paragraphs (runs of
non-blank lines). Each paragraph becomes a ParsedChunk with `page` and
`paragraph_index` (per-page) so citations can deep-link to the source.

For complex pharma PDFs with tables and multi-column layouts, the production
service should escalate to Document AI (architecture plan); pypdf is the
fallback / local-PoC default.
"""

from __future__ import annotations

import io
from collections.abc import Iterable

from pypdf import PdfReader

from shared.schemas import CanonicalDocument, ChunkKind, ParsedChunk, PdfLocator

from .parser import hash_text, new_chunk_id


class PdfParser:
    mime_types = ("application/pdf",)

    def parse(self, doc: CanonicalDocument, raw: bytes) -> Iterable[ParsedChunk]:
        reader = PdfReader(io.BytesIO(raw))
        for page_idx, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            paragraphs = _split_into_paragraphs(page_text)
            for paragraph_idx, paragraph in enumerate(paragraphs):
                yield ParsedChunk(
                    chunk_id=new_chunk_id(),
                    source_doc_id=doc.doc_id,
                    source_doc_version=doc.doc_version,
                    kind=ChunkKind.PARAGRAPH,
                    text=paragraph,
                    text_hash=hash_text(paragraph),
                    char_count=len(paragraph),
                    locator=PdfLocator(page=page_idx, paragraph_index=paragraph_idx),
                    tags=list(doc.tags),
                )


def _split_into_paragraphs(page_text: str) -> list[str]:
    """Split page text into paragraphs on blank lines.

    pypdf's extract_text() returns lines separated by '\n'; a paragraph is a
    run of non-blank lines, joined with a space. Empty paragraphs are dropped.
    """
    paragraphs: list[str] = []
    current: list[str] = []
    for line in page_text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append(" ".join(current))
                current = []
        else:
            current.append(stripped)
    if current:
        paragraphs.append(" ".join(current))
    return [p for p in paragraphs if p]
