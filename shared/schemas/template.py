"""Report template schema.

A template is a versioned JSON document describing the structure, generation
instructions, and data contract for a report type (e.g., ICH E6 Investigator's
Brochure). Approved templates are immutable; a populated `ReportInstance`
(modeled elsewhere) links back to `template_id + version`.

Section nodes are recursive and carry their own generation policy, data
bindings, citation rules, and validation rules — so the orchestrator can drive
a section in isolation from its parent.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TemplateStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class GenerationMode(StrEnum):
    """How a section's content is produced."""

    LLM = "llm"
    DETERMINISTIC = "deterministic"
    MANUAL = "manual"
    HYBRID = "hybrid"


class DataBindingType(StrEnum):
    """Where the data for a section comes from.

    `SQL_QUERY` and `NAMED_QUERY` are distinct: NAMED_QUERY references a
    pre-approved query in the registry (preferred); SQL_QUERY is LLM-drafted
    SQL that must pass the linter + dry-run + human-approval gate before
    execution against any production source.
    """

    NAMED_QUERY = "named_query"
    SQL_QUERY = "sql_query"
    FILE_SET = "file_set"
    FILE_REF = "file_ref"
    COMPUTED_METRIC = "computed_metric"
    FREE_TEXT_INPUT = "free_text_input"
    API_CALL = "api_call"


class OutputShape(StrEnum):
    PROSE = "prose"
    TABLE = "table"
    LIST = "list"


# --- Data bindings ----------------------------------------------------------


class NamedQueryBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal[DataBindingType.NAMED_QUERY] = DataBindingType.NAMED_QUERY
    binding_id: str
    source: str = Field(description="Logical name of the data source (e.g. 'edc_warehouse')")
    query_id: str = Field(description="ID in the named query registry (e.g. 'ae_summary_v3')")
    parameters: dict[str, str] = Field(
        default_factory=dict,
        description="Template-time parameter expressions, resolved per ReportInstance",
    )
    required: bool = True


class SqlQueryBinding(BaseModel):
    """LLM-drafted SQL. Executes only after linter + dry-run + human approval."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal[DataBindingType.SQL_QUERY] = DataBindingType.SQL_QUERY
    binding_id: str
    source: str
    sql: str = Field(description="The SQL drafted by the LLM (must be parameterized)")
    parameters: dict[str, str] = Field(default_factory=dict)
    required: bool = True


class FileSetBinding(BaseModel):
    """A filter that resolves to a set of CanonicalDocuments at generation time."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal[DataBindingType.FILE_SET] = DataBindingType.FILE_SET
    binding_id: str
    filter_tags: list[str] = Field(default_factory=list, description="Match documents tagged with all of these")
    source_systems: list[str] | None = None
    required: bool = True


