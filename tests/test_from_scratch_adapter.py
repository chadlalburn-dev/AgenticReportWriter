"""Tests for the FromScratchAdapter — LLM-proposed template structure."""

from __future__ import annotations

import pytest

from services.template_service import (
    FromScratchAdapter,
    FromScratchAdapterOptions,
    ScopingSpec,
    TemplateBuilder,
)
from shared.llm import (
    LlmRequest,
    LlmResponse,
    StubLlmClient,
    StructuredOutputError,
)
from shared.schemas import GenerationMode, TemplateStatus


def _build_outline_stub(outline_sections: list[dict]) -> StubLlmClient:
    """A StubLlmClient that returns a canned ProposedOutline."""
    stub = StubLlmClient(strict=True)

    def matcher(r: LlmRequest) -> bool:
        return r.response_schema_name == "ProposedOutline"

    def generator(r: LlmRequest) -> LlmResponse:
        return stub.make_response(parsed_json={"sections": outline_sections})

    stub.register_handler(matcher, generator)
    return stub


def test_adapter_proposes_flat_template() -> None:
    stub = _build_outline_stub(
        [
            {"section_id": "1", "title": "Introduction", "level": 1, "intent": "Set context for the IB.", "suggested_tag": None},
            {"section_id": "2", "title": "Methods", "level": 1, "intent": "Describe methods.", "suggested_tag": None},
            {"section_id": "3", "title": "Results", "level": 1, "intent": "Present results.", "suggested_tag": None},
        ]
    )
    adapter = FromScratchAdapter(stub, FromScratchAdapterOptions(template_id="t"))
    template = adapter.propose(
        ScopingSpec(report_type="custom", title="My Custom Report")
    )
    assert template.template_id == "t"
    assert template.title == "My Custom Report"
    assert template.status == TemplateStatus.DRAFT
    assert template.metadata.source_origin == "from_scratch"
    section_ids = [s.section_id for s in template.sections]
    assert section_ids == ["1", "2", "3"]


def test_adapter_builds_nested_tree() -> None:
    stub = _build_outline_stub(
        [
            {"section_id": "1", "title": "Introduction", "level": 1, "intent": "Introduce.", "suggested_tag": None},
            {"section_id": "2", "title": "Nonclinical", "level": 1, "intent": "Cover nonclinical work.", "suggested_tag": None},
            {"section_id": "2.1", "title": "Pharmacology", "level": 2, "intent": "Pharmacology details.", "suggested_tag": "pharmacology"},
            {"section_id": "2.2", "title": "Toxicology", "level": 2, "intent": "Toxicology details.", "suggested_tag": "toxicology"},
            {"section_id": "3", "title": "Clinical", "level": 1, "intent": "Clinical findings.", "suggested_tag": None},
        ]
    )
    template = FromScratchAdapter(
        stub, FromScratchAdapterOptions(template_id="t")
    ).propose(ScopingSpec(report_type="ich_ib", title="IB"))
    top_titles = [s.title for s in template.sections]
    assert top_titles == ["Introduction", "Nonclinical", "Clinical"]
    nonclinical = template.sections[1]
    child_titles = [c.title for c in nonclinical.children]
    assert child_titles == ["Pharmacology", "Toxicology"]


def test_adapter_attaches_file_set_binding_for_suggested_tag() -> None:
    stub = _build_outline_stub(
        [
            {"section_id": "1", "title": "Safety", "level": 1, "intent": "Cover safety.", "suggested_tag": "safety"},
        ]
    )
    template = FromScratchAdapter(
        stub, FromScratchAdapterOptions(template_id="t")
    ).propose(ScopingSpec(report_type="x", title="x"))
    safety = template.sections[0]
    binding_ids = {b.binding_id for b in safety.data_bindings}
    # product_name is always present
    assert "product_name" in binding_ids
    # The suggested_tag drives a file_set binding
    assert "safety_docs" in binding_ids


