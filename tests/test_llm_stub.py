"""Tests for the StubLlmClient — the test/dev backend that stands in for
Vertex AI Claude in unit tests."""

from __future__ import annotations

import pytest

from shared.llm import (
    LlmMessage,
    LlmRequest,
    LlmRole,
    ModelTier,
    StructuredOutputError,
    StubLlmClient,
)


def _req(text: str, *, tier: ModelTier = ModelTier.FILL, schema: str | None = None) -> LlmRequest:
    return LlmRequest(
        tier=tier,
        system="you are a tester",
        messages=[LlmMessage(role=LlmRole.USER, content=text)],
        response_schema_name=schema,
        response_schema_json={"type": "object"} if schema else None,
    )


def test_register_and_dispatch_text_response() -> None:
    stub = StubLlmClient(strict=True)
    req = _req("hello")
    stub.register_text(req, "world")
    resp = stub.generate(req)
    assert resp.text == "world"
    assert len(stub.calls) == 1


def test_strict_mode_raises_on_unregistered() -> None:
    stub = StubLlmClient(strict=True)
    with pytest.raises(StructuredOutputError):
        stub.generate(_req("never registered"))


def test_non_strict_returns_template_response() -> None:
    stub = StubLlmClient(strict=False)
    resp = stub.generate(_req("anything goes"))
    assert "[STUB::fill]" in resp.text


def test_handler_takes_precedence_over_fixture() -> None:
    stub = StubLlmClient(strict=True)
    req = _req("hello")
    stub.register_text(req, "from fixture")
    stub.register_handler(
        lambda r: "hello" in r.messages[-1].content,
        lambda r: stub.make_response(text="from handler"),
    )
    resp = stub.generate(req)
    assert resp.text == "from handler"


def test_handler_can_inspect_request_for_dynamic_response() -> None:
    """Use case: orchestrator allocates citation_ids per-call; the stub must
    parrot them back in a FillOutput-shaped response."""
    stub = StubLlmClient(strict=True)

    def matcher(r: LlmRequest) -> bool:
        return r.response_schema_name == "FillOutput"

    def generator(r: LlmRequest) -> "object":
        # Pretend we extracted citation_ids from the request; emit a single
        # paragraph citing the first one we see.
        # In real tests we'd parse [citation_id=xxx] tokens from the body.
        return stub.make_response(
            parsed_json={
                "paragraphs": [
                    {
                        "text": "Generated.",
                        "claims": [{"text": "claim", "citation_ids": []}],
                    }
                ]
            },
        )

    stub.register_handler(matcher, generator)
    resp = stub.generate(
        LlmRequest(
            tier=ModelTier.FILL,
            system="s",
            messages=[LlmMessage(role=LlmRole.USER, content="msg")],
            response_schema_name="FillOutput",
            response_schema_json={"type": "object"},
        )
    )
    assert resp.parsed_json is not None
    assert "paragraphs" in resp.parsed_json


def test_calls_history_is_recorded() -> None:
    stub = StubLlmClient(strict=False)
    stub.generate(_req("first"))
    stub.generate(_req("second"))
    assert len(stub.calls) == 2
    assert stub.calls[0].request.messages[-1].content == "first"
    assert stub.calls[1].request.messages[-1].content == "second"


def test_reset_clears_state() -> None:
    stub = StubLlmClient(strict=False)
    stub.generate(_req("hello"))
    assert len(stub.calls) == 1
    stub.reset()
    assert len(stub.calls) == 0
