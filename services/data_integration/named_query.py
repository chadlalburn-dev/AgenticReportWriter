"""Named-query registry.

A NamedQuery is a versioned, pre-approved, parameterized SQL query stored
as YAML in a registry directory. Each query carries an approval record so
the audit chain can verify provenance (who approved, when, what changed
between versions).

YAML schema (one query per file):

    id: ae_summary_by_soc_v3
    description: Adverse events grouped by MedDRA System Organ Class
    source: edc_warehouse
    version: 3
    sql: |
        SELECT soc AS MedDRA_SOC, SUM(any_grade) AS any_grade_n
        FROM ae_events
        WHERE compound_id = :compound_id
        GROUP BY soc
        ORDER BY any_grade_n DESC
    parameters:
      compound_id:
        type: string
        description: Compound identifier (e.g. XYZ-001)
        required: true
    output_columns:
      MedDRA_SOC: {type: string}
      any_grade_n: {type: integer}
    approval:
      approved_by: medical_writing_qa
      approved_at: 2026-04-15
      change_log:
        - v1: initial version
        - v2: added grade_3_4 column
        - v3: added ORDER BY

SQL placeholders use the standard `:name` parameter style — supported by
SQLite, Postgres (via psycopg/SQLAlchemy), and convertible to BigQuery's
`@name` form by the executor.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


ParameterType = Literal["string", "integer", "float", "boolean", "date", "datetime"]


class QueryParameter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: ParameterType
    description: str | None = None
    required: bool = True
    default: str | int | float | bool | None = None


class OutputColumn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: ParameterType
    description: str | None = None


class ApprovalRecord(BaseModel):
    """Provenance of a query's approval. Mirrors the change-control story
    that Validated-mode templates have."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    approved_by: str
    approved_at: date
    change_log: list[str] = Field(default_factory=list)


class NamedQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    description: str
    source: str = Field(description="Logical data source name (e.g. 'edc_warehouse')")
    version: int = Field(ge=1)
    sql: str
    parameters: dict[str, QueryParameter] = Field(default_factory=dict)
    output_columns: dict[str, OutputColumn] = Field(default_factory=dict)
    approval: ApprovalRecord

    def validate_args(self, args: dict[str, object]) -> dict[str, object]:
        """Ensure all required params are present and unknown args are rejected.

        Returns the validated args dict (with defaults filled in). Raises
        ValueError on missing required params or unknown args.
        """
        validated: dict[str, object] = {}
        for name, spec in self.parameters.items():
            if name in args:
                validated[name] = args[name]
            elif spec.required and spec.default is None:
                raise ValueError(
                    f"named query {self.id!r}: missing required parameter {name!r}"
                )
            elif spec.default is not None:
                validated[name] = spec.default

        unknown = set(args) - set(self.parameters)
        if unknown:
            raise ValueError(
                f"named query {self.id!r}: unknown parameter(s) {sorted(unknown)!r}"
            )
        return validated


class NamedQueryRegistry:
    """Loads named queries from a directory of YAML files.

    Layout convention: one query per file, filename is the query id with
    `.yaml`. Multiple registries can be stacked (e.g., a base registry
    shared across all programs + a program-specific overlay).
    """

    def __init__(self) -> None:
        self._queries: dict[str, NamedQuery] = {}

    @classmethod
    def from_directory(cls, directory: str | Path) -> "NamedQueryRegistry":
        registry = cls()
        registry.load_directory(directory)
        return registry

    def load_directory(self, directory: str | Path) -> int:
        """Load all .yaml files under `directory`. Returns the count loaded."""
        root = Path(directory)
        if not root.exists():
            raise FileNotFoundError(f"registry directory does not exist: {root}")
        n = 0
        for path in sorted(root.rglob("*.yaml")):
            self.register(self._load_file(path))
            n += 1
        return n

    @staticmethod
    def _load_file(path: Path) -> NamedQuery:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            return NamedQuery.model_validate(payload)
        except Exception as exc:
            raise ValueError(f"failed to load named query from {path}: {exc}") from exc

    def register(self, query: NamedQuery) -> None:
        if query.id in self._queries:
            existing = self._queries[query.id]
            raise ValueError(
                f"duplicate query id {query.id!r}: "
                f"already registered at version {existing.version}"
            )
        self._queries[query.id] = query

    def get(self, query_id: str) -> NamedQuery:
        try:
            return self._queries[query_id]
        except KeyError as exc:
            raise KeyError(
                f"unknown named query: {query_id!r}. "
                f"Registered: {sorted(self._queries.keys())!r}"
            ) from exc

    def ids(self) -> list[str]:
        return sorted(self._queries.keys())

    def __len__(self) -> int:
        return len(self._queries)
