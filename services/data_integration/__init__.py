"""Data integration: named-query registry + SQL safety gate.

Two query paths into source-of-truth data warehouses (BigQuery / CloudSQL
in production, SQLite for the local PoC):

1. **Named queries** — pre-approved, parameterized SQL stored as YAML
   files in a registry. The query was authored and reviewed by a human
   (and in Validated mode, signed off through change control). The agent
   invokes them by ID + parameters. No SQL drafting at runtime.

2. **LLM-drafted SQL** — when the agent needs data that no named query
   covers. Goes through the safety gate: parse with sqlglot, reject
   forbidden patterns (DDL, multi-statement, unconditional
   DELETE/UPDATE), dry-run against a read-replica, then human approval
   before the query touches any production source. Approved queries can
   be promoted into the named-query registry.

Both paths produce a ResolvedQueryResult that the generation orchestrator
threads into LLM prompts as a deterministic table (with citation_id),
matching the architecture decision that the LLM never re-derives table
values — only narrates around them.
"""

from services.data_integration.approval import (
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
    auto_approve,
    deny_all,
)
from services.data_integration.executor import (
    QueryExecutor,
    ResolvedQueryResult,
    SqliteQueryExecutor,
)
from services.data_integration.named_query import (
    NamedQuery,
    NamedQueryRegistry,
    QueryParameter,
)
from services.data_integration.safety_gate import (
    SafetyVerdict,
    SqlSafetyGate,
)
from services.data_integration.sql_safety import (
    SqlLinter,
    SqlSafetyViolation,
)

__all__ = [
    "ApprovalCallback",
    "ApprovalDecision",
    "ApprovalRequest",
    "NamedQuery",
    "NamedQueryRegistry",
    "QueryExecutor",
    "QueryParameter",
    "ResolvedQueryResult",
    "SafetyVerdict",
    "SqlLinter",
    "SqlSafetyGate",
    "SqlSafetyViolation",
    "SqliteQueryExecutor",
    "auto_approve",
    "deny_all",
]
