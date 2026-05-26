"""Cross-service LLM client.

Two implementations:
- StubLlmClient — deterministic canned responses; no cloud auth; for tests
  and dev work without Vertex access.
- VertexLlmClient — Claude on Vertex AI via the anthropic[vertex] SDK,
  authenticated through Application Default Credentials (workload identity
  in Cloud Run, `gcloud auth application-default login` locally).

The project NEVER uses ANTHROPIC_API_KEY — GSK has Kong + Vertex AI with
workload identity federation, which eliminates static keys.
"""

from shared.llm.client import (
    LlmClient,
    LlmMessage,
    LlmRequest,
    LlmResponse,
    LlmRole,
    LlmUsage,
    LlmValidationError,
    ModelTier,
    StructuredOutputError,
)
from shared.llm.stub import StubLlmClient
from shared.llm.vertex import VertexConfig, VertexLlmClient

__all__ = [
    "LlmClient",
    "LlmMessage",
    "LlmRequest",
    "LlmResponse",
    "LlmRole",
    "LlmUsage",
    "LlmValidationError",
    "ModelTier",
    "StructuredOutputError",
    "StubLlmClient",
    "VertexConfig",
    "VertexLlmClient",
]
