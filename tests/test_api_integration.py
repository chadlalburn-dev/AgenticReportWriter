"""Tests for the API connector layer (api_integration).

Covers: connector protocol, registry, gate policy enforcement, mock
connectors, and end-to-end wiring through the BindingResolver +
SectionFiller (api_call binding -> ApiCallResult -> table in the
LLM prompt -> Citation with source_type=API).
"""

from __future__ import annotations

import pytest

from services.api_integration import (
    ApiCallGate,
    ApiCallResult,
    ApiConnectorRegistry,
    ApiOperationError,
    ApiSafetyViolation,
    InMemoryApiConnector,
    MockChemblConnector,
)
from services.generation_orchestrator.retrieval import BindingResolver
from shared.schemas import DataBindingType, TemplateSection
from shared.schemas.template import (
    ApiCallBinding,
    CitationPolicy,
    GenerationPolicy,
)


# --- Registry --------------------------------------------------------------


def test_registry_register_and_get() -> None:
    registry = ApiConnectorRegistry()
    connector = MockChemblConnector()
    registry.register(connector)
    assert registry.get("mock_chembl") is connector
    assert "mock_chembl" in registry
    assert len(registry) == 1


def test_registry_rejects_duplicate_id() -> None:
    registry = ApiConnectorRegistry()
    registry.register(MockChemblConnector())
    with pytest.raises(ValueError, match="duplicate connector_id"):
        registry.register(MockChemblConnector())


def test_registry_unknown_raises_helpful_message() -> None:
    registry = ApiConnectorRegistry()
    registry.register(MockChemblConnector())
    with pytest.raises(KeyError, match="unknown connector_id"):
        registry.get("not_there")


# --- Gate ------------------------------------------------------------------


def test_gate_calls_through_registered_connector() -> None:
    registry = ApiConnectorRegistry()
    registry.register(MockChemblConnector())
    gate = ApiCallGate(registry)
    result = gate.call("mock_chembl", "target_search", {"gene_symbol": "egfr"})
    assert isinstance(result, ApiCallResult)
    assert result.connector_id == "mock_chembl"
    assert result.operation_id == "target_search"
    assert result.fetched_at is not None
    assert any("EGFR" in str(c) or "growth factor" in str(c).lower() for row in result.rows for c in row)


def test_gate_unknown_connector_raises() -> None:
    gate = ApiCallGate(ApiConnectorRegistry())
    with pytest.raises(ApiSafetyViolation) as exc:
        gate.call("does_not_exist", "anything", {})
    assert exc.value.code == "UNKNOWN_CONNECTOR"


def test_gate_disallowed_operation_raises() -> None:
    registry = ApiConnectorRegistry()
    registry.register(MockChemblConnector())
    gate = ApiCallGate(registry)
    with pytest.raises(ApiSafetyViolation) as exc:
        gate.call("mock_chembl", "delete_all_records", {})
    assert exc.value.code == "OPERATION_NOT_ALLOWED"
    assert "target_search" in exc.value.message  # lists allowed ops


def test_gate_lets_connector_raise_api_operation_error() -> None:
    """Upstream failures bubble up as ApiOperationError, not ApiSafetyViolation."""
    registry = ApiConnectorRegistry()
    registry.register(MockChemblConnector())
    gate = ApiCallGate(registry)
    # get_mechanism is allowed; unknown target_chembl_id raises in the connector.
    with pytest.raises(ApiOperationError, match="not found"):
        gate.call("mock_chembl", "get_mechanism", {"target_chembl_id": "BOGUS"})


# --- MockChemblConnector ---------------------------------------------------


def test_mock_chembl_target_search_finds_egfr() -> None:
    connector = MockChemblConnector()
    result = connector.call("target_search", {"gene_symbol": "EGFR"})
    assert result.row_count >= 1
    chembl_ids = [row[0] for row in result.rows]
    assert "CHEMBL203" in chembl_ids


def test_mock_chembl_target_search_finds_kinase_z_for_demo() -> None:
    connector = MockChemblConnector()
    result = connector.call("target_search", {"target_name": "Kinase Z"})
    assert result.row_count == 1
    assert "Kinase Z" in result.rows[0][1]


def test_mock_chembl_get_mechanism_returns_inhibitor() -> None:
    connector = MockChemblConnector()
    result = connector.call(
        "get_mechanism", {"target_chembl_id": "CHEMBL-MOCK-KINZ"}
    )
    assert result.row_count == 1
    row = result.rows[0]
    # action_type is in the table
    assert row[2] == "INHIBITOR"
    # mechanism summary text is in the last column
    assert "Kinase Z" in row[3]


