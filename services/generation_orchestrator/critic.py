"""Critique phase — QA-style review of one filled section.

Returns a pass/fail verdict + a list of issues. The orchestrator decides
whether to regenerate the section based on the verdict + retry budget.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from shared.llm import LlmClient, LlmMessage, LlmRequest, LlmRole, ModelTier
from shared.schemas import TemplateSection

from .prompts import CRITIQUE_SYSTEM_PROMPT, PROMPT_VERSION
from .types import GeneratedSection


class _CritiqueOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: str = Field(description="'pass' or 'fail'")
    issues: list[str] = Field(default_factory=list)


_NUMBER_RE = re.compile(r"\b\d[\d,]*(?:\.\d+)?\s*%?")


def _local_validation_issues(section: TemplateSection, gen: GeneratedSection) -> list[str]:
    """Cheap, deterministic checks that do not need the LLM.

    The LLM still runs (it catches subtler issues), but these guard against
    obvious failures up front so the LLM never sees clearly-broken output.
    """
    issues: list[str] = []

    if section.citation_policy.required:
        for paragraph in gen.paragraphs:
            numbers = _NUMBER_RE.findall(paragraph.text)
            if numbers and not any(c.citation_ids for c in paragraph.claims):
                issues.append(
                    f"Paragraph contains numeric values {numbers!r} but no claims "
                    f"with citations. Rule: must_cite_every_number."
                )

    total_words = sum(len(p.text.split()) for p in gen.paragraphs)
    lo = section.generation.expected_length_words_min
    hi = section.generation.expected_length_words_max
    if lo and total_words < lo:
        issues.append(f"Length {total_words} words is below min {lo}.")
    if hi and total_words > hi:
        issues.append(f"Length {total_words} words exceeds max {hi}.")

    return issues


class SectionCritic:
    def __init__(self, client: LlmClient) -> None:
        self._client = client

    def critique(
        self, *, section: TemplateSection, generated: GeneratedSection
    ) -> tuple[bool, list[str]]:
        # Layer 1: local checks
        issues = _local_validation_issues(section, generated)

        # Layer 2: LLM review
        body = "\n\n".join(
            f"### Paragraph {i}\n{p.text}\nClaims:\n"
            + "\n".join(f"  - {c.text} [{', '.join(c.citation_ids)}]" for c in p.claims)
            for i, p in enumerate(generated.paragraphs)
        )
        user_message = (
            f"# Section: {section.section_id} {section.title}\n\n"
            f"## Length policy\n"
            f"min={section.generation.expected_length_words_min}, "
            f"max={section.generation.expected_length_words_max}\n"
            f"## Style\n{section.generation.style_directives}\n"
            f"## Citation policy\nrequired={section.citation_policy.required}, "
            f"granularity={section.citation_policy.granularity}\n\n"
            f"## Draft\n{body}\n\n"
            "Call emit_structured_output with your verdict and list of issues."
        )

        request = LlmRequest(
            tier=ModelTier.PLAN_CRITIQUE,
            system=CRITIQUE_SYSTEM_PROMPT + f"\n\nprompt_version: {PROMPT_VERSION}",
            messages=[LlmMessage(role=LlmRole.USER, content=user_message)],
            max_tokens=1024,
            temperature=0.0,
            response_schema_name="CritiqueOutput",
            response_schema_json=_CritiqueOutput.model_json_schema(),
        )
        response = self._client.generate(request)
        if response.parsed_json is None:
            # If the model didn't respond structurally, treat as a fail with
            # the raw text as the issue.
            return False, issues + [f"critique model returned no structured output: {response.text[:200]}"]

        critique = _CritiqueOutput.model_validate(response.parsed_json)
        issues.extend(critique.issues)
        verdict_pass = critique.verdict.strip().lower() == "pass" and not issues
        return verdict_pass, issues
