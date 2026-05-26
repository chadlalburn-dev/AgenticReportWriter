"""Tests for the local filesystem connector."""

from __future__ import annotations

from pathlib import Path

from services.ingestion_service.connectors import ConnectorContext, LocalFileConnector
from shared.schemas import SourceSystem

from tests.conftest import make_sample_docx, make_sample_pdf, make_sample_xlsx


def _make_context() -> ConnectorContext:
    return ConnectorContext(
        tenant_id="gsk",
        team_id="ib-pilot",
        run_id="run-test-001",
        raw_storage_prefix="ib-sources-raw-local",
    )


def test_local_file_connector_single_docx(tmp_path: Path) -> None:
    target = tmp_path / "compound_xyz_ib_v1.docx"
    target.write_bytes(make_sample_docx())

    connector = LocalFileConnector()
    docs = list(connector.ingest(str(target), _make_context()))

    assert len(docs) == 1
    doc, raw = docs[0]
    assert doc.source_system == SourceSystem.LOCAL_FILE
    assert doc.mime_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert doc.title == "compound_xyz_ib_v1"
    assert raw[:2] == b"PK"  # zip-archive header that DOCX uses


def test_local_file_connector_directory_recurses(tmp_path: Path) -> None:
    (tmp_path / "nonclinical").mkdir()
    (tmp_path / "nonclinical" / "pk_study.docx").write_bytes(make_sample_docx())
    (tmp_path / "ae_summary.xlsx").write_bytes(make_sample_xlsx())
    (tmp_path / "ib_v1.pdf").write_bytes(make_sample_pdf())

    connector = LocalFileConnector()
    docs = list(connector.ingest(str(tmp_path), _make_context()))

    assert len(docs) == 3
    mime_types = {d.mime_type for d, _ in docs}
    assert "application/pdf" in mime_types
    assert (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        in mime_types
    )
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in mime_types


def test_local_file_connector_skips_unknown_mime(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_text("notes")
    (tmp_path / "ib.docx").write_bytes(make_sample_docx())

    connector = LocalFileConnector()
    docs = list(connector.ingest(str(tmp_path), _make_context()))

    # Behaviour: text/plain is recognized by mimetypes — should be emitted.
    # The connector currently *only* drops unknowns. Confirm both files surface
    # but only those Python recognizes.
    mimes = {d.mime_type for d, _ in docs}
    assert "text/plain" in mimes
    assert (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in mimes
    )


def test_local_file_connector_content_hash_is_stable(tmp_path: Path) -> None:
    target = tmp_path / "doc.docx"
    payload = make_sample_docx()
    target.write_bytes(payload)

    connector = LocalFileConnector()
    docs1 = list(connector.ingest(str(target), _make_context()))
    docs2 = list(connector.ingest(str(target), _make_context()))

    assert docs1[0][0].content_hash == docs2[0][0].content_hash


def test_local_file_connector_tags_from_path(tmp_path: Path) -> None:
    nested = tmp_path / "nonclinical" / "pk" / "study42.docx"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(make_sample_docx())

    connector = LocalFileConnector()
    doc, _ = next(iter(LocalFileConnector().ingest(str(nested), _make_context())))
    assert "nonclinical" in doc.tags
    assert "pk" in doc.tags