# --- InMemoryApiConnector for fixture-driven tests -------------------------


def test_in_memory_connector_returns_fixture() -> None:
    connector = InMemoryApiConnector(connector_id="fixture")
    expected = ApiCallResult(
        connector_id="fixture",
        operation_id="get_thing",
        parameters={"x": 1},
        columns=("a", "b"),
        rows=(("one", "two"),),
        row_count=1,
    )
    connector.register_response("get_thing", {"x": 1}, expected)
    got = connector.call("get_thing", {"x": 1})
    assert got is expected


def test_in_memory_connector_unregistered_raises() -> None:
    connector = InMemoryApiConnector(
        connector_id="fixture", allowed_operations=frozenset({"get_thing"})
    )
    with pytest.raises(ApiOperationError, match="no fixture"):
        connector.call("get_thing", {"x": 99})


# --- BindingResolver integration -------------------------------------------


def _section_with_api_binding(binding: ApiCallBinding) -> TemplateSection:
    return TemplateSection(
        section_id="bg",
        title="Background",
        level=1,
        generation=GenerationPolicy(),
        data_bindings=[binding],
        citation_policy=CitationPolicy(),
    )


def test_resolver_runs_api_call_through_gate() -> None:
    registry = ApiConnectorRegistry()
    registry.register(MockChemblConnector())
    gate = ApiCallGate(registry)
    resolver = BindingResolver(
        chunks_by_doc={},
        docs_by_id={},
        free_text_inputs={"target_gene": "EGFR"},
        api_gate=gate,
    )
    binding = ApiCallBinding(
        binding_id="target_data",
        connector_id="mock_chembl",
        endpoint="target_search",
        parameters={"gene_symbol": "{{report.target_gene}}"},
    )
    context = resolver.resolve(_section_with_api_binding(binding))
    rb = context.bindings[0]
    assert rb.binding_type == DataBindingType.API_CALL
    assert rb.api_result is not None
    assert rb.api_result.connector_id == "mock_chembl"
    assert rb.api_result.row_count >= 1


def test_resolver_emits_deferred_note_without_gate() -> None:
    resolver = BindingResolver(
        chunks_by_doc={},
        docs_by_id={},
        free_text_inputs={},
        api_gate=None,
    )
    binding = ApiCallBinding(
        binding_id="target_data",
        connector_id="mock_chembl",
        endpoint="target_search",
        parameters={"gene_symbol": "EGFR"},
    )
    context = resolver.resolve(_section_with_api_binding(binding))
    rb = context.bindings[0]
    assert rb.api_result is None
    assert rb.deferred_note is not None
    assert "target_data" in rb.deferred_note


def test_resolver_handles_disallowed_operation_as_deferred_note() -> None:
    registry = ApiConnectorRegistry()
    registry.register(MockChemblConnector())
    gate = ApiCallGate(registry)
    resolver = BindingResolver(
        chunks_by_doc={},
        docs_by_id={},
        free_text_inputs={},
        api_gate=gate,
    )
    # `delete_all_records` is NOT in MockChemblConnector.allowed_operations
    binding = ApiCallBinding(
        binding_id="bad_op",
        connector_id="mock_chembl",
        endpoint="delete_all_records",
        parameters={},
    )
    context = resolver.resolve(_section_with_api_binding(binding))
    rb = context.bindings[0]
    assert rb.api_result is None
    assert rb.deferred_note is not None
    assert "OPERATION_NOT_ALLOWED" in rb.deferred_note


def test_resolver_handles_connector_error_as_deferred_note() -> None:
    registry = ApiConnectorRegistry()
    registry.register(MockChemblConnector())
    gate = ApiCallGate(registry)
    resolver = BindingResolver(
        chunks_by_doc={},
        docs_by_id={},
        free_text_inputs={},
        api_gate=gate,
    )
    binding = ApiCallBinding(
        binding_id="mechanism",
        connector_id="mock_chembl",
        endpoint="get_mechanism",
        parameters={"target_chembl_id": "DOES_NOT_EXIST"},
    )
    context = resolver.resolve(_section_with_api_binding(binding))
    rb = context.bindings[0]
    assert rb.api_result is None
    assert rb.deferred_note is not None
    assert "not found" in rb.deferred_note.lower()
