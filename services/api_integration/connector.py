"""ApiConnector protocol + the ApiCallResult value type.

A connector is a stable, audit-friendly shim between the orchestrator
and an external system. It declares which operations it supports
(`allowed_operations`) and exposes a single `call(operation, params)`
method. All calls go through the ApiCallGate, which:
- Enforces the connector's allowed-operations policy
- Records the call as an AuditEvent (LLM_CALL-like, but for APIs)
- Wraps the response in ApiCallResult so downstream services see the
  same shape regardless of the underlying API

The Result intentionally exposes both a tabular `(columns, rows)`
projection (so the renderer can drop it into a doc as a table) AND
the raw JSON (`raw`) so the LLM can pull free-text fields for the
narrative.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class ApiOperationError(Exception):
    """Raised by a connector when the upstream call fails or returns an
    unrecognized payload. Distinct from ApiSafetyViolation (gate-level
    policy violation) — this is "the API said no" rather than "we won't
    call the API"."""


@dataclass(frozen=True)
class ApiCallResult:
    connector_id: str
    operation_id: str
    parameters: Mapping[str, Any]
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    raw: Mapping[str, Any] = field(default_factory=dict)
    fetched_at: datetime | None = None
    row_count: int = 0
    source: str = ""

    def __post_init__(self) -> None:
        # Set row_count after dataclass init so it always reflects rows.
        if self.row_count == 0 and self.rows:
            object.__setattr__(self, "row_count", len(self.rows))


class ApiConnector(Protocol):
    """The contract every external-service adapter implements.

    `allowed_operations` is an explicit list — the gate refuses any
    operation not in this set. This mirrors NamedQueryRegistry's
    pre-approval model: the human decides at registration time which
    endpoints are safe; the agent picks from those at runtime.
    """

    connector_id: str
    allowed_operations: frozenset[str]

    def call(
        self, operation_id: str, parameters: Mapping[str, Any]
    ) -> ApiCallResult: ...
