"""SampleReportsAdapter — derive a template from existing completed reports.

Third of four template-authoring entry points. The premise: you have
several historical IBs/CSRs/etc. and want a template that captures
their common structure. The adapter:

1. Walks each report's heading hierarchy using the existing DocxAdapter
2. For every (level, normalized_title) seen across the samples, counts
   how many reports contained it
3. Keeps sections present in at least `min_occurrence_ratio` of the
   samples (default 0.5 — appears in at least half)
4. For each kept section, gathers content_hints from the samples (the
   non-heading paragraphs that lived under that heading)
5. Synthesizes a draft prompt_template combining the section title,
   the LLM mode default, and the sample content hints (truncated)

Output: a DRAFT ReportTemplate the author reviews and refines. The
adapter intentionally does NOT call the LLM in v1 — the content_hints
gathered from the samples provide enough signal for a useful starting
prompt. An LLM enrichment pass (using the orchestrator's pattern) is a
follow-up.

Normalization rules:
- Titles are lowercased and stripped of leading numbering ("1.", "2.1 ",
  etc.) before comparing. So "1. Introduction" and "Introduction"
  group together.
- Whitespace collapsed.
"""

from __future__ import annotations

import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.text.paragraph import Paragraph

from shared.schemas import GenerationMode, ReportTemplate, TemplateSection
from shared.schemas.template import (
    CitationPolicy,
    DataBinding,
    FreeTextInputBinding,
    GenerationPolicy,
    GlobalStyle,
    OutputShape,
    TemplateMetadata,
    TemplateStatus,
    ValidationRule,
)


_HEADING_STYLE_RE = re.compile(r"^Heading (\d+)$")
_LEADING_NUMBER_RE = re.compile(r"^([\dA-Z]+(?:\.\d+)*\.?)\s+")


@dataclass(frozen=True)
class SampleReportsAdapterOptions:
    template_id: str
    title: str = "Derived template"
    report_type: str = "imported_from_samples"
    authored_by: str = "template-builder:sample-reports-adapter"
    # A section is included in the derived template if it appears in at
    # least this fraction of the source samples. 0.5 = appears in half
    # or more. Set to 1.0 to require every sample.
    min_occurrence_ratio: float = 0.5
    # Truncate sample content hints attached to a section's prompt to
    # this many characters total (across all samples).
    max_hint_chars_per_section: int = 500
    default_min_words: int = 300
    default_max_words: int = 1500
    auto_require_citations: bool = True


@dataclass
class _SectionObservation:
    """A single (level, normalized_title) sighting in one sample."""

    level: int
    normalized_title: str
    display_title: str  # original casing/numbering preserved
    content_hint: str  # joined non-heading paragraphs under this heading
    source_filename: str


def _normalize_title(text: str) -> str:
    stripped = _LEADING_NUMBER_RE.sub("", text).strip().lower()
    return " ".join(stripped.split())


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


def _walk_one_sample(raw: bytes, filename: str) -> list[_SectionObservation]:
    document = Document(io.BytesIO(raw))
    observations: list[_SectionObservation] = []
    current: _SectionObservation | None = None
    content_buffer: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        level = _heading_level(paragraph)
        if level is not None:
            if current is not None:
                observations.append(
                    _SectionObservation(
                        level=current.level,
                        normalized_title=current.normalized_title,
                        display_title=current.display_title,
                        content_hint=" ".join(content_buffer).strip(),
                        source_filename=current.source_filename,
                    )
                )
                content_buffer = []
            display = _LEADING_NUMBER_RE.sub("", text)
            current = _SectionObservation(
                level=level,
                normalized_title=_normalize_title(text),
                display_title=display,
                content_hint="",
                source_filename=filename,
            )
        else:
            content_buffer.append(text)
    if current is not None:
        observations.append(
            _SectionObservation(
                level=current.level,
                normalized_title=current.normalized_title,
                display_title=current.display_title,
                content_hint=" ".join(content_buffer).strip(),
                source_filename=current.source_filename,
            )
        )
    return observations


