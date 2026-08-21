"""AuditingLlmClient — decorator that emits LLM_CALL events for every call.

Wraps any LlmClient. The wrapped client is unaware of audit. Useful for
producing the per-call audit record required by Validated mode (every
LLM call's request shape + response model_version + token counts).

The decorator records what the audit ledger needs:
  - tier (which model tier)
  - temperature, max_tokens, response_schema_name
  - input/output token counts
  - model_version actually used
  - prompt + response payload references (PoC: not externalized; production
    writes these to GCS and stores the URI in payload_ref)
"""

from __future__ import annotations

from datetime import datetime, timezone

from shared.llm import LlmClient, LlmRequest, LlmResponse

from services.audit.schema import AuditAction, AuditEvent, ComplianceMode
from services.audit.store import AuditSink


class AuditingLlmClient(LlmClient):
    def __init__(
        self,
        inner: LlmClient,
        sink: AuditSink,
        *,
        tenant_id: str,
        project_id: str,
        actor_id: str,
        mode: ComplianceMode,
        report_instance_id: str | None = None,
    ) -> None:
        self._inner = inner
        self._sink = sink
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._actor_id = actor_id
        self._mode = mode
        self._report_instance_id = report_instance_id

    def generate(self, request: LlmRequest) -> LlmResponse:
        response = self._inner.generate(request)
        self._sink.emit(
            AuditEvent(
                action=AuditAction.LLM_CALL,
                tenant_id=self._tenant_id,
                project_id=self._project_id,
                mode=self._mode,
                actor_id=self._actor_id,
                target_type="llm_call",
                # "no-provider-id", not "anonymous". In an audit trail
                # "anonymous" reads as an unattributed ACTOR — someone auditing
                # this record would reasonably take it as activity with no
                # identity behind it. What it actually means is narrower and
                # duller: this provider returned no request id to correlate
                # against. The local Claude CLI never does, so every real call
                # would have carried that word.
                #
                # A generated id is deliberately NOT used here. The field is
                # documented as the PROVIDER's request id, and inventing one
                # that looks like the provider's is the precise kind of thing a
                # provenance record must not do. Correlation is already
                # possible: the event carries the model version, the timestamp,
                # and its place in the hash chain.
                target_id=response.request_id or "no-provider-id",
                target_version=response.model_version,
                timestamp_utc=datetime.now(timezone.utc),
                extra={
                    "tier": request.tier.value,
                    "temperature": request.temperature,
                    "max_tokens": request.max_tokens,
                    "response_schema": request.response_schema_name or "",
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "stop_reason": response.stop_reason or "",
                    "report_instance_id": self._report_instance_id or "",
                },
            )
        )
        return response
