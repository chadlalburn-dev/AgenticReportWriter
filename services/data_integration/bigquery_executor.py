"""BigQueryQueryExecutor — QueryExecutor backed by Google BigQuery.

Same protocol as SqliteQueryExecutor (the local dev stand-in), so named
queries and the SQL safety gate work unchanged; only the backend differs.
Swap SqliteQueryExecutor -> BigQueryQueryExecutor to point the named-query
layer at the real Benchling->BigQuery warehouse.

Auth: Application Default Credentials (workload identity / `gcloud auth
application-default login`). No keys. google-cloud-bigquery is imported
lazily and lives in the [gcp] extra, so the rest of the engine imports
without it.

Parameter style: our named queries use `:name` placeholders (SQLite/
Postgres style). BigQuery uses `@name`, so `execute`/`dry_run` translate
`:name` -> `@name` and build ScalarQueryParameters. This keeps a single
query text working across the SQLite stand-in and BigQuery.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from services.data_integration.executor import QueryExecutor, ResolvedQueryResult

# Match :name placeholders, but NOT the second colon of a `::type` cast
# (negative lookbehind for a preceding colon).
_PARAM_RE = re.compile(r"(?<!:):([A-Za-z_]\w*)")


def _to_bq(sql: str) -> str:
    """Rewrite `:name` placeholders to BigQuery `@name`."""
    return _PARAM_RE.sub(lambda m: "@" + m.group(1), sql)


def _referenced_params(sql: str) -> list[str]:
    return sorted(set(_PARAM_RE.findall(sql)))


class BigQueryQueryExecutor(QueryExecutor):
    def __init__(
        self,
        *,
        project: str,
        location: str = "EU",
        source: str = "bigquery",
        max_rows: int = 10_000,
        default_dataset: str | None = None,
    ) -> None:
        try:
            from google.cloud import bigquery  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "google-cloud-bigquery is not installed. Install the project's "
                "[gcp] extra before using BigQueryQueryExecutor."
            ) from exc
        self._bq = bigquery
        self._client = bigquery.Client(project=project, location=location)
        self._source = source
        self._max_rows = max_rows
        self._default_dataset = default_dataset

    # -- param binding ------------------------------------------------------

    def _params(self, sql: str, parameters: Mapping[str, Any]):
        out = []
        for name in _referenced_params(sql):
            if name not in parameters:
                raise RuntimeError(f"query references :{name} but no value was supplied")
            out.append(self._scalar(name, parameters[name]))
        return out

    def _scalar(self, name: str, value: Any):
        if isinstance(value, bool):
            t = "BOOL"
        elif isinstance(value, int):
            t = "INT64"
        elif isinstance(value, float):
            t = "FLOAT64"
        else:
            t = "STRING"
            value = str(value)
        return self._bq.ScalarQueryParameter(name, t, value)

    def _job_config(self, sql: str, parameters: Mapping[str, Any], *, dry_run: bool):
        cfg = self._bq.QueryJobConfig(
            query_parameters=self._params(sql, parameters),
            dry_run=dry_run,
            use_query_cache=not dry_run,
        )
        if self._default_dataset:
            cfg.default_dataset = self._default_dataset
        return cfg

    # -- QueryExecutor ------------------------------------------------------

    def execute(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> ResolvedQueryResult:
        params = dict(parameters or {})
        bq_sql = _to_bq(sql)
        job = self._client.query(bq_sql, job_config=self._job_config(bq_sql, params, dry_run=False))
        rows_iter = job.result(max_results=self._max_rows + 1)
        columns = tuple(f.name for f in rows_iter.schema)
        rows = [tuple(row.values()) for row in rows_iter]
        if len(rows) > self._max_rows:
            raise RuntimeError(
                f"query result exceeded max_rows={self._max_rows} — tighten the query"
            )
        return ResolvedQueryResult(
            columns=columns,
            rows=tuple(rows),
            row_count=len(rows),
            source=self._source,
            sql_executed=bq_sql,
            parameters=params,
        )

    def dry_run(self, sql: str, parameters: Mapping[str, Any] | None = None) -> None:
        params = dict(parameters or {})
        bq_sql = _to_bq(sql)
        try:
            self._client.query(bq_sql, job_config=self._job_config(bq_sql, params, dry_run=True))
        except Exception as exc:  # pragma: no cover - network dependent
            raise RuntimeError(f"BigQuery dry-run failed: {exc}") from exc
