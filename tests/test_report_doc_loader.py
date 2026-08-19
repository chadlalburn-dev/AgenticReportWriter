"""Tests for the Markdown report-template loader (report_doc.load_report_doc)."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.template_service import ReportDocError, load_report_doc
from shared.schemas import DataBindingType, GenerationMode, ReportTemplate

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "report-templates"

REPORT_DOCS = [
    "candidate_selection_dossier.md",
    "compound_profile_onepager.md",
    "nonclinical_safety_summary.md",
    "dmpk_adme_summary.md",
    "target_assessment.md",
    "ib_nonclinical_sections.md",
]


@pytest.mark.parametrize("name", REPORT_DOCS)
def test_every_report_doc_loads(name: str) -> None:
    t = load_report_doc(TEMPLATES / name)
    assert isinstance(t, ReportTemplate)
    assert t.all_sections(), f"{name} produced no sections"
    # Load-bearing invariant: every LLM/hybrid section requires citations.
    for s in t.all_sections():
        if s.generation.mode in (GenerationMode.LLM, GenerationMode.HYBRID):
            assert s.citation_policy.required, f"{name}/{s.section_id} not requiring citations"


def _binding_types(template: ReportTemplate) -> set[DataBindingType]:
    return {b.type for s in template.all_sections() for b in s.data_bindings}


def test_candidate_selection_maps_all_source_types() -> None:
    t = load_report_doc(TEMPLATES / "candidate_selection_dossier.md")
    ids = [s.section_id for s in t.sections]
    assert ids == ["1", "2", "3", "4", "5", "6", "7", "8"]
    types = _binding_types(t)
    assert DataBindingType.NAMED_QUERY in types      # bigquery source
    assert DataBindingType.API_CALL in types          # confluence source
    assert DataBindingType.FILE_SET in types          # file source
    assert DataBindingType.FREE_TEXT_INPUT in types   # inputs


def test_confluence_source_becomes_confluence_api_binding() -> None:
    t = load_report_doc(TEMPLATES / "candidate_selection_dossier.md")
    api_bindings = [
        b for s in t.all_sections() for b in s.data_bindings
        if b.type == DataBindingType.API_CALL
    ]
    connectors = {b.connector_id for b in api_bindings}
    assert "confluence" in connectors


def test_target_assessment_wires_external_connectors() -> None:
    t = load_report_doc(TEMPLATES / "target_assessment.md")
    connectors = {
        b.connector_id for s in t.all_sections() for b in s.data_bindings
        if b.type == DataBindingType.API_CALL
    }
    assert "mock_chembl" in connectors
    assert "mock_clinicaltrials" in connectors
    assert "confluence" in connectors


def test_placeholder_rewritten_inputs_to_report() -> None:
    """`{{inputs.X}}` in the template must be rewritten to `{{report.X}}`
    so the engine's resolver substitutes it from the run inputs."""
    t = load_report_doc(TEMPLATES / "candidate_selection_dossier.md")
    named = [
        b for s in t.all_sections() for b in s.data_bindings
        if b.type == DataBindingType.NAMED_QUERY
    ]
    assert named
    joined = " ".join(v for b in named for v in b.parameters.values())
    assert "{{report." in joined
    assert "{{inputs." not in joined


def test_instruction_becomes_prompt_template() -> None:
    t = load_report_doc(TEMPLATES / "compound_profile_onepager.md")
    identity = next(s for s in t.sections if s.title.lower().startswith("identity"))
    assert identity.generation.prompt_template
    assert "chemical class" in identity.generation.prompt_template.lower()


def test_missing_frontmatter_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.md"
    p.write_text("# no front matter here\n\n## 1. Section\n", encoding="utf-8")
    with pytest.raises(ReportDocError, match="front-matter"):
        load_report_doc(p)


def test_unknown_source_reference_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.md"
    p.write_text(
        "---\nreport_type: x\ntitle: X\nsources: []\n---\n\n"
        "## 1. S\n> Instruction: do\n> Sources: ghost\n",
        encoding="utf-8",
    )
    with pytest.raises(ReportDocError, match="unknown source"):
        load_report_doc(p)
