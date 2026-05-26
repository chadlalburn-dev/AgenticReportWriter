"""End-to-end test: BindingResolver dispatches named_query and sql_query
bindings through the SqlSafetyGate, and the resolved bindings carry the
correct ResolvedQueryResult + SafetyVerdict back to the orchestrator.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from services.data_integration import (
    NamedQueryRegistry,
    SqlSafetyGate,
    SqliteQueryExecutor,
    auto_approve,
)
from services.generation_orchestrator.retrieval import BindingResolver
from shared.schemas import DataBindingType, TemplateSection
from shared.schemas.template import (
    CitationPolicy,
    GenerationPolicy,
    NamedQueryBinding,
    SqlQueryBinding,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
QUERIES_DIR = REPO_ROOT / "samples" / "synthetic_compound" / "queries"
EDC_SQLITE = REPO_ROOT / "samples" / "synthetic_compound" / "edc.sqlite"


@pytest.fixture(scope="module", autouse=True)
def _seed_edc_db() -> None:
    if EDC_SQLITE.exists():
        return
    spec = importlib.util.spec_from_file_location(
        "_seed", REPO_ROOT / "samples" / "synthetic_compound" / "seed_db.py"
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.seed()


@pytest.fixture
def gate() -> SqlSafetyGate:
    registry = NamedQueryRegistry.from_directory(QUERIES_DIR)
    executor = SqliteQueryExecutor(EDC_SQLITE, source="edc_warehouse", read_only=True)
    return SqlSafetyGate(
        executor=executor, registry=registry, approval_callback=auto_approve()
    )


def _make_resolver(gate: SqlSafetyGate | None) -> BindingResolver:
    return BindingResolver(
        chunks_by_doc={},
        docs_by_id={},
        free_text_inputs={"compound_id": "XYZ-001"},
        safety_gate=gate,
    )


def _section_with_binding(binding) -> TemplateSection:
    return TemplateSection(
        section_id="4.2",
        title="Safety and Efficacy",
        level=2,
        generation=GenerationPolicy(),
        data_bindings=[binding],
        citation_policy=CitationPolicy(),
    )


def test_named_query_binding_resolves_through_gate(gate: SqlSafetyGate) -> None:
    resolver = _make_resolver(gate)
    binding = NamedQueryBinding(
        binding_id="ae_summary_table",
        source="edc_warehouse",
        query_id="ae_summary_by_soc_v3",
        parameters={"compound_id": "{{report.compound_id}}"},
        required=True,
    )
    context = resolver.resolve(_section_with_binding(binding))
    assert len(context.bindings) == 1
    resolved = context.bindings[0]
    assert resolved.binding_type == DataBindingType.NAMED_QUERY
    assert resolved.query_result is not None
    assert resolved.query_result.row_count > 0
    assert "MedDRA_SOC" in resolved.query_result.columns
    assert resolved.query_verdict is not None
    assert resolved.query_verdict.passed
    assert resolved.query_verdict.path == "named_query"


def test_named_query_binding_emits_deferred_note_without_gate() -> None:
    resolver = _make_resolver(None)
    binding = NamedQueryBinding(
        binding_id="ae_summary_table",
        source="edc_warehouse",
        query_id="ae_summary_by_soc_v3",
        parameters={"compound_id": "{{report.compound_id}}"},
        required=True,
    )
    context = resolver.resolve(_section_with_binding(binding))
    resolved = context.bindings[0]
    assert resolved.query_result is None
    assert resolved.deferred_note is not None
    assert "ae_summary_table" in resolved.deferred_note


def test_sql_query_binding_runs_lint_dry_run_approval(gate: SqlSafetyGate) -> None:
    """LLM-drafted SQL — passes lint, dry-run, and the auto-approve callback."""
    resolver = _make_resolver(gate)
    binding = SqlQueryBinding(
        binding_id="custom_sql",
        source="edc_warehouse",
        sql="SELECT soc, any_grade_n FROM ae_events_by_soc WHERE compound_id = :compound_id ORDER BY any_grade_n DESC",
        parameters={"compound_id": "{{report.compound_id}}"},
        required=True,
    )
    context = resolver.resolve(_section_with_binding(binding))
    resolved = context.bindings[0]
    assert resolved.binding_type == DataBindingType.SQL_QUERY
    assert resolved.query_result is not None
    assert resolved.query_verdict is not None
    assert resolved.query_verdict.passed
    assert resolved.query_verdict.path == "llm_drafted"


def test_sql_query_binding_blocked_by_linter_emits_deferred_note(
    gate: SqlSafetyGate,
) -> None:
    """A malicious LLM-drafted query is caught by the linter and yields a
    deferred_note rather than executing."""
    resolver = _make_resolver(gate)
    binding = SqlQueryBinding(
        binding_id="malicious_sql",
        source="edc_warehouse",
        sql="DROP TABLE ae_events_by_soc",
        parameters={},
        required=True,
    )
    context = resolver.resolve(_section_with_binding(binding))
    resolved = context.bindings[0]
    assert resolved.query_result is None
    assert resolved.deferred_note is not None
    assert "FORBIDDEN_OPERATION" in resolved.deferred_note


def test_report_param_substitution() -> None:
    """{{report.compound_id}} in binding parameters resolves from free_text_inputs."""
    resolver = BindingResolver(
        chunks_by_doc={},
        docs_by_id={},
        free_text_inputs={"compound_id": "TEST-42"},
        safety_gate=None,
    )
    out = resolver._substitute_report_params(  # type: ignore[attr-defined]
        {"id": "{{report.compound_id}}", "other": "constant"}
    )
    assert out == {"id": "TEST-42", "other": "constant"}
