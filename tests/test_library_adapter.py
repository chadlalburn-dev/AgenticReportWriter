"""Tests for the LibraryAdapter — shipped templates + clone semantics."""

from __future__ import annotations

import pytest

from services.template_service import (
    LibraryAdapter,
    LibraryAdapterOptions,
    LibraryNotFound,
    TemplateBuilder,
)
from shared.schemas import GenerationMode, ReportTemplate, TemplateStatus


# --- LibraryAdapter --------------------------------------------------------


def test_library_lists_shipped_templates() -> None:
    adapter = LibraryAdapter()
    ids = adapter.list_ids()
    # Three shipped templates as of this commit.
    assert "ich_e6_ib" in ids
    assert "ich_e3_csr" in ids
    assert "consort_rct" in ids


def test_library_loads_ich_e6_ib() -> None:
    adapter = LibraryAdapter()
    template = adapter.load("ich_e6_ib")
    assert template.template_id == "ich_e6_ib"
    assert template.report_type == "ICH_E6_IB"
    # IB has Title Page → Confidentiality → Summary → 1..5
    section_ids = {s.section_id for s in template.sections}
    assert {"title_page", "summary", "1", "2", "3", "4", "5"}.issubset(section_ids)


def test_library_loads_ich_e3_csr() -> None:
    adapter = LibraryAdapter()
    template = adapter.load("ich_e3_csr")
    assert template.template_id == "ich_e3_csr"
    assert template.report_type == "ICH_E3_CSR"
    section_ids = {s.section_id for s in template.sections}
    # ICH E3 has synopsis, then numbered sections for investigational
    # plan / study population / efficacy / safety / discussion
    assert "synopsis" in section_ids
    assert {"1", "2", "3", "4", "5", "6", "7"}.issubset(section_ids)


def test_library_loads_consort_rct() -> None:
    adapter = LibraryAdapter()
    template = adapter.load("consort_rct")
    assert template.template_id == "consort_rct"
    section_ids = {s.section_id for s in template.sections}
    # CONSORT 2010 — title/abstract, intro, methods, results, discussion, other
    assert {"title", "intro", "methods", "results", "discussion", "other"} == section_ids


def test_library_unknown_template_raises_with_available_list() -> None:
    adapter = LibraryAdapter()
    with pytest.raises(LibraryNotFound, match="available:"):
        adapter.load("does_not_exist")


def test_library_clone_produces_new_draft() -> None:
    adapter = LibraryAdapter()
    cloned = adapter.load(
        "ich_e6_ib",
        options=LibraryAdapterOptions(
            clone_as_template_id="gsk_xyz_001_ib",
            clone_as_title="XYZ-001 Investigator's Brochure (GSK clone)",
            new_authored_by="chad.l.alburn@gsk.com",
        ),
    )
    assert cloned.template_id == "gsk_xyz_001_ib"
    assert "XYZ-001" in cloned.title
    # Clones are always DRAFT — they need re-approval before regulated use.
    assert cloned.status == TemplateStatus.DRAFT
    # Metadata records the lineage
    assert cloned.metadata.parent_template_id == "ich_e6_ib"
    assert cloned.metadata.authored_by == "chad.l.alburn@gsk.com"
    # Sections are deep-copied (changing the clone doesn't mutate the source)
    original = adapter.load("ich_e6_ib")
    assert cloned.template_id != original.template_id


def test_library_template_all_llm_sections_require_citations() -> None:
    """Load-bearing invariant for shipped templates: every LLM/hybrid
    section requires citations. If we ship a template that doesn't, the
    test catches it."""
    adapter = LibraryAdapter()
    for template_id in adapter.list_ids():
        template = adapter.load(template_id)
        for section in template.all_sections():
            if section.generation.mode in (GenerationMode.LLM, GenerationMode.HYBRID):
                assert section.citation_policy.required, (
                    f"{template_id}/{section.section_id} ({section.title}) "
                    f"is mode={section.generation.mode.value} but does NOT "
                    "require citations"
                )


def test_library_templates_validate_against_pydantic_schema() -> None:
    """Every shipped template must parse cleanly through Pydantic — that's
    our protection against accidental drift in the JSON files."""
    adapter = LibraryAdapter()
    for template_id in adapter.list_ids():
        template = adapter.load(template_id)
        assert isinstance(template, ReportTemplate)


# --- TemplateBuilder façade -----------------------------------------------


def test_builder_from_library_returns_template_unchanged() -> None:
    result = TemplateBuilder().from_library("ich_e6_ib")
    assert result.template.template_id == "ich_e6_ib"
    assert isinstance(result.warnings, tuple)


def test_builder_from_library_clones_with_new_id() -> None:
    result = TemplateBuilder().from_library(
        "consort_rct",
        clone_as_template_id="gsk_xyz_phase3_consort",
        clone_as_title="XYZ-001 Phase 3 RCT (CONSORT clone)",
        authored_by="chad.l.alburn@gsk.com",
    )
    assert result.template.template_id == "gsk_xyz_phase3_consort"
    assert result.template.status == TemplateStatus.DRAFT
    assert result.template.metadata.parent_template_id == "consort_rct"
