"""SqlSafetyGate — orchestrates linter + dry-run + approval.

Two paths through the gate:
1. Named query → linter is informational only (the query was lint-passed
   at registration time), dry-run still runs, approval is auto-granted
   because the registry IS the approval record.
2. LLM-drafted SQL → linter is mandatory, dry-run is mandatory, approval
   callback is required.

Each gate execution emits a structured SafetyVerdict that the orchestrator
records in the audit chain.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from services.data_integration.approval import (
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
    deny_all,
)
from services.data_integration.executor import QueryExecutor, ResolvedQueryResult
from services.data_integration.named_query import NamedQuery, NamedQueryRegistry
from services.data_integration.sql_safety import SqlLinter, SqlSafetyViolation


@dataclass(frozen=True)
class SafetyVerdict:
    path: Literal["named_query", "llm_drafted"]
    passed: bool
    sql: str
    parameters: dict[str, object] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    approval: ApprovalDecision | None = None
    failure_code: str | None = None
    failure_message: str | None = None


class SqlSafetyGate:
    def __init__(
        self,
        *,
        executor: QueryExecutor,
        registry: NamedQueryRegistry | None = None,
        linter: SqlLinter | None = None,
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        self._executor = executor
        self._registry = registry or NamedQueryRegistry()
        self._linter = linter or SqlLinter()
        # Default to deny_all so the gate is fail-closed.
        self._approval = approval_callback or deny_all

    # -- Path 1: named query ------------------------------------------------

    def run_named_query(
        self,
        query_id: str,
        parameters: Mapping[str, Any],
        *,
        actor_id: str = "system:orchestrator",
    ) -> tuple[ResolvedQueryResult, SafetyVerdict]:
        query = self._registry.get(query_id)
        validated = query.validate_args(dict(parameters))
        # Dry-run is still useful (catches table-missing errors).
        try:
            self._executor.dry_run(query.sql, validated)
        except Exception as exc:
            verdict = SafetyVerdict(
                path="named_query",
                passed=False,
                sql=query.sql,
                parameters=dict(validated),
                failure_code="DRY_RUN_FAILED",
                failure_message=str(exc),
            )
            raise SqlSafetyViolation("DRY_RUN_FAILED", str(exc)) from exc
        result = self._executor.execute(query.sql, validated)
        verdict = SafetyVerdict(
            path="named_query",
            passed=True,
            sql=query.sql,
            parameters=dict(validated),
            notes=[
                f"named query {query.id} v{query.version}",
                f"approved_by={query.approval.approved_by}",
                f"approved_at={query.approval.approved_at.isoformat()}",
            ],
            approval=ApprovalDecision(
                verdict="approved",
                reviewer_id=query.approval.approved_by,
                reviewer_reason=f"registry approval, query v{query.version}",
            ),
        )
        return result, verdict

    # -- Path 2: LLM-drafted SQL --------------------------------------------

    def run_llm_drafted(
        self,
        sql: str,
        parameters: Mapping[str, Any],
        *,
        intent: str = "",
        actor_id: str = "system:orchestrator",
    ) -> tuple[ResolvedQueryResult | None, SafetyVerdict]:
        """Lint → dry-run → human approval → execute. Returns (result, verdict).

        On failure, result is None and the verdict carries the failure code.
        Callers should NOT execute the SQL themselves on a passed verdict —
        the gate executes once approval is granted.
        """
        # 1. Lint
        try:
            self._linter.lint(sql)
        except SqlSafetyViolation as exc:
            return None, SafetyVerdict(
                path="llm_drafted",
                passed=False,
                sql=sql,
                parameters=dict(parameters),
                failure_code=exc.code,
                failure_message=exc.message,
            )

        # 2. Dry-run
        dry_run_notes: list[str] = []
        try:
            self._executor.dry_run(sql, dict(parameters))
            dry_run_notes.append("dry_run=ok")
        except Exception as exc:
            return None, SafetyVerdict(
                path="llm_drafted",
                passed=False,
                sql=sql,
                parameters=dict(parameters),
                failure_code="DRY_RUN_FAILED",
                failure_message=str(exc),
            )

        # 3. Human approval
        request = ApprovalRequest(
            sql=sql,
            parameters=dict(parameters),
            source=getattr(self._executor, "_source", "unknown"),  # type: ignore[arg-type]
            intent=intent,
            dry_run_passed=True,
            dry_run_notes=dry_run_notes,
            requested_by=actor_id,
            requested_at=datetime.now(timezone.utc),
        )
        decision = self._approval(request)
        if decision.verdict != "approved":
            return None, SafetyVerdict(
                path="llm_drafted",
                passed=False,
                sql=sql,
                parameters=dict(parameters),
                failure_code="DENIED_BY_REVIEWER",
                failure_message=decision.reviewer_reason or "approval denied",
                approval=decision,
            )

        effective_sql = decision.sql_modifications or sql

        # 4. Execute the (possibly modified) approved SQL.
        # If the reviewer modified the SQL, re-lint it as a safety net
        # so an approver can't bypass the policy via a sneaky modification.
        if decision.sql_modifications:
            try:
                self._linter.lint(effective_sql)
            except SqlSafetyViolation as exc:
                return None, SafetyVerdict(
                    path="llm_drafted",
                    passed=False,
                    sql=effective_sql,
                    parameters=dict(parameters),
                    failure_code="MODIFIED_SQL_FAILED_LINT",
                    failure_message=exc.message,
                    approval=decision,
                )

        try:
            result = self._executor.execute(effective_sql, dict(parameters))
        except Exception as exc:
            return None, SafetyVerdict(
                path="llm_drafted",
                passed=False,
                sql=effective_sql,
                parameters=dict(parameters),
                failure_code="EXECUTION_FAILED",
                failure_message=str(exc),
                approval=decision,
            )

        verdict = SafetyVerdict(
            path="llm_drafted",
            passed=True,
            sql=effective_sql,
            parameters=dict(parameters),
            notes=dry_run_notes
            + [f"approved_by={decision.reviewer_id}", f"intent={intent or 'unspecified'}"],
            approval=decision,
        )
        return result, verdict
