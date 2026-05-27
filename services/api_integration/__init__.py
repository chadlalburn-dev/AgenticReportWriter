"""API integration: connector registry + safety gate for ApiCallBinding.

The architecture's third data path, alongside file ingestion and
SQL/named-query data integration. Each `ApiConnector` is a thin Python
adapter around an external service (ChEMBL, bioRxiv/medRxiv, Veeva,
LabVantage, a Cloud Function, etc.) with an explicit list of
`allowed_operations` so the binding can't invoke arbitrary endpoints.

Two paths analogous to data_integration's named/llm-drafted split:
1. **Pre-approved operations** — the connector exposes a fixed set
   (e.g., `chembl.target_search`). The binding picks one by name; the
   gate enforces that it's allowed.
2. **LLM-drafted endpoints** (future) — the LLM proposes an operation
   + params; the gate dry-runs (where supported) + requires human
   approval before execution. Same `ApprovalCallback` shape as the SQL
   gate.

Bundled connectors:
- `InMemoryApiConnector`: a fixture-driven connector for tests
- `MockChemblConnector`: synthetic ChEMBL-style responses keyed by
  target gene symbol — demonstrates the pattern without depending on a
  live network call. Production swaps in an `McpToolConnector` (sketched
  in mcp_connector.py) or a thin HTTP client.
"""

from services.api_integration.connector import (
    ApiCallResult,
    ApiConnector,
    ApiOperationError,
)
from services.api_integration.gate import (
    ApiCallGate,
    ApiSafetyViolation,
)
from services.api_integration.mock_connector import (
    InMemoryApiConnector,
    MockChemblConnector,
    MockClinicalTrialsConnector,
)
from services.api_integration.registry import (
    ApiConnectorRegistry,
)


__all__ = [
    "ApiCallGate",
    "ApiCallResult",
    "ApiConnector",
    "ApiConnectorRegistry",
    "ApiOperationError",
    "ApiSafetyViolation",
    "InMemoryApiConnector",
    "MockChemblConnector",
    "MockClinicalTrialsConnector",
]
