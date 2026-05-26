"""End-to-end test of the generation orchestrator on the synthetic IB corpus.

This test exercises the full plan → fill → critique loop using a smart
StubLlmClient that inspects each request, extracts the citation_ids the
orchestrator allocated, and produces a structurally-valid response.

It verifies:
- Every section that should be filled actually was filled
- All citation_ids the model "used" are valid (no fabrications)
- Citations point back to real source chunks
- Audit events are recorded per phase
- Re-assembled section tree mirrors the template structure
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from services.generation_orchestrator.orchestrator import ReportGenerator
from services.ingestion_service.connectors import ConnectorContext, LocalFileConnector
from services.parsing_service.registry import default_registry
from shared.llm import (
    LlmRequest,
    LlmResponse,
    ModelTier,
    StubLlmClient,
)
from shared.schemas import ReportTemplate


REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_ROOT = REPO_ROOT / "samples" / "synthetic_compound" / "sources"
IB_TEMPLATE_PATH = REPO_ROOT / "templates" / "library" / "ich_e6_ib.json"


@pytest.fixture(scope="module")
def corpus_chunks() -> tuple[list, dict]:
    """Ingest and parse the synthetic corpus once for the module."""
    if not CORPUS_ROOT.exists() or not any(CORPUS_ROOT.rglob("*.docx")):
        spec = importlib.util.spec_from_file_location(
            "_corpus_gen",
            REPO_ROOT / "samples" / "synthetic_compound" / "generate_corpus.py",
        )
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        assert spec and spec.loader
        spec.loader.exec_module(module)
        module.main()

    connector = LocalFileConnector()
    context = ConnectorContext(tenant_id="gsk", team_id="ib-pilot", run_id="r-e2e")
    registry = default_registry()
    docs = []
    chunks_by_doc: dict[str, list] = {}
    for doc, raw in connector.ingest(str(CORPUS_ROOT), context):
        docs.append(doc)
        chunks_by_doc[doc.doc_id] = registry.parse(doc, raw)
    return docs, chunks_by_doc


@pytest.fixture(scope="module")
def ib_template() -> ReportTemplate:
    return ReportTemplate.model_validate(
        json.loads(IB_TEMPLATE_PATH.read_text(encoding="utf-8"))
    )


_CITATION_RE = re.compile(r"\[citation_id=([0-9a-f-]+)\]")


def _build_smart_stub() -> StubLlmClient:
    """A stub that:
    - Plan: returns a minimal valid plan
    - Fill: parrots the first citation_id back in a one-paragraph response
    - Critique: returns 'pass' with no issues
    """
    stub = StubLlmClient(strict=True)

    def plan_match(r: LlmRequest) -> bool:
        return r.response_schema_name == "PlanOutput"

    def plan_gen(r: LlmRequest) -> LlmResponse:
        return stub.make_response(
            parsed_json={
                "overall_summary": "Synthetic plan for XYZ-001 IB",
                "section_plans": [],
            }
        )

    def fill_match(r: LlmRequest) -> bool:
        return r.response_schema_name == "FillOutput"

    def fill_gen(r: LlmRequest) -> LlmResponse:
        user_msg = r.messages[-1].content
        cite_ids = _CITATION_RE.findall(user_msg)

        # Match the length policy from the request so the critic doesn't reject
        # us for being too short. Extract min/max words from the user message.
        m_lo = re.search(r"Target length:\s*(\d+)-(\d+)\s*words", user_msg)
        target_lo = int(m_lo.group(1)) if m_lo else 200
        target_hi = int(m_lo.group(2)) if m_lo else 800
        target_words = (target_lo + target_hi) // 2

        # Build a paragraph of approximately target_words. Cite the first
        # available chunk on the numeric claim.
        primary = cite_ids[0] if cite_ids else ""

        sentences = [
            "Compound XYZ-001 was evaluated in a comprehensive nonclinical and clinical program.",
            "The pharmacokinetic profile supports once-daily oral dosing.",
            "Safety findings to date include manageable gastrointestinal events and reversible hepatic enzyme elevations.",
        ]
        body = " ".join(sentences)
        # Pad to reach approximately target_words
        padding_unit = (
            "Investigators are advised to follow the recommended monitoring schedule "
            "and to refer to the section-specific safety guidance in this brochure. "
        )
        current_words = len(body.split())
        if current_words < target_words:
            n_pads = (target_words - current_words) // len(padding_unit.split()) + 1
            body = body + " " + (padding_unit * n_pads)
        # Cap at target_hi
        words = body.split()
        if len(words) > target_hi:
            body = " ".join(words[:target_hi])

        claims = []
        if primary:
            claims.append({"text": "PK supports QD dosing.", "citation_ids": [primary]})

        return stub.make_response(
            parsed_json={"paragraphs": [{"text": body, "claims": claims}]}
        )

    def critique_match(r: LlmRequest) -> bool:
        return r.response_schema_name == "CritiqueOutput"

    def critique_gen(r: LlmRequest) -> LlmResponse:
        return stub.make_response(parsed_json={"verdict": "pass", "issues": []})

    stub.register_handler(plan_match, plan_gen)
    stub.register_handler(fill_match, fill_gen)
    stub.register_handler(critique_match, critique_gen)
    return stub


def test_orchestrator_runs_full_ib_template(
    corpus_chunks: tuple, ib_template: ReportTemplate
) -> None:
    docs, chunks_by_doc = corpus_chunks
    stub = _build_smart_stub()
    generator = ReportGenerator(fill_client=stub, max_retries_per_section=1)

    result = generator.generate(
        template=ib_template,
        documents=docs,
        chunks_by_doc=chunks_by_doc,
        free_text_inputs={
            "product_name": "XYZ-001",
            "sponsor_name": "Acme Therapeutics (synthetic)",
            "ib_edition": "Edition 1.0",
            "release_date": "2026-05-26",
        },
        compliance_mode="rd",
    )

    # The plan ran once
    plan_events = [e for e in result.audit_events if e.phase == "plan"]
    assert len(plan_events) == 1

    # Every LLM section in the template got a fill + critique event
    llm_section_ids = {
        s.section_id
        for s in ib_template.all_sections()
        if s.generation.mode.value in ("llm", "hybrid")
    }
    fill_sections = {e.section_id for e in result.audit_events if e.phase == "fill"}
    assert llm_section_ids == fill_sections

    # Every fill event paired with at least one critique event
    critique_sections = {e.section_id for e in result.audit_events if e.phase == "critique"}
    assert llm_section_ids == critique_sections

    # The instance has the expected top-level sections
    top_ids = {s.section_id for s in result.instance.sections}
    assert {"title_page", "confidentiality", "summary", "1", "2", "3", "4", "5"}.issubset(top_ids)

    # Citations point back to chunks we actually ingested
    chunk_ids_seen = {c.chunk_id for chunks in chunks_by_doc.values() for c in chunks}
    for citation in result.citations:
        assert citation.retrieval_chunk_id in chunk_ids_seen, (
            f"citation {citation.citation_id} points at unknown chunk "
            f"{citation.retrieval_chunk_id}"
        )

    # The reassembled tree has the expected nesting (Section 3 has 3.1/3.2/3.3)
    sec3 = next(s for s in result.instance.sections if s.section_id == "3")
    sub_ids = {c.section_id for c in sec3.children}
    assert sub_ids == {"3.1", "3.2", "3.3"}

    # At least one LLM-driven section produced citations (sanity)
    assert result.citations, "expected at least one citation to be emitted"


def test_orchestrator_rejects_fabricated_citation_ids(
    corpus_chunks: tuple, ib_template: ReportTemplate
) -> None:
    docs, chunks_by_doc = corpus_chunks
    stub = StubLlmClient(strict=True)

    # Plan: minimal valid
    stub.register_handler(
        lambda r: r.response_schema_name == "PlanOutput",
        lambda r: stub.make_response(
            parsed_json={"overall_summary": "x", "section_plans": []}
        ),
    )
    # Fill: emit a citation_id that was never in the chunk pool
    stub.register_handler(
        lambda r: r.response_schema_name == "FillOutput",
        lambda r: stub.make_response(
            parsed_json={
                "paragraphs": [
                    {
                        "text": "Bad citation.",
                        "claims": [
                            {
                                "text": "claim",
                                "citation_ids": ["00000000-0000-0000-0000-000000000000"],
                            }
                        ],
                    }
                ]
            }
        ),
    )

    from shared.llm import StructuredOutputError

    gen = ReportGenerator(fill_client=stub, max_retries_per_section=0)
    with pytest.raises(StructuredOutputError, match="fabricated"):
        gen.generate(
            template=ib_template,
            documents=docs,
            chunks_by_doc=chunks_by_doc,
            free_text_inputs={
                "product_name": "XYZ-001",
                "sponsor_name": "Acme",
                "ib_edition": "1.0",
                "release_date": "2026-05-26",
            },
        )
