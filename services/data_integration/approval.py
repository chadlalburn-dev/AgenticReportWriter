"""Human approval gate for LLM-drafted SQL.

The PoC uses a simple callback model: the caller supplies a function that
receives an ApprovalRequest and returns an ApprovalDecision. Production
wires this to a UI modal (Slack approval, dashboard prompt, etc.) — same
shape, different transport.

The audit trail captures every approval decision (who, when, what was
shown, what they decided) via the existing AuditEvent infrastructure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class ApprovalRequest:
    """What the human is being asked to approve.

    `dry_run_passed` carries the dry-run summary (rows that would be
    returned, tables touched). Reviewers should see this before deciding.
    """

    sql: str
    parameters: dict[str, object]
    source: str
    intent: str = field(default="")
    dry_run_passed: bool = False
    dry_run_notes: list[str] = field(default_factory=list)
    requested_by: str = "system:orchestrator"
    requested_at: datetime | None = None


@dataclass(frozen=True)
class ApprovalDecision:
    verdict: Literal["approved", "denied"]
    reviewer_id: str
    reviewer_reason: str = ""
    sql_modifications: str | None = None


ApprovalCallback = Callable[[ApprovalRequest], ApprovalDecision]


def deny_all(request: ApprovalRequest) -> ApprovalDecision:
    """Default policy when no approver is configured: deny every request.

    This makes the safety gate fail-closed: an LLM-drafted SQL query
    cannot reach production data unless an explicit approver was wired
    in. Use during unit tests or as a safe default.
    """

    return ApprovalDecision(
        verdict="denied",
        reviewer_id="system:default-deny",
        reviewer_reason=(
            "No human approver was configured; the safety gate is fail-closed. "
            "Wire an ApprovalCallback to the SqlSafetyGate to enable LLM-drafted SQL."
        ),
    )


def auto_approve(reviewer_id: str = "test:auto") -> ApprovalCallback:
    """Factory: approves every request. ONLY for tests and dev demos.

    Never wire this into anything that touches production data."""

    def _callback(request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            verdict="approved",
            reviewer_id=reviewer_id,
            reviewer_reason="auto-approve (dev/test)",
        )

    return _callback
