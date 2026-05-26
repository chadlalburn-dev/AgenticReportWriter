"""Tests for the named-query registry + SQL safety gate."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

from services.data_integration import (
    ApprovalRequest,
    NamedQuery,
    NamedQueryRegistry,
    SafetyVerdict,
    SqlLinter,
    SqlSafetyGate,
    SqlSafetyViolation,
    SqliteQueryExecutor,
    auto_approve,
    deny_all,
)
from services.data_integration.named_query import (
    ApprovalRecord,
    OutputColumn,
    QueryParameter,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
QUERIES_DIR = REPO_ROOT / "samples" / "synthetic_compound" / "queries"
EDC_SQLITE = REPO_ROOT / "samples" / "synthetic_compound" / "edc.sqlite"


@pytest.fixture(scope="module", autouse=True)
def _seed_edc_db() -> None:
    if EDC_SQLITE.exists():
        return
    spec = importlib.util.spec_from_file_location(
        "_seed", REPO_ROOT / "samples" / "synthetic_compound" / "seed_db.py"
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.seed()


# --- NamedQuery + Registry -------------------------------------------------


def _toy_query() -> NamedQuery:
    return NamedQuery(
        id="toy_v1",
        description="toy test query",
        source="local",
        version=1,
        sql="SELECT * FROM ae_events_by_soc WHERE compound_id = :compound_id",
        parameters={
            "compound_id": QueryParameter(type="string", required=True),
        },
        output_columns={"MedDRA_SOC": OutputColumn(type="string")},
        approval=ApprovalRecord(
            approved_by="tester", approved_at=date(2026, 5, 26), change_log=["v1"]
        ),
    )


def test_named_query_validate_args_fills_defaults() -> None:
    q = NamedQuery(
        id="q",
        description="d",
        source="s",
        version=1,
        sql="SELECT 1",
        parameters={
            "name": QueryParameter(type="string", required=True),
            "limit": QueryParameter(type="integer", required=False, default=10),
        },
        output_columns={},
        approval=ApprovalRecord(approved_by="t", approved_at=date(2026, 1, 1)),
    )
    args = q.validate_args({"name": "Alice"})
    assert args == {"name": "Alice", "limit": 10}


def test_named_query_validate_args_rejects_missing_required() -> None:
    q = _toy_query()
    with pytest.raises(ValueError, match="missing required parameter"):
        q.validate_args({})


def test_named_query_validate_args_rejects_unknown() -> None:
    q = _toy_query()
    with pytest.raises(ValueError, match="unknown parameter"):
        q.validate_args({"compound_id": "X", "bogus": 42})


def test_registry_loads_yaml_directory() -> None:
    registry = NamedQueryRegistry.from_directory(QUERIES_DIR)
    assert len(registry) >= 5
    assert "ae_summary_by_soc_v3" in registry.ids()
    assert registry.get("ae_summary_by_soc_v3").version == 3


def test_registry_rejects_duplicate_id() -> None:
    registry = NamedQueryRegistry()
    registry.register(_toy_query())
    with pytest.raises(ValueError, match="duplicate query id"):
        registry.register(_toy_query())


def test_registry_unknown_query_raises_with_helpful_message() -> None:
    registry = NamedQueryRegistry.from_directory(QUERIES_DIR)
    with pytest.raises(KeyError, match="unknown named query"):
        registry.get("does_not_exist")


# --- SqlLinter ------------------------------------------------------------


def test_linter_accepts_simple_select() -> None:
    linter = SqlLinter()
    result = linter.lint("SELECT a, b FROM t WHERE c = :param")
    assert "param" in result.parameters_referenced


def test_linter_accepts_with_cte() -> None:
    linter = SqlLinter()
    linter.lint("WITH x AS (SELECT * FROM t) SELECT * FROM x")


def test_linter_rejects_multi_statement() -> None:
    linter = SqlLinter()
    with pytest.raises(SqlSafetyViolation) as exc:
        linter.lint("SELECT * FROM t; DELETE FROM t")
    assert exc.value.code in ("MULTI_STATEMENT", "FORBIDDEN_OPERATION")


@pytest.mark.parametrize(
    "sql,expected_code",
    [
        ("DROP TABLE t", "FORBIDDEN_OPERATION"),
        ("CREATE TABLE t (x INT)", "FORBIDDEN_OPERATION"),
        ("DELETE FROM t", "FORBIDDEN_OPERATION"),
        ("UPDATE t SET x = 1", "FORBIDDEN_OPERATION"),
        ("INSERT INTO t VALUES (1)", "FORBIDDEN_OPERATION"),
    ],
)
def test_linter_rejects_forbidden_top_levels(sql: str, expected_code: str) -> None:
    with pytest.raises(SqlSafetyViolation) as exc:
        SqlLinter().lint(sql)
    assert exc.value.code == expected_code


def test_linter_rejects_parse_error() -> None:
    with pytest.raises(SqlSafetyViolation) as exc:
        SqlLinter().lint("SELECT FROM WHERE")
    assert exc.value.code == "PARSE_ERROR"


# --- SqliteQueryExecutor --------------------------------------------------


def test_executor_executes_named_query() -> None:
    executor = SqliteQueryExecutor(EDC_SQLITE, source="edc_warehouse", read_only=True)
    result = executor.execute(
        "SELECT soc, any_grade_n FROM ae_events_by_soc WHERE compound_id = :compound_id",
        {"compound_id": "XYZ-001"},
    )
    assert result.row_count > 0
    socs = [r[0] for r in result.rows]
    assert "Gastrointestinal disorders" in socs


def test_executor_read_only_blocks_writes() -> None:
    executor = SqliteQueryExecutor(EDC_SQLITE, source="edc_warehouse", read_only=True)
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        executor.execute("DELETE FROM ae_events_by_soc")


def test_executor_dry_run_validates_query() -> None:
    executor = SqliteQueryExecutor(EDC_SQLITE, source="edc_warehouse", read_only=True)
    # Valid: returns nothing.
    executor.dry_run("SELECT 1 FROM ae_events_by_soc WHERE compound_id = :compound_id", {"compound_id": "X"})
    # Invalid: missing table.
    with pytest.raises(RuntimeError, match="dry-run failed"):
        executor.dry_run("SELECT 1 FROM nonexistent_table", {})


# --- SqlSafetyGate ---------------------------------------------------------


def test_gate_named_query_path_executes_and_returns_verdict() -> None:
    registry = NamedQueryRegistry.from_directory(QUERIES_DIR)
    executor = SqliteQueryExecutor(EDC_SQLITE, source="edc_warehouse", read_only=True)
    gate = SqlSafetyGate(executor=executor, registry=registry)
    result, verdict = gate.run_named_query(
        "ae_summary_by_soc_v3", {"compound_id": "XYZ-001"}
    )
    assert verdict.passed
    assert verdict.path == "named_query"
    assert result.row_count > 0
    assert verdict.approval is not None
    assert verdict.approval.verdict == "approved"
    assert verdict.approval.reviewer_id == "medical_writing_qa"


def test_gate_llm_drafted_path_denied_by_default() -> None:
    """Without an approver, the gate must fail-closed."""
    executor = SqliteQueryExecutor(EDC_SQLITE, source="edc_warehouse", read_only=True)
    gate = SqlSafetyGate(executor=executor)  # no approval callback
    result, verdict = gate.run_llm_drafted(
        "SELECT * FROM ae_events_by_soc WHERE compound_id = :compound_id",
        {"compound_id": "XYZ-001"},
    )
    assert result is None
    assert not verdict.passed
    assert verdict.failure_code == "DENIED_BY_REVIEWER"


def test_gate_llm_drafted_path_with_auto_approve_executes() -> None:
    executor = SqliteQueryExecutor(EDC_SQLITE, source="edc_warehouse", read_only=True)
    gate = SqlSafetyGate(executor=executor, approval_callback=auto_approve())
    result, verdict = gate.run_llm_drafted(
        "SELECT soc FROM ae_events_by_soc WHERE compound_id = :compound_id",
        {"compound_id": "XYZ-001"},
        intent="get SOC list for safety summary",
    )
    assert verdict.passed
    assert result is not None
    assert result.row_count > 0
    assert verdict.approval is not None
    assert verdict.approval.verdict == "approved"


def test_gate_llm_drafted_blocks_forbidden_sql_before_approval() -> None:
    """A linter failure should short-circuit even if an auto-approver is wired."""
    executor = SqliteQueryExecutor(EDC_SQLITE, source="edc_warehouse", read_only=True)
    gate = SqlSafetyGate(executor=executor, approval_callback=auto_approve())
    _, verdict = gate.run_llm_drafted("DROP TABLE ae_events_by_soc", {})
    assert not verdict.passed
    assert verdict.failure_code == "FORBIDDEN_OPERATION"
    assert verdict.approval is None  # approval should never have been asked


def test_gate_llm_drafted_re_lints_reviewer_modifications() -> None:
    """If a reviewer modifies the SQL, the modification is re-linted."""
    executor = SqliteQueryExecutor(EDC_SQLITE, source="edc_warehouse", read_only=True)

    def approve_with_malicious_modification(req: ApprovalRequest):
        from services.data_integration.approval import ApprovalDecision

        return ApprovalDecision(
            verdict="approved",
            reviewer_id="rogue",
            sql_modifications="DELETE FROM ae_events_by_soc",
        )

    gate = SqlSafetyGate(
        executor=executor, approval_callback=approve_with_malicious_modification
    )
    _, verdict = gate.run_llm_drafted(
        "SELECT * FROM ae_events_by_soc WHERE compound_id = :compound_id",
        {"compound_id": "XYZ-001"},
    )
    assert not verdict.passed
    assert verdict.failure_code == "MODIFIED_SQL_FAILED_LINT"


def test_gate_named_query_dry_run_failure_raises() -> None:
    """If a named query's table doesn't exist (schema drift), the dry-run catches it."""
    # Register a query against a missing table on the fly.
    registry = NamedQueryRegistry()
    registry.register(
        NamedQuery(
            id="broken_v1",
            description="references a nonexistent table",
            source="edc_warehouse",
            version=1,
            sql="SELECT * FROM nonexistent_table WHERE compound_id = :compound_id",
            parameters={"compound_id": QueryParameter(type="string", required=True)},
            output_columns={},
            approval=ApprovalRecord(approved_by="t", approved_at=date(2026, 1, 1)),
        )
    )
    executor = SqliteQueryExecutor(EDC_SQLITE, source="edc_warehouse", read_only=True)
    gate = SqlSafetyGate(executor=executor, registry=registry)
    with pytest.raises(SqlSafetyViolation) as exc:
        gate.run_named_query("broken_v1", {"compound_id": "X"})
    assert exc.value.code == "DRY_RUN_FAILED"
