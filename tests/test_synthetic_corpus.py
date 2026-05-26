"""Smoke test: the synthetic IB corpus ingests and parses end-to-end.

If the synthetic corpus has not yet been generated, the test self-regenerates it
so a fresh checkout passes without manual setup.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from services.ingestion_service.connectors import ConnectorContext, LocalFileConnector
from services.parsing_service.registry import default_registry
from shared.schemas import ChunkKind, SourceSystem


REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_ROOT = REPO_ROOT / "samples" / "synthetic_compound" / "sources"


@pytest.fixture(scope="module", autouse=True)
def _ensure_corpus_generated() -> None:
    if CORPUS_ROOT.exists() and any(CORPUS_ROOT.rglob("*.docx")):
        return
    # Lazy import so the test doesn't require the corpus to exist for collection.
    spec = importlib.util.spec_from_file_location(
        "_corpus_gen",
        REPO_ROOT / "samples" / "synthetic_compound" / "generate_corpus.py",
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.main()


def test_synthetic_corpus_has_expected_documents() -> None:
    docx_files = list(CORPUS_ROOT.rglob("*.docx"))
    xlsx_files = list(CORPUS_ROOT.rglob("*.xlsx"))
    assert len(docx_files) >= 8, f"expected at least 8 DOCX, got {len(docx_files)}"
    assert len(xlsx_files) >= 4, f"expected at least 4 XLSX, got {len(xlsx_files)}"


def test_full_corpus_ingest_and_parse() -> None:
    connector = LocalFileConnector()
    context = ConnectorContext(tenant_id="gsk", team_id="ib-pilot", run_id="r-smoke")
    registry = default_registry()

    chunks_by_doc: dict[str, list] = {}
    for doc, raw in connector.ingest(str(CORPUS_ROOT), context):
        assert doc.source_system == SourceSystem.LOCAL_FILE
        chunks = registry.parse(doc, raw)
        assert chunks, f"no chunks produced for {doc.doc_id}"
        chunks_by_doc[doc.doc_id] = chunks

    assert len(chunks_by_doc) >= 12

    # Every document produced at least one chunk with text
    for doc_id, chunks in chunks_by_doc.items():
        for c in chunks:
            assert c.text.strip(), f"empty chunk in {doc_id}"


def test_safety_doc_contains_expected_facts() -> None:
    """Spot-check: the clinical safety summary should mention pneumonitis and the ORR."""
    connector = LocalFileConnector()
    context = ConnectorContext(tenant_id="gsk", team_id="ib-pilot", run_id="r-smoke")
    registry = default_registry()

    safety_path = CORPUS_ROOT / "clinical" / "safety" / "XYZ-101_XYZ-102_safety_summary.docx"
    docs = list(connector.ingest(str(safety_path), context))
    assert len(docs) == 1
    doc, raw = docs[0]
    chunks = registry.parse(doc, raw)
    full_text = " ".join(c.text for c in chunks)
    assert "pneumonitis" in full_text.lower()
    assert "31%" in full_text  # ORR


def test_ae_summary_xlsx_chunks_have_cell_ranges() -> None:
    connector = LocalFileConnector()
    context = ConnectorContext(tenant_id="gsk", team_id="ib-pilot", run_id="r-smoke")
    registry = default_registry()

    ae_path = CORPUS_ROOT / "clinical" / "data" / "ae_summary.xlsx"
    docs = list(connector.ingest(str(ae_path), context))
    assert docs
    chunks = registry.parse(docs[0][0], docs[0][1])
    # AE_by_SOC sheet + Top_AEs_PT sheet => at least 2 sheet-region chunks
    assert len(chunks) >= 2
    for c in chunks:
        assert c.kind == ChunkKind.SHEET_REGION
        # locator carries the cell range
        assert c.locator.cell_range  # type: ignore[union-attr]
