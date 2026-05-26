"""Fill phase — generates one section of prose from a resolved context.

Output is structured: every claim carries citation_ids that index back
into the chunks supplied with the prompt. The orchestrator translates
those citation_ids into Citation records pointing at the original source.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from shared.llm import (
    LlmClient,
    LlmMessage,
    LlmRequest,
    LlmRole,
    ModelTier,
    StructuredOutputError,
)
from shared.schemas import (
    Citation,
    CitationLocator,
    DocxLocator,
    ParsedChunk,
    PdfLocator,
    SourceType,
    XlsxLocator,
)

from .prompts import FILL_SYSTEM_PROMPT, PROMPT_VERSION
from .retrieval import ResolvedSectionContext
from .types import GeneratedClaim, GeneratedParagraph, GeneratedSection


# --- LLM output schema -----------------------------------------------------


class _FillClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    citation_ids: list[str] = Field(default_factory=list)


class _FillParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    claims: list[_FillClaim] = Field(default_factory=list)


class _FillOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paragraphs: list[_FillParagraph]


@dataclass
class FillResult:
    section: GeneratedSection
    citations: list[Citation]
    raw_text: str
    model_version: str


def _build_prompt_template(section_context: ResolvedSectionContext, free_text_inputs: dict[str, str]) -> str:
    """Substitute {{bindings.X}} placeholders in the section's prompt_template."""
    template = section_context.section.generation.prompt_template or ""
    # Resolve {{bindings.<binding_id>}} placeholders to a short reference.
    def repl(match: re.Match[str]) -> str:
        binding_id = match.group(1)
        return f"<binding:{binding_id}>"

    return re.sub(r"\{\{\s*bindings\.([\w]+)\s*\}\}", repl, template)


def _render_chunk_for_prompt(chunk: ParsedChunk, citation_id: str) -> str:
    """One chunk rendered as a single block the LLM can quote and cite."""
    if isinstance(chunk.locator, PdfLocator):
        loc = f"page {chunk.locator.page}"
    elif isinstance(chunk.locator, DocxLocator):
        trail = " > ".join(chunk.locator.heading_trail) or "(top)"
        loc = f"section: {trail}"
    elif isinstance(chunk.locator, XlsxLocator):
        loc = f"sheet: {chunk.locator.sheet} {chunk.locator.cell_range}"
    else:  # pragma: no cover - defensive
        loc = "(unknown)"
    return (
        f"[citation_id={citation_id}] "
        f"(source_doc_id={chunk.source_doc_id}; {loc})\n"
        f"{chunk.text}\n"
    )


def _source_type_for_chunk(chunk: ParsedChunk) -> SourceType:
    if isinstance(chunk.locator, PdfLocator):
        return SourceType.PDF
    if isinstance(chunk.locator, DocxLocator):
        return SourceType.DOCX
    if isinstance(chunk.locator, XlsxLocator):
        return SourceType.XLSX
    raise ValueError(f"unrecognized locator type: {type(chunk.locator)!r}")


def _citation_locator_from_chunk(chunk: ParsedChunk) -> CitationLocator:
    """Project the typed chunk locator into the looser citation locator shape."""
    if isinstance(chunk.locator, PdfLocator):
        return CitationLocator(
            page=chunk.locator.page, paragraph_index=chunk.locator.paragraph_index
        )
    if isinstance(chunk.locator, DocxLocator):
        return CitationLocator(
            heading_trail=chunk.locator.heading_trail,
            paragraph_index=chunk.locator.paragraph_index,
        )
    if isinstance(chunk.locator, XlsxLocator):
        return CitationLocator(sheet=chunk.locator.sheet, cell_range=chunk.locator.cell_range)
    raise ValueError(f"unrecognized locator: {type(chunk.locator)!r}")


