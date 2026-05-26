"""Sanity tests: the shared schemas import and the ICH E6 IB template parses."""

from __future__ import annotations

import json
from pathlib import Path

from shared.schemas import (
    DataBindingType,
    GenerationMode,
    ReportTemplate,
    TemplateSection,
    TemplateStatus,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
IB_TEMPLATE_PATH = REPO_ROOT / "templates" / "library" / "ich_e6_ib.json"


def _load_ib_template() -> ReportTemplate:
    return ReportTemplate.model_validate(json.loads(IB_TEMPLATE_PATH.read_text(encoding="utf-8")))


def test_ib_template_parses() -> None:
    template = _load_ib_template()
    assert template.template_id == "ich_e6_ib"
    assert template.report_type == "ICH_E6_IB"
    assert template.status == TemplateStatus.DRAFT


def test_ib_template_has_required_top_level_sections() -> None:
    template = _load_ib_template()
    section_ids = [s.section_id for s in template.sections]
    expected = {"title_page", "confidentiality", "summary", "1", "2", "3", "4", "5"}
    assert expected.issubset(set(section_ids)), f"missing top-level sections: {expected - set(section_ids)}"


def test_ib_template_nonclinical_subsections() -> None:
    template = _load_ib_template()
    nonclinical = next(s for s in template.sections if s.section_id == "3")
    sub_ids = {c.section_id for c in nonclinical.children}
    assert {"3.1", "3.2", "3.3"} == sub_ids


def test_ib_template_clinical_subsections() -> None:
    template = _load_ib_template()
    effects_in_humans = next(s for s in template.sections if s.section_id == "4")
    sub_ids = {c.section_id for c in effects_in_humans.children}
    assert {"4.1", "4.2", "4.3"} == sub_ids


def test_all_llm_sections_require_citations() -> None:
    """Every LLM-driven section in the IB requires citations — load-bearing for safety."""
    template = _load_ib_template()
    for section in template.all_sections():
        if section.generation.mode in (GenerationMode.LLM, GenerationMode.HYBRID):
            assert section.citation_policy.required, (
                f"section {section.section_id} '{section.title}' uses {section.generation.mode} "
                f"but does not require citations"
            )


def test_safety_section_has_named_queries() -> None:
    """Section 4.2 (Safety and Efficacy) must pull AE and exposure tables via named queries,
    not ad-hoc SQL — high-risk data path."""
    template = _load_ib_template()
    effects = next(s for s in template.sections if s.section_id == "4")
    safety = next(s for s in effects.children if s.section_id == "4.2")
    binding_types = {b.type for b in safety.data_bindings}
    assert DataBindingType.NAMED_QUERY in binding_types
    assert DataBindingType.SQL_QUERY not in binding_types, (
        "Section 4.2 should not use ad-hoc SQL — only registered named queries"
    )


def test_section_tree_flattening() -> None:
    template = _load_ib_template()
    flat = template.all_sections()
    section_ids = [s.section_id for s in flat]
    assert "3" in section_ids
    assert "3.1" in section_ids
    assert "3.2" in section_ids
    assert "3.3" in section_ids
    parent_idx = section_ids.index("3")
    child_idx = section_ids.index("3.1")
    assert parent_idx < child_idx, "parent should appear before children in depth-first flatten"


def test_recursive_section_works() -> None:
    """Sanity check that the recursive TemplateSection model rebuilds correctly."""
    s = TemplateSection(
        section_id="x",
        title="X",
        level=1,
        children=[TemplateSection(section_id="x.1", title="X.1", level=2)],
    )
    assert s.children[0].section_id == "x.1"
