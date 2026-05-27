"""Mock connectors — used by tests and the local demo.

`InMemoryApiConnector` is a fixture-driven generic connector for unit
tests: register response payloads keyed by (operation_id, frozen
parameters), call the connector, get the fixture back. No network.

`MockChemblConnector` is a small in-memory analog of the live ChEMBL
target_search/get_mechanism MCP tools. It returns ChEMBL-shaped data
keyed by target gene symbol so the synthetic IB demo can populate the
Background section with realistic-looking target biology without
hitting a live endpoint. Production swaps this for either an
McpToolConnector (calling the real ChEMBL MCP server) or a thin
google.api or httpx wrapper.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.api_integration.connector import (
    ApiCallResult,
    ApiConnector,
    ApiOperationError,
)


# --- InMemoryApiConnector --------------------------------------------------


def _freeze_params(parameters: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted((str(k), v) for k, v in parameters.items()))


class InMemoryApiConnector(ApiConnector):
    """Fixture-driven generic connector.

    Tests register response payloads with ``register_response(...)``.
    When ``call()`` is invoked, the connector looks up by
    (operation_id, frozen-params) and returns the registered payload.
    Unregistered calls raise ApiOperationError.
    """

    def __init__(
        self,
        *,
        connector_id: str = "in_memory",
        allowed_operations: frozenset[str] | None = None,
    ) -> None:
        self.connector_id = connector_id
        self.allowed_operations = allowed_operations or frozenset()
        self._fixtures: dict[
            tuple[str, tuple[tuple[str, Any], ...]], ApiCallResult
        ] = {}

    def register_response(
        self,
        operation_id: str,
        parameters: Mapping[str, Any],
        result: ApiCallResult,
    ) -> None:
        if operation_id not in self.allowed_operations:
            # Register-time, not call-time: tests can register one and forget.
            object.__setattr__(
                self,
                "allowed_operations",
                self.allowed_operations | {operation_id},
            )
        self._fixtures[(operation_id, _freeze_params(parameters))] = result

    def call(
        self, operation_id: str, parameters: Mapping[str, Any]
    ) -> ApiCallResult:
        key = (operation_id, _freeze_params(parameters))
        if key not in self._fixtures:
            raise ApiOperationError(
                f"InMemoryApiConnector(connector_id={self.connector_id!r}): no "
                f"fixture for operation_id={operation_id!r}, params={dict(parameters)!r}"
            )
        return self._fixtures[key]


# --- MockChemblConnector --------------------------------------------------


@dataclass(frozen=True)
class _ChemblTargetFixture:
    target_chembl_id: str
    target_name: str
    target_type: str
    organism: str
    components: tuple[str, ...]  # UniProt accessions
    description: str


# A small seed of plausible targets for the IB Background section.
# Numbers are synthetic; intent is to exercise the connector path,
# not to provide accurate ChEMBL data.
_TARGET_FIXTURES: dict[str, _ChemblTargetFixture] = {
    "kinz": _ChemblTargetFixture(
        target_chembl_id="CHEMBL-MOCK-KINZ",
        target_name="Kinase Z",
        target_type="SINGLE PROTEIN",
        organism="Homo sapiens",
        components=("P00000",),
        description=(
            "Receptor tyrosine kinase. Synthetic stand-in target used in the "
            "Report-Generator-Agent demo; not a real ChEMBL record."
        ),
    ),
    "egfr": _ChemblTargetFixture(
        target_chembl_id="CHEMBL203",
        target_name="Epidermal growth factor receptor erbB1",
        target_type="SINGLE PROTEIN",
        organism="Homo sapiens",
        components=("P00533",),
        description=(
            "Receptor tyrosine kinase frequently dysregulated in non-small "
            "cell lung cancer and other solid tumors."
        ),
    ),
}


class MockChemblConnector(ApiConnector):
    """Tiny in-memory analog of the ChEMBL target_search MCP tool.

    Supports two operations:
    - `target_search`: filter by gene_symbol/target_name; returns
      (target_chembl_id, target_name, target_type, organism, description)
    - `get_mechanism`: per-target mechanism summary. Returns
      (target_chembl_id, mechanism_summary).
    """

    connector_id = "mock_chembl"
    allowed_operations = frozenset({"target_search", "get_mechanism"})

    def call(
        self, operation_id: str, parameters: Mapping[str, Any]
    ) -> ApiCallResult:
        if operation_id == "target_search":
            return self._target_search(parameters)
        if operation_id == "get_mechanism":
            return self._get_mechanism(parameters)
        raise ApiOperationError(
            f"MockChemblConnector: unknown operation_id={operation_id!r}"
        )

    def _target_search(self, parameters: Mapping[str, Any]) -> ApiCallResult:
        needle = str(
            parameters.get("gene_symbol")
            or parameters.get("target_name")
            or ""
        ).strip().lower()
        matches: list[_ChemblTargetFixture] = []
        for key, fix in _TARGET_FIXTURES.items():
            if (
                needle == key
                or needle in fix.target_name.lower()
                or needle in fix.target_chembl_id.lower()
            ):
                matches.append(fix)
        columns = (
            "target_chembl_id",
            "target_name",
            "target_type",
            "organism",
            "description",
        )
        rows = tuple(
            (m.target_chembl_id, m.target_name, m.target_type, m.organism, m.description)
            for m in matches
        )
        return ApiCallResult(
            connector_id=self.connector_id,
            operation_id="target_search",
            parameters=dict(parameters),
            columns=columns,
            rows=rows,
            raw={"matches": [m.__dict__ for m in matches]},
            row_count=len(rows),
            source="mock_chembl",
        )

    def _get_mechanism(self, parameters: Mapping[str, Any]) -> ApiCallResult:
        target_id = str(parameters.get("target_chembl_id", "")).upper()
        # Match by either explicit id or by reverse-mapping name → id
        fixture: _ChemblTargetFixture | None = None
        for fix in _TARGET_FIXTURES.values():
            if fix.target_chembl_id.upper() == target_id:
                fixture = fix
                break
        if fixture is None:
            raise ApiOperationError(
                f"MockChemblConnector.get_mechanism: target_chembl_id="
                f"{target_id!r} not found"
            )
        # The "mechanism" payload is intentionally narrative; the IB
        # Background section quotes it via citation.
        mechanism_summary = (
            f"Inhibitors of {fixture.target_name} block downstream "
            "MAPK and PI3K-AKT signaling, with hypothesized antiproliferative "
            "effects in tumors driven by activating mutations or amplification "
            "of the receptor."
        )
        columns = ("target_chembl_id", "target_name", "action_type", "mechanism_summary")
        rows = (
            (
                fixture.target_chembl_id,
                fixture.target_name,
                "INHIBITOR",
                mechanism_summary,
            ),
        )
        return ApiCallResult(
            connector_id=self.connector_id,
            operation_id="get_mechanism",
            parameters=dict(parameters),
            columns=columns,
            rows=rows,
            raw={"mechanism_summary": mechanism_summary},
            row_count=1,
            source="mock_chembl",
        )


# --- MockClinicalTrialsConnector ------------------------------------------


@dataclass(frozen=True)
class _TrialFixture:
    nct_id: str
    title: str
    phase: str
    status: str
    sponsor: str
    intervention: str
    condition: str
    primary_endpoint: str
    n_enrolled: int


# A small seed of fictional trials anchored on Kinase Z so the synthetic
# IB demo can reference related-trial context in Section 4.
_TRIAL_FIXTURES: dict[str, _TrialFixture] = {
    "NCT-MOCK-00001": _TrialFixture(
        nct_id="NCT-MOCK-00001",
        title="A Phase 1 Dose-Escalation Study of XYZ-001 in Advanced Solid Tumors",
        phase="Phase 1",
        status="Completed",
        sponsor="Acme Therapeutics (synthetic)",
        intervention="XYZ-001 (oral, 25-400 mg QD)",
        condition="Advanced Kinase Z-positive solid tumors",
        primary_endpoint="Maximum tolerated dose (MTD); RP2D selection",
        n_enrolled=36,
    ),
    "NCT-MOCK-00002": _TrialFixture(
        nct_id="NCT-MOCK-00002",
        title="Phase 1/2 Expansion of XYZ-001 in Kinase Z-Positive Tumors",
        phase="Phase 1/2",
        status="Active, not recruiting",
        sponsor="Acme Therapeutics (synthetic)",
        intervention="XYZ-001 300 mg QD",
        condition="Kinase Z-amplified solid tumors",
        primary_endpoint="Objective response rate (RECIST 1.1)",
        n_enrolled=84,
    ),
    "NCT-MOCK-00010": _TrialFixture(
        nct_id="NCT-MOCK-00010",
        title="Comparator Phase 2 Study of Competitor Compound A in Kinase Z-Driven NSCLC",
        phase="Phase 2",
        status="Recruiting",
        sponsor="Competitor Pharma (synthetic)",
        intervention="Compound A (oral)",
        condition="Non-small cell lung cancer (Kinase Z-amplified)",
        primary_endpoint="Progression-free survival",
        n_enrolled=120,
    ),
}


class MockClinicalTrialsConnector(ApiConnector):
    """Tiny in-memory analog of a ClinicalTrials.gov-style registry.

    Mirrors the shape of the live MCP tools (search_trials,
    get_trial_details, search_by_sponsor) so a follow-up
    `McpToolConnector` can swap in real data without changing the
    binding contract. All data here is fictional; the seed compound is
    the same XYZ-001 used in the synthetic IB corpus so the demo can
    cite related-trial context coherently.
    """

    connector_id = "mock_clinicaltrials"
    allowed_operations = frozenset(
        {"search_trials", "get_trial_details", "search_by_sponsor"}
    )

    def call(
        self, operation_id: str, parameters: Mapping[str, Any]
    ) -> ApiCallResult:
        if operation_id == "search_trials":
            return self._search_trials(parameters)
        if operation_id == "get_trial_details":
            return self._get_trial_details(parameters)
        if operation_id == "search_by_sponsor":
            return self._search_by_sponsor(parameters)
        raise ApiOperationError(
            f"MockClinicalTrialsConnector: unknown operation_id={operation_id!r}"
        )

    def _search_trials(self, parameters: Mapping[str, Any]) -> ApiCallResult:
        condition = str(parameters.get("condition", "")).strip().lower()
        intervention = str(parameters.get("intervention", "")).strip().lower()
        matches: list[_TrialFixture] = []
        for trial in _TRIAL_FIXTURES.values():
            cond_match = not condition or condition in trial.condition.lower()
            interv_match = not intervention or intervention in trial.intervention.lower()
            if cond_match and interv_match:
                matches.append(trial)
        return self._trials_to_result("search_trials", parameters, matches)

    def _get_trial_details(self, parameters: Mapping[str, Any]) -> ApiCallResult:
        nct_id = str(parameters.get("nct_id", "")).upper()
        trial = _TRIAL_FIXTURES.get(nct_id)
        if trial is None:
            raise ApiOperationError(
                f"MockClinicalTrialsConnector.get_trial_details: "
                f"nct_id={nct_id!r} not found"
            )
        return self._trials_to_result("get_trial_details", parameters, [trial])

    def _search_by_sponsor(self, parameters: Mapping[str, Any]) -> ApiCallResult:
        sponsor = str(parameters.get("sponsor", "")).strip().lower()
        matches = [
            t for t in _TRIAL_FIXTURES.values() if sponsor and sponsor in t.sponsor.lower()
        ]
        return self._trials_to_result("search_by_sponsor", parameters, matches)

    @staticmethod
    def _trials_to_result(
        operation_id: str,
        parameters: Mapping[str, Any],
        trials: list[_TrialFixture],
    ) -> ApiCallResult:
        columns = (
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
        rows = tuple(
            (
                t.nct_id,
                t.title,
                t.phase,
                t.status,
                t.sponsor,
                t.intervention,
                t.condition,
                t.primary_endpoint,
                t.n_enrolled,
            )
            for t in trials
        )
        return ApiCallResult(
            connector_id="mock_clinicaltrials",
            operation_id=operation_id,
            parameters=dict(parameters),
            columns=columns,
            rows=rows,
            raw={"trials": [t.__dict__ for t in trials]},
            row_count=len(rows),
            source="mock_clinicaltrials",
        )
