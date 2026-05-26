"""Query executor — runs validated SQL against a backend.

Two implementations:
- SqliteQueryExecutor: local PoC backend. Used by tests and the demo.
- (BigQueryExecutor / CloudSqlExecutor would be production swap-ins;
  not built yet — they live behind the same protocol.)

The executor returns a ResolvedQueryResult: column names + row tuples +
a row_count for capping context size. The orchestrator wraps this as a
deterministic table in the LLM prompt.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ResolvedQueryResult:
    """The output of executing a (validated, approved) SQL statement.

    Identity: (query_id_or_hash, parameters, executed_at) — the executor
    audits each run so the same logical query against the same params can
    be correlated across runs."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int = 0
    source: str = ""
    sql_executed: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)


class QueryExecutor(Protocol):
    """Runs a parameterized SQL statement and returns a tabular result.

    Implementations must:
    - Bind parameters using the backend's safe parameter API (never string
      interpolation)
    - Cap the result row count to a configurable limit
    - Apply read-only enforcement (the linter is one layer; the backend
      connection should be a read-replica with no write grants as the
      second layer)
    """

    def execute(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> ResolvedQueryResult: ...

    def dry_run(self, sql: str, parameters: Mapping[str, Any] | None = None) -> None:
        """Validate the query without executing it (for the safety gate)."""
        ...


class SqliteQueryExecutor(QueryExecutor):
    """SQLite executor with row caps + read-only enforcement.

    `read_only`: when True, opens the database with `?mode=ro` URI so
    write attempts at the driver level fail (defense in depth on top of
    the linter). The demo uses this; tests can disable it to seed data.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        source: str = "sqlite",
        max_rows: int = 10_000,
        read_only: bool = True,
    ) -> None:
        self._db_path = str(db_path)
        self._source = source
        self._max_rows = max_rows
        self._read_only = read_only

    def _connect(self) -> sqlite3.Connection:
        if self._db_path == ":memory:":
            return sqlite3.connect(self._db_path)
        if self._read_only:
            uri = f"file:{self._db_path}?mode=ro"
            return sqlite3.connect(uri, uri=True)
        return sqlite3.connect(self._db_path)

    def execute(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> ResolvedQueryResult:
        params = dict(parameters or {})
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql, params)
            rows = cursor.fetchmany(self._max_rows + 1)
            if len(rows) > self._max_rows:
                raise RuntimeError(
                    f"query result exceeded max_rows={self._max_rows} — "
                    "consider tightening the query"
                )
            columns = tuple(d[0] for d in cursor.description or [])
            row_tuples = tuple(tuple(row) for row in rows)
            return ResolvedQueryResult(
                columns=columns,
                rows=row_tuples,
                row_count=len(row_tuples),
                source=self._source,
                sql_executed=sql,
                parameters=params,
            )

    def dry_run(self, sql: str, parameters: Mapping[str, Any] | None = None) -> None:
        """Use SQLite's EXPLAIN to validate the query without producing rows."""
        params = dict(parameters or {})
        with self._connect() as conn:
            try:
                conn.execute("EXPLAIN " + sql, params).fetchall()
            except sqlite3.Error as exc:
                raise RuntimeError(f"dry-run failed: {exc}") from exc
