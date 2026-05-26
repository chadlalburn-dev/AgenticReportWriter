"""Plan phase — outline what the report will assert before filling sections.

The plan is a structured outline keyed by section_id. It's consumed by
filler runs as additional context, helping the LLM keep cross-section
references consistent (e.g., the safety section's NOAEL recommendation
matches the actual NOAEL identified in the toxicology section).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from shared.llm import LlmClient, LlmMessage, LlmRequest, LlmRole, ModelTier
from shared.schemas import CanonicalDocument, ReportTemplate

from .prompts import PLAN_SYSTEM_PROMPT, PROMPT_VERSION


class _SectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: str
    intended_assertions: list[str] = Field(default_factory=list)
    source_docs_referenced: list[str] = Field(default_factory=list)
    cross_references: list[str] = Field(default_factory=list)


class _PlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overall_summary: str
    section_plans: list[_SectionPlan]


@dataclass(frozen=True)
class ReportPlan:
    overall_summary: str
    by_section_id: dict[str, _SectionPlan]
    model_version: str


class ReportPlanner:
    def __init__(self, client: LlmClient) -> None:
        self._client = client

    def plan(
        self,
        *,
        template: ReportTemplate,
        available_docs: list[CanonicalDocument],
        free_text_inputs: dict[str, str],
    ) -> ReportPlan:
        section_listing = "\n".join(
            f"- {s.section_id} {s.title} (level={s.level}, "
            f"mode={s.generation.mode.value})"
            for s in template.all_sections()
        )
        doc_listing = "\n".join(
            f"- {d.doc_id} title={d.title!r} mime={d.mime_type} tags={d.tags}"
            for d in available_docs
        )
        inputs_listing = "\n".join(f"- {k} = {v!r}" for k, v in free_text_inputs.items())

        user_message = (
            f"# Template: {template.title} (v{template.version})\n\n"
            f"## Sections\n{section_listing}\n\n"
            f"## Available source documents\n{doc_listing}\n\n"
            f"## User inputs\n{inputs_listing}\n\n"
            "Produce the plan via emit_structured_output."
        )

        request = LlmRequest(
            tier=ModelTier.PLAN_CRITIQUE,
            system=PLAN_SYSTEM_PROMPT + f"\n\nprompt_version: {PROMPT_VERSION}",
            messages=[LlmMessage(role=LlmRole.USER, content=user_message)],
            max_tokens=4096,
            temperature=0.0,
            response_schema_name="PlanOutput",
            response_schema_json=_PlanOutput.model_json_schema(),
        )

        response = self._client.generate(request)
        plan = _PlanOutput.model_validate(response.parsed_json or {})
        return ReportPlan(
            overall_summary=plan.overall_summary,
            by_section_id={p.section_id: p for p in plan.section_plans},
            model_version=response.model_version,
        )
