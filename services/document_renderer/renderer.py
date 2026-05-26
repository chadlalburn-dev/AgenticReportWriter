"""Renderer protocol + intermediate RenderSpec.

A RenderSpec is an ordered list of high-level render operations
(insert heading, insert paragraph with footnotes, insert table). Concrete
renderers translate this list into their backend's API:
  - GoogleDocsRenderer: Docs API batchUpdate requests
  - DryRunRenderer: a JSON dump of the spec for offline inspection

Keeping the spec separate from any one backend means we can add a
docx-direct renderer (python-docx) later without rewriting the section -> ops
translation logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from shared.schemas import Citation

from services.generation_orchestrator.types import (
    GeneratedSection,
    ReportInstance,
)


# --- Render operations ------------------------------------------------------


@dataclass(frozen=True)
class InsertHeading:
    text: str
    level: int  # 1..6


@dataclass(frozen=True)
class InsertParagraph:
    text: str
    # 0-based indices into the paragraph text where footnote markers should be
    # inserted, with the citation_id (which the renderer maps to a number) to
    # attach. The DryRunRenderer keeps them as raw citation_ids; the
    # GoogleDocsRenderer renumbers globally.
    footnote_anchors: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class InsertTable:
    caption: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    citation_id: str | None = None


@dataclass(frozen=True)
class InsertPageBreak:
    pass


@dataclass(frozen=True)
class InsertCitationsAppendix:
    """Render the citations section as a numbered list at the end of the doc.

    Each citation_id corresponds to a footnote anchor placed earlier in the
    document. The renderer is responsible for keeping numbering consistent.
    """

    citations: tuple[Citation, ...]


RenderOp = (
    InsertHeading
    | InsertParagraph
    | InsertTable
    | InsertPageBreak
    | InsertCitationsAppendix
)


@dataclass
class RenderSpec:
    title: str
    operations: list[RenderOp] = field(default_factory=list)


# --- Renderer protocol ------------------------------------------------------


class DocumentRenderer(Protocol):
    """Applies a RenderSpec.

    Returns a backend-specific artifact (Docs URL for live; JSON path for
    DryRun). Callers should not assume a specific shape — log it and present
    to the user via the orchestrator.
    """

    def render(self, spec: RenderSpec) -> object: ...


# --- Translation: ReportInstance + Citations -> RenderSpec -----------------


def spec_from_report(
    instance: ReportInstance, citations: list[Citation]
) -> RenderSpec:
    """Translate a generated report into render operations.

    Walks the section tree depth-first. Each section emits:
      - a heading at its level
      - one InsertParagraph per generated paragraph, with footnote anchors
        attached at the END of each cited claim's text within the paragraph
      - one InsertTable per generated table

    Citations are also collected for an appendix at the end so reviewers
    have a flat list of sources.
    """
    spec = RenderSpec(title=instance.report_title)

    def walk(section: GeneratedSection) -> None:
        spec.operations.append(InsertHeading(text=section.title, level=section.level))
        for paragraph in section.paragraphs:
            anchors = _anchor_positions_for_paragraph(paragraph.text, paragraph.claims)
            spec.operations.append(
                InsertParagraph(text=paragraph.text, footnote_anchors=tuple(anchors))
            )
        for table in section.tables:
            spec.operations.append(
                InsertTable(
                    caption=table.caption,
                    columns=tuple(table.columns),
                    rows=tuple(tuple(row) for row in table.rows),
                    citation_id=table.citation_id,
                )
            )
        for child in section.children:
            walk(child)

    for section in instance.sections:
        walk(section)

    if citations:
        spec.operations.append(InsertCitationsAppendix(citations=tuple(citations)))

    return spec


def _anchor_positions_for_paragraph(
    paragraph_text: str, claims: list
) -> list[tuple[int, str]]:
    """Place a footnote anchor at the end of each claim's text within the
    paragraph, if the claim's text appears there. Falls back to attaching
    the anchor at the end of the paragraph.

    Returns: list of (char_index, citation_id) pairs, sorted by index.
    """
    anchors: list[tuple[int, str]] = []
    para_len = len(paragraph_text)
    for claim in claims:
        for cid in claim.citation_ids:
            # Try to find the claim's text in the paragraph; place the
            # anchor at the end of that span. Fall back to the end of
            # the paragraph.
            if claim.text and claim.text in paragraph_text:
                idx = paragraph_text.index(claim.text) + len(claim.text)
            else:
                idx = para_len
            anchors.append((idx, cid))
    anchors.sort(key=lambda a: a[0])
    return anchors
