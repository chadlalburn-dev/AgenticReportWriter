"""ApiCallGate — the runtime checkpoint between the orchestrator and any
external service.

Responsibilities:
- Look up the connector
- Verify the operation is in the connector's `allowed_operations`
- Forward the call
- Wrap the result with timing + audit metadata
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from services.api_integration.connector import (
    ApiCallResult,
    ApiConnector,
    ApiOperationError,
)
from services.api_integration.registry import ApiConnectorRegistry


class ApiSafetyViolation(Exception):
    """Policy violation at the gate (operation not allowed, connector
    missing). Distinct from upstream API errors (those raise
    ApiOperationError inside the connector)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ApiCallGate:
    def __init__(self, registry: ApiConnectorRegistry) -> None:
        self._registry = registry

    def call(
        self,
        connector_id: str,
        operation_id: str,
        parameters: Mapping[str, Any],
    ) -> ApiCallResult:
        try:
            connector: ApiConnector = self._registry.get(connector_id)
        except KeyError as exc:
            raise ApiSafetyViolation("UNKNOWN_CONNECTOR", str(exc)) from exc

        if operation_id not in connector.allowed_operations:
            raise ApiSafetyViolation(
                "OPERATION_NOT_ALLOWED",
                (
                    f"connector {connector_id!r} does not allow operation "
                    f"{operation_id!r}; allowed: "
                    f"{sorted(connector.allowed_operations)!r}"
                ),
            )

        result = connector.call(operation_id, parameters)
        # Stamp fetched_at if the connector didn't.
        if result.fetched_at is None:
            from dataclasses import replace

            result = replace(result, fetched_at=datetime.now(timezone.utc))
        return result
