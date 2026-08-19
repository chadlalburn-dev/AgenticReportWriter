"""Tests for the Confluence connectors (mock + gate wiring)."""

from __future__ import annotations

import pytest

from services.api_integration import (
    ApiCallGate,
    ApiConnectorRegistry,
    ApiOperationError,
    ApiSafetyViolation,
    MockConfluenceConnector,
)


def test_search_pages_matches_by_keyword() -> None:
    c = MockConfluenceConnector()
    r = c.call("search_pages", {"cql": 'title ~ "Kinase Z target rationale"'})
    assert r.row_count >= 1
    page_ids = [row[0] for row in r.rows]
    assert "100001" in page_ids
    assert r.columns == ("page_id", "title", "space", "url", "excerpt")


def test_get_page_returns_one_with_body_in_raw() -> None:
    c = MockConfluenceConnector()
    r = c.call("get_page", {"page_id": "100002"})
    assert r.row_count == 1
    assert r.rows[0][1] == "XYZ-001 programme background"
    assert "pages" in r.raw and r.raw["pages"][0]["body"]


def test_get_page_unknown_raises() -> None:
    c = MockConfluenceConnector()
    with pytest.raises(ApiOperationError, match="not found"):
        c.call("get_page", {"page_id": "999999"})


def test_gate_routes_and_enforces_allowlist() -> None:
    reg = ApiConnectorRegistry()
    reg.register(MockConfluenceConnector())
    gate = ApiCallGate(reg)
    ok = gate.call("confluence", "search_pages", {"cql": "Kinase Z"})
    assert ok.connector_id == "confluence"
    with pytest.raises(ApiSafetyViolation) as exc:
        gate.call("confluence", "delete_page", {"page_id": "100001"})
    assert exc.value.code == "OPERATION_NOT_ALLOWED"


def test_connector_id_is_confluence() -> None:
    # The loader maps confluence sources to ApiCallBinding(connector_id="confluence"),
    # so the registered connector_id must match.
    assert MockConfluenceConnector.connector_id == "confluence"
