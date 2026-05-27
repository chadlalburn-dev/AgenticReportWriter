"""FromScratchAdapter — LLM-proposed template from a scoping spec.

Fourth (final) of the template-authoring entry points. The author
describes what they want in a `ScopingSpec` (report type, audience,
expected length, key themes, available source systems), and the
adapter asks the LLM to propose a section structure. Structured output
(via the LlmClient's tool-use pattern) keeps the proposal in a
validated shape.

The LLM does NOT generate report content — it generates the *outline*.
Per-section content is left for the generation orchestrator at run
time. The author reviews the proposed structure, accepts/edits, then
the template moves to status=APPROVED through the normal change-control
flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from shared.llm import (
    LlmClient,
    LlmMessage,
    LlmRequest,
    LlmRole,
    ModelTier,
    StructuredOutputError,
)
from shared.schemas import GenerationMode, ReportTemplate, TemplateSection
from shared.schemas.template import (
    CitationPolicy,
    FreeTextInputBinding,
    GenerationPolicy,
    GlobalStyle,
    OutputShape,
    TemplateMetadata,
    TemplateStatus,
    ValidationRule,
)


# --- Inputs ---------------------------------------------------------------


@dataclass(frozen=True)
class ScopingSpec:
    """What the author tells the adapter about the report they want.

    Kept narrow: 5-10 fields the LLM can reason about. Free-text fields
    (themes, intent) carry the most signal; the strict fields (audience,
    report_type) keep the proposal anchored.
    """

    report_type: str
    title: str
    audience: str = "internal medical writers"
    intent: str = ""
    key_themes: tuple[str, ...] = ()
    available_source_systems: tuple[str, ...] = ()
    expected_total_pages: tuple[int, int] | None = None
    additional_notes: str = ""


@dataclass(frozen=True)
class FromScratchAdapterOptions:
    template_id: str
    authored_by: str = "template-builder:from-scratch-adapter"
    default_min_words: int = 300
    default_max_words: int = 1500
    auto_require_citations: bool = True


# --- LLM output schema ----------------------------------------------------


class _ProposedSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: str = Field(description="dotted-decimal path, e.g. '3.1'")
    title: str
    level: int = Field(ge=1, le=6)
    intent: str = Field(description="What this section should accomplish")
    suggested_tag: str | None = Field(
        default=None,
        description=(
            "Optional file_set tag the orchestrator should filter on for "
            "this section, e.g. 'safety' or 'pharmacology'"
        ),
    )


class _ProposedOutline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[_ProposedSection]


# --- System prompt --------------------------------------------------------


_SYSTEM_PROMPT = """\
You are a senior medical writer designing a report template structure.
Given a scoping spec (report type, audience, themes, available sources),
propose a section outline that:

- Mirrors standards for the named report type (e.g., ICH E3 for CSRs,
  ICH E6 for IBs, CONSORT for RCT reports) when applicable
- Uses dotted-decimal section_ids (1, 1.1, 1.1.1) reflecting the
  hierarchy
- Keeps each section's `intent` to ONE clear sentence — this seeds the
  prompt_template the orchestrator will use
- Suggests a file_set tag only when the section is naturally tied to a
  recognizable source type (e.g., section "Toxicology" → suggested_tag
  "toxicology")
- Returns 8-20 top-level sections typically; fewer for short narratives,
  more for full CSRs
- Returns the result by calling emit_structured_output exactly once

