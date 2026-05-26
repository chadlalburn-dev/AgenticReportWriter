"""Output types for the generation orchestrator.

These shapes describe a populated report instance: each section carries
generated paragraphs, every paragraph carries claims, every claim cites
a chunk via citation_ids. Tables (deterministic data pulls) are kept
separate from prose so the LLM never reformats raw numbers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GeneratedClaim(BaseModel):
    """A single factual claim with its supporting citations."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str
    citation_ids: list[str] = Field(default_factory=list)


class GeneratedParagraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str
    claims: list[GeneratedClaim] = Field(default_factory=list)


class GeneratedTable(BaseModel):
    """A table rendered from deterministic data (named_query / file ref).

    The LLM does NOT generate table contents — they come straight from the
    bound data source. The LLM only writes the surrounding narrative.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    caption: str
    columns: list[str]
    rows: list[list[str]]
    source_binding_id: str
    citation_id: str | None = None


class GeneratedSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: str
    title: str
    level: int
    paragraphs: list[GeneratedParagraph] = Field(default_factory=list)
    tables: list[GeneratedTable] = Field(default_factory=list)
    children: list[GeneratedSection] = Field(default_factory=list)
    # populated by the critique phase
    critique_status: Literal["pending", "passed", "failed_after_retries"] = "pending"
    critique_notes: list[str] = Field(default_factory=list)


GeneratedSection.model_rebuild()


class ReportInstance(BaseModel):
    """A populated report linked to a specific template version.

    Identity: template_id + template_version + instance_id. Re-running with
    a newer template version produces a new instance — old preserved for
    audit (critical for regulated outputs).
    """

    model_config = ConfigDict(extra="forbid")
    instance_id: str
    template_id: str
    template_version: str
    compliance_mode: Literal["rd", "gxp", "part11"] = "rd"
    report_title: str
    free_text_inputs: dict[str, str] = Field(default_factory=dict)
    generated_at: datetime
    plan_summary: str | None = None
    sections: list[GeneratedSection] = Field(default_factory=list)
