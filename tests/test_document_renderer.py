"""Tests for the document renderer's spec generation + dry-run backend."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.document_renderer import (
    DryRunRenderer,
    RenderSpec,
    spec_from_report,
)
from services.document_renderer.renderer import (
    InsertCitationsAppendix,
    InsertHeading,
    InsertParagraph,
)
from services.generation_orchestrator.types import (
    GeneratedClaim,
    GeneratedParagraph,
    GeneratedSection,
    ReportInstance,
)
from shared.schemas import (
    Citation,
    CitationLocator,
    SourceType,
)


def _sample_instance_and_citations() -> tuple[ReportInstance, list[Citation]]:
    citation = Citation(
        citation_id="cit-1",
        report_instance_id="inst-1",
        source_type=SourceType.DOCX,
        source_uri="local://study42",
        source_doc_id="study42",
        source_doc_version="v1",
        locator=CitationLocator(heading_trail=["Nonclinical", "PK"], paragraph_index=4),
        snippet="Cmax 1840 ng/mL at 100 mg/kg",
        retrieved_at=datetime.now(timezone.utc),
    )

    sec_3 = GeneratedSection(
        section_id="3",
        title="Nonclinical Studies",
        level=1,
        children=[
            GeneratedSection(
                section_id="3.2",
                title="Pharmacokinetics",
                level=2,
                paragraphs=[
                    GeneratedParagraph(
                        text=(
                            "In rat, oral dosing produced Cmax 1840 ng/mL at 100 mg/kg. "
                            "Half-life was approximately 8 hours."
                        ),
                        claims=[
                            GeneratedClaim(
                                text="Cmax 1840 ng/mL at 100 mg/kg",
                                citation_ids=["cit-1"],
                            )
                        ],
                    )
                ],
            )
        ],
    )

    instance = ReportInstance(
        instance_id="inst-1",
        template_id="ich_e6_ib",
        template_version="0.1.0",
        compliance_mode="rd",
        report_title="Test IB",
        generated_at=datetime.now(timezone.utc),
        sections=[sec_3],
    )
    return instance, [citation]


def test_spec_from_report_walks_section_tree() -> None:
    instance, citations = _sample_instance_and_citations()
    spec = spec_from_report(instance, citations)

    # Headings come before paragraphs, in tree order; appendix is last.
    assert isinstance(spec.operations[0], InsertHeading)
    assert spec.operations[0].text == "Nonclinical Studies"
    assert spec.operations[0].level == 1

    assert isinstance(spec.operations[1], InsertHeading)
    assert spec.operations[1].text == "Pharmacokinetics"
    assert spec.operations[1].level == 2

    assert isinstance(spec.operations[2], InsertParagraph)
    assert "Cmax 1840 ng/mL" in spec.operations[2].text

    # The last op is the citations appendix
    assert isinstance(spec.operations[-1], InsertCitationsAppendix)
    assert spec.operations[-1].citations[0].citation_id == "cit-1"


def test_paragraph_footnote_anchors_point_at_claim_end() -> None:
    instance, citations = _sample_instance_and_citations()
    spec = spec_from_report(instance, citations)
    paragraph_op = next(op for op in spec.operations if isinstance(op, InsertParagraph))
    assert len(paragraph_op.footnote_anchors) == 1
    idx, cid = paragraph_op.footnote_anchors[0]
    # Anchor should land at the end of "Cmax 1840 ng/mL at 100 mg/kg" within the paragraph.
    claim_text = "Cmax 1840 ng/mL at 100 mg/kg"
    expected_idx = paragraph_op.text.index(claim_text) + len(claim_text)
    assert idx == expected_idx
    assert cid == "cit-1"


def test_dryrun_renderer_writes_expected_json(tmp_path: Path) -> None:
    instance, citations = _sample_instance_and_citations()
    spec = spec_from_report(instance, citations)
    output = tmp_path / "render.json"
    artifact = DryRunRenderer(output).render(spec)
    assert artifact.backend == "dry-run"
    assert artifact.json_path == output
    assert artifact.n_operations == len(spec.operations)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["title"] == "Test IB"
    kinds = [op["kind"] for op in payload["operations"]]
    assert "insert_heading" in kinds
    assert "insert_paragraph" in kinds
    assert "insert_citations_appendix" in kinds

    # The paragraph op preserved its footnote anchors
    paragraph_ops = [op for op in payload["operations"] if op["kind"] == "insert_paragraph"]
    assert len(paragraph_ops) == 1
    assert paragraph_ops[0]["footnote_anchors"][0]["citation_id"] == "cit-1"


def test_empty_citations_omits_appendix() -> None:
    """No citations → no appendix block (don't render an empty 'References' header)."""
    instance, _ = _sample_instance_and_citations()
    spec = spec_from_report(instance, [])
    assert not any(isinstance(op, InsertCitationsAppendix) for op in spec.operations)


def test_dryrun_renderer_creates_output_dir(tmp_path: Path) -> None:
    """The renderer should mkdir the output's parent if needed."""
    nested = tmp_path / "deep" / "nested" / "out.json"
    spec = RenderSpec(title="Test")
    DryRunRenderer(nested).render(spec)
    assert nested.exists()
