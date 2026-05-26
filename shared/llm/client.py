"""LlmClient protocol and request/response types.

The protocol abstracts over the concrete backend (Vertex AI Claude in
production, Stub in tests). Callers ask for a model by tier (FILL or
PLAN_CRITIQUE); the concrete client resolves that to a specific pinned
model version. Validated-mode runs require the model version + temp +
seed all be captured in the audit log (`LlmResponse.model_version`,
`request.temperature`, `request.seed`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class ModelTier(StrEnum):
    """Logical tier — the concrete client maps to a pinned model version.

    FILL is used for the per-section generation pass (high volume, must be
    cost-efficient — Sonnet). PLAN_CRITIQUE is used for the planning pass
    and per-section self-review (low volume, quality-critical — Opus).
    """

    FILL = "fill"
    PLAN_CRITIQUE = "plan_critique"


class LlmRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class LlmMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: LlmRole
    content: str


class LlmRequest(BaseModel):
    """A single LLM call.

    Every field that affects the output is captured here so the call can be
    serialized into the audit log and (in Validated mode) replayed for
    forensic re-derivation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: ModelTier
    system: str
    messages: list[LlmMessage]
    max_tokens: int = Field(default=4096, ge=1, le=64_000)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    # Top-p left to the implementation; not commonly tuned for our workload.
    seed: int | None = Field(
        default=None,
        description="Vertex AI does not honor seeds today; preserved in audit log",
    )
    response_schema_name: str | None = Field(
        default=None,
        description=(
            "If set, the model is constrained to return JSON conforming to "
            "this Pydantic model — the client validates and parses on return."
        ),
    )
    response_schema_json: dict[str, object] | None = Field(
        default=None,
        description="The JSON Schema for response_schema_name, attached for the model",
    )


class LlmUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)


class LlmResponse(BaseModel):
    """Response from an LLM call.

    `model_version` is the exact pinned version that handled the call —
    must be a specific identifier (e.g. `claude-sonnet-4-6@20260301`),
    never a moving alias like `latest`. This is the audit anchor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(description="Raw assistant text content concatenated")
    parsed_json: dict[str, object] | None = Field(
        default=None,
        description="If response_schema_name was set, the validated JSON dict",
    )
    model_version: str
    stop_reason: str | None = None
    usage: LlmUsage
    request_id: str | None = Field(
        default=None, description="Provider request ID for audit correlation"
    )


T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    """The model returned content that did not parse against the requested schema."""


class LlmValidationError(Exception):
    """The request was malformed before being sent (bad arguments, missing config)."""


class LlmClient(Protocol):
    """A backend-agnostic interface to a Claude model.

    Implementations: VertexLlmClient (Vertex AI via ADC), StubLlmClient (tests).
    The protocol intentionally exposes only one method — multi-turn handling,
    retries, and citation logic live one layer up in the orchestrator.
    """

    def generate(self, request: LlmRequest) -> LlmResponse: ...
