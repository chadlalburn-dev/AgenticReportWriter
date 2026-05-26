"""DOCX parser.

Walks the document body once, tracking the active heading trail (Heading 1
through Heading 6 styles). Emits one ParsedChunk per non-empty paragraph,
with the heading trail attached so a reviewer can see "this came from
section 3.2.1 of Study Report X" without opening the document.

Table cells are emitted as TABLE_ROW chunks; list items as LIST_ITEM.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from shared.schemas import CanonicalDocument, ChunkKind, DocxLocator, ParsedChunk

from .parser import hash_text, new_chunk_id


_HEADING_PATTERN = re.compile(r"^Heading (\d+)$")


def _heading_level(paragraph: Paragraph) -> int | None:
    """Return the heading level (1-6) if this paragraph is a heading, else None."""
    style_name = paragraph.style.name if paragraph.style else ""
    if style_name == "Title":
        return 1
    m = _HEADING_PATTERN.match(style_name)
    if m:
        level = int(m.group(1))
        if 1 <= level <= 6:
            return level
    return None


class DocxParser:
    mime_types = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    def parse(self, doc: CanonicalDocument, raw: bytes) -> Iterable[ParsedChunk]:
        document = Document(io.BytesIO(raw))

        heading_trail: list[str] = []
        paragraph_index = 0

        for element in _iter_block_items(document):
            if isinstance(element, Paragraph):
                text = element.text.strip()
                if not text:
                    paragraph_index += 1
                    continue

                level = _heading_level(element)
                if level is not None:
                    # Resize heading trail to this level - 1 (drop deeper siblings),
                    # then append the current heading.
                    heading_trail = heading_trail[: level - 1] + [text]
                    yield self._make_chunk(
                        doc=doc,
                        text=text,
                        kind=ChunkKind.HEADING,
                        heading_trail=list(heading_trail),
                        paragraph_index=paragraph_index,
                    )
                else:
                    kind = (
                        ChunkKind.LIST_ITEM
                        if element.style and element.style.name.startswith("List")
                        else ChunkKind.PARAGRAPH
                    )
                    yield self._make_chunk(
                        doc=doc,
                        text=text,
                        kind=kind,
                        heading_trail=list(heading_trail),
                        paragraph_index=paragraph_index,
                    )
                paragraph_index += 1

            elif isinstance(element, Table):
                for row in element.rows:
                    row_text = " | ".join(c.text.strip() for c in row.cells)
                    if not row_text.strip("| "):
                        paragraph_index += 1
                        continue
                    yield self._make_chunk(
                        doc=doc,
                        text=row_text,
                        kind=ChunkKind.TABLE_ROW,
                        heading_trail=list(heading_trail),
                        paragraph_index=paragraph_index,
                    )
                    paragraph_index += 1

    @staticmethod
    def _make_chunk(
        *,
        doc: CanonicalDocument,
        text: str,
        kind: ChunkKind,
        heading_trail: list[str],
        paragraph_index: int,
    ) -> ParsedChunk:
        return ParsedChunk(
            chunk_id=new_chunk_id(),
            source_doc_id=doc.doc_id,
            source_doc_version=doc.doc_version,
            kind=kind,
            text=text,
            text_hash=hash_text(text),
            char_count=len(text),
            locator=DocxLocator(heading_trail=heading_trail, paragraph_index=paragraph_index),
            tags=list(doc.tags),
        )


def _iter_block_items(document: Document) -> Iterable[Paragraph | Table]:
    """Yield paragraphs and tables in document order.

    python-docx exposes them separately but we need a single ordered traversal
    so paragraph_index reflects the true reading order.
    """
    from docx.oxml.ns import qn  # type: ignore[import-untyped]

    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)