class FileRefBinding(BaseModel):
    """A reference to a specific CanonicalDocument by doc_id."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal[DataBindingType.FILE_REF] = DataBindingType.FILE_REF
    binding_id: str
    doc_id: str
    required: bool = True


class ComputedMetricBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal[DataBindingType.COMPUTED_METRIC] = DataBindingType.COMPUTED_METRIC
    binding_id: str
    metric_id: str = Field(description="ID in the computed-metric registry")
    parameters: dict[str, str] = Field(default_factory=dict)
    required: bool = True


class FreeTextInputBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal[DataBindingType.FREE_TEXT_INPUT] = DataBindingType.FREE_TEXT_INPUT
    binding_id: str
    prompt: str = Field(description="What the human is asked to provide at instance creation")
    required: bool = True


class ApiCallBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal[DataBindingType.API_CALL] = DataBindingType.API_CALL
    binding_id: str
    connector_id: str = Field(description="Configured connector instance (e.g. 'veeva_prod')")
    endpoint: str
    parameters: dict[str, str] = Field(default_factory=dict)
    required: bool = True


DataBinding = Annotated[
    NamedQueryBinding
    | SqlQueryBinding
    | FileSetBinding
    | FileRefBinding
    | ComputedMetricBinding
    | FreeTextInputBinding
    | ApiCallBinding,
    Field(discriminator="type"),
]


# --- Generation policy ------------------------------------------------------


class GenerationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: GenerationMode = GenerationMode.LLM
    prompt_template: str | None = Field(
        default=None,
        description="Jinja2-style prompt; resolved bindings are made available as {{bindings.<binding_id>}}",
    )
    expected_length_words_min: int | None = None
    expected_length_words_max: int | None = None
    style_directives: list[str] = Field(default_factory=list)
    output_shape: OutputShape = OutputShape.PROSE
    output_json_shape: dict[str, object] | None = Field(
        default=None,
        description="When output_shape is TABLE or LIST, the expected JSON shape for structured output",
    )


# --- Citation & validation policy -------------------------------------------


class CitationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    required: bool = True
    granularity: Literal["claim", "paragraph", "section"] = "claim"
    min_citations_per_paragraph: int = 1


class ValidationRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rule: str = Field(description="Identifier of a registered validator (e.g. 'must_cite_every_number')")
    severity: Literal["error", "warn"] = "error"
    parameters: dict[str, str] = Field(default_factory=dict)


# --- Section & template -----------------------------------------------------


class TemplateSection(BaseModel):
    """Recursive section node.

    `section_id` is the dotted path within the template (e.g. "3.2.1").
    Children are optional; a leaf section produces its own content.
    """

    model_config = ConfigDict(extra="forbid")

    section_id: str
    title: str
    level: int = Field(ge=1, le=6)
    children: list[TemplateSection] = Field(default_factory=list)
    generation: GenerationPolicy = Field(default_factory=GenerationPolicy)
    data_bindings: list[DataBinding] = Field(default_factory=list)
    citation_policy: CitationPolicy = Field(default_factory=CitationPolicy)
    validation_rules: list[ValidationRule] = Field(default_factory=list)


class TemplateMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    authored_by: str
    authored_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    source_origin: Literal["from_docx", "from_samples", "from_library", "from_scratch"]
    parent_template_id: str | None = None


class GlobalStyle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    numbering: Literal["decimal", "roman", "alpha", "none"] = "decimal"
    citation_style: Literal["footnote_numbered", "footnote_lettered", "inline_tagged"] = (
        "footnote_numbered"
    )
    heading_levels: int = Field(default=4, ge=1, le=6)


# --- Classification tags ----------------------------------------------------
#
# `ReportTemplate.tags` is a FACET map: facet id -> ordered value ids, e.g.
#   {"domain": ["dmpk"], "compliance": ["non_gxp"], "modality": ["small_molecule"]}
#
# The *semantics* of a facet (its label, cardinality, whether it is required,
# which values exist) live in report-templates/taxonomy.yaml, never here. This
# module performs SHAPE normalisation only, so a schema that knows nothing
# about any particular taxonomy can still round-trip a tagged template.
#
# NOTE: this is unrelated to `FileSetBinding.filter_tags`, which selects source
# *documents* at generation time.

_FACET_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VALUE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _coerce_tag_map(value: object) -> dict[str, list[str]]:
    """Shape normalisation ONLY. Knows nothing about any taxonomy."""
    if value is None or value == "" or value == []:
        return {}
    raw: dict[str, object]
    if isinstance(value, (list, tuple)):
        raw = {}
        for element in value:
            token = str(element).strip()
            facet, sep, val = token.partition(":")
            if not sep:
                continue  # a stray string is not worth a load failure
            raw.setdefault(facet.strip(), []).append(val.strip())  # type: ignore[union-attr]
    elif isinstance(value, dict):
        raw = value
    else:
        raise ValueError("tags must be a mapping of facet id -> value id(s)")

    out: dict[str, list[str]] = {}
    for facet_key, facet_values in raw.items():
        facet = str(facet_key).strip().lower()
        if not _FACET_ID_RE.match(facet):
            raise ValueError(f"tags: invalid facet id {facet_key!r}")
        items = facet_values if isinstance(facet_values, (list, tuple)) else [facet_values]
        seen: list[str] = []
        for item in items:
            if item is None:
                continue
            val = str(item).strip().lower()
            if not val:
                continue
            if not _VALUE_ID_RE.match(val):
                raise ValueError(f"tags.{facet}: invalid value id {item!r}")
            if val not in seen:
                seen.append(val)
        out[facet] = seen  # [] is legal and meaningful
    return out


class ReportTemplate(BaseModel):
    """A versioned, approvable report template.

    Once `status == APPROVED`, the template is immutable — further changes
    require a new version. ReportInstances link back to (template_id, version).
    """

    model_config = ConfigDict(extra="forbid")

    template_id: str
    version: str = Field(description="Semver-ish; bump on any section/binding change")
    status: TemplateStatus = TemplateStatus.DRAFT
    report_type: str = Field(description="Stable identifier, e.g. 'ICH_E6_IB'")
    title: str
    description: str = ""
    metadata: TemplateMetadata
    global_style: GlobalStyle = Field(default_factory=GlobalStyle)
    sections: list[TemplateSection]
    tags: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Facet id -> ordered value ids. Facet and value semantics live in "
            "report-templates/taxonomy.yaml, never in this schema."
        ),
    )

    @field_validator("tags", mode="before")
    @classmethod
    def _validate_tags(cls, value: object) -> dict[str, list[str]]:
        return _coerce_tag_map(value)

    def tag_values(self, facet_id: str) -> list[str]:
        """The values set for one facet; `[]` when the facet is unset."""
        return list(self.tags.get(facet_id, ()))

    def tag_tokens(self) -> list[str]:
        """Derived `facet:value` tokens. Never persisted — URLs and markup only."""
        return [f"{f}:{v}" for f, vs in self.tags.items() for v in vs]

    def all_sections(self) -> list[TemplateSection]:
        """Flatten the section tree depth-first."""
        result: list[TemplateSection] = []

        def walk(s: TemplateSection) -> None:
            result.append(s)
            for child in s.children:
                walk(child)

        for s in self.sections:
            walk(s)
        return result


TemplateSection.model_rebuild()
