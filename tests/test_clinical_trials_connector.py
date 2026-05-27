"""Tests for the MockClinicalTrialsConnector."""

from __future__ import annotations

import pytest

from services.api_integration import (
    ApiCallGate,
    ApiConnectorRegistry,
    ApiOperationError,
    ApiSafetyViolation,
    MockClinicalTrialsConnector,
)


def test_search_trials_filters_by_condition() -> None:
    connector = MockClinicalTrialsConnector()
    result = connector.call("search_trials", {"condition": "Kinase Z"})
    assert result.row_count >= 2
    # nct_id is the first column
    nct_ids = [row[0] for row in result.rows]
    assert "NCT-MOCK-00001" in nct_ids


def test_search_trials_filters_by_intervention() -> None:
    connector = MockClinicalTrialsConnector()
    result = connector.call("search_trials", {"intervention": "XYZ-001"})
    assert result.row_count >= 2
    for row in result.rows:
        assert "XYZ-001" in row[5]  # intervention column


def test_search_trials_no_filter_returns_all() -> None:
    connector = MockClinicalTrialsConnector()
    result = connector.call("search_trials", {})
    # The seed has 3 trial fixtures
    assert result.row_count == 3


def test_get_trial_details_returns_one_trial() -> None:
    connector = MockClinicalTrialsConnector()
    result = connector.call("get_trial_details", {"nct_id": "NCT-MOCK-00001"})
    assert result.row_count == 1
    assert result.rows[0][0] == "NCT-MOCK-00001"
    assert "Phase 1" in result.rows[0][2]


def test_get_trial_details_unknown_raises() -> None:
    connector = MockClinicalTrialsConnector()
    with pytest.raises(ApiOperationError, match="not found"):
        connector.call("get_trial_details", {"nct_id": "DOES_NOT_EXIST"})


def test_search_by_sponsor_finds_acme() -> None:
    connector = MockClinicalTrialsConnector()
    result = connector.call("search_by_sponsor", {"sponsor": "Acme"})
    assert result.row_count == 2  # XYZ-001 Phase 1 + Phase 1/2
    for row in result.rows:
        assert "Acme" in row[4]  # sponsor column


def test_search_by_sponsor_unknown_returns_empty() -> None:
    connector = MockClinicalTrialsConnector()
    result = connector.call("search_by_sponsor", {"sponsor": "Nonexistent"})
    assert result.row_count == 0


def test_gate_enforces_clinical_trials_allowlist() -> None:
    registry = ApiConnectorRegistry()
    registry.register(MockClinicalTrialsConnector())
    gate = ApiCallGate(registry)
    # Not in allowed_operations → blocked
    with pytest.raises(ApiSafetyViolation) as exc:
        gate.call("mock_clinicaltrials", "delete_all_trials", {})
    assert exc.value.code == "OPERATION_NOT_ALLOWED"


def test_gate_routes_to_clinical_trials_connector() -> None:
    registry = ApiConnectorRegistry()
    registry.register(MockClinicalTrialsConnector())
    gate = ApiCallGate(registry)
    result = gate.call("mock_clinicaltrials", "search_trials", {"condition": "tumor"})
    assert result.row_count >= 1
    assert result.connector_id == "mock_clinicaltrials"


def test_connector_columns_are_stable() -> None:
    """Citation rendering depends on the column shape — pin it."""
    connector = MockClinicalTrialsConnector()
    result = connector.call("search_trials", {})
    assert result.columns == (
        "nct_id",
        "title",
        "phase",
        "status",
        "sponsor",
        "intervention",
        "condition",
        "primary_endpoint",
        "n_enrolled",
    )


def test_two_connectors_coexist_in_registry() -> None:
    """ChEMBL + ClinicalTrials in one registry — verifies the pattern
    scales to multiple domains."""
    from services.api_integration import MockChemblConnector

    registry = ApiConnectorRegistry()
    registry.register(MockChemblConnector())
    registry.register(MockClinicalTrialsConnector())
    assert set(registry.ids()) == {"mock_chembl", "mock_clinicaltrials"}
    gate = ApiCallGate(registry)
    # Each routes to its own connector
    chembl = gate.call("mock_chembl", "target_search", {"gene_symbol": "EGFR"})
    trials = gate.call("mock_clinicaltrials", "search_by_sponsor", {"sponsor": "Acme"})
    assert chembl.connector_id == "mock_chembl"
    assert trials.connector_id == "mock_clinicaltrials"
