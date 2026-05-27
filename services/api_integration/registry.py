"""ApiConnectorRegistry — lookup table of registered connectors."""

from __future__ import annotations

from services.api_integration.connector import ApiConnector


class ApiConnectorRegistry:
    """Holds connector instances keyed by `connector_id`.

    Multiple instances of the same connector type (e.g., two ChEMBL
    connectors against different endpoints) are allowed as long as they
    have distinct connector_ids — that's the audit identifier the gate
    records on every call.
    """

    def __init__(self) -> None:
        self._connectors: dict[str, ApiConnector] = {}

    def register(self, connector: ApiConnector) -> None:
        if connector.connector_id in self._connectors:
            raise ValueError(
                f"duplicate connector_id={connector.connector_id!r}; "
                "already registered"
            )
        self._connectors[connector.connector_id] = connector

    def get(self, connector_id: str) -> ApiConnector:
        try:
            return self._connectors[connector_id]
        except KeyError as exc:
            raise KeyError(
                f"unknown connector_id={connector_id!r}. "
                f"Registered: {sorted(self._connectors.keys())!r}"
            ) from exc

    def ids(self) -> list[str]:
        return sorted(self._connectors.keys())

    def __len__(self) -> int:
        return len(self._connectors)

    def __contains__(self, connector_id: object) -> bool:
        return connector_id in self._connectors
