"""StubLlmClient — deterministic responses for tests and dev work.

Three response modes (checked in order):
1. Callable handlers: a (matcher, generator) pair where the matcher inspects
   the request and the generator produces the response. Needed when the
   request contains run-specific data (e.g. citation_ids generated per
   call) that the test cannot know in advance.
2. A keyed fixture registry: pre-registered exact-match responses.
3. An echo-and-template default: if no fixture matches and strict is False,
   the stub returns a synthetic response derived from the request.

The stub records every call it received, so tests can assert on the
sequence of prompts the orchestrator generated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from shared.llm.client import (
    LlmClient,
    LlmRequest,
    LlmResponse,
    LlmUsage,
    ModelTier,
    StructuredOutputError,
)


def _hash_request(request: LlmRequest) -> str:
    """Stable hash of the parts of a request that determine the response.

    Excludes the seed (we never depend on it for stub matching) and
    everything not deterministic. The hash is the key into the fixture
    registry.
    """
    payload = {
        "tier": request.tier.value,
        "system": request.system,
        "messages": [{"role": m.role.value, "content": m.content} for m in request.messages],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "response_schema_name": request.response_schema_name,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class _RecordedCall:
    request: LlmRequest
    response: LlmResponse


class StubLlmClient(LlmClient):
    def __init__(
        self,
        *,
        default_model_version: str = "stub-claude-sonnet-4-6@stub",
        strict: bool = False,
    ) -> None:
        """
        Args:
            default_model_version: The model_version returned by responses
                that fall through to the template default.
            strict: If True, an unregistered prompt raises; if False,
                an echo-template response is returned. Strict is the right
                default for unit tests; non-strict is useful for exploratory
                runs.
        """
        self._fixtures: dict[str, LlmResponse] = {}
        self._handlers: list[
            tuple[Callable[[LlmRequest], bool], Callable[[LlmRequest], LlmResponse]]
        ] = []
        self._calls: list[_RecordedCall] = []
        self._default_model_version = default_model_version
        self._strict = strict

    # -- fixture management ---------------------------------------------------

    def register(self, request: LlmRequest, response: LlmResponse) -> None:
        """Pre-register a response for a specific request."""
        self._fixtures[_hash_request(request)] = response

    def register_text(
        self,
        request: LlmRequest,
        text: str,
        *,
        model_version: str | None = None,
        parsed_json: dict[str, object] | None = None,
    ) -> None:
        """Convenience: register a plain-text response for a request."""
        self.register(
            request,
            LlmResponse(
                text=text,
                parsed_json=parsed_json,
                model_version=model_version or self._default_model_version,
                stop_reason="end_turn",
                usage=LlmUsage(input_tokens=len(request.system) // 4, output_tokens=len(text) // 4),
            ),
        )

    def register_handler(
        self,
        matcher: Callable[[LlmRequest], bool],
        generator: Callable[[LlmRequest], LlmResponse],
    ) -> None:
        """Register a (matcher, generator) callable pair.

        Useful when the response depends on data inside the request itself
        (e.g. citation_ids the orchestrator just allocated). The first
        matcher to return True wins.
        """
        self._handlers.append((matcher, generator))

    def make_response(
        self,
        *,
        text: str = "",
        parsed_json: dict[str, object] | None = None,
        model_version: str | None = None,
    ) -> LlmResponse:
        """Convenience for building responses inside handler generators."""
        return LlmResponse(
            text=text,
            parsed_json=parsed_json,
            model_version=model_version or self._default_model_version,
            stop_reason="end_turn",
            usage=LlmUsage(
                input_tokens=max(1, len(text) // 4), output_tokens=max(1, len(text) // 4)
            ),
        )

    # -- inspection -----------------------------------------------------------

    @property
    def calls(self) -> list[_RecordedCall]:
        return list(self._calls)

    def reset(self) -> None:
        self._fixtures.clear()
        self._handlers.clear()
        self._calls.clear()

    # -- LlmClient interface --------------------------------------------------

    def generate(self, request: LlmRequest) -> LlmResponse:
        for matcher, generator in self._handlers:
            if matcher(request):
                response = generator(request)
                self._calls.append(_RecordedCall(request=request, response=response))
                return response

        key = _hash_request(request)
        if key in self._fixtures:
            response = self._fixtures[key]
        elif self._strict:
            raise StructuredOutputError(
                f"StubLlmClient(strict=True) received an unregistered request: "
                f"tier={request.tier.value}, system={request.system[:80]!r}..."
            )
        else:
            response = self._template_response(request)
        self._calls.append(_RecordedCall(request=request, response=response))
        return response

    def _template_response(self, request: LlmRequest) -> LlmResponse:
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role.value == "user"),
            "",
        )
        text = (
            f"[STUB::{request.tier.value}] Response to prompt "
            f"(len={len(last_user)}): {last_user[:200]}"
        )
        parsed_json: dict[str, object] | None = None
        if request.response_schema_name:
            parsed_json = {
                "_stub": True,
                "schema": request.response_schema_name,
                "tier": request.tier.value,
            }
        return LlmResponse(
            text=text,
            parsed_json=parsed_json,
            model_version=self._default_model_version,
            stop_reason="end_turn",
            usage=LlmUsage(
                input_tokens=max(1, len(last_user) // 4),
                output_tokens=max(1, len(text) // 4),
            ),
        )
