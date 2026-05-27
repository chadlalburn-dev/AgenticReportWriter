"""Generation orchestrator — drives the plan → fill → critique loop end-to-end.

Inputs:
  - ReportTemplate (validated, approved in production)
  - Pool of CanonicalDocuments + their ParsedChunks (from ingestion/parsing)
  - free_text_inputs (user-provided per binding_id)
  - compliance_mode + retry budget
  - (optional) AuditSink — when supplied, the orchestrator wraps every LLM
    client with AuditingLlmClient so every call is recorded, and it emits
    canonical AuditEvents for each generation phase. If not supplied, an
    InMemoryAuditStore is used so the loop still runs (events are
    ephemeral).

Outputs:
  - ReportInstance (sections, paragraphs, claims)
  - list[Citation]
  - list[AuditEvent] (events emitted during this generation run)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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

from services.api_integration import ApiCallGate
from services.audit import (
    AuditAction,
    AuditEvent,
    AuditQuery,
    AuditSink,
    AuditingLlmClient,
    ComplianceMode,
    InMemoryAuditStore,
)
from services.data_integration import SqlSafetyGate

from .critic import SectionCritic
from .filler import FillResult, SectionFiller
from .planner import ReportPlan, ReportPlanner
from .retrieval import BindingResolver
from .types import GeneratedSection, ReportInstance


@dataclass
class GenerationResult:
    instance: ReportInstance
    citations: list[Citation]
    plan: ReportPlan | None
    audit_events: list[AuditEvent]


class ReportGenerator:
    def __init__(
        self,
        *,
        fill_client: LlmClient,
        plan_client: LlmClient | None = None,
        critique_client: LlmClient | None = None,
        audit_sink: AuditSink | None = None,
        safety_gate: SqlSafetyGate | None = None,
        api_gate: ApiCallGate | None = None,
        max_retries_per_section: int = 2,
    ) -> None:
        # Inner clients are stored raw; we wrap them per-run inside generate()
        # so each run carries the right project_id / instance_id in audit metadata.
        self._fill_client_inner = fill_client
        self._plan_client_inner = plan_client or fill_client
        self._critique_client_inner = critique_client or fill_client
        self._audit_sink = audit_sink or AuditSink(InMemoryAuditStore())
        # Optional: when provided, named_query and sql_query bindings execute
        # through the gate. Otherwise they emit deferred_notes.
        self._safety_gate = safety_gate
        # Optional: when provided, api_call bindings execute through this gate.
        self._api_gate = api_gate
        self._max_retries = max_retries_per_section

    def generate(
        self,
        *,
        template: ReportTemplate,
        documents: list[CanonicalDocument],
        chunks_by_doc: dict[str, list[ParsedChunk]],
        free_text_inputs: dict[str, str],
        compliance_mode: ComplianceMode = "rd",
        project_id: str = "default-project",
        tenant_id: str = "default-tenant",
        actor_id: str = "system:orchestrator",
        run_plan_phase: bool = True,
    ) -> GenerationResult:
        instance_id = str(uuid.uuid4())
        generated_at = datetime.now(timezone.utc)

        # Wrap clients with auditing decorators bound to this run's identifiers.
        wrap = lambda c: AuditingLlmClient(  # noqa: E731
            c,
            self._audit_sink,
            tenant_id=tenant_id,
            project_id=project_id,
            actor_id=actor_id,
            mode=compliance_mode,
            report_instance_id=instance_id,
        )
        planner = ReportPlanner(wrap(self._plan_client_inner))
        filler = SectionFiller(wrap(self._fill_client_inner))
        critic = SectionCritic(wrap(self._critique_client_inner))

        # Mark the start of this generation run on the chain.
        self._emit(
            action=AuditAction.GENERATION_REQUESTED,
            tenant_id=tenant_id,
            project_id=project_id,
            actor_id=actor_id,
            mode=compliance_mode,
            target_type="report_instance",
            target_id=instance_id,
            target_version=f"{template.template_id}@{template.version}",
            extra={
                "template_id": template.template_id,
                "template_version": template.version,
                "compliance_mode": compliance_mode,
                "n_documents": len(documents),
                "n_chunks": sum(len(c) for c in chunks_by_doc.values()),
            },
        )

        plan: ReportPlan | None = None
        if run_plan_phase:
            plan = planner.plan(
                template=template,
                available_docs=documents,
                free_text_inputs=free_text_inputs,
            )
            self._emit(
                action=AuditAction.GENERATION_PLAN_COMPLETED,
                tenant_id=tenant_id,
                project_id=project_id,
                actor_id=actor_id,
                mode=compliance_mode,
                target_type="report_instance",
                target_id=instance_id,
                target_version=plan.model_version,
                extra={"overall_summary_len": len(plan.overall_summary)},
            )

        resolver = BindingResolver(
            chunks_by_doc=chunks_by_doc,
            docs_by_id={d.doc_id: d for d in documents},
            free_text_inputs=free_text_inputs,
            safety_gate=self._safety_gate,
            api_gate=self._api_gate,
        )

        all_sections = self._collect_template_sections(template)
        generated_by_id: dict[str, GeneratedSection] = {}
        all_citations: list[Citation] = []

        for section in all_sections:
            generated, citations = self._generate_section(
                section=section,
                resolver=resolver,
                filler=filler,
                critic=critic,
                free_text_inputs=free_text_inputs,
                report_instance_id=instance_id,
                retrieved_at=generated_at,
                tenant_id=tenant_id,
                project_id=project_id,
                actor_id=actor_id,
                mode=compliance_mode,
            )
            generated_by_id[section.section_id] = generated
            all_citations.extend(citations)

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
        self._emit(
            action=AuditAction.GENERATION_COMPLETED,
            tenant_id=tenant_id,
            project_id=project_id,
            actor_id=actor_id,
            mode=compliance_mode,
            target_type="report_instance",
            target_id=instance_id,
            extra={
                "n_sections": len(generated_by_id),
                "n_citations": len(all_citations),
            },
        )

        # Return the events emitted for THIS instance (filter by report_instance
        # extra in the chain — for compactness, also includes phase-level events
        # whose target_id is the instance_id).
        events_this_run = self._events_for_instance(
            project_id=project_id, instance_id=instance_id
        )
        return GenerationResult(
            instance=instance,
            citations=all_citations,
            plan=plan,
            audit_events=events_this_run,
        )

    def _generate_section(
        self,
        *,
        section: TemplateSection,
        resolver: BindingResolver,
        filler: SectionFiller,
        critic: SectionCritic,
        free_text_inputs: dict[str, str],
        report_instance_id: str,
        retrieved_at: datetime,
        tenant_id: str,
        project_id: str,
        actor_id: str,
        mode: ComplianceMode,
    ) -> tuple[GeneratedSection, list[Citation]]:
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
            )

        context = resolver.resolve(section)

        fill_attempts = 0
        last_result: FillResult | None = None
        last_issues: list[str] = []
        while fill_attempts <= self._max_retries:
            fill_result = filler.fill(
                section_context=context,
                free_text_inputs=free_text_inputs,
                report_instance_id=report_instance_id,
                retrieved_at=retrieved_at,
            )
            last_result = fill_result
            self._emit(
                action=AuditAction.GENERATION_SECTION_FILLED,
                tenant_id=tenant_id,
                project_id=project_id,
                actor_id=actor_id,
                mode=mode,
                target_type="section",
                target_id=section.section_id,
                target_version=fill_result.model_version,
                extra={
                    "report_instance_id": report_instance_id,
                    "attempt": fill_attempts + 1,
                    "n_paragraphs": len(fill_result.section.paragraphs),
                    "n_citations": len(fill_result.citations),
                },
            )
            for citation in fill_result.citations:
                self._emit(
                    action=AuditAction.CITATION_CREATED,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    actor_id=actor_id,
                    mode=mode,
                    target_type="citation",
                    target_id=citation.citation_id,
                    extra={
                        "report_instance_id": report_instance_id,
                        "section_id": section.section_id,
                        "source_doc_id": citation.source_doc_id,
                        "source_type": citation.source_type.value,
                    },
                )

            verdict_pass, issues = critic.critique(
                section=section, generated=fill_result.section
            )
            last_issues = issues
            self._emit(
                action=AuditAction.GENERATION_SECTION_CRITIQUED,
                tenant_id=tenant_id,
                project_id=project_id,
                actor_id=actor_id,
                mode=mode,
                target_type="section",
                target_id=section.section_id,
                target_version=fill_result.model_version,
                notes=issues,
                extra={
                    "report_instance_id": report_instance_id,
                    "verdict": "pass" if verdict_pass else "fail",
                    "attempt": fill_attempts + 1,
                },
            )
            if verdict_pass:
                fill_result.section.critique_status = "passed"
                fill_result.section.critique_notes = []
                return fill_result.section, fill_result.citations
            fill_attempts += 1

        # All retries exhausted — flag and return the last attempt.
        assert last_result is not None
        last_result.section.critique_status = "failed_after_retries"
        last_result.section.critique_notes = last_issues
        return last_result.section, last_result.citations

    def _emit(
        self,
        *,
        action: AuditAction,
        tenant_id: str,
        project_id: str,
        actor_id: str,
        mode: ComplianceMode,
        target_type: str,
        target_id: str,
        target_version: str | None = None,
        notes: list[str] | None = None,
        extra: dict[str, object] | None = None,
    ) -> AuditEvent:
        return self._audit_sink.emit(
            AuditEvent(
                action=action,
                tenant_id=tenant_id,
                project_id=project_id,
                actor_id=actor_id,
                mode=mode,
                target_type=target_type,
                target_id=target_id,
                target_version=target_version,
                timestamp_utc=datetime.now(timezone.utc),
                notes=notes or [],
                extra={k: (v if isinstance(v, (str, int, float, bool)) else str(v))
                       for k, v in (extra or {}).items()},
            )
        )

    def _events_for_instance(
        self, *, project_id: str, instance_id: str
    ) -> list[AuditEvent]:
        """Return events from this generation run.

        Picks up events whose target_id is the instance_id (phase events) and
        events whose extra.report_instance_id matches (per-section events).
        Order: insertion order from the store.
        """
        sink = self._audit_sink
        # The sink doesn't expose its store directly; we re-read through query.
        store = sink._store  # type: ignore[attr-defined]
        all_events = list(store.query(AuditQuery(project_id=project_id)))
        results: list[AuditEvent] = []
        for event in all_events:
            if event.target_id == instance_id:
                results.append(event)
            elif event.extra.get("report_instance_id") == instance_id:
                results.append(event)
        return results

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
