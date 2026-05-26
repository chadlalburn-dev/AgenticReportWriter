"""Tests for the DOCX, XLSX, and PDF parsers.

Each parser is fed an in-memory CanonicalDocument + bytes (generated in
conftest) and asserted against the chunks it emits — focusing on the
load-bearing invariant: every chunk carries a locator precise enough to
back a citation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from services.parsing_service.docx_parser import DocxParser
from services.parsing_service.pdf_parser import PdfParser
from services.parsing_service.registry import default_registry
from services.parsing_service.xlsx_parser import XlsxParser
from shared.schemas import (
    CanonicalDocument,
    ChunkKind,
    DocxLocator,
    PdfLocator,
    SourceSystem,
    XlsxLocator,
)

from tests.conftest import make_sample_docx, make_sample_pdf, make_sample_xlsx


def _make_doc(mime_type: str, doc_id: str = "test::doc") -> CanonicalDocument:
    return CanonicalDocument(
        doc_id=doc_id,
        doc_version="v1",
        content_hash="0" * 64,
        source_system=SourceSystem.LOCAL_FILE,
        source_id=doc_id,
        system_of_record_url=f"file:///{doc_id}",
        mime_type=mime_type,
        retrieval_timestamp=datetime.now(timezone.utc),
        storage_uri=f"local://{doc_id}",
        tags=["nonclinical", "pk"],
    )


# --- DOCX ---


def test_docx_parser_emits_headings_paragraphs_and_tables() -> None:
    doc = _make_doc(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    chunks = list(DocxParser().parse(doc, make_sample_docx()))

    assert len(chunks) > 0

    kinds = [c.kind for c in chunks]
    assert ChunkKind.HEADING in kinds
    assert ChunkKind.PARAGRAPH in kinds
    assert ChunkKind.TABLE_ROW in kinds

    for c in chunks:
        assert isinstance(c.locator, DocxLocator)
        assert c.text_hash
        assert c.char_count == len(c.text)


def test_docx_parser_tracks_heading_trail() -> None:
    doc = _make_doc(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    chunks = list(DocxParser().parse(doc, make_sample_docx()))

    # The "NOAEL was identified..." paragraph appears under H1 "Nonclinical Studies"
    # > H2 "Toxicology". Verify the trail.
    noael_chunk = next(c for c in chunks if "NOAEL" in c.text)
    assert isinstance(noael_chunk.locator, DocxLocator)
    assert noael_chunk.locator.heading_trail == ["Nonclinical Studies", "Toxicology"]


def test_docx_parser_inherits_tags_from_document() -> None:
    doc = _make_doc(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    chunks = list(DocxParser().parse(doc, make_sample_docx()))
    for c in chunks:
        assert c.tags == ["nonclinical", "pk"]


# --- XLSX ---


def test_xlsx_parser_emits_sheet_region_with_cell_range() -> None:
    doc = _make_doc(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    chunks = list(XlsxParser().parse(doc, make_sample_xlsx()))

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.kind == ChunkKind.SHEET_REGION
    assert isinstance(chunk.locator, XlsxLocator)
    assert chunk.locator.sheet == "AE_Summary"
    assert chunk.locator.cell_range == "A1:C3"
    assert "Gastrointestinal disorders" in chunk.text
    assert "Nervous system disorders" in chunk.text


# --- PDF ---


def test_pdf_parser_emits_chunks_with_page_numbers() -> None:
    doc = _make_doc("application/pdf")
    chunks = list(PdfParser().parse(doc, make_sample_pdf()))

    assert len(chunks) >= 2
    # Page 1 chunks should be page=1, page 2 chunks should be page=2
    pages = {chunk.locator.page for chunk in chunks if isinstance(chunk.locator, PdfLocator)}
    assert 1 in pages
    assert 2 in pages

    # The compound name should appear on page 1
    page_1_text = " ".join(
        c.text for c in chunks if isinstance(c.locator, PdfLocator) and c.locator.page == 1
    )
    assert "Compound XYZ-001" in page_1_text


# --- Registry dispatch ---


def test_registry_dispatches_by_mime_type() -> None:
    registry = default_registry()
    docx_chunks = registry.parse(
        _make_doc(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        make_sample_docx(),
    )
    pdf_chunks = registry.parse(_make_doc("application/pdf"), make_sample_pdf())
    xlsx_chunks = registry.parse(
        _make_doc("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        make_sample_xlsx(),
    )
    assert docx_chunks and pdf_chunks and xlsx_chunks


def test_registry_raises_for_unknown_mime_type() -> None:
    from services.parsing_service.parser import UnsupportedMimeType

    registry = default_registry()
    try:
        registry.parse(_make_doc("application/x-weird-format"), b"")
    except UnsupportedMimeType:
        pass
    else:
        raise AssertionError("expected UnsupportedMimeType")


# --- End-to-end: connector + parser ---


def test_end_to_end_connector_to_parser(tmp_path) -> None:
    from services.ingestion_service.connectors import ConnectorContext, LocalFileConnector

    (tmp_path / "nonclinical").mkdir()
    (tmp_path / "nonclinical" / "study.docx").write_bytes(make_sample_docx())
    (tmp_path / "ae_summary.xlsx").write_bytes(make_sample_xlsx())

    connector = LocalFileConnector()
    context = ConnectorContext(tenant_id="gsk", team_id="ib-pilot", run_id="r1")

    registry = default_registry()
    all_chunks = []
    for doc, raw in connector.ingest(str(tmp_path), context):
        all_chunks.extend(registry.parse(doc, raw))

    # Should have at least: headings, paragraphs, table rows from DOCX + sheet region from XLSX
    kinds = {c.kind for c in all_chunks}
    assert ChunkKind.HEADING in kinds
    assert ChunkKind.PARAGRAPH in kinds
    assert ChunkKind.TABLE_ROW in kinds
    assert ChunkKind.SHEET_REGION in kinds
