"""Tests for the BigQuery executor's pure helpers + lazy-import behaviour.

The executor can't be exercised against live BigQuery here (no creds), so we
test the deterministic parameter-translation logic and that the symbol is
exported and fails cleanly without the [gcp] extra.
"""

from __future__ import annotations

import pytest

from services.data_integration import BigQueryQueryExecutor
from services.data_integration.bigquery_executor import _referenced_params, _to_bq


def test_to_bq_rewrites_named_params() -> None:
    assert _to_bq("SELECT * FROM t WHERE id = :compound_id") == (
        "SELECT * FROM t WHERE id = @compound_id"
    )


def test_to_bq_rewrites_multiple_params() -> None:
    sql = "SELECT * FROM t WHERE a = :a AND b = :b AND a2 = :a"
    out = _to_bq(sql)
    assert ":a" not in out and ":b" not in out
    assert "@a" in out and "@b" in out


def test_referenced_params_dedupes_and_sorts() -> None:
    assert _referenced_params("... :b ... :a ... :a ...") == ["a", "b"]


def test_referenced_params_ignores_casts() -> None:
    # Postgres-style ::type casts should not be picked up as params.
    assert _referenced_params("SELECT x::text FROM t WHERE id = :id") == ["id"]


def test_executor_symbol_exported() -> None:
    assert BigQueryQueryExecutor is not None


def test_executor_requires_gcp_extra() -> None:
    """Without google-cloud-bigquery installed, construction raises a clear
    RuntimeError rather than an obscure ImportError deep in a call."""
    try:
        import google.cloud.bigquery  # type: ignore[import-not-found]  # noqa: F401
        pytest.skip("google-cloud-bigquery is installed; skipping missing-dep check")
    except ImportError:
        pass
    with pytest.raises(RuntimeError, match="google-cloud-bigquery"):
        BigQueryQueryExecutor(project="x")