class SectionFiller:
    def __init__(self, client: LlmClient) -> None:
        self._client = client

    def fill(
        self,
        *,
        section_context: ResolvedSectionContext,
        free_text_inputs: dict[str, str],
        report_instance_id: str,
        retrieved_at: object,  # datetime — kept typed loosely to avoid an import cycle in callers
    ) -> FillResult:
        from datetime import datetime as _dt  # local to avoid unused-at-module-level

        section = section_context.section

        # Assign one citation_id per chunk (chunk_id -> citation_id).
        chunk_to_citation: dict[str, str] = {
            c.chunk_id: str(uuid.uuid4()) for c in section_context.all_chunks
        }

        # Build the user message: prompt + bindings + chunk pool.
        prompt_body = _build_prompt_template(section_context, free_text_inputs)

        binding_summaries: list[str] = []
        for b in section_context.bindings:
            if b.text_value is not None:
                binding_summaries.append(f"<binding:{b.binding_id}> = {b.text_value!r}")
            elif b.deferred_note:
                binding_summaries.append(
                    f"<binding:{b.binding_id}> — {b.deferred_note}"
                )
            else:
                binding_summaries.append(
                    f"<binding:{b.binding_id}> — {len(b.chunks)} source chunks in pool"
                )

        chunk_blocks: list[str] = []
        for chunk in section_context.all_chunks:
            citation_id = chunk_to_citation[chunk.chunk_id]
            chunk_blocks.append(_render_chunk_for_prompt(chunk, citation_id))

        length_hint = ""
        if section.generation.expected_length_words_min or section.generation.expected_length_words_max:
            lo = section.generation.expected_length_words_min or 0
            hi = section.generation.expected_length_words_max or 0
            length_hint = f"Target length: {lo}-{hi} words.\n"

        style_hint = ""
        if section.generation.style_directives:
            style_hint = "Style: " + ", ".join(section.generation.style_directives) + ".\n"

        user_message = (
            f"# Section: {section.section_id} {section.title}\n\n"
            f"## Instructions\n{prompt_body}\n\n"
            f"{length_hint}{style_hint}\n"
            f"## Bindings\n" + "\n".join(binding_summaries) + "\n\n"
            f"## Source chunk pool (each tagged with a citation_id)\n\n"
            + ("\n".join(chunk_blocks) if chunk_blocks else "(no chunks retrieved for this section)\n")
            + "\n\nProduce the section by calling emit_structured_output."
        )

        request = LlmRequest(
            tier=ModelTier.FILL,
            system=FILL_SYSTEM_PROMPT + f"\n\nprompt_version: {PROMPT_VERSION}",
            messages=[LlmMessage(role=LlmRole.USER, content=user_message)],
            max_tokens=4096,
            temperature=0.0,
            response_schema_name="FillOutput",
            response_schema_json=_FillOutput.model_json_schema(),
        )

        response = self._client.generate(request)
        if response.parsed_json is None:
            raise StructuredOutputError(
                f"FillOutput missing from response (text head={response.text[:200]!r})"
            )
        fill = _FillOutput.model_validate(response.parsed_json)

        # Validate citation IDs the LLM used: they must all come from the pool.
        valid_citation_ids = set(chunk_to_citation.values())
        used_citation_ids: set[str] = set()
        for paragraph in fill.paragraphs:
            for claim in paragraph.claims:
                for cid in claim.citation_ids:
                    if cid not in valid_citation_ids:
                        raise StructuredOutputError(
                            f"section {section.section_id!r}: model referenced "
                            f"unknown citation_id={cid!r} (fabricated)"
                        )
                    used_citation_ids.add(cid)

        # Build Citation records only for the citation_ids the model actually used.
        citations: list[Citation] = []
        retrieval_ts = retrieved_at if isinstance(retrieved_at, _dt) else _dt.utcnow()
        for chunk in section_context.all_chunks:
            citation_id = chunk_to_citation[chunk.chunk_id]
            if citation_id not in used_citation_ids:
                continue
            citations.append(
                Citation(
                    citation_id=citation_id,
                    report_instance_id=report_instance_id,
                    source_type=_source_type_for_chunk(chunk),
                    source_uri=f"local://{chunk.source_doc_id}",
                    source_doc_id=chunk.source_doc_id,
                    source_doc_version=chunk.source_doc_version,
                    locator=_citation_locator_from_chunk(chunk),
                    snippet=chunk.text[:500],
                    retrieved_at=retrieval_ts,
                    retrieval_chunk_id=chunk.chunk_id,
                )
            )

        generated_section = GeneratedSection(
            section_id=section.section_id,
            title=section.title,
            level=section.level,
            paragraphs=[
                GeneratedParagraph(
                    text=p.text,
                    claims=[
                        GeneratedClaim(text=c.text, citation_ids=c.citation_ids)
                        for c in p.claims
                    ],
                )
                for p in fill.paragraphs
            ],
        )
        return FillResult(
            section=generated_section,
            citations=citations,
            raw_text=response.text,
            model_version=response.model_version,
        )
