"""DocxAdapter — Word .docx → draft ReportTemplate.

Walks the document body in order, tracking the active heading hierarchy
via Word's built-in `Heading 1`...`Heading 6` paragraph styles. Each
heading opens a new TemplateSection at that level; non-heading paragraphs
between headings are accumulated as `content_hints` for the LLM
enricher (optional, see builder.py).

What the adapter outputs:
- A ReportTemplate with status=DRAFT
- section_id auto-numbered (1, 1.1, 1.2, 2, 2.1, ...) — explicit
  numbering in the heading text is preserved in the title but not used
  for section_id
- generation.prompt_template = a short hint derived from heading + any
  content_hint paragraphs (or a placeholder if none)
- data_bindings = [free_text_input("product_name")] for every LLM
  section so the renderer always has a compound name handle, plus a
  heuristically-derived file_set binding tagged from the section title

The output is a STARTING POINT — the human author is expected to review
and refine each section's prompt + bindings before approval.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from docx import Document
from docx.text.paragraph import Paragraph

from shared.schemas import GenerationMode, ReportTemplate, TemplateSection
from shared.schemas.template import (
    CitationPolicy,
    DataBinding,
    FileSetBinding,
    FreeTextInputBinding,
    GenerationPolicy,
    GlobalStyle,
    OutputShape,
    TemplateMetadata,
    TemplateStatus,
    ValidationRule,
)


_HEADING_STYLE_RE = re.compile(r"^Heading (\d+)$")
# Strip leading numbers like "3.", "3.1.", "3.1.2 " from heading text so the
# auto-generated section_id doesn't double up with the heading text's number.
_LEADING_NUMBER_RE = re.compile(r"^([\dA-Z]+(?:\.\d+)*\.?)\s+")

# Cheap heuristic: phrases in a section title that suggest a file_set tag.
# Approximate; the human reviewer is expected to fix incorrect guesses.
_TITLE_TO_TAG_HINTS = {
    "pharmacology": "pharmacology",
    "pharmacokinetic": "PK",
    "metabolism": "PK",
    "toxicology": "toxicology",
    "safety": "safety",
    "efficacy": "efficacy",
    "nonclinical": "nonclinical",
    "clinical": "clinical",
    "formulation": "CMC",
    "stability": "CMC",
    "chemistry": "CMC",
    "manufacturing": "CMC",
    "pharmacovigilance": "pharmacovigilance",
    "marketing": "pharmacovigilance",
    "post-marketing": "pharmacovigilance",
    "risk management": "risk_management",
}


@dataclass(frozen=True)
class DocxAdapterOptions:
    template_id: str
    report_type: str = "imported_from_docx"
    authored_by: str = "template-builder:docx-adapter"
    title: str | None = None  # if None, derived from the Title style or filename
    default_min_words: int = 200
    default_max_words: int = 1200
    auto_require_citations: bool = True


@dataclass
class _SectionDraft:
    """Mutable intermediate used during the walk."""

    section_id: str
    title: str
    level: int
    content_hints: list[str]
    children: list["_SectionDraft"]


class DocxAdapter:
    def __init__(self, options: DocxAdapterOptions) -> None:
        self._options = options

    def from_file(self, path: str | Path) -> ReportTemplate:
        with open(path, "rb") as handle:
            return self.from_bytes(handle.read(), filename=Path(path).name)

    def from_bytes(self, raw: bytes, *, filename: str | None = None) -> ReportTemplate:
        return self._build(io.BytesIO(raw), default_title_hint=filename)

    def _build(
        self, source: IO[bytes], *, default_title_hint: str | None
    ) -> ReportTemplate:
        document = Document(source)

        drafts: list[_SectionDraft] = []
        stack: list[_SectionDraft] = []  # path from root to current deepest open section
        counters: dict[int, int] = {}  # level -> next number at this level
        title_from_doc: str | None = None

        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style_name = paragraph.style.name if paragraph.style else ""

            if style_name == "Title" and title_from_doc is None:
                title_from_doc = text
                continue

            level = self._heading_level(paragraph)
            if level is not None:
                # Closing rule: when entering a heading at level L, pop the stack
                # until we're inside a section of level < L.
                while stack and stack[-1].level >= level:
                    stack.pop()
                # Reset all counters deeper than this level.
                for deeper in list(counters):
                    if deeper > level:
                        del counters[deeper]
                counters[level] = counters.get(level, 0) + 1
                section_id = self._compose_section_id(counters, level)
                title = _LEADING_NUMBER_RE.sub("", text)  # strip any explicit numbering
                draft = _SectionDraft(
                    section_id=section_id,
                    title=title,
                    level=level,
                    content_hints=[],
                    children=[],
                )
                if stack:
                    stack[-1].children.append(draft)
                else:
                    drafts.append(draft)
                stack.append(draft)
            else:
                if stack:
                    stack[-1].content_hints.append(text)
                # else: orphan content before any heading — ignored

        sections = [self._draft_to_template_section(d) for d in drafts]

        title = (
            self._options.title
            or title_from_doc
            or (default_title_hint and Path(default_title_hint).stem)
            or self._options.template_id
        )

        return ReportTemplate(
            template_id=self._options.template_id,
            version="0.1.0",
            status=TemplateStatus.DRAFT,
            report_type=self._options.report_type,
            title=title,
            metadata=TemplateMetadata(
                authored_by=self._options.authored_by,
                authored_at=datetime.now(timezone.utc),
                source_origin="from_docx",
            ),
            global_style=GlobalStyle(),
            sections=sections,
        )

    @staticmethod
    def _heading_level(paragraph: Paragraph) -> int | None:
        style_name = paragraph.style.name if paragraph.style else ""
        if style_name == "Title":
            return 1
        m = _HEADING_STYLE_RE.match(style_name)
        if m:
            level = int(m.group(1))
            if 1 <= level <= 6:
                return level
        return None

    @staticmethod
    def _compose_section_id(counters: dict[int, int], level: int) -> str:
        parts: list[str] = []
        for lvl in sorted(counters):
            if lvl <= level:
                parts.append(str(counters[lvl]))
        return ".".join(parts)

    def _draft_to_template_section(self, draft: _SectionDraft) -> TemplateSection:
        return TemplateSection(
            section_id=draft.section_id,
            title=draft.title,
            level=draft.level,
            children=[self._draft_to_template_section(c) for c in draft.children],
            generation=self._draft_generation_policy(draft),
            data_bindings=self._infer_bindings(draft),
            citation_policy=self._draft_citation_policy(),
            validation_rules=self._draft_validation_rules(),
        )

    def _draft_generation_policy(self, draft: _SectionDraft) -> GenerationPolicy:
        prompt = self._compose_prompt(draft)
        return GenerationPolicy(
            mode=GenerationMode.LLM,
            prompt_template=prompt,
            expected_length_words_min=self._options.default_min_words,
            expected_length_words_max=self._options.default_max_words,
            style_directives=["formal", "factual_only"],
            output_shape=OutputShape.PROSE,
        )

    def _compose_prompt(self, draft: _SectionDraft) -> str:
        title = draft.title
        if draft.content_hints:
            # Take up to the first ~3 hint paragraphs to keep the prompt compact.
            hint = " ".join(draft.content_hints[:3])
            if len(hint) > 500:
                hint = hint[:497] + "..."
            return (
                f"Generate the {title!r} section of the report. "
                f"Use {{{{bindings.product_name}}}} consistently. "
                f"Content hint from source document: {hint}"
            )
        return (
            f"Generate the {title!r} section of the report. "
            f"Use {{{{bindings.product_name}}}} consistently. "
            "Every factual claim must carry a citation drawn from the supplied source chunks."
        )

    def _infer_bindings(self, draft: _SectionDraft) -> list[DataBinding]:
        bindings: list[DataBinding] = [
            FreeTextInputBinding(
                binding_id="product_name", prompt="Product name", required=True
            )
        ]
        tag = self._tag_for_title(draft.title)
        if tag:
            bindings.append(
                FileSetBinding(
                    binding_id=f"{tag.lower()}_docs",
                    filter_tags=[tag],
                    required=False,
                )
            )
        return bindings

    @staticmethod
    def _tag_for_title(title: str) -> str | None:
        lower = title.lower()
        for keyword, tag in _TITLE_TO_TAG_HINTS.items():
            if keyword in lower:
                return tag
        return None

    def _draft_citation_policy(self) -> CitationPolicy:
        return CitationPolicy(
            required=self._options.auto_require_citations,
            granularity="claim",
            min_citations_per_paragraph=1,
        )

    @staticmethod
    def _draft_validation_rules() -> list[ValidationRule]:
        return [
            ValidationRule(rule="must_cite_every_number", severity="error"),
            ValidationRule(rule="no_unbound_claims", severity="error"),
            ValidationRule(rule="length_within_bounds", severity="warn"),
        ]