You do NOT write the content. You only propose the structure.
"""


# --- Adapter --------------------------------------------------------------


class FromScratchAdapter:
    def __init__(self, client: LlmClient, options: FromScratchAdapterOptions) -> None:
        self._client = client
        self._options = options

    def propose(self, scoping: ScopingSpec) -> ReportTemplate:
        user_message = self._compose_user_message(scoping)
        request = LlmRequest(
            tier=ModelTier.PLAN_CRITIQUE,
            system=_SYSTEM_PROMPT,
            messages=[LlmMessage(role=LlmRole.USER, content=user_message)],
            max_tokens=4096,
            temperature=0.0,
            response_schema_name="ProposedOutline",
            response_schema_json=_ProposedOutline.model_json_schema(),
        )
        response = self._client.generate(request)
        if response.parsed_json is None:
            raise StructuredOutputError(
                f"FromScratchAdapter: model returned no structured output "
                f"(text head={response.text[:200]!r})"
            )
        proposed = _ProposedOutline.model_validate(response.parsed_json)
        if not proposed.sections:
            raise StructuredOutputError(
                "FromScratchAdapter: model returned zero sections"
            )

        sections = self._build_tree(proposed.sections)
        return ReportTemplate(
            template_id=self._options.template_id,
            version="0.1.0",
            status=TemplateStatus.DRAFT,
            report_type=scoping.report_type,
            title=scoping.title,
            metadata=TemplateMetadata(
                authored_by=self._options.authored_by,
                authored_at=datetime.now(timezone.utc),
                source_origin="from_scratch",
            ),
            global_style=GlobalStyle(),
            sections=sections,
        )

    @staticmethod
    def _compose_user_message(scoping: ScopingSpec) -> str:
        themes = ", ".join(scoping.key_themes) if scoping.key_themes else "(none stated)"
        sources = (
            ", ".join(scoping.available_source_systems)
            if scoping.available_source_systems
            else "(none stated)"
        )
        pages = (
            f"{scoping.expected_total_pages[0]}-{scoping.expected_total_pages[1]} pages"
            if scoping.expected_total_pages
            else "(unspecified)"
        )
        return (
            f"# Scoping spec\n\n"
            f"- report_type: {scoping.report_type}\n"
            f"- title: {scoping.title}\n"
            f"- audience: {scoping.audience}\n"
            f"- intent: {scoping.intent or '(unspecified)'}\n"
            f"- key_themes: {themes}\n"
            f"- available_source_systems: {sources}\n"
            f"- expected_total_pages: {pages}\n"
            f"- additional_notes: {scoping.additional_notes or '(none)'}\n\n"
            "Propose the section outline. Emit via emit_structured_output."
        )

    def _build_tree(
        self, proposed: list[_ProposedSection]
    ) -> list[TemplateSection]:
        """Convert the flat proposed list into a nested TemplateSection tree
        using the level field. Same algorithm as SampleReportsAdapter."""
        top_level: list[TemplateSection] = []
        parent_at_level: dict[int, TemplateSection] = {}
        for proposed_section in proposed:
            section = self._proposed_to_template_section(proposed_section)
            # Drop deeper parents from the map.
            for deeper in [k for k in parent_at_level if k >= section.level]:
                del parent_at_level[deeper]
            shallower = [lvl for lvl in parent_at_level if lvl < section.level]
            if shallower:
                parent_at_level[max(shallower)].children.append(section)
            else:
                top_level.append(section)
            parent_at_level[section.level] = section
        return top_level

    def _proposed_to_template_section(
        self, proposed: _ProposedSection
    ) -> TemplateSection:
        bindings = [
            FreeTextInputBinding(
                binding_id="product_name", prompt="Product name", required=True
            )
        ]
        if proposed.suggested_tag:
            from shared.schemas.template import FileSetBinding

            bindings.append(
                FileSetBinding(
                    binding_id=f"{proposed.suggested_tag.lower()}_docs",
                    filter_tags=[proposed.suggested_tag],
                    required=False,
                )
            )
        return TemplateSection(
            section_id=proposed.section_id,
            title=proposed.title,
            level=proposed.level,
            generation=GenerationPolicy(
                mode=GenerationMode.LLM,
                prompt_template=(
                    f"{proposed.intent} Use {{{{bindings.product_name}}}} "
                    "consistently and cite every factual claim from the "
                    "supplied source chunks."
                ),
                expected_length_words_min=self._options.default_min_words,
                expected_length_words_max=self._options.default_max_words,
                style_directives=["formal", "factual_only"],
                output_shape=OutputShape.PROSE,
            ),
            data_bindings=bindings,
            citation_policy=CitationPolicy(
                required=self._options.auto_require_citations,
                granularity="claim",
                min_citations_per_paragraph=1,
            ),
            validation_rules=[
                ValidationRule(rule="must_cite_every_number", severity="error"),
                ValidationRule(rule="no_unbound_claims", severity="error"),
                ValidationRule(rule="length_within_bounds", severity="warn"),
            ],
        )
