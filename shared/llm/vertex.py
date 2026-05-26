"""VertexLlmClient — Claude on GSK corporate Vertex AI.

Authentication is via Application Default Credentials. Local dev: run
`gcloud auth application-default login` once. In Cloud Run: workload
identity binds the service account automatically — no keys, no
credential files.

This client intentionally does NOT accept an `api_key` argument. ANTHROPIC_API_KEY
is not used in this project (corporate policy + audit reasons). If you find
yourself wanting to add one, stop and check with the GSK Vertex team
(Douglas Scheesley in Gene's group) about the correct corporate channel.

Structured output is implemented via Anthropic's tool-use API: when
`response_schema_json` is set, a single tool with that input_schema is
defined and the model is forced to call it. The tool's input becomes the
parsed_json on the response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from shared.llm.client import (
    LlmClient,
    LlmRequest,
    LlmResponse,
    LlmRole,
    LlmUsage,
    LlmValidationError,
    ModelTier,
    StructuredOutputError,
)


# Default model identifiers — pinned to specific versions per the
# Validated-mode requirement. Update via change control when re-qualifying
# against a new Claude release.
DEFAULT_MODELS = {
    ModelTier.FILL: "claude-sonnet-4-6@20260301",
    ModelTier.PLAN_CRITIQUE: "claude-opus-4-7@20260115",
}

STRUCTURED_OUTPUT_TOOL_NAME = "emit_structured_output"


@dataclass(frozen=True)
class VertexConfig:
    """How to reach Vertex AI.

    For direct Vertex access: region + project_id are required, base_url unset.
    For Kong-fronted Vertex (GSK production path): set base_url to the Kong
    endpoint; region/project_id may still be needed depending on Kong's
    upstream config. Confirm with the platform team.
    """

    project_id: str
    region: str = "us-east5"
    base_url: str | None = None
    models: dict[ModelTier, str] | None = None

    def model_for_tier(self, tier: ModelTier) -> str:
        if self.models and tier in self.models:
            return self.models[tier]
        return DEFAULT_MODELS[tier]


class VertexLlmClient(LlmClient):
    def __init__(self, config: VertexConfig) -> None:
        # Imported lazily so the rest of the project can be imported on
        # machines without the SDK installed (or without GCP credentials).
        try:
            from anthropic import AnthropicVertex  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LlmValidationError(
                "anthropic[vertex] is not installed. Run "
                "`pip install 'anthropic[vertex]>=0.40'` in the project venv."
            ) from exc

        client_kwargs: dict[str, object] = {
            "region": config.region,
            "project_id": config.project_id,
        }
        if config.base_url:
            client_kwargs["base_url"] = config.base_url
        self._client = AnthropicVertex(**client_kwargs)  # type: ignore[arg-type]
        self._config = config

    def generate(self, request: LlmRequest) -> LlmResponse:
        model = self._config.model_for_tier(request.tier)

        sdk_messages = [
            {"role": m.role.value, "content": m.content} for m in request.messages
        ]

        create_kwargs: dict[str, object] = {
            "model": model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": request.system,
            "messages": sdk_messages,
        }

        if request.response_schema_json:
            if not request.response_schema_name:
                raise LlmValidationError(
                    "response_schema_json was set but response_schema_name was not"
                )
            tool = {
                "name": STRUCTURED_OUTPUT_TOOL_NAME,
                "description": (
                    f"Emit a {request.response_schema_name} value conforming "
                    "to the input_schema. Call this exactly once."
                ),
                "input_schema": request.response_schema_json,
            }
            create_kwargs["tools"] = [tool]
            create_kwargs["tool_choice"] = {
                "type": "tool",
                "name": STRUCTURED_OUTPUT_TOOL_NAME,
            }

        message = self._client.messages.create(**create_kwargs)  # type: ignore[arg-type]

        text_parts: list[str] = []
        parsed_json: dict[str, object] | None = None
        for block in message.content:  # type: ignore[attr-defined]
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(block.text)  # type: ignore[attr-defined]
            elif block_type == "tool_use" and block.name == STRUCTURED_OUTPUT_TOOL_NAME:  # type: ignore[attr-defined]
                tool_input = block.input  # type: ignore[attr-defined]
                if not isinstance(tool_input, dict):
                    raise StructuredOutputError(
                        f"tool_use input was not a dict: {type(tool_input)!r}"
                    )
                parsed_json = tool_input

        if request.response_schema_json and parsed_json is None:
            raise StructuredOutputError(
                f"Model returned no tool_use for {request.response_schema_name!r}; "
                f"stop_reason={message.stop_reason!r}"
            )

        usage = LlmUsage(
            input_tokens=getattr(message.usage, "input_tokens", 0),  # type: ignore[attr-defined]
            output_tokens=getattr(message.usage, "output_tokens", 0),  # type: ignore[attr-defined]
            cache_read_input_tokens=getattr(
                message.usage, "cache_read_input_tokens", 0
            ),  # type: ignore[attr-defined]
            cache_creation_input_tokens=getattr(
                message.usage, "cache_creation_input_tokens", 0
            ),  # type: ignore[attr-defined]
        )

        return LlmResponse(
            text="\n".join(text_parts) if text_parts else json.dumps(parsed_json or {}),
            parsed_json=parsed_json,
            model_version=model,
            stop_reason=message.stop_reason,  # type: ignore[attr-defined]
            usage=usage,
            request_id=getattr(message, "id", None),
        )