class SampleReportsAdapter:
    def __init__(self, options: SampleReportsAdapterOptions) -> None:
        self._options = options

    def from_directory(self, root: str | Path) -> ReportTemplate:
        paths = sorted(Path(root).rglob("*.docx"))
        return self.from_files(paths)

    def from_files(self, paths: list[Path]) -> ReportTemplate:
        if not paths:
            raise ValueError("at least one sample report must be provided")
        sample_count = len(paths)
        per_sample_obs: list[list[_SectionObservation]] = []
        for path in paths:
            raw = path.read_bytes()
            per_sample_obs.append(_walk_one_sample(raw, path.name))

        # Build a per-section accumulator keyed by (level, normalized_title).
        # We use the first display_title we saw for the section heading
        # text in the derived template.
        occurrence_counter: Counter[tuple[int, str]] = Counter()
        display_titles: dict[tuple[int, str], str] = {}
        hints_by_key: dict[tuple[int, str], list[str]] = defaultdict(list)
        for sample_obs in per_sample_obs:
            seen_in_sample: set[tuple[int, str]] = set()
            for obs in sample_obs:
                key = (obs.level, obs.normalized_title)
                if key in seen_in_sample:
                    # Don't double-count if a sample has the same heading twice
                    continue
                seen_in_sample.add(key)
                occurrence_counter[key] += 1
                display_titles.setdefault(key, obs.display_title)
                if obs.content_hint:
                    hints_by_key[key].append(
                        f"[{obs.source_filename}] {obs.content_hint}"
                    )

        # Filter by the inclusion threshold.
        threshold = max(1, int(round(self._options.min_occurrence_ratio * sample_count)))
        kept_keys = [
            key for key, count in occurrence_counter.items() if count >= threshold
        ]
        # Stable ordering by FIRST appearance across all samples. Note: do
        # NOT sort by (level, order) — that would group every level-1
        # together followed by every level-2, breaking parent-child
        # attachment when a level-2 section's natural position is between
        # two level-1s. Document reading order is what we need.
        first_seen_order: dict[tuple[int, str], int] = {}
        order_counter = 0
        for sample_obs in per_sample_obs:
            for obs in sample_obs:
                key = (obs.level, obs.normalized_title)
                if key not in first_seen_order:
                    first_seen_order[key] = order_counter
                    order_counter += 1
        kept_keys.sort(key=lambda k: first_seen_order.get(k, 1 << 30))

        # Assemble the section tree by walking kept_keys in order. Maintain
        # an explicit stack so children attach to the most recent parent at
        # the appropriate level.
        section_counter: dict[int, int] = {}
        stack: list[tuple[int, list[TemplateSection]]] = []
        top_level: list[TemplateSection] = []
        for level, normalized_title in kept_keys:
            while stack and stack[-1][0] >= level:
                stack.pop()
            for deeper in list(section_counter):
                if deeper > level:
                    del section_counter[deeper]
            section_counter[level] = section_counter.get(level, 0) + 1
            section_id = ".".join(
                str(section_counter[lvl]) for lvl in sorted(section_counter) if lvl <= level
            )
            key = (level, normalized_title)
            display = display_titles[key]
            hints = hints_by_key.get(key, [])
            prompt = self._compose_prompt(display, hints)
            section = TemplateSection(
                section_id=section_id,
                title=display,
                level=level,
                generation=GenerationPolicy(
                    mode=GenerationMode.LLM,
                    prompt_template=prompt,
                    expected_length_words_min=self._options.default_min_words,
                    expected_length_words_max=self._options.default_max_words,
                    style_directives=["formal", "factual_only"],
                    output_shape=OutputShape.PROSE,
                ),
                data_bindings=self._default_bindings(),
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
            children_collector: list[TemplateSection] = []
            if stack:
                stack[-1][1].append(section)
            else:
                top_level.append(section)
            stack.append((level, children_collector))
            # Build child collector into the section after the fact — we mutate
            # via a fresh copy in _attach_children below.

        # Section.children must be set before model is frozen-like. Re-walk
        # to attach children collectors as the section's `children` list.
        # Since TemplateSection isn't frozen, we can mutate after creation.
        # (Verified: TemplateSection has extra="forbid" but allows mutation.)
        def _attach_children(sections: list[TemplateSection]) -> None:
            for sec in sections:
                # find the collector matching this section in `stack`-built
                # data by walking the kept_keys structure — simpler: rebuild
                # the tree from kept_keys with a proper recursive walk.
                pass

        # Easier: rebuild via the standard tree algorithm now that we have
        # `top_level` already populated with parents but their children=[] at
        # this point. We need to actually populate children. The trick: when
        # we constructed each section we appended it to its parent's
        # children_collector but never wrote that back. Re-do with a proper
        # second pass:
        top_level = self._build_tree(kept_keys, display_titles, hints_by_key)

        return ReportTemplate(
            template_id=self._options.template_id,
            version="0.1.0",
            status=TemplateStatus.DRAFT,
            report_type=self._options.report_type,
            title=self._options.title,
            metadata=TemplateMetadata(
                authored_by=self._options.authored_by,
                authored_at=datetime.now(timezone.utc),
                source_origin="from_samples",
            ),
            global_style=GlobalStyle(),
            sections=top_level,
        )

    def _build_tree(
        self,
        kept_keys: list[tuple[int, str]],
        display_titles: dict[tuple[int, str], str],
        hints_by_key: dict[tuple[int, str], list[str]],
    ) -> list[TemplateSection]:
        """Build the section tree from kept_keys (in order) by attaching
        children to the deepest open parent. Simpler version of the
        algorithm than the inline attempt above."""
        section_counter: dict[int, int] = {}
        # Track parents by level: parent_at_level[L] is the most recently
        # opened section at level L. Children of a section S at level L
        # attach to S's children list.
        parent_at_level: dict[int, TemplateSection] = {}
        top_level: list[TemplateSection] = []
        for level, normalized_title in kept_keys:
            # Drop any deeper parents from the map; they're closed.
            for deeper in [k for k in parent_at_level if k >= level]:
                del parent_at_level[deeper]
            for deeper in [k for k in section_counter if k > level]:
                del section_counter[deeper]
            section_counter[level] = section_counter.get(level, 0) + 1
            section_id = ".".join(
                str(section_counter[lvl])
                for lvl in sorted(section_counter)
                if lvl <= level
            )
            key = (level, normalized_title)
            section = TemplateSection(
                section_id=section_id,
                title=display_titles[key],
                level=level,
                generation=GenerationPolicy(
                    mode=GenerationMode.LLM,
                    prompt_template=self._compose_prompt(
                        display_titles[key], hints_by_key.get(key, [])
                    ),
                    expected_length_words_min=self._options.default_min_words,
                    expected_length_words_max=self._options.default_max_words,
                    style_directives=["formal", "factual_only"],
                    output_shape=OutputShape.PROSE,
                ),
                data_bindings=self._default_bindings(),
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
            # Find the deepest open parent (highest level number < this level)
            shallower_levels = [lvl for lvl in parent_at_level if lvl < level]
            if shallower_levels:
                parent = parent_at_level[max(shallower_levels)]
                parent.children.append(section)
            else:
                top_level.append(section)
            parent_at_level[level] = section
        return top_level

    def _compose_prompt(self, display_title: str, hints: list[str]) -> str:
        max_chars = self._options.max_hint_chars_per_section
        truncated_hints: list[str] = []
        running_chars = 0
        for hint in hints:
            remaining = max_chars - running_chars
            if remaining <= 0:
                break
            piece = hint[: remaining + 1] if len(hint) > remaining else hint
            if len(piece) > remaining:
                piece = piece[:remaining].rstrip() + "..."
            truncated_hints.append(piece)
            running_chars += len(piece)
        hint_text = " | ".join(truncated_hints) if truncated_hints else ""
        if hint_text:
            return (
                f"Generate the {display_title!r} section. Use "
                "{{bindings.product_name}} consistently and cite every factual "
                "claim. Excerpts from prior reports for tone/coverage reference: "
                f"{hint_text}"
            )
        return (
            f"Generate the {display_title!r} section. Use "
            "{{bindings.product_name}} consistently and cite every factual claim "
            "drawn from the supplied source chunks."
        )

    def _default_bindings(self) -> list[DataBinding]:
        return [
            FreeTextInputBinding(
                binding_id="product_name", prompt="Product name", required=True
            )
        ]