def test_adapter_proposed_sections_default_to_llm_mode_with_citations() -> None:
    stub = _build_outline_stub(
        [
            {"section_id": "1", "title": "Section", "level": 1, "intent": "Do thing.", "suggested_tag": None},
        ]
    )
    template = FromScratchAdapter(
        stub, FromScratchAdapterOptions(template_id="t")
    ).propose(ScopingSpec(report_type="x", title="x"))
    section = template.sections[0]
    assert section.generation.mode == GenerationMode.LLM
    assert section.citation_policy.required


def test_adapter_intent_appears_in_prompt_template() -> None:
    stub = _build_outline_stub(
        [
            {"section_id": "1", "title": "Section", "level": 1, "intent": "Describe the drug's mechanism in 2-3 paragraphs.", "suggested_tag": None},
        ]
    )
    template = FromScratchAdapter(
        stub, FromScratchAdapterOptions(template_id="t")
    ).propose(ScopingSpec(report_type="x", title="x"))
    section = template.sections[0]
    assert "Describe the drug's mechanism" in (section.generation.prompt_template or "")


def test_adapter_raises_on_empty_outline() -> None:
    stub = _build_outline_stub([])
    with pytest.raises(StructuredOutputError, match="zero sections"):
        FromScratchAdapter(
            stub, FromScratchAdapterOptions(template_id="t")
        ).propose(ScopingSpec(report_type="x", title="x"))


def test_adapter_raises_when_model_returns_no_structured_output() -> None:
    stub = StubLlmClient(strict=True)
    # Return a response with no parsed_json.
    stub.register_handler(
        lambda r: r.response_schema_name == "ProposedOutline",
        lambda r: stub.make_response(text="just text, no JSON"),
    )
    with pytest.raises(StructuredOutputError, match="no structured output"):
        FromScratchAdapter(
            stub, FromScratchAdapterOptions(template_id="t")
        ).propose(ScopingSpec(report_type="x", title="x"))


def test_adapter_passes_scoping_spec_to_llm() -> None:
    """The scoping spec's fields must surface in the LLM prompt."""
    captured: dict[str, str] = {}
    stub = StubLlmClient(strict=True)

    def matcher(r: LlmRequest) -> bool:
        return r.response_schema_name == "ProposedOutline"

    def generator(r: LlmRequest) -> LlmResponse:
        captured["user_message"] = r.messages[-1].content
        return stub.make_response(
            parsed_json={"sections": [{"section_id": "1", "title": "x", "level": 1, "intent": "do", "suggested_tag": None}]}
        )

    stub.register_handler(matcher, generator)
    FromScratchAdapter(
        stub, FromScratchAdapterOptions(template_id="t")
    ).propose(
        ScopingSpec(
            report_type="ICH E6 IB",
            title="XYZ-001 IB",
            audience="external regulators",
            intent="Support a Phase 2 IND submission",
            key_themes=("kinase Z biology", "first-in-human safety"),
            available_source_systems=("Veeva", "Medidata"),
            expected_total_pages=(50, 100),
            additional_notes="Avoid GxP-restricted PK numbers in summary.",
        )
    )
    msg = captured["user_message"]
    assert "ICH E6 IB" in msg
    assert "external regulators" in msg
    assert "kinase Z biology" in msg
    assert "Veeva, Medidata" in msg
    assert "50-100" in msg
    assert "GxP-restricted" in msg


# --- Builder façade --------------------------------------------------------


def test_builder_from_scratch_returns_template() -> None:
    stub = _build_outline_stub(
        [
            {"section_id": "1", "title": "Intro", "level": 1, "intent": "Intro.", "suggested_tag": None},
            {"section_id": "2", "title": "Body", "level": 1, "intent": "Body.", "suggested_tag": None},
            {"section_id": "3", "title": "End", "level": 1, "intent": "End.", "suggested_tag": None},
        ]
    )
    result = TemplateBuilder().from_scratch(
        ScopingSpec(report_type="memo", title="A Memo"),
        client=stub,
        template_id="memo_1",
    )
    assert result.template.template_id == "memo_1"
    assert len(result.template.sections) == 3
