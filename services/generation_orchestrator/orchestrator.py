"""Generation orchestrator — drives the plan → fill → critique loop end-to-end.

Inputs:
  - ReportTemplate (validated, approved in production)
  - Pool of CanonicalDocuments + their ParsedChunks (from ingestion/parsing)
  - free_text_inputs (user-provided per binding_id)
  - compliance_mode + retry budget

Outputs:
  - ReportInstance (sections, paragraphs, claims)
  - list[Citation]
  - GenerationAuditEvent log (every LLM call's request/response captured)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from shared.llm import LlmClient
from shared.schemas import (
    CanonicalDocument,
    Citation,
    GenerationMode,
    ParsedChunk,
    ReportTemplate,
    TemplateSection,
)

from .critic import SectionCritic
from .filler import FillResult, SectionFiller
from .planner import ReportPlan, ReportPlanner
from .retrieval import BindingResolver
from .types import GeneratedSection, ReportInstance


@dataclass
class GenerationAuditEvent:
    event_id: str
    section_id: str | None
    phase: Literal["plan", "fill", "critique"]
    model_version: str
    timestamp: datetime
    status: Literal["ok", "regenerated", "failed"]
    notes: list[str] = field(default_factory=list)


@dataclass
class GenerationResult:
    instance: ReportInstance
    citations: list[Citation]
    plan: ReportPlan | None
    audit_events: list[GenerationAuditEvent]


class ReportGenerator:
    def __init__(
        self,
        *,
        fill_client: LlmClient,
        plan_client: LlmClient | None = None,
        critique_client: LlmClient | None = None,
        max_retries_per_section: int = 2,
    ) -> None:
        # Same client for all three by default; tests/production can split.
        self._planner = ReportPlanner(plan_client or fill_client)
        self._filler = SectionFiller(fill_client)
        self._critic = SectionCritic(critique_client or fill_client)
        self._max_retries = max_retries_per_section

    def generate(
        self,
        *,
        template: ReportTemplate,
        documents: list[CanonicalDocument],
        chunks_by_doc: dict[str, list[ParsedChunk]],
        free_text_inputs: dict[str, str],
        compliance_mode: Literal["rd", "gxp", "part11"] = "rd",
        run_plan_phase: bool = True,
    ) -> GenerationResult:
        instance_id = str(uuid.uuid4())
        generated_at = datetime.now(timezone.utc)
        audit: list[GenerationAuditEvent] = []

        plan: ReportPlan | None = None
        if run_plan_phase:
            plan = self._planner.plan(
                template=template,
                available_docs=documents,
                free_text_inputs=free_text_inputs,
            )
            audit.append(
                GenerationAuditEvent(
                    event_id=str(uuid.uuid4()),
                    section_id=None,
                    phase="plan",
                    model_version=plan.model_version,
                    timestamp=datetime.now(timezone.utc),
                    status="ok",
                )
            )

        resolver = BindingResolver(
            chunks_by_doc=chunks_by_doc,
            docs_by_id={d.doc_id: d for d in documents},
            free_text_inputs=free_text_inputs,
        )

        all_sections = self._collect_template_sections(template)
        generated_by_id: dict[str, GeneratedSection] = {}
        all_citations: list[Citation] = []

        for section in all_sections:
            generated, citations, events = self._generate_section(
                section=section,
                resolver=resolver,
                free_text_inputs=free_text_inputs,
                report_instance_id=instance_id,
                retrieved_at=generated_at,
            )
            generated_by_id[section.section_id] = generated
            all_citations.extend(citations)
            audit.extend(events)

        # Re-assemble the section tree
        top_level = [
            self._assemble_tree(s, generated_by_id) for s in template.sections
        ]

        instance = ReportInstance(
            instance_id=instance_id,
            template_id=template.template_id,
            template_version=template.version,
            compliance_mode=compliance_mode,
            report_title=template.title,
            free_text_inputs=free_text_inputs,
            generated_at=generated_at,
            plan_summary=plan.overall_summary if plan else None,
            sections=top_level,
        )
        return GenerationResult(
            instance=instance, citations=all_citations, plan=plan, audit_events=audit
        )

    def _generate_section(
        self,
        *,
        section: TemplateSection,
        resolver: BindingResolver,
        free_text_inputs: dict[str, str],
        report_instance_id: str,
        retrieved_at: datetime,
    ) -> tuple[GeneratedSection, list[Citation], list[GenerationAuditEvent]]:
        events: list[GenerationAuditEvent] = []

        # Sections that aren't LLM-generated still get a stub GeneratedSection so
        # the renderer can place them. No fill, no critique.
        if section.generation.mode in (GenerationMode.DETERMINISTIC, GenerationMode.MANUAL):
            return (
                GeneratedSection(
                    section_id=section.section_id,
                    title=section.title,
                    level=section.level,
                    critique_status="passed",
                ),
                [],
                events,
            )

        context = resolver.resolve(section)

        fill_attempts = 0
        last_result: FillResult | None = None
        last_issues: list[str] = []
        while fill_attempts <= self._max_retries:
            fill_result = self._filler.fill(
                section_context=context,
                free_text_inputs=free_text_inputs,
                report_instance_id=report_instance_id,
                retrieved_at=retrieved_at,
            )
            last_result = fill_result
            events.append(
                GenerationAuditEvent(
                    event_id=str(uuid.uuid4()),
                    section_id=section.section_id,
                    phase="fill",
                    model_version=fill_result.model_version,
                    timestamp=datetime.now(timezone.utc),
                    status="ok" if fill_attempts == 0 else "regenerated",
                )
            )

            verdict_pass, issues = self._critic.critique(
                section=section, generated=fill_result.section
            )
            last_issues = issues
            events.append(
                GenerationAuditEvent(
                    event_id=str(uuid.uuid4()),
                    section_id=section.section_id,
                    phase="critique",
                    model_version=fill_result.model_version,
                    timestamp=datetime.now(timezone.utc),
                    status="ok" if verdict_pass else "failed",
                    notes=issues,
                )
            )
            if verdict_pass:
                fill_result.section.critique_status = "passed"
                fill_result.section.critique_notes = []
                return fill_result.section, fill_result.citations, events
            fill_attempts += 1

        # All retries exhausted — flag and return the last attempt.
        assert last_result is not None
        last_result.section.critique_status = "failed_after_retries"
        last_result.section.critique_notes = last_issues
        return last_result.section, last_result.citations, events

    @staticmethod
    def _collect_template_sections(template: ReportTemplate) -> list[TemplateSection]:
        # Use the template's depth-first flatten — preserves order so parents are
        # filled before children, useful for the plan-references-cross-section pattern.
        return template.all_sections()

    @staticmethod
    def _assemble_tree(
        section: TemplateSection, by_id: dict[str, GeneratedSection]
    ) -> GeneratedSection:
        generated = by_id[section.section_id]
        # Defensive: a generated section is frozen-ish (model_validate); we want a
        # mutable copy with child references replaced.
        return GeneratedSection(
            section_id=generated.section_id,
            title=generated.title,
            level=generated.level,
            paragraphs=generated.paragraphs,
            tables=generated.tables,
            children=[
                ReportGenerator._assemble_tree(child, by_id) for child in section.children
            ],
            critique_status=generated.critique_status,
            critique_notes=generated.critique_notes,
        )
