"""Run store — template catalog, preflight, background generation, presenters.

This module is the ONLY place the UI touches the generation engine. `ui.py`
imports nothing from `services.generation_orchestrator`, `services.*_integration`,
`services.template_service` or `shared.*` — it calls `RunStore` and renders the
view models built here.

Everything runs FULLY OFFLINE:

  * `StubLlmClient` for plan / fill / critique — no Vertex, no ADC, no keys.
  * `SqliteQueryExecutor` over `samples/synthetic_compound/edc.sqlite` behind a
    `TolerantSqlSafetyGate` (fail-closed approval callback; unknown query ids
    degrade to a deferred note instead of killing the run).
  * `ApiCallGate` over the three bundled mock connectors (Confluence, ChEMBL,
    ClinicalTrials).
  * `LocalFileConnector` + the parser registry over the bundled synthetic
    corpus (or any local folder the scientist points at).

Nothing here writes outside `var/`. Nothing here opens a socket.

This is DISCOVERY RESEARCH tooling. Every string that reaches the UI is written
for a preclinical scientist reviewing a draft — never regulatory-process
language.
"""

from __future__ import annotations

import copy
import csv
import dataclasses
import difflib
import io
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence
from urllib.parse import urlencode

from services.api_integration import (
    ApiCallGate,
    ApiConnectorRegistry,
    MockChemblConnector,
    MockClinicalTrialsConnector,
    MockConfluenceConnector,
)
from services.api_integration.sharepoint import MockSharePointConnector
from services.audit import AuditEvent, AuditSink, AuditStore, InMemoryAuditStore
from shared.connectivity import ConnectorStatus
from shared.http_transport import HttpTransport
from services.audit.schema import AuditAction
from services.data_integration import (
    NamedQueryRegistry,
    SqlSafetyGate,
    SqlSafetyViolation,
    SqliteQueryExecutor,
)
from services.generation_orchestrator.orchestrator import ReportGenerator
from services.generation_orchestrator.prompts import PROMPT_VERSION
from services.generation_orchestrator.retrieval import BindingResolver
from services.ingestion_service.connectors import ConnectorContext, LocalFileConnector
from services.parsing_service.registry import default_registry
from services.template_service import ReportDocError, load_report_doc
from shared.llm import (
    ClaudeCliConfig,
    ClaudeCliLlmClient,
    ClaudeCliUnavailable,
    LlmClient,
    LlmRequest,
    LlmResponse,
    StubLlmClient,
    find_claude_binary,
)
from pydantic import ValidationError

from services.document_renderer.charts import ChartDataError, render_chart
from shared.schemas import CanonicalDocument, ParsedChunk, ReportTemplate, TemplateSection
from shared.schemas.template import VisualSpec
from shared.schemas.template import (
    ApiCallBinding,
    ComputedMetricBinding,
    FileRefBinding,
    FileSetBinding,
    FreeTextInputBinding,
    GenerationMode,
    NamedQueryBinding,
    SqlQueryBinding,
)

# ---------------------------------------------------------------------------
# §7.1 Module constants
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
TEMPLATES_DIR: Path = REPO_ROOT / "report-templates"

#: Where a user's own templates live, under the shared library directory.
USER_TEMPLATES_DIR_NAME = "users"

#: "universal" is visible to everyone and editable by anyone who can reach the
#: app; "user" belongs to one person and nobody else sees it.
TemplateScope = Literal["universal", "user"]

_USER_SLUG_UNSAFE = re.compile(r"[^a-z0-9._-]+")


def user_dir_slug(user_id: str) -> str:
    """A directory name for a user id.

    Identities here are whatever the proxy or the OS hands over, and in the GSK
    deployment that is an email — `chad.l.alburn@gsk.com`. That cannot be a path
    segment as-is, so everything outside a conservative set collapses to an
    underscore. Lossy and deliberately so: this is a storage location, not an
    identity, and `resolve_user` remains the only thing that decides who someone
    is.

    Guarded rather than trusted, because a user id reaching a filesystem path is
    exactly where a traversal would live if one could.
    """
    slug = _USER_SLUG_UNSAFE.sub("_", (user_id or "").strip().lower()).strip("._-")
    return slug or "unknown"


RUNS_ROOT: Path = REPO_ROOT / "var" / "runs"
CORPUS_DIR: Path = REPO_ROOT / "samples" / "synthetic_compound" / "sources"
QUERIES_DIR: Path = REPO_ROOT / "samples" / "synthetic_compound" / "queries"
EDC_SQLITE: Path = REPO_ROOT / "samples" / "synthetic_compound" / "edc.sqlite"
IB_TEMPLATE: Path = REPO_ROOT / "templates" / "library" / "ich_e6_ib.json"

MAX_WORKERS: int = 2
MAX_INPUT_LEN: int = 200
MAX_RETRIES_PER_SECTION: int = 1
KEEP_RUNS: int = 50

INPUT_DEFAULTS: dict[str, str] = {
    "compound_id": "XYZ-001",
    "product_name": "XYZ-001",
    "target_name": "Kinase Z",
    "indication_keyword": "Kinase Z",
}

DRAFT_NOTICE: str = (
    "AI-generated draft for human review. Generated {date} from "
    "{template_id}@{version} using {model_version}. Every value must be "
    "verified against its citation before use."
)

#: Appended to DRAFT_NOTICE when one or more sections were drafted with no
#: resolved source data. Never omitted — a hollow section must look hollow.
DRAFT_NOTICE_GAPS: str = (
    " Drafted with no source data, verify from scratch: {sections}."
)

#: Appended when the engine was the offline stub.
#:
#: The notice already names the model version, but "stub-claude-sonnet-4-6@stub"
#: reads like a real Sonnet build to anyone who does not know the codebase — and
#: this notice travels: it is the first line of every exported markdown file,
#: read by people who never saw the app. The page says "PLACEHOLDER text from an
#: offline stub" in plain words and the artifact that leaves the building should
#: not say less.
DRAFT_NOTICE_STUB: str = (
    " The section prose is PLACEHOLDER text from an offline stub, not a "
    "model: retrieval, citations and the audit trail are real, the sentences "
    "are not."
)

STUB_LLM_WARNING: str = (
    "Stub LLM — the narrative is placeholder text. Citations and data are "
    "real; the prose is not."
)

_PRIMARY_INPUT_ORDER = ("compound_id", "product_name", "target_name", "indication_keyword")

_PARSEABLE_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx"})

#: Where `write_template` keeps its silent safety net, and where a deleted
#: template goes. Both live under the already-gitignored `var/`.
TEMPLATE_BACKUPS_DIR: Path = REPO_ROOT / "var" / "template-backups"
TEMPLATE_TRASH_DIR: Path = REPO_ROOT / "var" / "template-trash"

#: A `report-templates/*.md` stem that is safe to put in a URL and to join
#: onto a directory. Deliberately WIDER than the writer's `report_type`
#: charset: existing files (README, SKILL.template) must stay addressable.
TEMPLATE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: Opaque editor row key. The browser mints `[A-Za-z0-9_-]{1,16}`; the server
#: mints `i1`/`s1`/`t1`. Neither side ever renumbers the other's keys.
_ROW_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$")

GROUP_NONE: str = "none"
#: Facet ids that are DERIVED from the card, never configured (contract R11).
#: Groupings computed from a card rather than read off its tags. "scope" is one
#: of these: which library a template lives in is a property of where the file
#: is, not something an author tags it with.
DERIVED_GROUPS: tuple[str, ...] = ("scope", "owner", "readiness")

SORT_OPTIONS: list[tuple[str, str]] = [
    ("name", "Name (A–Z)"),
    ("updated", "Recently updated"),
    ("sections", "Most sections"),
    ("ready", "Source readiness (ready first)"),
    ("owner", "Owning team (A–Z)"),
]
_SORT_IDS: frozenset[str] = frozenset(k for k, _ in SORT_OPTIONS)

GRANULARITY_OPTIONS: list[tuple[str, str]] = [
    ("claim", "Per claim"),
    ("paragraph", "Per paragraph"),
    ("section", "Per section"),
]

SOURCE_KIND_OPTIONS: list[tuple[str, str]] = [
    ("bigquery", "BigQuery"),
    ("oracle", "Oracle database"),
    ("confluence", "Confluence"),
    ("sharepoint", "SharePoint / OneDrive"),
    ("file", "Local documents"),
    ("api", "API connector"),
]

#: Every field name a source row can carry, for all four kinds at once, so
#: switching kind back and forth in the editor never loses what was typed.
SOURCE_FIELD_NAMES: tuple[str, ...] = (
    "dataset",
    "query_id",
    "sql",
    "space",
    "cql",
    "page_id",
    "filter_tags",
    "service",
    "site",
    "folder",
    "file_types",
    "query",
    "connector",
    "endpoint",
    "params",
)

_READINESS_ORDER: dict[str, int] = {"ready": 0, "gaps": 1, "blocked": 2, "broken": 3}
_READINESS_GROUP_LABEL: dict[str, str] = {
    "ready": "Every source ready",
    "gaps": "Some sources not ready",
    "blocked": "Sources blocked",
    "broken": "Not runnable",
}

#: Hard cap on tag chips per gallery card (contract §5.4).
MAX_CARD_CHIPS: int = 2


# ---------------------------------------------------------------------------
# Authoring bridge — tag taxonomy (ENG-1) and template writer (ENG-2)
# ---------------------------------------------------------------------------
#
# `services/template_service/__init__.py` is frozen, so both modules are
# imported by full path (contract §1). Nothing here imports the engine.

from services.template_service.report_doc_writer import (  # noqa: E402
    SOURCE_KINDS,
    DraftInput,
    DraftIssue,
    DraftSection,
    DraftSource,
    TemplateConflict,
    TemplateDraft,
    TemplateWriteError,
    backup_template,
    blank_draft,
    clone_draft,
    draft_from_path,
    find_trashed,
    read_sha256,
    restore_trashed,
    serialize_draft,
    trash_template,
    validate_draft,
    write_template,
)
from services.template_service.taxonomy import (  # noqa: E402
    UNTAGGED,
    load_taxonomy,
    parse_token,
    slugify_value,
)


# ---------------------------------------------------------------------------
# §7.2 Literals
# ---------------------------------------------------------------------------

RunStatus = Literal[
    "queued",
    "preflight",
    "ingesting",
    "planning",
    "generating",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]
SectionStatus = Literal[
    "pending", "running", "retrying", "passed", "failed", "skipped", "cancelled"
]
Severity = Literal["blocker", "warning"]
Readiness = Literal["ready", "gaps", "blocked", "broken"]
Anchor = Literal["exact", "normalized", "sentence", "unanchored"]
BandKind = Literal["none", "no_data", "failed"]

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)

STATUS_LABEL: dict[str, str] = {
    "queued": "Queued",
    "preflight": "Checking sources",
    "ingesting": "Reading evidence folder",
    "planning": "Planning sections",
    "generating": "Drafting sections",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "interrupted": "Interrupted",
}

STATUS_STATE: dict[str, str] = {
    "queued": "neutral",
    "preflight": "running",
    "ingesting": "running",
    "planning": "running",
    "generating": "running",
    "completed": "ok",
    "failed": "error",
    "cancelled": "warn",
    "interrupted": "warn",
}

#: Must stay byte-identical to the SECTION_LABEL map in /static/app.js (§6.2).
SECTION_LABEL: dict[str, str] = {
    "pending": "Waiting",
    "running": "Drafting…",
    "retrying": "Retrying",
    "passed": "Drafted · checks passed",
    "failed": "Drafted · checks failed",
    "skipped": "Not generated (deterministic/manual section)",
    "cancelled": "Cancelled",
}

SECTION_STATE: dict[str, str] = {
    "pending": "neutral",
    "running": "running",
    "retrying": "warn",
    "passed": "ok",
    "failed": "error",
    "skipped": "neutral",
    # neutral, to stay in step with app.js SECTION_STATE and the progress_row
    # macro in base.html — a chip must never change colour on the first poll.
    "cancelled": "neutral",
}

PHASE_FOR_STATUS: dict[str, str] = {
    "queued": "ingest",
    "preflight": "ingest",
    "ingesting": "ingest",
    "planning": "plan",
    "generating": "draft",
    "completed": "done",
    "failed": "done",
    "cancelled": "done",
    "interrupted": "done",
}

POLL_AFTER_MS: dict[str, int] = {
    "queued": 2500,
    "preflight": 1000,
    "ingesting": 1000,
    "planning": 1000,
    "generating": 1000,
}

SOURCE_WORD: dict[str, str] = {
    "pdf": "PDF document",
    "docx": "Word document",
    "xlsx": "Excel workbook",
    "sql": "Database query",
    "api": "External API",
    "computed": "Computed value",
}

SOURCE_CAPTION: dict[str, str] = {
    "pdf": "",
    "docx": "Word documents have no fixed pages; located by heading.",
    "xlsx": "No deep link into a workbook is possible.",
    "sql": "Values as captured at {retrieved}.",
    "api": (
        "External API — response captured at retrieval time; not re-fetched."
    ),
    "computed": "Derived value — provenance not yet captured.",
}

BINDING_KIND_LABEL: dict[str, str] = {
    "named_query": "Registered query",
    "sql_query": "Inline SQL",
    "file_set": "Evidence documents",
    "file_ref": "Evidence document",
    "computed_metric": "Computed metric",
    "api_call": "API connector",
    "free_text_input": "Run input",
}

AUDIT_GROUP: dict[str, tuple[str, str]] = {
    AuditAction.GENERATION_REQUESTED.value: ("request", "Generation requested"),
    AuditAction.GENERATION_PLAN_COMPLETED.value: ("plan", "Plan completed"),
    AuditAction.GENERATION_SECTION_FILLED.value: ("section", "Section drafted"),
    AuditAction.GENERATION_SECTION_CRITIQUED.value: ("section", "Section checked"),
    AuditAction.CITATION_CREATED.value: ("citation", "Citation captured"),
    AuditAction.LLM_CALL.value: ("llm", "Model call"),
    AuditAction.GENERATION_COMPLETED.value: ("complete", "Generation completed"),
}

_EXTRA_LABEL: dict[str, str] = {
    "attempt": "Attempt",
    "compliance_mode": "Mode",
    "n_chunks": "Chunks in pool",
    "n_citations": "Citations",
    "n_documents": "Documents",
    "n_paragraphs": "Paragraphs",
    "n_sections": "Sections",
    "overall_summary_len": "Plan summary length",
    "section_id": "Section",
    "source_doc_id": "Source",
    "source_type": "Source type",
    "template_id": "Template",
    "template_version": "Template version",
    "verdict": "Verdict",
}


class RunNotTerminal(RuntimeError):
    """Raised when a result is requested for a run that is still working."""


class RunCancelled(RuntimeError):
    """Raised inside the worker thread when the user cancelled the run."""


# ---------------------------------------------------------------------------
# §7.3 Dataclasses
# ---------------------------------------------------------------------------


class _Dict:
    """Mixin giving every view model a JSON-safe `to_dict()`."""

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(dataclasses.asdict(self))  # type: ignore[call-overload]


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Path):
        return str(value)
    return value


@dataclass
class FormField(_Dict):
    binding_id: str
    prompt: str
    required: bool
    default: str
    used_by_sections: int


@dataclass
class SourceSpec(_Dict):
    binding_id: str
    kind: str
    label: str
    detail: str
    section_ids: list[str]
    status: str
    status_text: str
    fix_hint: str


@dataclass
class SectionOutline(_Dict):
    section_id: str
    title: str
    level: int
    instruction: str
    source_ids: list[str]
    mode: str


@dataclass
class TagChip(_Dict):
    facet_id: str
    value_id: str
    label: str


@dataclass
class TemplateCard(_Dict):
    key: str
    path: str
    ok: bool
    error: str | None
    template_id: str
    title: str
    description: str
    version: str
    owner: str
    n_sections: int
    form_fields: list[FormField]
    source_counts: dict[str, int]
    sources_ready: int
    sources_total: int
    readiness: Readiness
    readiness_text: str
    # --- classification (contract §5.1). All defaulted: every construction
    # --- site is keyword-only, and the JSON surface stays additive.
    tags: dict[str, list[str]] = field(default_factory=dict)
    tag_tokens: list[str] = field(default_factory=list)
    chips: list[TagChip] = field(default_factory=list)
    n_more_tags: int = 0
    tag_aria: str = ""
    updated: str = ""
    #: "universal" or "user". Shown on every card, because "who else can see
    #: this" is not something a reader should have to infer from a folder.
    scope: str = "universal"
    #: Set only for user-scoped templates: whose it is.
    owned_by: str = ""
    updated_ts: float = 0.0
    search: str = ""


@dataclass
class FacetValueView(_Dict):
    id: str
    label: str
    token: str
    count: int
    selected: bool


@dataclass
class FacetView(_Dict):
    id: str
    label: str
    description: str
    multi: bool
    groupable: bool
    untagged_label: str
    values: list[FacetValueView]
    n_selected: int


@dataclass
class GroupView(_Dict):
    key: str
    facet_id: str
    value_id: str
    label: str
    heading_id: str
    untagged: bool
    count: int
    cards: list[TemplateCard]


@dataclass
class GalleryView(_Dict):
    """Everything `GET /` needs that depends on group / sort / tag / q."""

    group: str
    sort: str
    q: str
    facets: list[FacetView]
    groups: list[GroupView]
    cards: list[TemplateCard]
    unavailable: list[TemplateCard]
    group_options: list[tuple[str, str]]
    sort_options: list[tuple[str, str]]
    n_shown: int
    n_total: int
    n_active: int
    active_summary: list[str]
    clear_url: str
    filtering: bool
    taxonomy_ok: bool


# --- editor view models (contract §5.3) ------------------------------------


@dataclass
class EditorFacetValue(_Dict):
    id: str
    label: str
    description: str
    selected: bool
    deprecated: bool
    unknown: bool


@dataclass
class EditorFacet(_Dict):
    id: str
    label: str
    description: str
    multi: bool
    required: bool
    open_mode: bool
    field_name: str
    new_field_name: str
    selected: list[str]
    values: list[EditorFacetValue]
    error: str
    new_value_text: str


@dataclass
class EditorInputRow(_Dict):
    key: str
    id: str
    prompt: str
    required: bool
    errors: dict[str, str]


@dataclass
class EditorSourceRow(_Dict):
    key: str
    id: str
    kind: str
    required: bool
    legend: str
    fields: dict[str, str]
    errors: dict[str, str]


@dataclass
class EditorSectionRow(_Dict):
    key: str
    number: int
    heading: str
    instruction: str
    source_keys: list[str]
    table_key: str
    #: The `> Visual:` directive, as authored. Round-tripped verbatim so opening
    #: a template in the editor and saving it unchanged cannot delete a figure
    #: the author declared — which is exactly what happened before the writer
    #: learned to emit this line, and the round-trip test caught it.
    visual: str
    errors: dict[str, str]


@dataclass
class PreflightIssue(_Dict):
    severity: Severity
    code: str
    binding_id: str
    section_id: str
    message: str
    fix_hint: str


@dataclass
class PreflightReport(_Dict):
    verdict: str
    headline: str
    sources: list[SourceSpec]
    issues: list[PreflightIssue]
    sections_without_data: list[str]
    blocked: bool


@dataclass
class SectionProgress(_Dict):
    section_id: str
    title: str
    level: int
    status: SectionStatus = "pending"
    status_label: str = "Waiting"
    attempts: int = 0
    n_paragraphs: int = 0
    n_citations: int = 0
    notes: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class RunError(_Dict):
    kind: str
    message: str
    detail: str


@dataclass
class RunSummary(_Dict):
    run_id: str
    template_key: str
    template_title: str
    template_version: str
    title: str
    inputs: dict[str, str]
    primary_input: str
    evidence_folder: str
    status: RunStatus
    status_label: str
    status_state: str
    terminal: bool
    created_at: str
    started_at: str | None
    finished_at: str | None
    created_human: str
    duration_s: float | None
    duration_human: str
    model_version: str
    instance_id: str | None
    n_sections: int
    n_sections_cited: int
    n_sections_no_data: int
    n_sections_failed: int
    n_claims: int
    n_claims_cited: int
    n_uncited_numbers: int
    n_citations: int
    n_bindings_resolved: int
    n_bindings_deferred: int
    coverage_text: str
    owner: str = ""


@dataclass
class RunRecord(_Dict):
    run_id: str
    template_key: str
    template_title: str
    template_version: str
    template_id: str
    title: str
    inputs: dict[str, str]
    evidence_folder: str
    status: RunStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    sections: list[SectionProgress] = field(default_factory=list)
    preflight: list[PreflightIssue] = field(default_factory=list)
    instance_id: str | None = None
    model_version: str = "stub"
    prompt_version: str = ""
    compliance_mode: str = "rd"
    # Attribution for the "My compounds / All compounds" split. Defaulted so
    # run.json files written before this field existed still rehydrate.
    owner: str = ""
    n_documents: int = 0
    n_chunks: int = 0
    n_citations: int = 0
    n_audit_events: int = 0
    plan_summary: str | None = None
    error: RunError | None = None
    version: int = 0


# --- draft view models -----------------------------------------------------


@dataclass
class NumberChip(_Dict):
    token: str
    found: bool
    label: str


@dataclass
class CitationRef(_Dict):
    n: int
    citation_id: str
    source_type: str
    aria_label: str
    approx: bool


@dataclass
class CitationView(_Dict):
    n: int
    citation_id: str
    source_type: str
    source_word: str
    title: str
    uri_display: str
    uri_copy: str
    open_url: str | None
    locator_rows: list[tuple[str, str]]
    snippet: str
    snippet_grid: list[list[str]] | None
    retrieved_iso: str
    retrieved_human: str
    version_label: str
    version_value: str
    number_chips: list[NumberChip]
    claim_text: str
    section_id: str
    section_title: str
    chunk_id: str | None
    doc_id: str
    instance_id: str
    caption: str


@dataclass
class ClaimView(_Dict):
    claim_idx: int
    anchor: Anchor
    text: str
    citations: list[CitationRef]
    uncited: bool


@dataclass
class Segment(_Dict):
    kind: str
    text: str
    claim: ClaimView | None
    marks: list[tuple[str, bool]] | None


@dataclass
class ParagraphView(_Dict):
    para_idx: int
    segments: list[Segment]
    n_uncited_numbers: int
    orphan_claims: list[ClaimView]


@dataclass
class DataTableView(_Dict):
    binding_id: str
    caption: str
    columns: list[str]
    rows: list[list[str]]
    source_label: str
    row_count: int
    citation_n: int | None
    citation_id: str | None
    retrieved_human: str
    status: str
    deferred_note: str | None
    vh_note: str


@dataclass
class ChartView(_Dict):
    """A figure for one section, already rendered to SVG markup.

    `svg` and `unavailable_reason` are mutually exclusive: either the chart drew,
    or it did not and the page says why. There is deliberately no third state
    where an empty frame renders and the reader is left to guess whether the
    margins are zero or the query is broken.
    """

    binding_id: str
    kind: str
    title: str
    svg: str
    caption: str
    citation_n: int | None
    unavailable_reason: str


@dataclass
class SectionView(_Dict):
    section_id: str
    title: str
    level: int
    heading_tag: str
    critique_status: str
    #: The same fact in English, and required rather than defaulted so a new
    #: construction site cannot forget it. `critique_status` is an internal
    #: Literal ("pending" / "passed" / "failed_after_retries") and it was being
    #: printed straight into the section meta line, so a nonclinical safety
    #: summary told a reader "failed_after_retries" beside a section with no
    #: citations, while every other string on that page is written prose. The
    #: raw value stays for tests and the audit trail; this is what renders.
    critique_label: str
    critique_notes: list[str]
    notes_short: list[str]
    paragraphs: list[ParagraphView]
    tables: list[DataTableView]
    #: At most one, because the template declares at most one. A section that
    #: grew a second figure would stop being comparable with the same section of
    #: the next report, which is the whole reason the figure is declared in the
    #: template rather than chosen per run.
    chart: ChartView | None
    n_citations: int
    n_uncited_numbers: int
    band: BandKind
    band_title: str
    band_body: str


@dataclass
class LedgerRow(_Dict):
    """One binding, as it actually behaved during the run.

    `status` is one of `cited`, `resolved_uncited` or `unavailable`. Note that
    this vocabulary is NOT the preflight one — `SourceSpec.status` uses
    `ready` / `gap` / `unavailable`, because before a run you know whether a
    source *can* resolve and afterwards you know whether it *did* and whether
    the draft used it. The two are genuinely different questions.

    They were confused once, and templates are the wrong place to find out:
    the sources tab tested `status == "ready"`, which is never true here, so
    every row rendered in the failure treatment — hollow dot, accent-coloured
    value — including the two that resolved and were cited. In a product whose
    entire claim is provenance, telling a reader that a cited source failed is
    about the worst available error. Hence `resolved` and `returned_text`:
    the vocabulary is interpreted here, once.
    """

    binding_id: str
    kind: str
    label: str
    detail: str
    status: str
    status_text: str
    row_count: int | None
    section_ids: list[str]
    citation_ns: list[int]
    deferred_note: str | None
    fix_hint: str | None
    columns: list[str]
    rows: list[list[str]]
    #: The same cells before they were turned into display strings. Charts read
    #: these, never `rows`: a figure has to plot the number the query returned,
    #: not a number recovered from the text a table happened to render. The
    #: chart renderer refuses non-numeric input on purpose, and re-parsing
    #: "15.0" back out of a string would route around that refusal instead of
    #: honouring it.
    typed_rows: list[list[object]]
    sql: str | None

    @property
    def resolved(self) -> bool:
        """Did anything come back? Says nothing about whether the draft used it."""
        return self.status != "unavailable"

    @property
    def used(self) -> bool:
        """Resolved AND cited by the draft — the only fully good outcome."""
        return self.status == "cited"

    @property
    def returned_text(self) -> str:
        """What came back, short enough for a fixed-width column.

        `status_text` is a sentence for a tooltip; this is a value for a cell.
        """
        if self.row_count is None:
            return "nothing"
        unit = "extract" if self.kind in ("file_set", "file_ref") else "row"
        return f"{self.row_count} {_plural(self.row_count, unit)}"


@dataclass
class EventView(_Dict):
    """One row of the run log, which is the provenance record.

    Three time fields, because they answer different questions and one string
    cannot. `ts_human` is minute precision and is what the run lists use, where
    seconds would be noise. The log is different: a run finishes inside a
    minute, so all 46 events rendered the identical string "21 Aug 2026, 16:29"
    — the date repeated 46 times carrying nothing, while the seconds that
    distinguish the steps were truncated away. On a page whose lede promises
    "every step this run took, in order", how long each step took is the thing
    a reader came for.
    """

    ts_human: str
    #: Wall clock with seconds, for the log. No date — that belongs once at the
    #: top of the page, not on every row.
    ts_precise: str
    #: Elapsed since the first event, e.g. "+0.4s". The column that actually
    #: says where the run spent its time.
    offset_human: str
    action: str
    action_label: str
    target: str
    notes: list[str]
    extra_pairs: list[tuple[str, str]]
    group: str


@dataclass
class OutlineItem(_Dict):
    section_id: str
    title: str
    level: int
    state: str
    label: str


@dataclass
class TrustBar(_Dict):
    n_sections: int
    n_claims: int
    n_claims_cited: int
    n_uncited_numbers: int
    n_sections_no_data: int
    n_sections_failed: int
    n_citations: int
    n_integrity_errors: int
    clean: bool
    state: str
    headline: str


@dataclass
class DraftView(_Dict):
    trust: TrustBar
    outline: list[OutlineItem]
    sections: list[SectionView]
    citations: list[CitationView]
    ledger: list[LedgerRow]
    ledger_summary: str
    coverage_columns: list[str]
    coverage_rows: list[tuple[str, list[int]]]
    events: list[EventView]
    notice: str
    hollow: bool


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    dt = value or _now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clock_ts(value: str | datetime | None) -> str:
    """Wall clock with seconds and no date, for the run log.

    Separate from `_human_ts` rather than a parameter on it: the run lists want
    minute precision and a date, the log wants the opposite, and one function
    trying to be both is how the log ended up showing the same string 46 times.
    """
    dt = _parse_iso(value) if isinstance(value, str) else value
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%H:%M:%S")


def _offset_human(value: str | datetime | None, start: str | datetime | None) -> str:
    """Elapsed since the run's first event, e.g. "+0.4s".

    Sub-second because that is the scale these steps run at: a stub section
    completes in milliseconds and a real model call in seconds, and the whole
    point of showing this column is telling those two apart.
    """
    dt = _parse_iso(value) if isinstance(value, str) else value
    origin = _parse_iso(start) if isinstance(start, str) else start
    if dt is None or origin is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if origin.tzinfo is None:
        origin = origin.replace(tzinfo=timezone.utc)
    seconds = (dt - origin).total_seconds()
    if seconds < 0:
        return ""                      # clock skew; say nothing rather than lie
    if seconds < 10:
        return f"+{seconds:.1f}s"
    if seconds < 600:
        return f"+{seconds:.0f}s"
    return f"+{seconds / 60:.0f}m"


#: Internal critique states, in the words a reader should see. "pending" and
#: "passed" map to nothing on purpose: the meta line only mentions the state
#: when it is worth mentioning, and "passed" beside a citation count would be
#: noise on every section that worked.
CRITIQUE_LABEL: dict[str, str] = {
    "failed_after_retries": "checks failed after a retry",
    "pending": "",
    "passed": "",
}


def _human_ts(value: str | datetime | None) -> str:
    dt = _parse_iso(value) if isinstance(value, str) else value
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    return local.strftime("%d %b %Y, %H:%M")


def _human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return "under a second"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


# ---------------------------------------------------------------------------
# §7.6 Engine wiring (offline)
# ---------------------------------------------------------------------------

_CITE_RE = re.compile(r"\[citation_id=([0-9a-f-]+)\]")


class TolerantSqlSafetyGate(SqlSafetyGate):
    """`SqlSafetyGate` that fails *soft* for the two engine-fatal cases.

    `NamedQueryRegistry.get()` raises `KeyError` and `NamedQuery.validate_args`
    raises `ValueError`; `BindingResolver._resolve_named_query` only catches
    `SqlSafetyViolation`, so either would abort the whole run. Converting them
    means an unresolved binding degrades to a `deferred_note`, which the UI
    surfaces loudly instead of losing the run.
    """

    def run_named_query(
        self,
        query_id: str,
        parameters: Any,
        *,
        actor_id: str = "system:orchestrator",
    ) -> Any:
        try:
            return super().run_named_query(query_id, parameters, actor_id=actor_id)
        except KeyError as exc:
            raise SqlSafetyViolation(
                "UNKNOWN_NAMED_QUERY",
                f"{query_id!r} is not in the named-query registry",
            ) from exc
        except ValueError as exc:
            raise SqlSafetyViolation("BAD_QUERY_PARAMETERS", str(exc)) from exc


#: Which engine drafts the prose. `auto` prefers the local Claude Code CLI and
#: falls back to the stub when it is not signed in; `stub` and `cli` force one.
ENGINE_ENV = "REPORTGEN_ENGINE"

#: How long a resolved engine is trusted before it is probed again.
#:
#: Probing means spawning `claude -p` and waiting for a round trip — seconds,
#: not milliseconds. Every page names the engine, so resolving per request made
#: the whole app as slow as a subprocess launch. It is cached instead.
#:
#: The TTL exists because the answer legitimately changes underneath us: the
#: fix for the stub state is "run `claude`, then `/login`", which happens in
#: another window while this process is running. A short expiry means the app
#: notices within a minute, with no restart. Longer would be cheaper and would
#: leave someone staring at a stale "stub text" chip after they did what the
#: message told them to.
ENGINE_TTL_S = 60.0

_ENGINE_LOCK = threading.Lock()
#: (resolved_at_monotonic, choice, EngineInfo) — `choice` is part of the key so
#: flipping REPORTGEN_ENGINE takes effect at once, which the tests rely on.
_ENGINE_CACHE: tuple[float, str, "EngineInfo"] | None = None
#: True while a background probe is in flight, so page renders coalesce
#: onto one subprocess instead of launching one each.
_ENGINE_PROBING = False


@dataclass(frozen=True)
class EngineInfo:
    """What actually generated a draft — surfaced in the UI.

    Without this the app cannot tell a user whether they are reading real
    Claude prose or placeholder text, which in a product whose entire claim is
    provenance is the one ambiguity least affordable.
    """

    kind: str          # "cli" | "stub"
    label: str         # short, for the header
    detail: str        # one sentence, for the draft notice
    real: bool         # False = placeholder text, not a model's words
    #: What the user should do to reach the real engine, phrased for the
    #: reason they are on the stub. Empty when nothing needs doing. Resolved
    #: here rather than in a template, because the right sentence depends on
    #: *why* the CLI is unavailable and a template cannot know that.
    fix: str = ""
    #: The CLI's own words, for diagnostics. Never the on-screen instruction:
    #: it is a Python exception message, not a sentence for a scientist.
    hint: str = ""


def resolve_engine() -> EngineInfo:
    """Name the engine without ever blocking a page render.

    Every page discloses the engine, and the only way to ask the CLI whether it
    is signed in is to run it — seconds, not milliseconds. So a page render
    never waits: it answers from cache, kicks off a refresh in the background,
    and until the first probe lands it claims the *stub*.

    Claiming the stub while unsure is the safe direction of error. Saying
    "placeholder text" about real Claude prose costs a reader nothing; the
    reverse would put the product's provenance claim behind a guess.

    A forced `cli` is different — that is an operator asking for the truth, so
    it probes synchronously and lets the failure surface.
    """
    choice = (os.environ.get(ENGINE_ENV) or "auto").strip().lower()
    if choice == "cli":
        # Synchronous for the FIRST answer only. An operator who forced `cli`
        # wants the failure surfaced rather than a reassuring placeholder — but
        # that is one answer, not one per click. This branch wrote to the cache
        # and never read it, so every HTML page spawned a fresh probe: a ~330MB
        # binary booting a Node runtime, measured at 15-19s, for the chip in the
        # header. Static files and the JSON API came back in 10ms because they
        # never render a template; a 404 took 16s because it does.
        #
        # A stale answer is bounded by ENGINE_TTL_S, the same window every other
        # mode already accepts, and a cached failure keeps surfacing as a
        # failure.
        cached = _cached_engine(choice)
        if cached is not None:
            return cached
        return _cache_engine(choice, _probe_engine(choice))

    cached = _cached_engine(choice)
    if cached is not None:
        return cached
    if choice == "auto":
        _refresh_engine_async(choice)
        return _stub_engine(hint="", choice=choice)
    return _cache_engine(choice, _probe_engine(choice))


def resolve_engine_now() -> EngineInfo:
    """A definite answer, for starting a run.

    A run takes minutes and its entire output depends on which engine drafted
    it, so this one place is worth waiting for — the alternative is discovering
    twenty sections in that nothing was signed in.

    It still prefers a fresh cached answer. The probe costs ~27s on this
    machine, the startup prime has almost always already paid it, and paying it
    again per run buys nothing: if the CLI died in the last minute, section one
    fails with the same actionable message.
    """
    choice = (os.environ.get(ENGINE_ENV) or "auto").strip().lower()
    cached = _cached_engine(choice)
    if cached is not None:
        return cached
    return _cache_engine(choice, _probe_engine(choice))


#: Model-version strings that mean "no model wrote this". Observed values in
#: the store are `stub` and `stub-claude-sonnet-4-6@stub`; the prefix covers
#: both and anything else the stub client tags itself with later.
_STUB_VERSION_PREFIX = "stub"


def engine_for_run(model_version: str) -> EngineInfo:
    """Which engine drafted THIS run, from the run's own record.

    Not the same question as `resolve_engine()`, and confusing the two is a
    provenance bug rather than a cosmetic one. `resolve_engine()` describes the
    app right now; a draft was written at some point in the past, possibly by a
    different engine. The draft page was rendering the ambient answer, so a
    report drafted by Claude read "PLACEHOLDER text from an offline stub" — and
    once the CLI is signed in, the same code would have labelled every existing
    stub-drafted report as the model's own words. That direction is the one that
    matters: it would put invented prose behind a real provenance claim.
    """
    version = (model_version or "").strip()
    if not version or version.lower().startswith(_STUB_VERSION_PREFIX):
        # `fix` is cleared deliberately. It carries "run `claude`, then
        # `/login`" or "unset REPORTGEN_ENGINE", which are instructions for
        # configuring the app — nonsense attached to a run that already
        # finished. Nothing renders it today; leaving it populated would arm a
        # trap for whoever does. An unrecorded version claims the stub because
        # under-claiming costs a reader nothing and over-claiming voids the
        # only thing this product asserts.
        return dataclasses.replace(_stub_engine(hint="", choice="stub"), fix="")
    return EngineInfo(
        # "model" for anything that is neither the CLI nor the stub — a Vertex
        # model version, once that path exists. `resolve_engine()` only ever
        # returns "cli" or "stub", so this is the one producer of that value.
        kind="cli" if version == "claude-code-cli" else "model",
        label="local Claude" if version == "claude-code-cli" else version,
        detail=(
            "Drafted by the Claude Code CLI on this machine. Prose is the "
            "model's; every table and number is still pulled deterministically "
            "from source."
            if version == "claude-code-cli"
            else f"Drafted by {version}. Prose is the model's; every table and "
            "number is still pulled deterministically from source."
        ),
        real=True,
    )


def prime_engine() -> None:
    """Start the engine probe at boot.

    Without this the first visitor lands inside the ~25s probe window and is
    told the app is "still checking". Kicking it off at startup means the
    answer is already there by the time anyone navigates, while still keeping
    every request non-blocking.
    """
    choice = (os.environ.get(ENGINE_ENV) or "auto").strip().lower()
    if choice == "auto":
        _refresh_engine_async(choice)


def reset_engine_cache() -> None:
    """Forget the cached engine. For tests, and for an explicit re-check."""
    with _ENGINE_LOCK:
        globals()["_ENGINE_CACHE"] = None


def _cached_engine(choice: str) -> EngineInfo | None:
    with _ENGINE_LOCK:
        cached = _ENGINE_CACHE
    if cached is None or cached[1] != choice:
        return None
    if time.monotonic() - cached[0] >= ENGINE_TTL_S:
        return None
    return cached[2]


def _cache_engine(choice: str, engine: EngineInfo) -> EngineInfo:
    with _ENGINE_LOCK:
        globals()["_ENGINE_CACHE"] = (time.monotonic(), choice, engine)
    return engine


def _refresh_engine_async(choice: str) -> None:
    """Probe in the background, at most one probe in flight.

    A daemon thread: a pending engine check must never hold up shutdown.
    """
    global _ENGINE_PROBING
    with _ENGINE_LOCK:
        if _ENGINE_PROBING:
            return
        globals()["_ENGINE_PROBING"] = True

    def work() -> None:
        try:
            _cache_engine(choice, _probe_engine(choice))
        except Exception:  # noqa: BLE001 - a background probe must not crash
            _cache_engine(choice, _stub_engine(hint="", choice=choice))
        finally:
            with _ENGINE_LOCK:
                globals()["_ENGINE_PROBING"] = False

    threading.Thread(target=work, name="engine-probe", daemon=True).start()


def _probe_engine(choice: str) -> EngineInfo:
    """The uncached resolution: may spawn a subprocess."""

    if choice in ("auto", "cli"):
        try:
            client = ClaudeCliLlmClient()
            client.check()
            return EngineInfo(
                kind="cli",
                label="local Claude",
                detail=(
                    "Drafted by the Claude Code CLI on this machine. Prose is "
                    "the model's; every table and number is still pulled "
                    "deterministically from source."
                ),
                real=True,
            )
        except ClaudeCliUnavailable as exc:
            if choice == "cli":
                raise
            hint = str(exc)

    else:
        hint = ""

    return _stub_engine(hint=hint, choice=choice)


#: Reasons a run falls back to the stub, and the sentence that fixes each.
#:
#: These are resolved server-side because the correct instruction depends on
#: why the CLI is unavailable, and getting it wrong is worse than saying
#: nothing: telling someone to run `/login` when they set REPORTGEN_ENGINE=stub
#: themselves sends them chasing a problem that does not exist.
#: Signing in, phrased so the command can actually be pasted.
#:
#: This said "run `claude`" flatly, which is wrong whenever the CLI is not on
#: PATH — and after `npm install -g` on this machine it is not: the npm global
#: bin is absent from the user PATH, so `claude` resolves to nothing. An
#: instruction that fails when followed is worse than no instruction, so the
#: resolved path is substituted when there is one to substitute.
_FIX_NOT_SIGNED_IN = (
    "Open a terminal, run `{binary}`, then `/login`. This page picks it up "
    "within a minute — no restart, and nothing else to configure."
)
_FIX_NOT_INSTALLED = (
    "The Claude Code CLI was not found on this machine. Install it, or point "
    "REPORTGEN_CLAUDE_BIN at it."
)
_FIX_FORCED = (
    "The stub is in force because REPORTGEN_ENGINE is set to `stub`. Unset it "
    "to use the local Claude CLI."
)
_FIX_CHECKING = (
    "Still checking whether the local Claude CLI can generate — it takes a "
    "few seconds after the app starts. Reload to see the answer."
)


def _fix_for(*, choice: str, hint: str) -> str:
    """Turn the CLI's exception message into an instruction for a person."""
    if choice == "stub":
        return _FIX_FORCED
    if not hint:
        return _FIX_CHECKING
    low = hint.lower()
    if "not found" in low or "could not be executed" in low:
        return _FIX_NOT_INSTALLED
    # Name the binary that was actually found. `claude` is only the right thing
    # to type when the CLI is on PATH, and after an npm global install it is
    # not — so the bare word sends someone to a CommandNotFoundException.
    binary = find_claude_binary() or "claude"
    if binary != "claude" and shutil.which("claude"):
        binary = "claude"          # on PATH after all: the short form is kinder
    return _FIX_NOT_SIGNED_IN.format(binary=binary)


def _stub_engine(*, hint: str, choice: str = "auto") -> EngineInfo:
    """The placeholder engine, described as placeholder.

    The wording is deliberate: a reader must not be able to mistake these
    sentences for a model's.
    """
    return EngineInfo(
        kind="stub",
        label="stub text",
        detail=(
            "Section prose is PLACEHOLDER text from an offline stub, not a "
            "model. Retrieval, citations and the audit trail are real; the "
            "sentences are not."
        ),
        real=False,
        fix=_fix_for(choice=choice, hint=hint),
        hint=hint,
    )


#: How many CLI processes to keep warm. Overridable because the trade is real:
#: each warm process is a booted Node runtime holding RSS, and on a memory-tight
#: machine an operator may prefer the ~45s cold cost per call. `0` disables
#: warming entirely.
#:
#: Tests use it to pin the cold path, which they need because the two paths use
#: DIFFERENT subprocess functions — the cold one `run`, the pool one `Popen` —
#: so a fake that patches one does not intercept the other.
CLI_POOL_ENV = "REPORTGEN_CLI_POOL"
CLI_POOL_DEFAULT = 3


def _cli_pool_size() -> int:
    raw = (os.environ.get(CLI_POOL_ENV) or "").strip()
    if not raw:
        return CLI_POOL_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        # A typo should not silently disable warming or crash a run.
        return CLI_POOL_DEFAULT


_CLI_CLIENT: ClaudeCliLlmClient | None = None
_CLI_CLIENT_LOCK = threading.Lock()


def build_llm_client() -> LlmClient:
    """The generation engine: the local Claude CLI if it is usable, else the stub.

    One swap point for all three phases. `VertexLlmClient` slots in here the
    same way when cloud access lands.

    The CLI client is a SINGLETON, and that is a lifecycle requirement rather
    than a micro-optimisation. It owns a pool of pre-warmed CLI processes, and
    this function is called once per run; a fresh client per run would warm
    three more processes and never reap them, so fifty runs would leave a
    hundred and fifty CLI processes resident. Sharing one pool is also safe:
    isolation lives at the level of a single process serving a single call, not
    at the level of the client.
    """
    if resolve_engine_now().kind != "cli":
        return build_stub_client()
    global _CLI_CLIENT
    with _CLI_CLIENT_LOCK:
        if _CLI_CLIENT is None:
            _CLI_CLIENT = ClaudeCliLlmClient(
                ClaudeCliConfig(
                    # Report sections are long; the default 240s covers a
                    # section comfortably without hanging a whole run on one
                    # stall.
                    timeout_s=240.0,
                    pool_size=_cli_pool_size(),
                )
            )
        return _CLI_CLIENT


def shutdown_llm_client() -> None:
    """Reap the warm process pool. Called from the app's lifespan."""
    global _CLI_CLIENT
    with _CLI_CLIENT_LOCK:
        if _CLI_CLIENT is not None:
            _CLI_CLIENT.close()
            _CLI_CLIENT = None


def build_stub_client() -> StubLlmClient:
    """Deterministic offline stub — plan / fill / critique.

    Kept as the fallback and as the engine every test uses: it makes runs
    reproducible and free, which a real model cannot be.
    """
    stub = StubLlmClient(strict=True)

    stub.register_handler(
        lambda r: r.response_schema_name == "PlanOutput",
        lambda r: stub.make_response(
            parsed_json={"overall_summary": "Dev-server stub plan.", "section_plans": []}
        ),
    )

    def _fill(r: LlmRequest) -> LlmResponse:
        msg = r.messages[-1].content
        ids = _CITE_RE.findall(msg)
        m = re.search(r"Target length:\s*(\d+)-(\d+)", msg)
        lo, hi = (int(m.group(1)), int(m.group(2))) if m else (200, 800)
        body = (
            "This section was produced by the local dev-server stub LLM; "
            "wire Vertex AI Claude for real text. "
        ) * max(1, ((lo + hi) // 2) // 20)
        words = body.split()
        if len(words) > hi:
            body = " ".join(words[:hi])
        claims = [{"text": "Stub claim.", "citation_ids": [ids[0]]}] if ids else []
        return stub.make_response(
            parsed_json={"paragraphs": [{"text": body, "claims": claims}]}
        )

    stub.register_handler(lambda r: r.response_schema_name == "FillOutput", _fill)
    stub.register_handler(
        lambda r: r.response_schema_name == "CritiqueOutput",
        lambda r: stub.make_response(parsed_json={"verdict": "pass", "issues": []}),
    )
    return stub


def build_api_gate() -> ApiCallGate:
    """Confluence + ChEMBL + ClinicalTrials + SharePoint mocks. No network.

    Mocks rather than the real connectors, and that choice is visible rather
    than implied: `connector_statuses()` reports what each one actually is, so
    a reader is never left to assume a fixture was the live system. Swapping in
    `SharePointConnector` needs an Entra registration this app cannot provision,
    and the real thing says so from configuration alone rather than failing
    somewhere inside a run.
    """
    registry = ApiConnectorRegistry()
    registry.register(MockConfluenceConnector())
    registry.register(MockChemblConnector())
    registry.register(MockClinicalTrialsConnector())
    registry.register(MockSharePointConnector())
    return ApiCallGate(registry)


def connector_statuses() -> list[ConnectorStatus]:
    """Every configured source, and whether it can be used from here.

    Configuration only — no network. A page render that probes a warehouse is
    the mistake that made every page in this app take sixteen seconds, so
    reachability stays unknown until someone asks for it explicitly.
    """
    out: list[ConnectorStatus] = []
    gate_registry = ApiConnectorRegistry()
    gate_registry.register(MockConfluenceConnector())
    gate_registry.register(MockChemblConnector())
    gate_registry.register(MockClinicalTrialsConnector())
    gate_registry.register(MockSharePointConnector())
    for cid in gate_registry.ids():
        connector = gate_registry.get(cid)
        report = getattr(connector, "status", None)
        if callable(report):
            out.append(report())
        else:
            # A connector with no opinion about itself gets the honest answer
            # rather than an assumed one.
            out.append(
                ConnectorStatus(
                    connector_id=cid,
                    kind="api",
                    configured=True,
                    reachable=None,
                    detail="This connector does not report its own status.",
                )
            )

    # The named-query backend the app actually runs against.
    out.append(
        ConnectorStatus(
            connector_id="local-sqlite",
            kind="bigquery",
            configured=QUERIES_DIR.is_dir(),
            reachable=EDC_SQLITE.is_file() or None,
            detail=(
                f"Named queries run against {EDC_SQLITE.name} on this machine. "
                "The BigQuery and Oracle executors implement the same protocol "
                "and are not wired up here — neither is reachable from this "
                "machine."
            ),
        )
    )
    return out


_QUERY_REGISTRY: NamedQueryRegistry | None = None
_QUERY_REGISTRY_LOCK = threading.Lock()


def query_registry() -> NamedQueryRegistry:
    """Process-wide named-query registry over `samples/synthetic_compound/queries`."""
    global _QUERY_REGISTRY
    with _QUERY_REGISTRY_LOCK:
        if _QUERY_REGISTRY is None:
            registry = NamedQueryRegistry()
            if QUERIES_DIR.is_dir():
                try:
                    registry.load_directory(QUERIES_DIR)
                except Exception:  # pragma: no cover - malformed local YAML
                    pass
            _QUERY_REGISTRY = registry
        return _QUERY_REGISTRY


def build_sql_gate() -> TolerantSqlSafetyGate:
    """Read-only SQLite gate. The approval callback stays fail-closed
    (`deny_all` by default), so inline LLM-drafted SQL never executes."""
    executor = SqliteQueryExecutor(EDC_SQLITE, source="sqlite", read_only=True)
    return TolerantSqlSafetyGate(executor=executor, registry=query_registry())


_CORPUS_CACHE: dict[str, tuple[list[CanonicalDocument], dict[str, list[ParsedChunk]]]] = {}
_CORPUS_LOCK = threading.Lock()


def load_corpus(
    folder: str | Path,
) -> tuple[list[CanonicalDocument], dict[str, list[ParsedChunk]]]:
    """Ingest + parse a local evidence folder. Cached per process, per folder.

    The result is immutable after build and shared read-only across workers.
    Measured on the bundled corpus: 13 documents / 123 chunks / ~0.3 s.
    """
    key = str(Path(folder).resolve())
    with _CORPUS_LOCK:
        cached = _CORPUS_CACHE.get(key)
    if cached is not None:
        return cached

    connector = LocalFileConnector()
    context = ConnectorContext(tenant_id="gsk", team_id="local", run_id="report-agent-ui")
    parsers = default_registry()
    documents: list[CanonicalDocument] = []
    chunks_by_doc: dict[str, list[ParsedChunk]] = {}
    for doc, raw in connector.ingest(key, context):
        try:
            chunks = list(parsers.parse(doc, raw))
        except Exception:
            # An unparseable file must not take the whole folder down.
            continue
        documents.append(doc)
        chunks_by_doc[doc.doc_id] = chunks

    built = (documents, chunks_by_doc)
    with _CORPUS_LOCK:
        _CORPUS_CACHE.setdefault(key, built)
        return _CORPUS_CACHE[key]


def _infer_tags(path: Path) -> list[str]:
    """Mirror of `LocalFileConnector._infer_tags_from_path` (engine is read-only).

    Used by preflight so we can answer "do any documents match these tags?"
    without reading or parsing a single byte.
    """
    anchor = path.anchor.rstrip("\\/")
    return [p.name.lower() for p in path.parents if p.name and p.name != anchor]


@dataclass(frozen=True)
class _ScannedDoc:
    title: str
    path: str
    tags: tuple[str, ...]


def scan_evidence_folder(folder: str | Path) -> list[_ScannedDoc]:
    """Cheap metadata-only walk (no bytes read) used by preflight."""
    root = Path(folder)
    if not root.is_dir():
        return []
    out: list[_ScannedDoc] = []
    try:
        candidates = sorted(root.rglob("*"))
    except OSError:
        return []
    for path in candidates:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if path.suffix.lower() not in _PARSEABLE_SUFFIXES:
            mime, _ = mimetypes.guess_type(path.name)
            if mime is None:
                continue
        out.append(
            _ScannedDoc(
                title=path.stem,
                path=path.resolve().as_posix(),
                tags=tuple(_infer_tags(path)),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Progress plumbing
# ---------------------------------------------------------------------------


class ProgressAuditSink(AuditSink):
    """Audit sink that mirrors generation events into live run progress.

    Two hard rules:
      * the progress callback may never break generation — every exception it
        raises is swallowed;
      * cancellation is checked *before* the callback and propagates as
        `RunCancelled` so the worker can unwind cleanly.
    """

    def __init__(
        self,
        store: AuditStore,
        *,
        on_event: Any,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__(store)
        self._on_event = on_event
        self._cancel = cancel_event

    def emit(self, event: AuditEvent) -> AuditEvent:
        stamped = super().emit(event)
        if self._cancel.is_set():
            raise RunCancelled("cancelled by the user")
        try:
            self._on_event(stamped)
        except Exception:  # noqa: BLE001 - progress must never break generation
            pass
        return stamped


# ---------------------------------------------------------------------------
# Anchoring, numbers, citation presentation (§7.7)
# ---------------------------------------------------------------------------

#: Copied, not imported — `services/generation_orchestrator/critic.py` is
#: read-only for this workstream.
NUMBER_RE = re.compile(r"\b\d[\d,]*(?:\.\d+)?\s*%?")

#: Suppresses the digits inside a compound code (``XYZ-001``, ``CHEMBL203``).
_CODE_CTX_RE = re.compile(r"[A-Za-z]-?$")

#: Sentence terminator: `.`/`!`/`?` followed by whitespace or end of string.
#: Deliberately NOT `[^.!?]+` — that splits "12.5 mg" mid-number and would
#: anchor a citation marker to the fragment "5 mg".
_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s|$)")

_FOLD = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "−": "-",
    " ": " ",
    "…": ".",
}

_STOPWORDS = frozenset(
    """
    the and for with from that this was were are been being have has had not but
    its their which when where while into over under than then they them was
    """.split()
)

_TIER_RANK = {"exact": 0, "normalized": 1, "sentence": 2}
_DICE_THRESHOLD = 0.60


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Whitespace-collapsed, quote-folded, lowercased text + offset map.

    `index_map[i]` is the raw offset of normalized character `i`; the map has
    one extra trailing entry equal to `len(text)` so a normalized end offset
    maps to a raw end offset.
    """
    out: list[str] = []
    index_map: list[int] = []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            out.append(" ")
            index_map.append(i)
            prev_space = True
            continue
        prev_space = False
        folded = _FOLD.get(ch, ch).lower()
        if len(folded) != 1:
            folded = folded[0] if folded else ch
        out.append(folded)
        index_map.append(i)
    index_map.append(len(text))
    return "".join(out), index_map


def _content_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS]


def _dice(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    if len(a) < 2 or len(b) < 2:
        set_a, set_b = set(a), set(b)
    else:
        set_a = {(a[i], a[i + 1]) for i in range(len(a) - 1)}
        set_b = {(b[i], b[i + 1]) for i in range(len(b) - 1)}
    if not set_a or not set_b:
        return 0.0
    return 2 * len(set_a & set_b) / (len(set_a) + len(set_b))


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        s, e = _trim_span(text, start, match.end())
        if e > s:
            spans.append((s, e))
        start = match.end()
    if start < len(text):
        s, e = _trim_span(text, start, len(text))
        if e > s:
            spans.append((s, e))
    return spans


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _find_claim_span(
    paragraph: str,
    norm_paragraph: str,
    index_map: list[int],
    claim_text: str,
) -> tuple[int, int, str] | None:
    """Return (start, end, tier) of the paragraph span backing `claim_text`."""
    needle = claim_text.strip()
    if not needle:
        return None

    # Tier A — exact substring.
    pos = paragraph.find(needle)
    if pos >= 0:
        start, end = _trim_span(paragraph, pos, pos + len(needle))
        if end > start:
            return start, end, "exact"

    # Tier B — normalised substring.
    norm_needle, _ = _normalize_with_map(needle)
    norm_needle = norm_needle.strip()
    if norm_needle:
        pos = norm_paragraph.find(norm_needle)
        if pos >= 0:
            raw_start = index_map[pos]
            raw_end = index_map[min(pos + len(norm_needle), len(index_map) - 1)]
            start, end = _trim_span(paragraph, raw_start, raw_end)
            if end > start:
                return start, end, "normalized"

    # Tier C — best sentence by Dice over content-word bigrams.
    claim_tokens = _content_tokens(needle)
    if claim_tokens:
        best: tuple[float, int, int] | None = None
        for s, e in _sentence_spans(paragraph):
            score = _dice(claim_tokens, _content_tokens(paragraph[s:e]))
            if best is None or score > best[0]:
                best = (score, s, e)
        if best is not None and best[0] >= _DICE_THRESHOLD:
            return best[1], best[2], "sentence"

    return None


def _number_marks(text: str) -> tuple[list[tuple[str, bool]], int]:
    """Split a run of prose into (fragment, is_uncited_number) pairs.

    Digits that are part of an identifier (``XYZ-001``, ``CHEMBL203``) are
    emitted as ordinary text, never as an uncited-number warning — the engine's
    critic does flag them, which is exactly the false positive we suppress here.
    """
    marks: list[tuple[str, bool]] = []
    flagged = 0
    cursor = 0
    for match in NUMBER_RE.finditer(text):
        start, end = match.start(), match.end()
        context = text[max(0, start - 6) : start]
        suppressed = bool(_CODE_CTX_RE.search(context))
        if suppressed:
            continue
        if start > cursor:
            marks.append((text[cursor:start], False))
        marks.append((text[start:end], True))
        flagged += 1
        cursor = end
    if cursor < len(text):
        marks.append((text[cursor:], False))
    if not marks and text:
        marks.append((text, False))
    return marks, flagged


def _number_tokens(text: str) -> list[str]:
    out: list[str] = []
    for match in NUMBER_RE.finditer(text):
        context = text[max(0, match.start() - 6) : match.start()]
        if _CODE_CTX_RE.search(context):
            continue
        token = match.group(0).strip()
        if token and token not in out:
            out.append(token)
    return out


def _number_found_in(token: str, haystack: str) -> bool:
    bare = token.rstrip("%").strip()
    plain = bare.replace(",", "")
    hay_plain = haystack.replace(",", "")
    if bare and bare in haystack:
        return True
    if plain and plain in hay_plain:
        return True
    try:
        value = float(plain)
    except ValueError:
        return False
    for match in NUMBER_RE.finditer(hay_plain):
        candidate = match.group(0).strip().rstrip("%").replace(",", "")
        try:
            if abs(float(candidate) - value) < 1e-9:
                return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Template catalog
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class _ParsedTemplate:
    key: str
    path: Path
    template: ReportTemplate | None
    error: str | None
    description: str
    owner: str
    #: Shape-normalised front-matter tags. Read from the front matter directly
    #: (contract §5.1) so a file that fails to load still groups and sorts.
    #: Taxonomy conformance is applied at render time, in `_card_for`.
    tags: dict[str, list[str]] = field(default_factory=dict)
    updated: str = ""
    updated_ts: float = 0.0


def _read_front_matter(path: Path) -> dict[str, Any]:
    try:
        import yaml

        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        if not match:
            return {}
        loaded = yaml.safe_load(match.group(1))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:  # noqa: BLE001 - front matter is best-effort metadata
        return {}


def _clean_error(message: str) -> str:
    """Strip the absolute path prefix `load_report_doc` bakes into its errors."""
    text = str(message)
    if ": " in text:
        head, _, tail = text.partition(": ")
        if ("\\" in head or "/" in head) and tail:
            return tail
    return text


# --- tags on a card --------------------------------------------------------


_FACET_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VALUE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _shape_tag_map(value: object) -> dict[str, list[str]]:
    """Shape-only, never-raises coercion of a raw `tags:` value.

    Deliberately more tolerant than `ReportTemplate`'s validator: a bad tag
    block must never stop a gallery card from being built, and a card that
    cannot be built is a card the scientist cannot see. Taxonomy conformance
    (aliases, cardinality, defaults) happens later, in `Taxonomy.normalize`.
    """
    if value is None or value == "" or value == []:
        return {}
    raw: dict[str, object] = {}
    if isinstance(value, (list, tuple)):
        for element in value:
            facet, sep, item = str(element).strip().partition(":")
            if sep:
                raw.setdefault(facet.strip(), []).append(item.strip())  # type: ignore[union-attr]
    elif isinstance(value, dict):
        raw = dict(value)
    else:
        return {}

    out: dict[str, list[str]] = {}
    for facet_key, facet_values in raw.items():
        facet = str(facet_key).strip().lower()
        if not _FACET_ID_RE.match(facet):
            continue
        items = facet_values if isinstance(facet_values, (list, tuple)) else [facet_values]
        seen: list[str] = []
        for item in items:
            if item is None:
                continue
            text = str(item).strip().lower()
            if text and _VALUE_ID_RE.match(text) and text not in seen:
                seen.append(text)
        out[facet] = seen
    return out


def _date_to_epoch(value: str) -> float:
    """`2026-08-19` -> epoch seconds (UTC). 0.0 when it is not a date."""
    text = str(value or "").strip()[:10]
    try:
        return (
            datetime.strptime(text, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return 0.0


def _chips_for(
    tags: dict[str, list[str]], taxonomy: Any, *, suppress_facet: str = ""
) -> tuple[list[TagChip], int]:
    """Contract §5.4 — at most `MAX_CARD_CHIPS` chips plus a `+N tags` count.

    The chip budget is a DISPLAY rule only. `card_chip` and `notable` are
    compared generically against configured strings; nothing here branches on
    any particular value.
    """
    candidates: list[TagChip] = []
    for facet in taxonomy.facets:
        if facet.id == suppress_facet or facet.card_chip == "never":
            continue
        notable = tuple(facet.notable or ())
        for value_id in tags.get(facet.id, ()) or ():
            if facet.card_chip == "notable" and value_id not in notable:
                continue
            candidates.append(
                TagChip(
                    facet_id=facet.id,
                    value_id=value_id,
                    label=taxonomy.label_for(facet.id, value_id),
                )
            )
    chips = candidates[:MAX_CARD_CHIPS]
    total = sum(len(v or ()) for v in tags.values())
    return chips, max(total - len(chips), 0)


def _tag_aria(tags: dict[str, list[str]], taxonomy: Any) -> str:
    """Every tag label, in facet order — never abridged by the chip budget."""
    labels: list[str] = []
    for facet in taxonomy.facets:
        for value_id in tags.get(facet.id, ()) or ():
            labels.append(taxonomy.label_for(facet.id, value_id))
    for facet_id, values in tags.items():
        if taxonomy.facet(facet_id) is None:
            labels.extend(values or ())
    return ", ".join(labels)


def _search_haystack(
    *,
    card_bits: Iterable[str],
    tags: dict[str, list[str]],
    taxonomy: Any,
    section_titles: Iterable[str] = (),
    prompts: Iterable[str] = (),
) -> str:
    parts: list[str] = [str(b or "") for b in card_bits]
    for facet_id, values in tags.items():
        for value_id in values or ():
            parts.append(value_id)
            parts.append(taxonomy.label_for(facet_id, value_id))
        parts.append(taxonomy.facet_label(facet_id))
    parts.extend(str(t or "") for t in section_titles)
    parts.extend(str(p or "") for p in prompts)
    return " ".join(p for p in parts if p).casefold()


# ---------------------------------------------------------------------------
# RunStore
# ---------------------------------------------------------------------------


class RunStore:
    """Template catalog + preflight + threaded generation + presenters.

    Thread-safety: one `RLock` guards the in-memory record map. Every mutation
    bumps `RunRecord.version` and rewrites `run.json` atomically, so a browser
    refresh (or a process restart) always sees a coherent state.
    """

    def __init__(
        self,
        root: Path = RUNS_ROOT,
        templates_dir: Path = TEMPLATES_DIR,
        backups_dir: Path = TEMPLATE_BACKUPS_DIR,
        trash_dir: Path = TEMPLATE_TRASH_DIR,
    ) -> None:
        self._root = Path(root)
        self._templates_dir = Path(templates_dir)
        self._backups_dir = Path(backups_dir)
        self._trash_dir = Path(trash_dir)
        self._lock = threading.RLock()
        self._records: dict[str, RunRecord] = {}
        self._metrics: dict[str, dict[str, int]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._futures: dict[str, Future[None]] = {}
        self._draft_cache: dict[str, DraftView] = {}
        self._template_cache: dict[str, tuple[tuple[float, int], _ParsedTemplate]] = {}
        self._template_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_WORKERS, thread_name_prefix="rg-run"
        )
        self._closed = False

        self._root.mkdir(parents=True, exist_ok=True)
        self._rehydrate()
        self.prune(keep=KEEP_RUNS)

    # -- templates ---------------------------------------------------------

    def _parse_template(self, path: Path) -> _ParsedTemplate:
        key = path.stem
        try:
            stat = path.stat()
            stamp = (stat.st_mtime, stat.st_size)
        except OSError:
            stamp = (0.0, 0)
        with self._template_lock:
            cached = self._template_cache.get(key)
            if cached is not None and cached[0] == stamp:
                return cached[1]

        description = ""
        owner = ""
        tags: dict[str, list[str]] = {}
        updated = ""
        front = _read_front_matter(path)
        if front:
            description = " ".join(str(front.get("description", "") or "").split())
            owner = str(front.get("owner", "") or "")
            tags = _shape_tag_map(front.get("tags"))
            raw_updated = front.get("updated")
            if isinstance(raw_updated, (date, datetime)):
                updated = raw_updated.strftime("%Y-%m-%d")
            else:
                updated = str(raw_updated or "").strip()
        updated_ts = _date_to_epoch(updated) or float(stamp[0] or 0.0)

        try:
            template = load_report_doc(path)
            parsed = _ParsedTemplate(
                key, path, template, None, description, owner, tags, updated, updated_ts
            )
        except Exception as exc:  # noqa: BLE001 - ReportDocError + pydantic errors
            parsed = _ParsedTemplate(
                key,
                path,
                None,
                _clean_error(exc),
                description,
                owner,
                tags,
                updated,
                updated_ts,
            )

        with self._template_lock:
            self._template_cache[key] = (stamp, parsed)
        return parsed

    def _user_templates_dir(self, user_id: str) -> Path:
        return self._templates_dir / USER_TEMPLATES_DIR_NAME / user_dir_slug(user_id)

    def _template_paths(self, user_id: str | None = None) -> list[Path]:
        """Universal templates, plus `user_id`'s own if a user is given.

        Never anyone else's. `user_id=None` means "the universal library only",
        which is the right default for anything that is not answering a request
        — a background task has no user and should not inherit one.
        """
        if not self._templates_dir.is_dir():
            return []
        # Non-recursive: the users/ subtree is reached deliberately below, so a
        # stray .md dropped anywhere else under the directory is not silently
        # published to everyone.
        paths = sorted(self._templates_dir.glob("*.md"))
        if user_id:
            mine = self._user_templates_dir(user_id)
            if mine.is_dir():
                paths += sorted(mine.glob("*.md"))
        return paths

    def scope_of(self, path: Path) -> tuple[str, str]:
        """`(scope, owner_slug)` for a template file."""
        parent = Path(path).parent
        if parent.name and parent.parent.name == USER_TEMPLATES_DIR_NAME:
            return "user", parent.name
        return "universal", ""

    def _card_for(self, parsed: _ParsedTemplate) -> TemplateCard:
        taxonomy = load_taxonomy()
        tags, _tag_issues = taxonomy.normalize(parsed.tags)
        tokens = list(taxonomy.tokens_for(tags))
        chips, n_more = _chips_for(tags, taxonomy)
        aria = _tag_aria(tags, taxonomy)
        scope, owned_by = self.scope_of(parsed.path)

        if parsed.template is None:
            return TemplateCard(
                key=parsed.key,
                path=str(parsed.path),
                ok=False,
                error=parsed.error,
                template_id="",
                title=parsed.key,
                description=parsed.description,
                version="",
                owner=parsed.owner,
                n_sections=0,
                form_fields=[],
                source_counts={},
                sources_ready=0,
                sources_total=0,
                readiness="broken",
                readiness_text="Not runnable — this file is not a report template.",
                tags=tags,
                tag_tokens=tokens,
                chips=chips,
                n_more_tags=n_more,
                tag_aria=aria,
                scope=scope,
                owned_by=owned_by,
                updated=parsed.updated,
                updated_ts=parsed.updated_ts,
                search=_search_haystack(
                    card_bits=(parsed.key, parsed.description, parsed.owner),
                    tags=tags,
                    taxonomy=taxonomy,
                ),
            )

        template = parsed.template
        sections = template.all_sections()
        fields = _form_fields(sections)
        specs = _source_specs(
            sections,
            inputs={f.binding_id: f.default for f in fields},
            evidence_folder=CORPUS_DIR,
        )
        counts: dict[str, int] = {}
        for spec in specs:
            counts[spec.kind] = counts.get(spec.kind, 0) + 1
        ready = sum(1 for s in specs if s.status == "ready")
        total = len(specs)

        if total == 0:
            readiness: Readiness = "gaps"
            readiness_text = "No bound data sources — narrative only."
        elif ready == total:
            readiness = "ready"
            readiness_text = f"All {total} {_plural(total, 'source')} ready"
        elif ready == 0:
            readiness = "gaps"
            readiness_text = f"0 of {total} sources ready"
        else:
            readiness = "gaps"
            readiness_text = f"{ready} of {total} sources ready"

        return TemplateCard(
            key=parsed.key,
            path=str(parsed.path),
            ok=True,
            error=None,
            template_id=template.template_id,
            title=template.title,
            description=parsed.description,
            version=template.version,
            owner=parsed.owner,
            n_sections=len(sections),
            form_fields=fields,
            source_counts=counts,
            sources_ready=ready,
            sources_total=total,
            readiness=readiness,
            readiness_text=readiness_text,
            tags=tags,
            tag_tokens=tokens,
            chips=chips,
            n_more_tags=n_more,
            tag_aria=aria,
            scope=scope,
            owned_by=owned_by,
            updated=parsed.updated,
            updated_ts=parsed.updated_ts,
            search=_search_haystack(
                card_bits=(
                    template.title,
                    parsed.description,
                    parsed.owner,
                    parsed.key,
                    template.template_id,
                    template.version,
                ),
                tags=tags,
                taxonomy=taxonomy,
                section_titles=[s.title for s in sections],
                prompts=[f.prompt for f in fields],
            ),
        )

    def list_templates(
        self, user_id: str | None = None
    ) -> tuple[list[TemplateCard], list[TemplateCard]]:
        runnable: list[TemplateCard] = []
        unavailable: list[TemplateCard] = []
        for path in self._template_paths(user_id):
            card = self._card_for(self._parse_template(path))
            (runnable if card.ok else unavailable).append(card)
        # Personal templates first within each group. Someone who made one is
        # looking for it, and a list ordered purely by title buries it among
        # a dozen shared ones.
        runnable.sort(key=lambda c: (c.scope != "user", c.title.lower()))
        unavailable.sort(key=lambda c: (c.scope != "user", c.key.lower()))
        return runnable, unavailable

    def _parsed_or_raise(
        self, key: str, user_id: str | None = None
    ) -> _ParsedTemplate:
        if "/" in key or "\\" in key:
            raise KeyError(f"unknown report template: {key!r}")
        try:
            path = self.template_path(key, user_id)
        except KeyError:
            raise KeyError(f"unknown report template: {key!r}") from None
        if not path.is_file():
            raise KeyError(f"unknown report template: {key!r}")
        return self._parse_template(path)

    def get_template(self, key: str, user_id: str | None = None) -> TemplateCard:
        return self._card_for(self._parsed_or_raise(key, user_id))

    def _template_or_raise(
        self, key: str, user_id: str | None = None
    ) -> ReportTemplate:
        parsed = self._parsed_or_raise(key, user_id)
        if parsed.template is None:
            raise ValueError(parsed.error or f"{key} could not be parsed")
        return parsed.template

    def template_outline(
        self, key: str, user_id: str | None = None
    ) -> list[SectionOutline]:
        parsed = self._parsed_or_raise(key, user_id)
        if parsed.template is None:
            return []
        out: list[SectionOutline] = []
        for section in parsed.template.all_sections():
            out.append(
                SectionOutline(
                    section_id=section.section_id,
                    title=section.title,
                    level=section.level,
                    instruction=section.generation.prompt_template or "",
                    source_ids=[
                        b.binding_id
                        for b in section.data_bindings
                        if not isinstance(b, FreeTextInputBinding)
                    ],
                    mode=section.generation.mode.value,
                )
            )
        return out

    def default_inputs(self, key: str, user_id: str | None = None) -> dict[str, str]:
        card = self.get_template(key, user_id)
        return {f.binding_id: f.default for f in card.form_fields}

    # -- gallery: group / sort / filter ------------------------------------

    def gallery_view(
        self,
        *,
        group: str | None = None,
        sort: str | None = None,
        tags: Iterable[str] = (),
        q: str = "",
        user_id: str | None = None,
    ) -> GalleryView:
        """Everything `GET /` renders, filtered and grouped SERVER-SIDE.

        Non-matching cards are omitted, never rendered-then-hidden, so there
        is exactly one predicate in exactly one language (contract R7).

        `user_id` widens the library to include that person's own templates.
        Omitting it shows the universal ones only — the safe direction, since
        the failure mode of forgetting to pass it is a template the owner cannot
        find, not someone else's private template on a shared page.
        """
        runnable, unavailable = self.list_templates(user_id)
        return _build_gallery_view(
            runnable,
            unavailable,
            load_taxonomy(),
            group=group,
            sort=sort,
            tags=tags,
            q=q,
        )

    # -- authoring ---------------------------------------------------------

    @property
    def templates_dir(self) -> Path:
        return self._templates_dir

    @property
    def trash_dir(self) -> Path:
        return self._trash_dir

    def template_path(
        self, key: str, user_id: str | None = None, *, scope: str | None = None
    ) -> Path:
        """Where `{key}.md` lives, or KeyError for anything unsafe.

        Keys are unique across both scopes rather than shadowing each other, and
        that is a provenance decision rather than a convenience one: a run
        record stores `template_key`, so if a universal and a personal template
        could share a key, an existing report would no longer say which template
        produced it. `new_template_key` enforces the uniqueness on the way in.

        With `scope` given, this returns where a template *would* live — used
        when creating. Without it, the caller's own directory is checked first
        and the universal library second.
        """
        if not TEMPLATE_KEY_RE.match(str(key or "")):
            raise KeyError(f"unknown report template: {key!r}")
        if scope == "user":
            if not user_id:
                raise KeyError("a user-scoped template needs a user")
            return self._user_templates_dir(user_id) / f"{key}.md"
        if scope == "universal":
            return self._templates_dir / f"{key}.md"
        if user_id:
            mine = self._user_templates_dir(user_id) / f"{key}.md"
            if mine.is_file():
                return mine
        return self._templates_dir / f"{key}.md"

    def template_keys(self, user_id: str | None = None) -> list[str]:
        """Every `*.md` stem visible to `user_id` right now — re-globbed on
        every call, so a template created through the editor is visible
        immediately."""
        return [p.stem for p in self._template_paths(user_id)]

    def template_exists(self, key: str, user_id: str | None = None) -> bool:
        try:
            return self.template_path(key, user_id).is_file()
        except KeyError:
            return False

    def invalidate_template(self, key: str) -> None:
        """Drop one key from the parse cache after the file changed on disk."""
        with self._template_lock:
            self._template_cache.pop(key, None)

    def raw_template_text(self, key: str) -> str:
        path = self.template_path(key)
        if not path.is_file():
            raise KeyError(f"unknown report template: {key!r}")
        return path.read_text(encoding="utf-8")

    def draft_for(self, key: str, user_id: str | None = None) -> Any:
        """`TemplateDraft` for an existing file. KeyError when it is missing;
        `TemplateWriteError` when the file is not a report template."""
        path = self.template_path(key, user_id)
        if not path.is_file():
            raise KeyError(f"unknown report template: {key!r}")
        return draft_from_path(path)

    def runs_using_template(self, key: str) -> int:
        with self._lock:
            return sum(1 for r in self._records.values() if r.template_key == key)

    def save_draft(
        self,
        draft: Any,
        *,
        create: bool,
        expected_sha256: str | None = None,
        user_id: str | None = None,
        scope: str | None = None,
    ) -> Any:
        """The only path in the UI that writes into `report-templates/`.

        On create, `scope` decides where the file lands. On an edit it is left
        alone: a template does not change scope by being saved, because that
        would let a save quietly publish someone's personal draft to everyone
        or withdraw a shared one. Moving between scopes is its own action.
        """
        key = draft.report_type
        if create:
            if scope == "user" and not user_id:
                raise ValueError("a personal template needs a user")
            path = self.template_path(key, user_id, scope=scope or "universal")
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path = self.template_path(key, user_id)
        result = write_template(
            draft,
            path,
            facet_order=load_taxonomy().facet_order_ids(),
            backup_dir=self._backups_dir,
            expected_sha256=expected_sha256,
            create=create,
        )
        self.invalidate_template(draft.report_type)
        return result

    def delete_template(self, key: str, user_id: str | None = None) -> Path:
        """Back the file up, then move it to the trash folder. Recoverable."""
        path = self.template_path(key, user_id)
        if not path.is_file():
            raise KeyError(f"unknown report template: {key!r}")
        backup_template(path, self._backups_dir)
        moved = trash_template(path, self._trash_dir)
        self.invalidate_template(key)
        return moved

    def trashed_template(self, key: str) -> Path | None:
        try:
            self.template_path(key)
        except KeyError:
            return None
        return find_trashed(key, self._trash_dir)

    def undelete_template(self, key: str) -> Path:
        trashed = self.trashed_template(key)
        if trashed is None:
            raise KeyError(f"nothing in the trash for {key!r}")
        dest = self.template_path(key)
        restore_trashed(trashed, dest)
        self.invalidate_template(key)
        return dest

    def suggest_template_key(self, base: str) -> str:
        """A free `report_type` derived from `base`, for Clone."""
        stem = re.sub(r"[^a-z0-9_]+", "_", str(base or "").lower()).strip("_")[:52]
        stem = stem or "new_template"
        if not stem[0].isalpha():
            stem = f"t_{stem}"[:56]
        # Both scopes, because keys are unique across them — a personal copy
        # that collides with a universal key would make an existing run record
        # ambiguous about which template drafted it.
        taken = {k.lower() for k in self.template_keys()} | {
            p.stem.lower()
            for p in (self._templates_dir / USER_TEMPLATES_DIR_NAME).glob("*/*.md")
        }
        candidate = f"{stem}_copy"
        n = 2
        while candidate.lower() in taken:
            candidate = f"{stem}_copy{n}"
            n += 1
        return candidate

    # -- evidence folder ---------------------------------------------------

    def describe_evidence(
        self, folder: str | None = None
    ) -> tuple[str, str, str | None]:
        """(resolved_path, human_label, inline_error).

        Never raises and never blocks the primary action: an unusable folder
        falls back to the bundled synthetic corpus and reports the reason.
        """
        raw = (folder or "").strip()
        error: str | None = None
        target = CORPUS_DIR
        if raw:
            candidate = Path(raw).expanduser()
            if candidate.is_dir():
                target = candidate
            else:
                error = (
                    f"{raw} is not a readable folder — using the bundled sample "
                    "corpus instead."
                )
        resolved = target.resolve()
        docs = scan_evidence_folder(resolved)
        is_sample = resolved == CORPUS_DIR.resolve()
        name = "Sample corpus (synthetic XYZ-001)" if is_sample else resolved.name
        label = f"{name} · {len(docs)} {_plural(len(docs), 'document')}"
        with _CORPUS_LOCK:
            cached = _CORPUS_CACHE.get(str(resolved))
        if cached is not None:
            n_chunks = sum(len(c) for c in cached[1].values())
            label += f" · {n_chunks} {_plural(n_chunks, 'chunk')}"
        return str(resolved), label, error

    def _resolve_evidence(self, folder: str | None) -> tuple[Path, PreflightIssue | None]:
        raw = (folder or "").strip()
        if not raw:
            return CORPUS_DIR.resolve(), None
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            return candidate.resolve(), None
        return (
            CORPUS_DIR.resolve(),
            PreflightIssue(
                severity="warning",
                code="EVIDENCE_FOLDER_UNREADABLE",
                binding_id="evidence_folder",
                section_id="",
                message=(
                    f"Evidence folder {raw!r} could not be read; the bundled "
                    "sample corpus was used instead."
                ),
                fix_hint=(
                    "Point this at a folder of .pdf / .docx / .xlsx files on "
                    "this machine, or leave it blank for the sample corpus."
                ),
            ),
        )

    # -- preflight ---------------------------------------------------------

    def preflight(
        self,
        key: str,
        inputs: dict[str, str],
        evidence_folder: str | None = None,
        user_id: str | None = None,
    ) -> PreflightReport:
        parsed = self._parsed_or_raise(key, user_id)
        folder, folder_issue = self._resolve_evidence(evidence_folder)
        if parsed.template is None:
            return PreflightReport(
                verdict="blocked",
                headline="This file is not a runnable report template.",
                sources=[],
                issues=[
                    PreflightIssue(
                        severity="blocker",
                        code="TEMPLATE_UNPARSEABLE",
                        binding_id="",
                        section_id="",
                        message=parsed.error or "The template could not be parsed.",
                        fix_hint="Fix the template file and reload this page.",
                    )
                ],
                sections_without_data=[],
                blocked=True,
            )

        sections = parsed.template.all_sections()
        fields = _form_fields(sections)
        cleaned = {f.binding_id: (inputs.get(f.binding_id) or "").strip() for f in fields}
        specs, codes = _source_specs_with_codes(
            sections, inputs=cleaned, evidence_folder=folder
        )

        issues: list[PreflightIssue] = []
        for f in fields:
            if f.required and not cleaned.get(f.binding_id):
                issues.append(
                    PreflightIssue(
                        severity="blocker",
                        code="MISSING_REQUIRED_INPUT",
                        binding_id=f.binding_id,
                        section_id="",
                        message=f"{f.prompt} is required.",
                        fix_hint="Enter a value to continue.",
                    )
                )
        if folder_issue is not None:
            issues.append(folder_issue)
        for spec in specs:
            if spec.status == "ready":
                continue
            issues.append(
                PreflightIssue(
                    severity="warning",
                    code=codes.get(spec.binding_id, "SOURCE_NOT_READY"),
                    binding_id=spec.binding_id,
                    section_id=spec.section_ids[0] if spec.section_ids else "",
                    message=spec.status_text,
                    fix_hint=spec.fix_hint,
                )
            )

        by_binding = {s.binding_id: s for s in specs}
        without_data: list[str] = []
        for section in sections:
            bound = [
                b.binding_id
                for b in section.data_bindings
                if not isinstance(b, FreeTextInputBinding)
            ]
            if not bound or all(
                by_binding.get(bid) is None or by_binding[bid].status != "ready"
                for bid in bound
            ):
                without_data.append(section.title)

        blocked = any(i.severity == "blocker" for i in issues)
        ready = sum(1 for s in specs if s.status == "ready")
        total = len(specs)
        if blocked:
            verdict = "blocked"
            headline = "Fill in the required inputs before drafting."
        elif ready == total and not without_data:
            verdict = "ready"
            headline = (
                f"All {total} {_plural(total, 'source')} resolved — every section "
                "has data to cite."
            )
        else:
            verdict = "gaps"
            n_gap = len(without_data)
            headline = (
                f"{ready} of {total} sources ready. "
                f"{n_gap} {_plural(n_gap, 'section')} will be drafted with no "
                "source data — treat those as unverified."
            )
        issues.sort(key=lambda i: (0 if i.severity == "blocker" else 1, i.binding_id))
        return PreflightReport(
            verdict=verdict,
            headline=headline,
            sources=specs,
            issues=issues,
            sections_without_data=without_data,
            blocked=blocked,
        )

    # -- run lifecycle -----------------------------------------------------

    def validate_inputs(
        self, key: str, raw: dict[str, str], user_id: str | None = None
    ) -> tuple[dict[str, str], dict[str, str]]:
        card = self.get_template(key, user_id)
        cleaned: dict[str, str] = {}
        errors: dict[str, str] = {}
        for f in card.form_fields:
            value = str(raw.get(f.binding_id, "") or "").strip()
            if not value:
                if f.required:
                    errors[f.binding_id] = f"{f.prompt} is required."
                continue
            if len(value) > MAX_INPUT_LEN:
                errors[f.binding_id] = (
                    f"Keep this under {MAX_INPUT_LEN} characters "
                    f"(currently {len(value)})."
                )
                cleaned[f.binding_id] = value[:MAX_INPUT_LEN]
                continue
            cleaned[f.binding_id] = value
        # Unknown keys are dropped, not surfaced — the form posts extras
        # (evidence_folder) that are not template inputs.
        return cleaned, errors

    def create(
        self,
        key: str,
        inputs: dict[str, str],
        evidence_folder: str | None = None,
        owner: str = "",
    ) -> RunRecord:
        card = self.get_template(key, owner or None)
        if not card.ok:
            raise ValueError(card.error or "This template cannot be run.")

        cleaned, errors = self.validate_inputs(key, inputs, owner or None)
        if errors:
            raise ValueError("; ".join(f"{k}: {v}" for k, v in sorted(errors.items())))

        report = self.preflight(key, cleaned, evidence_folder, owner or None)
        if report.blocked:
            raise ValueError(report.headline)

        folder, _ = self._resolve_evidence(evidence_folder)
        template = self._template_or_raise(key, owner or None)

        run_id = uuid.uuid4().hex[:12]
        created = _iso()
        primary = _primary_input(cleaned)
        sections = [
            SectionProgress(
                section_id=s.section_id,
                title=s.title,
                level=s.level,
                status=(
                    "skipped"
                    if s.generation.mode
                    in (GenerationMode.DETERMINISTIC, GenerationMode.MANUAL)
                    else "pending"
                ),
                status_label=(
                    SECTION_LABEL["skipped"]
                    if s.generation.mode
                    in (GenerationMode.DETERMINISTIC, GenerationMode.MANUAL)
                    else SECTION_LABEL["pending"]
                ),
            )
            for s in template.all_sections()
        ]
        record = RunRecord(
            run_id=run_id,
            template_key=key,
            template_title=card.title,
            template_version=card.version,
            template_id=card.template_id,
            title=f"{card.title} — {primary}" if primary else card.title,
            inputs=cleaned,
            evidence_folder=str(folder),
            status="queued",
            created_at=created,
            sections=sections,
            preflight=list(report.issues),
            prompt_version=PROMPT_VERSION,
            compliance_mode="rd",
            owner=owner,
        )

        with self._lock:
            self._records[run_id] = record
            self._cancel_events[run_id] = threading.Event()
            record.version = 1
            self._flush(record)
            if self._closed:
                raise RuntimeError("run store is shutting down")
            self._futures[run_id] = self._executor.submit(self._worker, run_id)
            return copy.deepcopy(record)

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise KeyError(f"unknown run: {run_id!r}")
            return copy.deepcopy(record)

    def summary(self, run_id: str) -> RunSummary:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise KeyError(f"unknown run: {run_id!r}")
            return self._summary_locked(record)

    def list_runs(self, limit: int = 200) -> list[RunSummary]:
        with self._lock:
            records = sorted(
                self._records.values(), key=lambda r: r.created_at, reverse=True
            )
            return [self._summary_locked(r) for r in records[: max(0, limit)]]

    def recent(self, limit: int = 3) -> list[RunSummary]:
        return self.list_runs(limit=limit)

    def progress_payload(self, run_id: str) -> dict[str, object]:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise KeyError(f"unknown run: {run_id!r}")
            record = copy.deepcopy(record)

        total = len(record.sections)
        done = sum(1 for s in record.sections if s.status in ("passed", "skipped"))
        failed = sum(1 for s in record.sections if s.status == "failed")
        skipped = sum(1 for s in record.sections if s.status == "skipped")
        percent = int(round(100 * (done + failed) / total)) if total else 100
        terminal = record.status in TERMINAL_STATUSES

        return {
            "run_id": record.run_id,
            "version": record.version,
            "status": record.status,
            "status_label": STATUS_LABEL.get(record.status, record.status),
            "terminal": terminal,
            "poll_after_ms": 0 if terminal else POLL_AFTER_MS.get(record.status, 1500),
            "template_key": record.template_key,
            "template_title": record.template_title,
            "template_version": record.template_version,
            "title": record.title,
            "inputs": dict(record.inputs),
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "phase": PHASE_FOR_STATUS.get(record.status, "draft"),
            "progress": {
                "sections_total": total,
                "sections_done": done,
                "sections_failed": failed,
                "sections_skipped": skipped,
                "percent": percent if not terminal else 100,
            },
            "sections": [
                {
                    "section_id": s.section_id,
                    "title": s.title,
                    "level": s.level,
                    "status": s.status,
                    "status_label": s.status_label,
                    "attempts": s.attempts,
                    "n_paragraphs": s.n_paragraphs,
                    "n_citations": s.n_citations,
                    "notes": [_truncate(n, 240) for n in s.notes],
                }
                for s in record.sections
            ],
            "preflight": [i.to_dict() for i in record.preflight],
            "totals": {
                "documents": record.n_documents,
                "chunks": record.n_chunks,
                "citations": record.n_citations,
                "audit_events": record.n_audit_events,
            },
            "instance_id": record.instance_id,
            "model_version": record.model_version,
            "error": record.error.to_dict() if record.error else None,
            "result_url": f"/api/runs/{record.run_id}" if terminal else None,
            "html_url": f"/runs/{record.run_id}",
        }

    def result_payload(self, run_id: str) -> dict[str, object]:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise KeyError(f"unknown run: {run_id!r}")
            if record.status not in TERMINAL_STATUSES:
                raise RunNotTerminal(record.status)
            summary = self._summary_locked(record)
        payload = self._read_json(self._run_dir(run_id) / "result.json") or {
            "instance": None,
            "citations": [],
            "audit_events": [],
        }
        return {
            "run": summary.to_dict(),
            "instance": payload.get("instance"),
            "citations": payload.get("citations", []),
            "audit_events": payload.get("audit_events", []),
        }

    def draft_view(self, run_id: str) -> DraftView | None:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise KeyError(f"unknown run: {run_id!r}")
            if record.status not in TERMINAL_STATUSES:
                return None
            cached = self._draft_cache.get(run_id)
            if cached is not None:
                return cached
            snapshot = copy.deepcopy(record)

        result = self._read_json(self._run_dir(run_id) / "result.json")
        if not result or not result.get("instance"):
            return None
        ledger_raw = self._read_json(self._run_dir(run_id) / "sources.json") or []
        view = _build_draft_view(snapshot, result, ledger_raw)
        with self._lock:
            self._draft_cache[run_id] = view
            self._metrics[run_id] = _metrics_from_draft(view, snapshot)
            self._records[run_id].version += 1
            self._flush(self._records[run_id])
        return view

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            record = self._records.get(run_id)
            if record is None or record.status in TERMINAL_STATUSES:
                return False
            event = self._cancel_events.get(run_id)
            if event is not None:
                event.set()
            future = self._futures.get(run_id)
            if future is not None and future.cancel():
                self._finish_locked(record, "cancelled", None)
            return True

    def delete(self, run_id: str) -> bool:
        with self._lock:
            record = self._records.pop(run_id, None)
            event = self._cancel_events.pop(run_id, None)
            self._futures.pop(run_id, None)
            self._draft_cache.pop(run_id, None)
            self._metrics.pop(run_id, None)
        if event is not None:
            event.set()
        if record is None:
            return False
        _rmtree(self._run_dir(run_id))
        return True

    # -- exports / files ---------------------------------------------------

    def markdown_export(self, run_id: str) -> str:
        summary = self.summary(run_id)
        record = self.get(run_id)
        draft = self.draft_view(run_id)
        return _markdown_export(summary, record, draft)

    def citations_csv(self, run_id: str) -> str:
        summary = self.summary(run_id)
        draft = self.draft_view(run_id)
        return _citations_csv(summary, draft)

    def source_path(self, run_id: str, doc_id: str) -> Path | None:
        try:
            record = self.get(run_id)
        except KeyError:
            return None
        raw = str(doc_id or "")
        for prefix in ("local://local::", "local://", "local::"):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :]
                break
        if not raw:
            return None
        try:
            candidate = Path(raw).resolve()
            root = Path(record.evidence_folder).resolve()
        except OSError:
            return None
        if not candidate.is_file():
            return None
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    # -- demo + shutdown ---------------------------------------------------

    def ib_demo(self) -> dict[str, object]:
        """The synthetic Investigator's Brochure demo — unchanged shape.

        Moved verbatim from `main.py::generate_ib_demo`.
        """
        from collections import Counter

        template = ReportTemplate.model_validate(
            json.loads(IB_TEMPLATE.read_text(encoding="utf-8"))
        )
        documents, chunks_by_doc = load_corpus(CORPUS_DIR)
        generator = ReportGenerator(
            fill_client=build_llm_client(),
            api_gate=build_api_gate(),
            max_retries_per_section=1,
        )
        result = generator.generate(
            template=template,
            documents=list(documents),
            chunks_by_doc=dict(chunks_by_doc),
            free_text_inputs={
                "product_name": "XYZ-001",
                "compound_id": "XYZ-001",
                "target_name": "Kinase Z",
                "indication_keyword": "Kinase Z",
                "sponsor_name": "Acme Therapeutics (synthetic)",
                "ib_edition": "Edition 1.0",
                "release_date": "2026-05-27",
            },
            project_id="dev/api-demo",
            tenant_id="gsk",
            actor_id="api-gateway-dev",
        )
        action_counts = Counter(e.action.value for e in result.audit_events)
        return {
            "instance_id": result.instance.instance_id,
            "template": (
                f"{result.instance.template_id}@{result.instance.template_version}"
            ),
            "documents_ingested": len(documents),
            "chunks": sum(len(c) for c in chunks_by_doc.values()),
            "sections": len(template.all_sections()),
            "citations": len(result.citations),
            "audit_events": len(result.audit_events),
            "audit_by_action": dict(action_counts),
            "note": (
                "Generated with the dev-server StubLlmClient. Wire Vertex AI "
                "Claude (VertexLlmClient) for real text."
            ),
        }

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            self._closed = True
            for event in self._cancel_events.values():
                event.set()
        try:
            self._executor.shutdown(wait=wait, cancel_futures=True)
        except TypeError:  # pragma: no cover - Python < 3.9
            self._executor.shutdown(wait=wait)

    def prune(self, keep: int = KEEP_RUNS) -> int:
        """Drop the oldest runs beyond `keep`. Returns the number removed."""
        with self._lock:
            records = sorted(
                self._records.values(), key=lambda r: r.created_at, reverse=True
            )
            doomed = [
                r.run_id
                for r in records[keep:]
                if r.status in TERMINAL_STATUSES
            ]
        for run_id in doomed:
            self.delete(run_id)
        return len(doomed)

    # -- internals: persistence -------------------------------------------

    def _run_dir(self, run_id: str) -> Path:
        return self._root / run_id

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | list[Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _flush(self, record: RunRecord) -> None:
        """Write `run.json`. Caller holds the lock."""
        payload = record.to_dict()
        payload["metrics"] = self._metrics.get(record.run_id, {})
        try:
            self._write_json(self._run_dir(record.run_id) / "run.json", payload)
        except OSError:
            pass

    def _rehydrate(self) -> None:
        for run_json in sorted(self._root.glob("*/run.json")):
            run_id = run_json.parent.name
            payload = self._read_json(run_json)
            if not isinstance(payload, dict):
                self._records[run_id] = _unreadable_record(run_id)
                continue
            try:
                record = _record_from_dict(payload)
            except Exception:  # noqa: BLE001 - a corrupt file must not kill boot
                self._records[run_id] = _unreadable_record(run_id)
                continue
            metrics = payload.get("metrics")
            if isinstance(metrics, dict):
                self._metrics[run_id] = {
                    str(k): int(v) for k, v in metrics.items() if isinstance(v, (int, float))
                }
            if record.status not in TERMINAL_STATUSES:
                record.status = "interrupted"
                record.finished_at = record.finished_at or _iso()
                record.error = RunError(
                    kind="process_restart",
                    message=(
                        "The app restarted while this run was still drafting, so "
                        "it was stopped."
                    ),
                    detail=(
                        "No worker thread survives a restart. Start the run again "
                        "from the template — nothing was written to any source."
                    ),
                )
                for section in record.sections:
                    if section.status in ("running", "retrying"):
                        section.status = "failed"
                        section.status_label = SECTION_LABEL["failed"]
                    elif section.status == "pending":
                        section.status = "cancelled"
                        section.status_label = SECTION_LABEL["cancelled"]
                record.version += 1
                self._records[run_id] = record
                self._flush(record)
            else:
                self._records[run_id] = record
            self._cancel_events[run_id] = threading.Event()

    # -- internals: mutation ----------------------------------------------

    def _mutate(self, run_id: str, mutator: Any) -> None:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return
            mutator(record)
            record.version += 1
            self._flush(record)

    def _finish_locked(
        self, record: RunRecord, status: RunStatus, error: RunError | None
    ) -> None:
        record.status = status
        record.finished_at = _iso()
        record.error = error
        for section in record.sections:
            if section.status in ("running", "retrying"):
                section.status = "failed" if status == "failed" else "cancelled"
                section.status_label = SECTION_LABEL[section.status]
                section.finished_at = record.finished_at
            elif section.status == "pending":
                section.status = "cancelled"
                section.status_label = SECTION_LABEL["cancelled"]
        record.version += 1
        self._flush(record)

    def _summary_locked(self, record: RunRecord) -> RunSummary:
        metrics = self._metrics.get(record.run_id, {})
        started = _parse_iso(record.started_at)
        finished = _parse_iso(record.finished_at)
        duration = (finished - started).total_seconds() if started and finished else None
        n_sections = len(record.sections)
        n_cited = metrics.get("n_sections_cited", 0)
        return RunSummary(
            run_id=record.run_id,
            template_key=record.template_key,
            template_title=record.template_title,
            template_version=record.template_version,
            title=record.title,
            inputs=dict(record.inputs),
            primary_input=_primary_input(record.inputs),
            evidence_folder=record.evidence_folder,
            status=record.status,
            status_label=STATUS_LABEL.get(record.status, record.status),
            status_state=STATUS_STATE.get(record.status, "neutral"),
            terminal=record.status in TERMINAL_STATUSES,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            created_human=_human_ts(record.created_at),
            duration_s=duration,
            duration_human=_human_duration(duration),
            model_version=record.model_version,
            instance_id=record.instance_id,
            n_sections=n_sections,
            n_sections_cited=n_cited,
            n_sections_no_data=metrics.get("n_sections_no_data", 0),
            n_sections_failed=metrics.get(
                "n_sections_failed",
                sum(1 for s in record.sections if s.status == "failed"),
            ),
            n_claims=metrics.get("n_claims", 0),
            n_claims_cited=metrics.get("n_claims_cited", 0),
            n_uncited_numbers=metrics.get("n_uncited_numbers", 0),
            n_citations=metrics.get("n_citations", record.n_citations),
            n_bindings_resolved=metrics.get("n_bindings_resolved", 0),
            n_bindings_deferred=metrics.get("n_bindings_deferred", 0),
            coverage_text=f"{n_cited}/{n_sections} sections cited"
            if n_sections
            else "no sections",
            owner=record.owner,
        )

    # -- internals: the worker --------------------------------------------

    def _worker(self, run_id: str) -> None:
        cancel = self._cancel_events.get(run_id) or threading.Event()
        try:
            self._run_generation(run_id, cancel)
        except RunCancelled:
            with self._lock:
                record = self._records.get(run_id)
                if record is not None and record.status not in TERMINAL_STATUSES:
                    self._finish_locked(record, "cancelled", None)
        except BaseException as exc:  # noqa: BLE001 - a run must never hang forever
            detail = "".join(traceback.format_exception(exc))[-4000:]
            with self._lock:
                record = self._records.get(run_id)
                if record is not None and record.status not in TERMINAL_STATUSES:
                    self._finish_locked(
                        record,
                        "failed",
                        RunError(
                            kind=type(exc).__name__,
                            message=_truncate(str(exc) or type(exc).__name__, 400),
                            detail=detail,
                        ),
                    )
            if not isinstance(exc, Exception):
                raise
        finally:
            with self._lock:
                self._futures.pop(run_id, None)

    def _run_generation(self, run_id: str, cancel: threading.Event) -> None:
        record = self.get(run_id)
        if cancel.is_set():
            raise RunCancelled("cancelled before start")

        # `record.owner`, not an ambient user: this runs on a worker thread
        # with no request, and a run started from someone's personal template
        # has to keep resolving it after they have closed the tab.
        template = self._template_or_raise(record.template_key, record.owner or None)
        section_index = {s.section_id: s for s in template.all_sections()}

        self._mutate(
            run_id,
            lambda r: (
                setattr(r, "status", "preflight"),
                setattr(r, "started_at", _iso()),
            ),
        )

        # --- ingest -------------------------------------------------------
        self._mutate(run_id, lambda r: setattr(r, "status", "ingesting"))
        documents, chunks_by_doc = load_corpus(record.evidence_folder)
        n_chunks = sum(len(c) for c in chunks_by_doc.values())
        self._mutate(
            run_id,
            lambda r: (
                setattr(r, "n_documents", len(documents)),
                setattr(r, "n_chunks", n_chunks),
            ),
        )
        if cancel.is_set():
            raise RunCancelled("cancelled during ingestion")

        # --- wiring -------------------------------------------------------
        sql_gate = build_sql_gate()
        api_gate = build_api_gate()
        store = InMemoryAuditStore()
        sink = ProgressAuditSink(
            store,
            on_event=lambda e: self._on_audit_event(run_id, e),
            cancel_event=cancel,
        )
        generator = ReportGenerator(
            fill_client=build_llm_client(),
            audit_sink=sink,
            safety_gate=sql_gate,
            api_gate=api_gate,
            max_retries_per_section=MAX_RETRIES_PER_SECTION,
        )

        self._mutate(run_id, lambda r: setattr(r, "status", "planning"))
        result = generator.generate(
            template=template,
            documents=list(documents),
            chunks_by_doc=dict(chunks_by_doc),
            free_text_inputs=dict(record.inputs),
            compliance_mode="rd",
            project_id=f"local/{run_id}",
            tenant_id="gsk",
            actor_id="report-generator-ui",
        )
        if cancel.is_set():
            raise RunCancelled("cancelled during generation")

        # --- persist the engine output -----------------------------------
        payload = {
            "instance": result.instance.model_dump(mode="json"),
            "citations": [c.model_dump(mode="json") for c in result.citations],
            "audit_events": [e.model_dump(mode="json") for e in result.audit_events],
            # The figure each section asked for, recorded on the run rather than
            # read back off the template at view time. Editing a template must
            # not silently redraw a report that shipped months ago — the same
            # reason model_version is stored per run instead of resolved from
            # the ambient engine.
            "visuals": {
                s.section_id: s.visual.model_dump(mode="json")
                for s in _iter_template_sections(template)
                if s.visual is not None
            },
        }
        self._write_json(self._run_dir(run_id) / "result.json", payload)

        # --- sources ledger (the GeneratedTable the engine never emits) ---
        ledger = _build_ledger(
            template=template,
            inputs=dict(record.inputs),
            documents=documents,
            chunks_by_doc=chunks_by_doc,
            citations=payload["citations"],
            sql_gate=sql_gate,
            api_gate=api_gate,
            evidence_folder=Path(record.evidence_folder),
        )
        self._write_json(
            self._run_dir(run_id) / "sources.json", [row.to_dict() for row in ledger]
        )

        model_version = _model_version_from_events(result.audit_events)
        n_citations = len(result.citations)
        n_events = len(result.audit_events)
        instance_id = result.instance.instance_id
        plan_summary = result.instance.plan_summary
        finished = _iso()

        # Build the review view BEFORE flipping the run to `completed`, so the
        # history table never sees a terminal run with empty metrics.
        snapshot = self.get(run_id)
        snapshot.instance_id = instance_id
        snapshot.model_version = model_version
        snapshot.n_citations = n_citations
        snapshot.n_audit_events = n_events
        snapshot.finished_at = finished
        snapshot.status = "completed"
        try:
            view: DraftView | None = _build_draft_view(
                snapshot, payload, [row.to_dict() for row in ledger]
            )
            metrics = _metrics_from_draft(view, snapshot) if view else {}
        except Exception:  # noqa: BLE001 - a presenter bug must not fail the run
            view, metrics = None, {}

        def _complete(r: RunRecord) -> None:
            r.instance_id = instance_id
            r.model_version = model_version
            r.n_citations = n_citations
            r.n_audit_events = n_events
            r.plan_summary = plan_summary
            r.status = "completed"
            r.finished_at = finished
            if view is not None:
                self._draft_cache[run_id] = view
                self._metrics[run_id] = metrics
            for generated in _walk_generated(result.instance.sections):
                sp = _find_section(r, generated.section_id)
                if sp is None:
                    continue
                template_section = section_index.get(generated.section_id)
                is_stub_section = template_section is not None and (
                    template_section.generation.mode
                    in (GenerationMode.DETERMINISTIC, GenerationMode.MANUAL)
                )
                sp.n_paragraphs = len(generated.paragraphs)
                sp.notes = list(generated.critique_notes)
                sp.finished_at = r.finished_at
                if is_stub_section:
                    sp.status = "skipped"
                elif generated.critique_status == "failed_after_retries":
                    sp.status = "failed"
                else:
                    sp.status = "passed"
                sp.status_label = SECTION_LABEL[sp.status]
            for sp in r.sections:
                if sp.status in ("pending", "running", "retrying"):
                    sp.status = "failed"
                    sp.status_label = SECTION_LABEL["failed"]
                    sp.finished_at = r.finished_at

        self._mutate(run_id, _complete)

    def _on_audit_event(self, run_id: str, event: AuditEvent) -> None:
        """Mirror one engine audit event into live run progress.

        Exactly one mutation (and therefore one `run.json` write) per event.
        Called from the worker thread via `ProgressAuditSink`.
        """
        action = event.action.value
        extra = event.extra or {}
        section_id = event.target_id
        attempt = int(extra.get("attempt", 1) or 1)
        version = event.target_version or ""
        notes = [str(n) for n in (event.notes or [])]

        def _apply(r: RunRecord) -> None:
            r.n_audit_events += 1

            if action == AuditAction.GENERATION_REQUESTED.value:
                r.status = "planning"
                r.instance_id = event.target_id
                return

            if action == AuditAction.GENERATION_PLAN_COMPLETED.value:
                r.status = "generating"
                return

            if action == AuditAction.GENERATION_SECTION_FILLED.value:
                r.status = "generating"
                if version:
                    r.model_version = version
                sp = _find_section(r, section_id)
                if sp is None:
                    return
                sp.status = "running"
                sp.status_label = SECTION_LABEL["running"]
                sp.attempts = attempt
                sp.n_paragraphs = int(extra.get("n_paragraphs", 0) or 0)
                sp.n_citations = int(extra.get("n_citations", 0) or 0)
                sp.started_at = sp.started_at or _iso()
                return

            if action == AuditAction.GENERATION_SECTION_CRITIQUED.value:
                sp = _find_section(r, section_id)
                if sp is None:
                    return
                sp.attempts = attempt
                sp.notes = notes
                if str(extra.get("verdict", "")) == "pass":
                    sp.status = "passed"
                    sp.finished_at = _iso()
                elif attempt <= MAX_RETRIES_PER_SECTION:
                    sp.status = "retrying"
                else:
                    sp.status = "failed"
                    sp.finished_at = _iso()
                sp.status_label = SECTION_LABEL[sp.status]
                return

            if action == AuditAction.CITATION_CREATED.value:
                r.n_citations += 1
                return

            if action == AuditAction.GENERATION_COMPLETED.value:
                r.n_citations = int(extra.get("n_citations", 0) or 0) or r.n_citations

        self._mutate(run_id, _apply)


# ---------------------------------------------------------------------------
# Module-level helpers used by RunStore
# ---------------------------------------------------------------------------


def _find_section(record: RunRecord, section_id: str) -> SectionProgress | None:
    for section in record.sections:
        if section.section_id == section_id:
            return section
    return None


def _walk_generated(sections: list[Any]) -> list[Any]:
    """Depth-first flatten of a `GeneratedSection` tree."""
    out: list[Any] = []

    def walk(items: list[Any]) -> None:
        for item in items:
            out.append(item)
            walk(list(getattr(item, "children", []) or []))

    walk(list(sections))
    return out


def _model_version_from_events(events: list[AuditEvent]) -> str:
    for event in events:
        if (
            event.action == AuditAction.GENERATION_SECTION_FILLED
            and event.target_version
        ):
            return event.target_version
    return "stub"


def _primary_input(inputs: dict[str, str]) -> str:
    for key in _PRIMARY_INPUT_ORDER:
        value = (inputs.get(key) or "").strip()
        if value:
            return value
    for value in inputs.values():
        if str(value).strip():
            return str(value).strip()
    return ""


def _unreadable_record(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        template_key="",
        template_title="",
        template_version="",
        template_id="",
        title=f"Unreadable run — {run_id}",
        inputs={},
        evidence_folder="",
        status="interrupted",
        created_at=_iso(datetime.fromtimestamp(0, tz=timezone.utc)),
        finished_at=_iso(),
        error=RunError(
            kind="unreadable_run",
            message="This run's saved state could not be read.",
            detail="var/runs/{}/run.json is missing or corrupt.".format(run_id),
        ),
    )


def _record_from_dict(payload: dict[str, Any]) -> RunRecord:
    sections = [
        SectionProgress(
            section_id=str(s.get("section_id", "")),
            title=str(s.get("title", "")),
            level=int(s.get("level", 2) or 2),
            status=s.get("status", "pending"),
            status_label=str(s.get("status_label", "")),
            attempts=int(s.get("attempts", 0) or 0),
            n_paragraphs=int(s.get("n_paragraphs", 0) or 0),
            n_citations=int(s.get("n_citations", 0) or 0),
            notes=list(s.get("notes", []) or []),
            started_at=s.get("started_at"),
            finished_at=s.get("finished_at"),
        )
        for s in payload.get("sections", []) or []
    ]
    preflight = [
        PreflightIssue(
            severity=i.get("severity", "warning"),
            code=str(i.get("code", "")),
            binding_id=str(i.get("binding_id", "")),
            section_id=str(i.get("section_id", "")),
            message=str(i.get("message", "")),
            fix_hint=str(i.get("fix_hint", "")),
        )
        for i in payload.get("preflight", []) or []
    ]
    error_raw = payload.get("error")
    error = (
        RunError(
            kind=str(error_raw.get("kind", "")),
            message=str(error_raw.get("message", "")),
            detail=str(error_raw.get("detail", "")),
        )
        if isinstance(error_raw, dict)
        else None
    )
    return RunRecord(
        owner=str(payload.get("owner", "") or ""),
        run_id=str(payload["run_id"]),
        template_key=str(payload.get("template_key", "")),
        template_title=str(payload.get("template_title", "")),
        template_version=str(payload.get("template_version", "")),
        template_id=str(payload.get("template_id", "")),
        title=str(payload.get("title", "")),
        inputs={str(k): str(v) for k, v in (payload.get("inputs") or {}).items()},
        evidence_folder=str(payload.get("evidence_folder", "")),
        status=payload.get("status", "interrupted"),
        created_at=str(payload.get("created_at", _iso())),
        started_at=payload.get("started_at"),
        finished_at=payload.get("finished_at"),
        sections=sections,
        preflight=preflight,
        instance_id=payload.get("instance_id"),
        model_version=str(payload.get("model_version", "stub")),
        prompt_version=str(payload.get("prompt_version", "")),
        compliance_mode=str(payload.get("compliance_mode", "rd")),
        n_documents=int(payload.get("n_documents", 0) or 0),
        n_chunks=int(payload.get("n_chunks", 0) or 0),
        n_citations=int(payload.get("n_citations", 0) or 0),
        n_audit_events=int(payload.get("n_audit_events", 0) or 0),
        plan_summary=payload.get("plan_summary"),
        error=error,
        version=int(payload.get("version", 0) or 0),
    )


def _rmtree(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:  # pragma: no cover - defensive
        pass


# ---------------------------------------------------------------------------
# Template introspection helpers
# ---------------------------------------------------------------------------


def _form_fields(sections: list[TemplateSection]) -> list[FormField]:
    seen: dict[str, FormField] = {}
    for section in sections:
        for binding in section.data_bindings:
            if not isinstance(binding, FreeTextInputBinding):
                continue
            existing = seen.get(binding.binding_id)
            if existing is None:
                seen[binding.binding_id] = FormField(
                    binding_id=binding.binding_id,
                    prompt=binding.prompt,
                    required=binding.required,
                    default=INPUT_DEFAULTS.get(binding.binding_id, ""),
                    used_by_sections=1,
                )
            else:
                existing.used_by_sections += 1
    return list(seen.values())


def _substitute(value: str, inputs: dict[str, str]) -> str:
    return re.sub(
        r"\{\{\s*report\.([\w]+)\s*\}\}",
        lambda m: inputs.get(m.group(1)) or m.group(0),
        str(value),
    )


def _params_detail(parameters: dict[str, str], inputs: dict[str, str]) -> str:
    if not parameters:
        return "no parameters"
    return ", ".join(
        f"{k} = {_substitute(v, inputs)}" for k, v in sorted(parameters.items())
    )


def _near_miss(query_id: str, known: list[str]) -> str | None:
    matches = difflib.get_close_matches(query_id, known, n=1, cutoff=0.7)
    return matches[0] if matches else None


def _source_specs_with_codes(
    sections: list[TemplateSection],
    *,
    inputs: dict[str, str],
    evidence_folder: Path,
) -> tuple[list[SourceSpec], dict[str, str]]:
    """One `SourceSpec` per distinct non-input binding, in first-seen order,
    plus the preflight `code` for every spec that is not ready."""
    registry = query_registry()
    known_queries = registry.ids()
    api_registry = _api_connector_index()
    scanned = scan_evidence_folder(evidence_folder)

    order: list[str] = []
    by_id: dict[str, SourceSpec] = {}
    codes: dict[str, str] = {}

    for section in sections:
        for binding in section.data_bindings:
            if isinstance(binding, FreeTextInputBinding):
                continue
            bid = binding.binding_id
            if bid in by_id:
                if section.section_id not in by_id[bid].section_ids:
                    by_id[bid].section_ids.append(section.section_id)
                continue
            order.append(bid)
            spec, code = _spec_for_binding(
                binding,
                section_id=section.section_id,
                inputs=inputs,
                registry=registry,
                known_queries=known_queries,
                api_registry=api_registry,
                scanned=scanned,
            )
            by_id[bid] = spec
            if code:
                codes[bid] = code
    return [by_id[bid] for bid in order], codes


def _source_specs(
    sections: list[TemplateSection],
    *,
    inputs: dict[str, str],
    evidence_folder: Path,
) -> list[SourceSpec]:
    return _source_specs_with_codes(
        sections, inputs=inputs, evidence_folder=evidence_folder
    )[0]


_API_INDEX: dict[str, frozenset[str]] | None = None


def _api_connector_index() -> dict[str, frozenset[str]]:
    global _API_INDEX
    if _API_INDEX is None:
        _API_INDEX = {
            c.connector_id: frozenset(c.allowed_operations)
            for c in (
                MockConfluenceConnector(),
                MockChemblConnector(),
                MockClinicalTrialsConnector(),
            )
        }
    return _API_INDEX


def _spec_for_binding(
    binding: Any,
    *,
    section_id: str,
    inputs: dict[str, str],
    registry: NamedQueryRegistry,
    known_queries: list[str],
    api_registry: dict[str, frozenset[str]],
    scanned: list[_ScannedDoc],
) -> tuple[SourceSpec, str | None]:
    """Return the source spec and, when it is not ready, its preflight code."""
    bid = binding.binding_id
    sections = [section_id]

    def spec(
        kind: str,
        label: str,
        detail: str,
        status: str,
        status_text: str,
        fix_hint: str = "",
    ) -> SourceSpec:
        return SourceSpec(
            binding_id=bid,
            kind=kind,
            label=label,
            detail=detail,
            section_ids=sections,
            status=status,
            status_text=status_text,
            fix_hint=fix_hint,
        )

    if isinstance(binding, NamedQueryBinding):
        label = (
            f"Registered query {binding.source}.{binding.query_id}"
            if binding.source
            else f"Registered query {binding.query_id}"
        )
        detail = _params_detail(binding.parameters, inputs)
        if binding.query_id not in known_queries:
            hint = _near_miss(binding.query_id, known_queries)
            fix = (f"Did you mean {hint!r}? " if hint else "") + (
                f"Add a YAML file with id: {binding.query_id} to "
                f"{QUERIES_DIR.as_posix()}/"
            )
            return (
                spec(
                    "named_query",
                    label,
                    detail,
                    "gap",
                    f"Named query {binding.query_id!r} is not in the registry; "
                    "sections using it will be drafted without that table.",
                    fix,
                ),
                "UNKNOWN_NAMED_QUERY",
            )
        query = registry.get(binding.query_id)
        params = {k: _substitute(v, inputs) for k, v in binding.parameters.items()}
        try:
            query.validate_args(dict(params))
        except ValueError as exc:
            return (
                spec(
                    "named_query",
                    label,
                    detail,
                    "gap",
                    f"Query parameters do not match the registry: {exc}",
                    "Registered parameters: "
                    + (", ".join(sorted(query.parameters)) or "none"),
                ),
                "BAD_QUERY_PARAMETERS",
            )
        n = len(query.parameters)
        return (
            spec(
                "named_query",
                label,
                detail,
                "ready",
                f"Ready — registered query, {n} {_plural(n, 'parameter')}",
            ),
            None,
        )

    if isinstance(binding, ApiCallBinding):
        label = f"{binding.connector_id}.{binding.endpoint}"
        detail = _params_detail(binding.parameters, inputs)
        allowed = api_registry.get(binding.connector_id)
        if allowed is None:
            known = ", ".join(sorted(api_registry)) or "none"
            return (
                spec(
                    "api_call",
                    label,
                    detail,
                    "gap",
                    f"Connector {binding.connector_id!r} is not registered; this "
                    "source will be skipped.",
                    f"Registered connectors: {known}",
                ),
                "UNKNOWN_CONNECTOR",
            )
        if binding.endpoint not in allowed:
            return (
                spec(
                    "api_call",
                    label,
                    detail,
                    "gap",
                    f"Operation {binding.endpoint!r} is not allowed on connector "
                    f"{binding.connector_id!r}.",
                    "Allowed operations: " + ", ".join(sorted(allowed)),
                ),
                "OPERATION_NOT_ALLOWED",
            )
        return (
            spec(
                "api_call",
                label,
                detail,
                "ready",
                "Ready — connector registered, operation allowed",
            ),
            None,
        )

    if isinstance(binding, FileSetBinding):
        tags = {t.lower() for t in binding.filter_tags}
        detail = "matches any of: " + (", ".join(binding.filter_tags) or "(no tags)")
        matches = [d for d in scanned if tags & set(d.tags)] if tags else []
        if not matches:
            return (
                spec(
                    "file_set",
                    "Evidence documents",
                    detail,
                    "gap",
                    "No documents in the evidence folder match any of these tags.",
                    "Tags come from the folder names above each file. Put the "
                    "documents in a folder named after one of the tags.",
                ),
                "NO_MATCHING_DOCUMENTS",
            )
        n = len(matches)
        return (
            spec(
                "file_set",
                "Evidence documents",
                detail,
                "ready",
                f"Ready — {n} matching {_plural(n, 'document')}",
            ),
            None,
        )

    if isinstance(binding, FileRefBinding):
        found = any(d.path and d.path in binding.doc_id for d in scanned)
        if not found:
            return (
                spec(
                    "file_ref",
                    "Evidence document",
                    binding.doc_id,
                    "gap",
                    "That document is not in the evidence folder.",
                    "Copy the file into the evidence folder, or edit the template.",
                ),
                "NO_MATCHING_DOCUMENTS",
            )
        return (
            spec(
                "file_ref",
                "Evidence document",
                binding.doc_id,
                "ready",
                "Ready — document found",
            ),
            None,
        )

    if isinstance(binding, SqlQueryBinding):
        return (
            spec(
                "sql_query",
                f"Inline SQL against {binding.source or 'the warehouse'}",
                _truncate(binding.sql, 160),
                "unavailable",
                "Inline SQL is never executed here — only registered queries run.",
                "Promote this SQL to a reviewed named query in "
                f"{QUERIES_DIR.as_posix()}/ and reference it by query_id.",
            ),
            "INLINE_SQL_NOT_APPROVED",
        )

    if isinstance(binding, ComputedMetricBinding):
        return (
            spec(
                "computed_metric",
                f"Computed metric {binding.metric_id}",
                _params_detail(binding.parameters, inputs),
                "unavailable",
                "Computed metrics are not executed in this build.",
                "Use a registered query for now.",
            ),
            "COMPUTED_METRIC_NOT_EXECUTED",
        )

    return (  # pragma: no cover - exhaustiveness guard
        spec(
            getattr(getattr(binding, "type", None), "value", "unknown"),
            bid,
            "",
            "unavailable",
            "Unrecognised binding type.",
        ),
        "UNKNOWN_BINDING_TYPE",
    )


# ---------------------------------------------------------------------------
# Sources ledger (§7.6) — replaces the GeneratedTable the filler never emits
# ---------------------------------------------------------------------------


def _build_ledger(
    *,
    template: ReportTemplate,
    inputs: dict[str, str],
    documents: list[CanonicalDocument],
    chunks_by_doc: dict[str, list[ParsedChunk]],
    citations: list[dict[str, Any]],
    sql_gate: SqlSafetyGate,
    api_gate: ApiCallGate,
    evidence_folder: Path,
) -> list[LedgerRow]:
    resolver = BindingResolver(
        chunks_by_doc=chunks_by_doc,
        docs_by_id={d.doc_id: d for d in documents},
        free_text_inputs=inputs,
        safety_gate=sql_gate,
        api_gate=api_gate,
    )
    doc_titles = {d.doc_id: (d.title or Path(d.source_id).stem) for d in documents}

    # Join keys: SQL/API citations set source_doc_id == binding_id; file
    # citations carry retrieval_chunk_id.
    cited_binding_ids = {
        str(c.get("source_doc_id"))
        for c in citations
        if c.get("source_type") in ("sql", "api")
    }
    cited_chunk_ids = {
        str(c.get("retrieval_chunk_id"))
        for c in citations
        if c.get("retrieval_chunk_id")
    }

    specs = {
        s.binding_id: s
        for s in _source_specs(
            template.all_sections(), inputs=inputs, evidence_folder=evidence_folder
        )
    }

    rows: list[LedgerRow] = []
    seen: set[str] = set()
    for section in template.all_sections():
        for binding in section.data_bindings:
            if isinstance(binding, FreeTextInputBinding):
                continue
            bid = binding.binding_id
            if bid in seen:
                for row in rows:
                    if row.binding_id == bid and section.section_id not in row.section_ids:
                        row.section_ids.append(section.section_id)
                continue
            seen.add(bid)
            rows.append(
                _ledger_row(
                    binding=binding,
                    section_id=section.section_id,
                    resolver=resolver,
                    spec=specs.get(bid),
                    doc_titles=doc_titles,
                    cited_binding_ids=cited_binding_ids,
                    cited_chunk_ids=cited_chunk_ids,
                )
            )
    return rows


class _OneBindingSection:
    """Minimal duck-type so `BindingResolver.resolve` can run one binding."""

    def __init__(self, binding: Any) -> None:
        self.data_bindings = [binding]


def _ledger_row(
    *,
    binding: Any,
    section_id: str,
    resolver: BindingResolver,
    spec: SourceSpec | None,
    doc_titles: dict[str, str],
    cited_binding_ids: set[str],
    cited_chunk_ids: set[str],
) -> LedgerRow:
    bid = binding.binding_id
    kind = getattr(getattr(binding, "type", None), "value", "unknown")
    label = spec.label if spec else bid
    detail = (spec.detail.split("|", 1)[-1] if spec else "") or ""
    fix_hint = spec.fix_hint if spec and spec.fix_hint else None

    columns: list[str] = []
    rows: list[list[str]] = []
    typed_rows: list[list[object]] = []
    row_count: int | None = None
    deferred: str | None = None
    sql_text: str | None = None

    try:
        resolved = resolver.resolve(_OneBindingSection(binding)).bindings[0]  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - the ledger must never fail the page
        resolved = None
        deferred = f"Could not resolve this source: {exc}"

    is_cited = False
    if resolved is not None:
        deferred = resolved.deferred_note
        if resolved.query_result is not None:
            q = resolved.query_result
            columns = list(q.columns)
            rows = [["" if c is None else str(c) for c in row] for row in q.rows]
            typed_rows = [list(row) for row in q.rows]
            row_count = q.row_count
            is_cited = bid in cited_binding_ids
        elif resolved.api_result is not None:
            a = resolved.api_result
            columns = list(a.columns)
            rows = [["" if c is None else str(c) for c in row] for row in a.rows]
            typed_rows = [list(row) for row in a.rows]
            row_count = a.row_count
            is_cited = bid in cited_binding_ids
        elif resolved.chunks:
            per_doc: dict[str, int] = {}
            for chunk in resolved.chunks:
                per_doc[chunk.source_doc_id] = per_doc.get(chunk.source_doc_id, 0) + 1
            columns = ["Document", "Extracts in pool"]
            rows = [
                [doc_titles.get(doc_id, Path(doc_id).name), str(n)]
                for doc_id, n in sorted(per_doc.items())
            ]
            row_count = len(resolved.chunks)
            is_cited = any(c.chunk_id in cited_chunk_ids for c in resolved.chunks)

    if isinstance(binding, NamedQueryBinding):
        try:
            sql_text = query_registry().get(binding.query_id).sql
        except KeyError:
            sql_text = None

    unit = "extract" if kind in ("file_set", "file_ref") else "row"
    if deferred or (row_count is None and not columns):
        status = "unavailable"
        status_text = "Not resolved — nothing was pulled for this source."
    elif is_cited:
        status = "cited"
        status_text = (
            f"Pulled and cited — {row_count} {_plural(row_count or 0, unit)}"
        )
    else:
        status = "resolved_uncited"
        status_text = (
            f"Pulled but not cited — {row_count} "
            f"{_plural(row_count or 0, unit)} went unused by the draft"
        )

    return LedgerRow(
        binding_id=bid,
        kind=kind,
        label=label,
        detail=detail,
        status=status,
        status_text=status_text,
        row_count=row_count,
        section_ids=[section_id],
        citation_ns=[],
        deferred_note=deferred,
        fix_hint=fix_hint,
        columns=columns,
        rows=rows,
        typed_rows=typed_rows,
        sql=sql_text,
    )


def _ledger_from_dicts(payload: list[Any]) -> list[LedgerRow]:
    out: list[LedgerRow] = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        out.append(
            LedgerRow(
                binding_id=str(item.get("binding_id", "")),
                kind=str(item.get("kind", "")),
                label=str(item.get("label", "")),
                detail=str(item.get("detail", "")),
                status=str(item.get("status", "unavailable")),
                status_text=str(item.get("status_text", "")),
                row_count=item.get("row_count"),
                section_ids=[str(s) for s in item.get("section_ids", []) or []],
                citation_ns=[int(n) for n in item.get("citation_ns", []) or []],
                deferred_note=item.get("deferred_note"),
                fix_hint=item.get("fix_hint"),
                columns=[str(c) for c in item.get("columns", []) or []],
                rows=[[str(c) for c in row] for row in item.get("rows", []) or []],
                # `typed_rows`, not `rows` — reading the stringified copy here
                # defeated the whole point of storing both, and the chart
                # correctly refused to plot the string "15.0". Falls back to
                # `rows` for runs written before the field existed, where the
                # strings are all there is.
                typed_rows=[
                    list(row)
                    for row in (item.get("typed_rows") or item.get("rows") or [])
                ],
                sql=item.get("sql"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Draft presenters (§7.7)
# ---------------------------------------------------------------------------


def _flatten_instance_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(items: list[dict[str, Any]]) -> None:
        for item in items:
            out.append(item)
            walk(list(item.get("children") or []))

    walk(list(sections or []))
    return out


def _source_uri_parts(citation: dict[str, Any]) -> tuple[str, str, str]:
    """(title, uri_display, uri_copy) for a citation's `source_uri`."""
    uri = str(citation.get("source_uri", ""))
    display = uri
    for prefix in ("local://local::", "local://", "local::"):
        if display.startswith(prefix):
            display = display[len(prefix) :]
            break
    title = Path(display).name if display else uri
    return (title or uri), display, display


def _locator_rows(
    citation: dict[str, Any], retrieved_human: str
) -> tuple[list[tuple[str, str]], str, str, str]:
    """(<dt>/<dd> rows, caption, version_label, version_value)."""
    source_type = str(citation.get("source_type", ""))
    locator = citation.get("locator") or {}
    version = str(citation.get("source_doc_version", ""))
    rows: list[tuple[str, str]] = []
    caption = SOURCE_CAPTION.get(source_type, "")

    if source_type == "pdf":
        if locator.get("page") is not None:
            rows.append(("Page", str(locator["page"])))
        if locator.get("paragraph_index") is not None:
            rows.append(("Paragraph", str(locator["paragraph_index"])))
        trail = locator.get("heading_trail") or []
        if trail:
            rows.append(("Heading trail", " › ".join(str(t) for t in trail)))
        rows.append(("Content hash", version))
        rows.append(("Retrieved", retrieved_human))
        return rows, caption, "Content hash", version

    if source_type == "docx":
        trail = locator.get("heading_trail") or []
        rows.append(("Heading trail", " › ".join(str(t) for t in trail) or "(top)"))
        if locator.get("paragraph_index") is not None:
            rows.append(("Paragraph", str(locator["paragraph_index"])))
        rows.append(("Content hash", version))
        rows.append(("Retrieved", retrieved_human))
        return rows, caption, "Content hash", version

    if source_type == "xlsx":
        rows.append(("Sheet", str(locator.get("sheet", ""))))
        rows.append(("Cell range", str(locator.get("cell_range", ""))))
        rows.append(("Content hash", version))
        rows.append(("Retrieved", retrieved_human))
        return rows, caption, "Content hash", version

    if source_type == "sql":
        rows.append(("Query", str(locator.get("query_id", ""))))
        params = locator.get("query_parameters") or {}
        rows.append(
            (
                "Parameters",
                ", ".join(f"{k} = {v}" for k, v in sorted(params.items())) or "none",
            )
        )
        if locator.get("row_filter"):
            rows.append(("Row filter", str(locator["row_filter"])))
        rows.append(("Rows returned", version))
        rows.append(("Retrieved", retrieved_human))
        return (
            rows,
            caption.format(retrieved=retrieved_human),
            "Rows returned",
            version,
        )

    if source_type == "api":
        endpoint = str(locator.get("endpoint", ""))
        connector, _, operation = endpoint.partition(".")
        rows.append(("Connector", connector or endpoint))
        rows.append(("Operation", operation or "—"))
        params = locator.get("api_parameters") or {}
        rows.append(
            (
                "Parameters",
                ", ".join(f"{k} = {v}" for k, v in sorted(params.items())) or "none",
            )
        )
        rows.append(("Rows returned", version))
        rows.append(("Retrieved", retrieved_human))
        return rows, caption, "Rows returned", version

    rows.append(("Retrieved", retrieved_human))
    return rows, caption, "Version", version


def _iter_template_sections(template: ReportTemplate) -> list[TemplateSection]:
    """Every section in the template tree, parents included.

    Separate from `_flatten_instance_sections`, which walks the *generated*
    instance. The two trees carry different things — one has the authored spec,
    the other has the drafted prose — and conflating them is how a figure ends
    up attached to the wrong section.
    """
    out: list[TemplateSection] = []

    def walk(nodes: list[TemplateSection]) -> None:
        for node in nodes:
            out.append(node)
            walk(list(node.children))

    walk(list(template.sections))
    return out


def _build_draft_view(
    record: RunRecord,
    result: dict[str, Any],
    ledger_raw: list[Any],
) -> DraftView:
    instance = result.get("instance") or {}
    instance_id = str(instance.get("instance_id", "")) or (record.instance_id or "")
    citations_raw: list[dict[str, Any]] = list(result.get("citations") or [])
    citations_by_id = {str(c.get("citation_id")): c for c in citations_raw}
    # The figures this run recorded for itself. Absent on runs generated before
    # visuals existed, which is why this is a plain .get rather than a lookup
    # that assumes the key: an old report shows no figure, not a stack trace.
    visuals_by_section = dict(result.get("visuals") or {})
    generated_sections = _flatten_instance_sections(list(instance.get("sections") or []))

    # --- pass 1: number citations by first appearance in document order ---
    numbering: dict[str, int] = {}
    first_use: dict[str, tuple[str, str, str]] = {}  # cid -> (section_id, title, claim)
    for gen in generated_sections:
        section_id = str(gen.get("section_id", ""))
        section_title = str(gen.get("title", ""))
        for paragraph in gen.get("paragraphs") or []:
            for claim in paragraph.get("claims") or []:
                for cid in claim.get("citation_ids") or []:
                    cid = str(cid)
                    if cid in numbering:
                        continue
                    numbering[cid] = len(numbering) + 1
                    first_use[cid] = (
                        section_id,
                        section_title,
                        str(claim.get("text", "")),
                    )
    for cid in citations_by_id:
        if cid not in numbering:
            numbering[cid] = len(numbering) + 1
            first_use.setdefault(cid, ("", "", ""))

    integrity_errors = 0

    def _ref_for(cid: str, approx: bool) -> CitationRef:
        nonlocal integrity_errors
        citation = citations_by_id.get(cid)
        if citation is None:
            integrity_errors += 1
            return CitationRef(
                n=0,
                citation_id=cid,
                source_type="computed",
                aria_label=(
                    "Broken citation — the draft references a source that was "
                    "not captured."
                ),
                approx=approx,
            )
        n = numbering[cid]
        source_type = str(citation.get("source_type", "computed"))
        title, _, _ = _source_uri_parts(citation)
        return CitationRef(
            n=n,
            citation_id=cid,
            source_type=source_type,
            aria_label=(
                f"Citation {n}, {SOURCE_WORD.get(source_type, source_type)}: {title}"
                + (" (approximate placement)" if approx else "")
            ),
            approx=approx,
        )

    # --- pass 2: sections, paragraphs, anchoring --------------------------
    ledger = _ledger_from_dicts(ledger_raw)
    ledger_by_id = {row.binding_id: row for row in ledger}
    citation_ns_by_binding: dict[str, list[int]] = {}
    citation_ids_by_binding: dict[str, list[str]] = {}
    for cid, citation in citations_by_id.items():
        if str(citation.get("source_type")) in ("sql", "api"):
            binding_id = str(citation.get("source_doc_id"))
            citation_ns_by_binding.setdefault(binding_id, []).append(numbering[cid])
            citation_ids_by_binding.setdefault(binding_id, []).append(cid)

    section_views: list[SectionView] = []
    outline: list[OutlineItem] = []
    total_claims = 0
    total_claims_cited = 0
    total_uncited_numbers = 0
    coverage_rows: list[tuple[str, list[int]]] = []
    coverage_columns = [row.binding_id for row in ledger]

    for gen in generated_sections:
        section_id = str(gen.get("section_id", ""))
        title = str(gen.get("title", ""))
        level = int(gen.get("level", 2) or 2)
        critique_status = str(gen.get("critique_status", "pending"))
        critique_notes = [str(n) for n in gen.get("critique_notes") or []]

        paragraphs: list[ParagraphView] = []
        section_cids: set[str] = set()
        section_uncited = 0
        for p_idx, paragraph in enumerate(gen.get("paragraphs") or []):
            view = _anchor_paragraph(p_idx, paragraph, _ref_for)
            paragraphs.append(view)
            section_uncited += view.n_uncited_numbers
            for claim in paragraph.get("claims") or []:
                total_claims += 1
                cids = [str(c) for c in claim.get("citation_ids") or []]
                if cids:
                    total_claims_cited += 1
                section_cids.update(cids)
        total_uncited_numbers += section_uncited

        # Deterministic data blocks for this section, from the ledger.
        tables = [
            _table_view(
                ledger_by_id[bid],
                citation_ns_by_binding.get(bid, []),
                citation_ids_by_binding.get(bid, []),
                record,
            )
            for bid in _section_binding_ids(ledger, section_id)
            if bid in ledger_by_id
        ]

        chart = None
        visual_raw = visuals_by_section.get(section_id)
        if isinstance(visual_raw, dict):
            chart = _chart_view(visual_raw, ledger_by_id, citation_ns_by_binding)

        n_section_citations = len(section_cids)
        if critique_status == "failed_after_retries":
            band: BandKind = "failed"
            band_title = "Checks failed after a retry"
            band_body = (
                "The automated check flagged this section and the retry did not "
                "clear it. Read the notes below and rewrite anything you cannot "
                "verify."
            )
        elif n_section_citations == 0:
            band = "no_data"
            band_title = "No source data"
            band_body = (
                "No source data resolved for this section. Narrative only — "
                "treat every statement as unverified."
            )
        else:
            band = "none"
            band_title = ""
            band_body = ""

        section_views.append(
            SectionView(
                section_id=section_id,
                title=title,
                level=level,
                heading_tag=f"h{min(max(level, 2), 4)}",
                critique_status=critique_status,
                critique_label=CRITIQUE_LABEL.get(critique_status, ""),
                critique_notes=critique_notes,
                notes_short=[_truncate(n, 240) for n in critique_notes],
                paragraphs=paragraphs,
                tables=tables,
                chart=chart,
                n_citations=n_section_citations,
                n_uncited_numbers=section_uncited,
                band=band,
                band_title=band_title,
                band_body=band_body,
            )
        )
        outline.append(
            OutlineItem(
                section_id=section_id,
                title=title,
                level=level,
                state="error"
                if band == "failed"
                else ("warn" if band == "no_data" or section_uncited else "ok"),
                label=(
                    "Checks failed"
                    if band == "failed"
                    else (
                        "No source data"
                        if band == "no_data"
                        else (
                            f"{section_uncited} uncited "
                            f"{_plural(section_uncited, 'number')}"
                            if section_uncited
                            else f"{n_section_citations} "
                            f"{_plural(n_section_citations, 'citation')}"
                        )
                    )
                ),
            )
        )

        counts: list[int] = []
        for bid in coverage_columns:
            ns = set(citation_ns_by_binding.get(bid, []))
            counts.append(
                sum(1 for cid in section_cids if numbering.get(cid, -1) in ns)
                if ns
                else _file_citation_count(section_cids, citations_by_id, bid, ledger_by_id)
            )
        coverage_rows.append((title, counts))

    # --- pass 3: citation appendix ---------------------------------------
    citation_views: list[CitationView] = []
    for cid, n in sorted(numbering.items(), key=lambda kv: kv[1]):
        citation = citations_by_id.get(cid)
        if citation is None:
            continue
        citation_views.append(
            _citation_view(
                n=n,
                citation=citation,
                first_use=first_use.get(cid, ("", "", "")),
                run_id=record.run_id,
                instance_id=instance_id,
            )
        )

    # --- ledger citation numbers -----------------------------------------
    for row in ledger:
        row.citation_ns = sorted(citation_ns_by_binding.get(row.binding_id, []))
        if not row.citation_ns and row.kind in ("file_set", "file_ref"):
            row.citation_ns = sorted(
                {
                    numbering[cid]
                    for cid, c in citations_by_id.items()
                    if str(c.get("source_type")) in ("pdf", "docx", "xlsx")
                    and _doc_in_ledger_row(c, row)
                }
            )

    n_cited_rows = sum(1 for r in ledger if r.status == "cited" or r.citation_ns)
    n_unavailable = sum(1 for r in ledger if r.status == "unavailable")
    # Three states, and the summary must account for all of them. It named
    # only "cited" and "could not be resolved", so a source that WAS pulled and
    # then went unused by the draft vanished from the arithmetic — 8 bound, 3
    # cited, 4 unresolved, and one unaccounted for. In a provenance product
    # that omission is the interesting case: it means retrieval worked and the
    # draft ignored the result, which is a reviewer's problem, not a silent one.
    n_unused = sum(1 for row in ledger if row.status == "resolved_uncited")
    parts = [
        f"{len(ledger)} bound {_plural(len(ledger), 'source')}",
        f"{n_cited_rows} cited in the draft",
    ]
    if n_unused:
        parts.append(f"{n_unused} pulled but unused")
    if n_unavailable:
        parts.append(f"{n_unavailable} could not be resolved")
    ledger_summary = " · ".join(parts)

    # --- trust bar --------------------------------------------------------
    n_no_data = sum(1 for s in section_views if s.band == "no_data")
    n_failed = sum(1 for s in section_views if s.band == "failed")
    n_citations = len(citation_views)
    clean = (
        n_no_data == 0
        and n_failed == 0
        and total_uncited_numbers == 0
        and integrity_errors == 0
        and n_citations > 0
    )
    if integrity_errors or n_failed:
        state = "error"
    elif not clean:
        state = "warn"
    else:
        state = "ok"
    if n_citations == 0:
        headline = (
            "No citations at all — nothing in this draft is backed by a source."
        )
    elif clean:
        headline = (
            f"{total_claims_cited} of {total_claims} claims cited across "
            f"{len(section_views)} sections; no uncited numbers."
        )
    else:
        parts = []
        if n_no_data:
            parts.append(f"{n_no_data} {_plural(n_no_data, 'section')} with no data")
        if n_failed:
            parts.append(f"{n_failed} failed {_plural(n_failed, 'check')}")
        if total_uncited_numbers:
            parts.append(
                f"{total_uncited_numbers} uncited "
                f"{_plural(total_uncited_numbers, 'number')}"
            )
        if integrity_errors:
            parts.append(
                f"{integrity_errors} broken {_plural(integrity_errors, 'citation')}"
            )
        headline = (
            f"{total_claims_cited} of {total_claims} claims cited — "
            + ", ".join(parts)
            + ". Verify before use."
        )

    trust = TrustBar(
        n_sections=len(section_views),
        n_claims=total_claims,
        n_claims_cited=total_claims_cited,
        n_uncited_numbers=total_uncited_numbers,
        n_sections_no_data=n_no_data,
        n_sections_failed=n_failed,
        n_citations=n_citations,
        n_integrity_errors=integrity_errors,
        clean=clean,
        state=state,
        headline=headline,
    )

    raw_events = result.get("audit_events") or []
    # The origin for the elapsed column. Taken from the first event rather than
    # the run's created_at: the log measures the run's own steps, and a run can
    # sit queued for a while before the first one happens.
    first_ts = str(raw_events[0].get("timestamp_utc", "")) if raw_events else ""
    section_titles = {s.section_id: s.title for s in section_views}
    events = [_event_view(e, section_titles, first_ts) for e in raw_events]

    notice = _notice_for(record, [s.title for s in section_views if s.band == "no_data"])

    return DraftView(
        trust=trust,
        outline=outline,
        sections=section_views,
        citations=citation_views,
        ledger=ledger,
        ledger_summary=ledger_summary,
        coverage_columns=coverage_columns,
        coverage_rows=coverage_rows,
        events=events,
        notice=notice,
        hollow=n_citations == 0,
    )


def _notice_for(record: RunRecord, hollow_sections: list[str]) -> str:
    notice = DRAFT_NOTICE.format(
        date=_human_ts(record.finished_at or record.created_at),
        template_id=record.template_id or record.template_key,
        version=record.template_version,
        model_version=record.model_version,
    )
    if hollow_sections:
        notice += DRAFT_NOTICE_GAPS.format(sections="; ".join(hollow_sections))
    if not engine_for_run(record.model_version).real:
        notice += DRAFT_NOTICE_STUB
    return notice


def _section_binding_ids(ledger: list[LedgerRow], section_id: str) -> list[str]:
    return [row.binding_id for row in ledger if section_id in row.section_ids]


def _doc_in_ledger_row(citation: dict[str, Any], row: LedgerRow) -> bool:
    """Does this file citation come from a document this ledger row pulled?

    File-set rows list documents by their display title, which is the file
    stem — so the join key is the stem, not the full filename.
    """
    if row.kind not in ("file_set", "file_ref"):
        return False
    stem = Path(str(citation.get("source_doc_id", ""))).stem
    if not stem:
        return False
    return any(stem == (cell or "").strip() for cells in row.rows for cell in cells)


def _file_citation_count(
    section_cids: set[str],
    citations_by_id: dict[str, dict[str, Any]],
    binding_id: str,
    ledger_by_id: dict[str, LedgerRow],
) -> int:
    row = ledger_by_id.get(binding_id)
    if row is None or row.kind not in ("file_set", "file_ref"):
        return 0
    total = 0
    for cid in section_cids:
        citation = citations_by_id.get(cid)
        if citation is None:
            continue
        if str(citation.get("source_type")) not in ("pdf", "docx", "xlsx"):
            continue
        if _doc_in_ledger_row(citation, row):
            total += 1
    return total


def _table_view(
    row: LedgerRow,
    citation_ns: list[int],
    citation_ids: list[str],
    record: RunRecord,
) -> DataTableView:
    n = citation_ns[0] if citation_ns else None
    cid = citation_ids[0] if citation_ids else None
    if row.status == "unavailable":
        status = "unavailable"
    elif citation_ns or row.status == "cited":
        status = "cited"
    else:
        status = "uncited"
    caption = f"{row.label} — {row.binding_id}"
    vh_note = (
        "Deterministic data table. Values inserted unchanged from "
        f"{BINDING_KIND_LABEL.get(row.kind, row.kind)} {row.binding_id}; "
        "not written by the model."
    )
    return DataTableView(
        binding_id=row.binding_id,
        caption=caption,
        columns=list(row.columns),
        rows=[list(r) for r in row.rows],
        source_label=row.label,
        row_count=row.row_count or 0,
        citation_n=n,
        citation_id=cid,
        retrieved_human=_human_ts(record.finished_at or record.created_at),
        status=status,
        deferred_note=row.deferred_note,
        vh_note=vh_note,
    )


def _chart_view(
    spec_raw: dict[str, Any],
    ledger_by_id: dict[str, LedgerRow],
    citation_ns_by_binding: dict[str, list[int]],
) -> ChartView | None:
    """One section's figure, or a ChartView carrying the reason there is none.

    Returns None only when the template asked for nothing. Every other outcome
    renders something: a chart, or a sentence saying why the chart is absent.
    Silently dropping a declared figure leaves a report quietly missing a piece
    the template asked for, and nobody reads a log to discover that.
    """
    try:
        spec = VisualSpec.model_validate(spec_raw)
    except ValidationError as exc:
        return ChartView(
            binding_id=str(spec_raw.get("binding_id", "")),
            kind=str(spec_raw.get("kind", "")),
            title="Figure",
            svg="",
            caption="",
            citation_n=None,
            unavailable_reason=f"The figure this section declares is not valid: {exc}",
        )

    row = ledger_by_id.get(spec.binding_id)
    title = spec.title or f"{spec.y} by {spec.x}"
    ns = citation_ns_by_binding.get(spec.binding_id, [])
    base = {
        "binding_id": spec.binding_id,
        "kind": spec.kind.value,
        "title": title,
        "citation_n": ns[0] if ns else None,
    }

    if row is None:
        return ChartView(
            **base,
            svg="",
            caption="",
            unavailable_reason=(
                f"No figure: this section declares a chart over "
                f"{spec.binding_id!r}, which is not one of its sources."
            ),
        )
    if row.status == "unavailable":
        return ChartView(
            **base,
            svg="",
            caption="",
            unavailable_reason=(
                f"No figure: {spec.binding_id} did not resolve, so there are no "
                f"values to plot. {row.deferred_note or ''}".strip()
            ),
        )

    try:
        svg = render_chart(
            spec,
            tuple(row.columns),
            tuple(tuple(cells) for cells in row.typed_rows),
        )
    except ChartDataError as exc:
        return ChartView(
            **base, svg="", caption="", unavailable_reason=f"No figure: {exc}"
        )

    return ChartView(
        **base,
        svg=svg,
        caption=(
            f"Plotted from {row.label} — {spec.binding_id}. Values are the "
            f"query's own; the model did not write them."
        ),
        unavailable_reason="",
    )


def _anchor_paragraph(
    para_idx: int, paragraph: dict[str, Any], ref_for: Any
) -> ParagraphView:
    text = str(paragraph.get("text", ""))
    claims = list(paragraph.get("claims") or [])
    norm_text, index_map = _normalize_with_map(text)

    candidates: list[tuple[int, int, int, str, int]] = []  # rank, -len, start, tier, idx
    tiers: dict[int, str] = {}
    spans: dict[int, tuple[int, int]] = {}
    for idx, claim in enumerate(claims):
        found = _find_claim_span(text, norm_text, index_map, str(claim.get("text", "")))
        if found is None:
            tiers[idx] = "unanchored"
            continue
        start, end, tier = found
        candidates.append((_TIER_RANK[tier], -(end - start), start, tier, idx))
        spans[idx] = (start, end)
        tiers[idx] = tier

    accepted: dict[int, tuple[int, int, str]] = {}
    taken: list[tuple[int, int]] = []
    for _rank, _neg_len, start, tier, idx in sorted(candidates):
        end = spans[idx][1]
        if any(start < t_end and end > t_start for t_start, t_end in taken):
            tiers[idx] = "unanchored"
            continue
        taken.append((start, end))
        accepted[idx] = (start, end, tier)

    def _claim_view(idx: int, span_text: str) -> ClaimView:
        claim = claims[idx]
        cids = [str(c) for c in claim.get("citation_ids") or []]
        approx = tiers.get(idx) == "sentence"
        return ClaimView(
            claim_idx=idx,
            anchor=tiers.get(idx, "unanchored"),  # type: ignore[arg-type]
            text=span_text,
            citations=[ref_for(cid, approx) for cid in cids],
            uncited=not cids,
        )

    segments: list[Segment] = []
    n_uncited = 0
    cursor = 0
    for idx, (start, end, _tier) in sorted(accepted.items(), key=lambda kv: kv[1][0]):
        if start > cursor:
            marks, flagged = _number_marks(text[cursor:start])
            segments.append(
                Segment(kind="text", text=text[cursor:start], claim=None, marks=marks)
            )
            n_uncited += flagged
        span_text = text[start:end]
        segments.append(
            Segment(
                kind="claim",
                text=span_text,
                claim=_claim_view(idx, span_text),
                marks=None,
            )
        )
        cursor = end
    if cursor < len(text):
        marks, flagged = _number_marks(text[cursor:])
        segments.append(Segment(kind="text", text=text[cursor:], claim=None, marks=marks))
        n_uncited += flagged

    orphans = [
        _claim_view(idx, str(claims[idx].get("text", "")))
        for idx in range(len(claims))
        if idx not in accepted
    ]

    return ParagraphView(
        para_idx=para_idx,
        segments=segments,
        n_uncited_numbers=n_uncited,
        orphan_claims=orphans,
    )


def _citation_view(
    *,
    n: int,
    citation: dict[str, Any],
    first_use: tuple[str, str, str],
    run_id: str,
    instance_id: str,
) -> CitationView:
    source_type = str(citation.get("source_type", "computed"))
    retrieved_iso = str(citation.get("retrieved_at", ""))
    retrieved_human = _human_ts(retrieved_iso)
    title, uri_display, uri_copy = _source_uri_parts(citation)
    rows, caption, version_label, version_value = _locator_rows(citation, retrieved_human)
    snippet = str(citation.get("snippet", ""))
    doc_id = str(citation.get("source_doc_id", ""))
    locator = citation.get("locator") or {}
    if source_type == "sql":
        title = str(locator.get("query_id") or doc_id or title)
    elif source_type == "api":
        title = str(locator.get("endpoint") or doc_id or title)

    open_url: str | None = None
    if source_type in ("pdf", "docx", "xlsx") and doc_id:
        from urllib.parse import quote

        open_url = f"/runs/{run_id}/source/{quote(doc_id, safe='')}"
        page = (citation.get("locator") or {}).get("page")
        if source_type == "pdf" and page:
            open_url += f"#page={page}"

    snippet_grid: list[list[str]] | None = None
    if source_type == "xlsx" and snippet:
        snippet_grid = [line.split("\t") for line in snippet.splitlines() if line.strip()]

    claim_text = first_use[2]
    chips = [
        NumberChip(
            token=token,
            found=_number_found_in(token, snippet),
            label=(
                f"{token} — found in snippet"
                if _number_found_in(token, snippet)
                else f"{token} — not found in the captured "
                f"{len(snippet)}-character snippet"
            ),
        )
        for token in _number_tokens(claim_text)
    ]

    return CitationView(
        n=n,
        citation_id=str(citation.get("citation_id", "")),
        source_type=source_type,
        source_word=SOURCE_WORD.get(source_type, source_type),
        title=title,
        uri_display=uri_display,
        uri_copy=uri_copy,
        open_url=open_url,
        locator_rows=rows,
        snippet=snippet,
        snippet_grid=snippet_grid,
        retrieved_iso=retrieved_iso,
        retrieved_human=retrieved_human,
        version_label=version_label,
        version_value=version_value,
        number_chips=chips,
        claim_text=claim_text,
        section_id=first_use[0],
        section_title=first_use[1],
        chunk_id=citation.get("retrieval_chunk_id"),
        doc_id=doc_id,
        instance_id=instance_id,
        caption=caption,
    )


def _event_view(
    event: dict[str, Any], titles: dict[str, str], first_ts: str = ""
) -> EventView:
    """`first_ts` is the run's earliest event, for the elapsed column. Defaulted
    so a caller that does not care about offsets is not forced to compute one."""
    action = str(event.get("action", ""))
    group, label = AUDIT_GROUP.get(
        action, ("section", action.replace("_", " ").capitalize())
    )
    target_id = str(event.get("target_id", ""))
    target_type = str(event.get("target_type", ""))
    if target_type == "section":
        target = f"{target_id} · {titles.get(target_id, '')}".strip(" ·")
    elif target_type == "report_instance":
        target = "This report"
    else:
        target = target_id
    extra = event.get("extra") or {}
    pairs = [
        (_EXTRA_LABEL.get(str(k), str(k).replace("_", " ").capitalize()), str(v))
        for k, v in extra.items()
        if k != "report_instance_id" and v not in (None, "")
    ]
    return EventView(
        ts_human=_human_ts(str(event.get("timestamp_utc", ""))),
        ts_precise=_clock_ts(str(event.get("timestamp_utc", ""))),
        offset_human=_offset_human(
            str(event.get("timestamp_utc", "")), first_ts
        ),
        action=action,
        action_label=label,
        target=target,
        notes=[_truncate(n, 240) for n in event.get("notes") or []],
        extra_pairs=pairs,
        group=group,
    )


def _metrics_from_draft(view: DraftView, record: RunRecord) -> dict[str, int]:
    return {
        "n_sections_cited": sum(1 for s in view.sections if s.n_citations > 0),
        "n_sections_no_data": view.trust.n_sections_no_data,
        "n_sections_failed": view.trust.n_sections_failed,
        "n_claims": view.trust.n_claims,
        "n_claims_cited": view.trust.n_claims_cited,
        "n_uncited_numbers": view.trust.n_uncited_numbers,
        "n_citations": view.trust.n_citations,
        "n_integrity_errors": view.trust.n_integrity_errors,
        "n_bindings_resolved": sum(
            1 for r in view.ledger if r.status != "unavailable"
        ),
        "n_bindings_deferred": sum(
            1 for r in view.ledger if r.status == "unavailable"
        ),
        "n_documents": record.n_documents,
        "n_chunks": record.n_chunks,
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def _markdown_export(
    summary: RunSummary, record: RunRecord, draft: DraftView | None
) -> str:
    out: list[str] = []
    out.append(f"# {record.title}")
    out.append("")
    if draft is not None:
        out.append(f"> **{draft.notice}**")
    else:
        out.append(
            "> **AI-generated draft for human review. This run produced no draft "
            f"— status: {summary.status_label}.**"
        )
    out.append(">")
    out.append(f"> {STUB_LLM_WARNING}")
    out.append("")
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| Report type | {record.template_title} ({record.template_key}) |")
    out.append(f"| Template | {record.template_id}@{record.template_version} |")
    out.append(f"| Run | {record.run_id} |")
    out.append(f"| Created | {summary.created_human} |")
    out.append(f"| Status | {summary.status_label} |")
    out.append(f"| Model | {record.model_version} |")
    out.append(f"| Prompt version | {record.prompt_version} |")
    out.append(f"| Mode | R&D / discovery |")
    out.append(f"| Evidence folder | {record.evidence_folder} |")
    for key, value in sorted(record.inputs.items()):
        out.append(f"| Input · {key} | {value} |")
    out.append("")

    if draft is None:
        out.append("_No draft was produced for this run._")
        if record.error:
            out.append("")
            out.append(f"**{record.error.kind}** — {record.error.message}")
        return "\n".join(out) + "\n"

    out.append(f"_{draft.trust.headline}_")
    out.append("")

    for section in draft.sections:
        out.append(f"{'#' * min(max(section.level, 2), 4)} {section.title}")
        out.append("")
        if section.band != "none":
            out.append(f"> **{section.band_title}.** {section.band_body}")
            out.append("")
        for paragraph in section.paragraphs:
            buf: list[str] = []
            for segment in paragraph.segments:
                if segment.kind == "claim" and segment.claim is not None:
                    marker = "".join(f"[{c.n}]" for c in segment.claim.citations)
                    buf.append(segment.text + marker)
                else:
                    buf.append(segment.text)
            out.append(" ".join("".join(buf).split()))
            out.append("")
            for claim in paragraph.orphan_claims:
                marker = "".join(f"[{c.n}]" for c in claim.citations)
                flag = "" if claim.citations else " *(uncited)*"
                out.append(f"- {claim.text}{marker}{flag}")
            if paragraph.orphan_claims:
                out.append("")
        for table in section.tables:
            out.append(f"**{table.caption}** — {table.source_label}")
            out.append("")
            if table.deferred_note or not table.columns:
                out.append(
                    f"> Not resolved. {table.deferred_note or 'No data was pulled.'}"
                )
                out.append("")
                continue
            out.append("| " + " | ".join(table.columns) + " |")
            out.append("|" + "|".join("---" for _ in table.columns) + "|")
            for row in table.rows:
                out.append("| " + " | ".join(str(c) for c in row) + " |")
            out.append("")
            out.append(f"_{table.vh_note}_")
            out.append("")
        if section.notes_short:
            out.append("_Check notes:_")
            for note in section.notes_short:
                out.append(f"- {note}")
            out.append("")

    out.append("## References")
    out.append("")
    for citation in draft.citations:
        out.append(f"**[{citation.n}]** {citation.title} — {citation.source_word}")
        out.append("")
        for term, value in citation.locator_rows:
            out.append(f"- {term}: {value}")
        out.append(f"- Source: {citation.uri_display}")
        if citation.caption:
            out.append(f"- Note: {citation.caption}")
        out.append("")
        snippet = _truncate(citation.snippet, 500)
        if snippet:
            out.append(f"> {snippet}")
            out.append("")

    out.append("## Sources pulled")
    out.append("")
    out.append(f"_{draft.ledger_summary}_")
    out.append("")
    out.append("| Source | Kind | Status | Rows | Cited as |")
    out.append("|---|---|---|---|---|")
    for row in draft.ledger:
        ns = ", ".join(f"[{n}]" for n in row.citation_ns) or "—"
        out.append(
            f"| {row.binding_id} | {BINDING_KIND_LABEL.get(row.kind, row.kind)} | "
            f"{row.status_text} | {row.row_count if row.row_count is not None else '—'} "
            f"| {ns} |"
        )
    out.append("")
    return "\n".join(out) + "\n"


_CSV_COLUMNS = [
    "n",
    "citation_id",
    "source_type",
    "source_word",
    "title",
    "source_uri",
    "locator",
    "version_label",
    "version_value",
    "retrieved_at",
    "section_id",
    "section_title",
    "claim_text",
    "snippet",
    "chunk_id",
    "doc_id",
    "run_id",
    "instance_id",
    "template",
    "model_version",
    "notice",
]


def _citations_csv(summary: RunSummary, draft: DraftView | None) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_CSV_COLUMNS)
    if draft is None:
        return buffer.getvalue()
    template = f"{summary.template_key}@{summary.template_version}"
    for citation in draft.citations:
        writer.writerow(
            [
                citation.n,
                citation.citation_id,
                citation.source_type,
                citation.source_word,
                citation.title,
                citation.uri_display,
                "; ".join(f"{t}: {v}" for t, v in citation.locator_rows),
                citation.version_label,
                citation.version_value,
                citation.retrieved_iso,
                citation.section_id,
                citation.section_title,
                citation.claim_text,
                citation.snippet,
                citation.chunk_id or "",
                citation.doc_id,
                summary.run_id,
                citation.instance_id,
                template,
                summary.model_version,
                draft.notice,
            ]
        )
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Gallery: filter / group / sort  (contract §5.2, §5.6, §5.7)
# ---------------------------------------------------------------------------


def gallery_url(
    *,
    group: str = "",
    sort: str = "",
    tags: Iterable[str] = (),
    q: str = "",
    taxonomy: Any = None,
) -> str:
    """Canonical `GET /` URL. Default group/sort are left out of the query."""
    params: list[tuple[str, str]] = []
    if group and (taxonomy is None or group != taxonomy.default_group):
        params.append(("group", group))
    if sort and (taxonomy is None or sort != taxonomy.default_sort):
        params.append(("sort", sort))
    params.extend(("tag", str(t)) for t in tags)
    if q:
        params.append(("q", q))
    return "/" + (f"?{urlencode(params)}" if params else "")


def _card_matches(
    card: TemplateCard, selected: Mapping[str, list[str]], needle: str
) -> bool:
    """OR within a facet, AND across facets, plus a plain substring search."""
    if needle and needle not in card.search:
        return False
    tokens = set(card.tag_tokens)
    for facet_id, values in selected.items():
        if not values:
            continue
        if not tokens & {f"{facet_id}:{v}" for v in values}:
            return False
    return True


def _sort_key(sort: str) -> Any:
    if sort == "updated":
        return lambda c: (-c.updated_ts, c.title.casefold(), c.key.casefold())
    if sort == "sections":
        return lambda c: (-c.n_sections, c.title.casefold(), c.key.casefold())
    if sort == "ready":
        return lambda c: (
            _READINESS_ORDER.get(c.readiness, 9),
            -(c.sources_ready / max(c.sources_total, 1)),
            c.title.casefold(),
            c.key.casefold(),
        )
    if sort == "owner":
        return lambda c: (c.owner.casefold() or "￿", c.title.casefold(), c.key.casefold())
    return lambda c: (c.title.casefold(), c.key.casefold())


def _heading_id(prefix: str, value: str, used: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-") or "none"
    candidate = f"g-{prefix}-{slug}"
    n = 2
    while candidate in used:
        candidate = f"g-{prefix}-{slug}-{n}"
        n += 1
    used.add(candidate)
    return candidate


def _group_cards(
    cards: list[TemplateCard], group: str, taxonomy: Any, discovered: Mapping[str, list[str]]
) -> list[GroupView]:
    """Group order is STABLE and never touched by `sort`; untagged is last."""
    used: set[str] = set()

    if group == GROUP_NONE:
        return [
            GroupView(
                key="all",
                facet_id="",
                value_id="",
                label="",
                heading_id=_heading_id("all", "", used),
                untagged=False,
                count=len(cards),
                cards=list(cards),
            )
        ]

    if group == "owner":
        buckets: dict[str, list[TemplateCard]] = {}
        for card in cards:
            buckets.setdefault(card.owner.strip(), []).append(card)
        order = sorted((o for o in buckets if o), key=str.casefold)
        if "" in buckets:
            order.append("")
        return [
            GroupView(
                key=f"owner:{owner or UNTAGGED}",
                facet_id="owner",
                value_id=owner or UNTAGGED,
                label=owner or "No owning team",
                heading_id=_heading_id("owner", owner or "none", used),
                untagged=not owner,
                count=len(buckets[owner]),
                cards=buckets[owner],
            )
            for owner in order
        ]

    if group == "scope":
        # "Mine" first, deliberately. Someone grouping by this is looking for
        # their own templates; putting the shared library first would bury them
        # under a dozen rows they did not come for. Grouping is where this
        # belongs rather than a sort, because the page groups before it sorts —
        # a scope-aware sort was written first and never showed, since the
        # grouping ran ahead of it.
        buckets = {}
        for card in cards:
            buckets.setdefault(card.scope, []).append(card)
        return [
            GroupView(
                key=f"scope:{scope}",
                facet_id="scope",
                value_id=scope,
                label="Just me" if scope == "user" else "Everyone",
                heading_id=_heading_id("scope", scope, used),
                untagged=False,
                count=len(buckets[scope]),
                cards=buckets[scope],
            )
            for scope in ("user", "universal")
            if scope in buckets
        ]

    if group == "readiness":
        buckets = {}
        for card in cards:
            buckets.setdefault(str(card.readiness), []).append(card)
        order = sorted(buckets, key=lambda r: _READINESS_ORDER.get(r, 9))
        return [
            GroupView(
                key=f"readiness:{state}",
                facet_id="readiness",
                value_id=state,
                label=_READINESS_GROUP_LABEL.get(state, state),
                heading_id=_heading_id("readiness", state, used),
                untagged=False,
                count=len(buckets[state]),
                cards=buckets[state],
            )
            for state in order
        ]

    facet = taxonomy.facet(group)
    value_order: list[str] = list(facet.value_ids) if facet is not None else []
    known = set(value_order)
    extra = [v for v in discovered.get(group, ()) if v not in known]
    value_order.extend(sorted(extra, key=lambda v: taxonomy.label_for(group, v).casefold()))

    groups: list[GroupView] = []
    for value_id in value_order:
        members = [c for c in cards if value_id in (c.tags.get(group) or ())]
        if not members:
            continue
        groups.append(
            GroupView(
                key=f"{group}:{value_id}",
                facet_id=group,
                value_id=value_id,
                label=taxonomy.label_for(group, value_id),
                heading_id=_heading_id(group, value_id, used),
                untagged=False,
                count=len(members),
                cards=members,
            )
        )
    untagged = [c for c in cards if not (c.tags.get(group) or ())]
    if untagged:
        groups.append(
            GroupView(
                key=f"{group}:{UNTAGGED}",
                facet_id=group,
                value_id=UNTAGGED,
                label=taxonomy.untagged_label_for(group),
                heading_id=_heading_id(group, "none", used),
                untagged=True,
                count=len(untagged),
                cards=untagged,
            )
        )
    return groups


def _build_gallery_view(
    runnable: list[TemplateCard],
    unavailable: list[TemplateCard],
    taxonomy: Any,
    *,
    group: str | None,
    sort: str | None,
    tags: Iterable[str],
    q: str,
) -> GalleryView:
    text = " ".join(str(q or "").split())
    needle = text.casefold()

    # --- the vocabulary that actually exists: config + what the corpus uses
    discovered: dict[str, list[str]] = {}
    for card in runnable:
        for facet_id, values in card.tags.items():
            bucket = discovered.setdefault(facet_id, [])
            for value_id in values or ():
                if value_id not in bucket:
                    bucket.append(value_id)

    facet_ids: list[str] = [f.id for f in taxonomy.facets]
    for facet_id in discovered:
        if facet_id not in facet_ids:
            facet_ids.append(facet_id)

    def known_values(facet_id: str) -> set[str]:
        facet = taxonomy.facet(facet_id)
        out = set(facet.value_ids) if facet is not None else set()
        out.update(discovered.get(facet_id, ()))
        return out

    # --- canonicalise the query string. A stale bookmark widens the result
    # --- set; it never 404s and never renders an empty page.
    selected: dict[str, list[str]] = {}
    for token in tags:
        pair = parse_token(str(token))
        if pair is None:
            continue
        facet_id, value_id = pair
        if facet_id not in facet_ids:
            continue
        if value_id != UNTAGGED and value_id not in known_values(facet_id):
            continue
        bucket = selected.setdefault(facet_id, [])
        if value_id not in bucket:
            bucket.append(value_id)
    selected = {k: v for k, v in selected.items() if v}

    groupable = {f.id for f in taxonomy.groupable_facets()}
    valid_groups = groupable | set(DERIVED_GROUPS) | {GROUP_NONE}
    active_group = str(group or "").strip().lower()
    if active_group not in valid_groups:
        active_group = (
            taxonomy.default_group
            if taxonomy.default_group in valid_groups
            else GROUP_NONE
        )

    active_sort = str(sort or "").strip().lower()
    if active_sort not in _SORT_IDS:
        active_sort = taxonomy.default_sort if taxonomy.default_sort in _SORT_IDS else "name"

    # --- the filter rail. Counts for a facet ignore that facet's OWN
    # --- selections, so a value showing (12) really does yield >= 12.
    facets: list[FacetView] = []
    for facet_id in facet_ids:
        facet = taxonomy.facet(facet_id)
        others = {k: v for k, v in selected.items() if k != facet_id}
        pool = [c for c in runnable if _card_matches(c, others, needle)]

        counts: dict[str, int] = {}
        n_untagged = 0
        for card in pool:
            values = card.tags.get(facet_id) or ()
            if not values:
                n_untagged += 1
            for value_id in values:
                counts[value_id] = counts.get(value_id, 0) + 1

        here = selected.get(facet_id, [])
        views: list[FacetValueView] = []
        configured = list(facet.values) if facet is not None else []
        for value in configured:
            if value.deprecated and not counts.get(value.id) and value.id not in here:
                continue
            views.append(
                FacetValueView(
                    id=value.id,
                    label=value.label,
                    token=f"{facet_id}:{value.id}",
                    count=counts.get(value.id, 0),
                    selected=value.id in here,
                )
            )
        known = {v.id for v in configured}
        for value_id in sorted(
            (v for v in discovered.get(facet_id, ()) if v not in known),
            key=lambda v: (taxonomy.label_for(facet_id, v).casefold(), v),
        ):
            views.append(
                FacetValueView(
                    id=value_id,
                    label=taxonomy.label_for(facet_id, value_id),
                    token=f"{facet_id}:{value_id}",
                    count=counts.get(value_id, 0),
                    selected=value_id in here,
                )
            )
        if n_untagged or UNTAGGED in here:
            views.append(
                FacetValueView(
                    id=UNTAGGED,
                    label=taxonomy.untagged_label_for(facet_id),
                    token=f"{facet_id}:{UNTAGGED}",
                    count=n_untagged,
                    selected=UNTAGGED in here,
                )
            )
        if not views:
            continue
        facets.append(
            FacetView(
                id=facet_id,
                label=taxonomy.facet_label(facet_id),
                description=(facet.description if facet is not None else ""),
                multi=(facet.is_multi if facet is not None else True),
                groupable=(facet.groupable if facet is not None else False),
                untagged_label=taxonomy.untagged_label_for(facet_id),
                values=views,
                n_selected=len(here),
            )
        )

    shown = [c for c in runnable if _card_matches(c, selected, needle)]
    shown.sort(key=_sort_key(active_sort))

    # The grouping facet is already named by every section heading, so its
    # chip is redundant on the card. This one rule is what keeps the default
    # view calm (contract §5.4 rule 1).
    suppress = active_group if taxonomy.facet(active_group) is not None else ""
    for card in shown:
        card.chips, card.n_more_tags = _chips_for(
            card.tags, taxonomy, suppress_facet=suppress
        )

    groups = _group_cards(shown, active_group, taxonomy, discovered)

    summary: list[str] = []
    for facet_id in facet_ids:
        values = selected.get(facet_id) or []
        if not values:
            continue
        labels = [
            taxonomy.untagged_label_for(facet_id)
            if v == UNTAGGED
            else taxonomy.label_for(facet_id, v)
            for v in values
        ]
        summary.append(f"{taxonomy.facet_label(facet_id)}: " + " or ".join(labels))
    if text:
        summary.append(f"Search: “{text}”")

    n_active = sum(len(v) for v in selected.values())
    group_options = [(f.id, f.label) for f in taxonomy.groupable_facets()]
    group_options += [
        ("scope", "Who can see it"),
        ("owner", "Owning team"),
        ("readiness", "Source readiness"),
        (GROUP_NONE, "Nothing (one flat list)"),
    ]

    return GalleryView(
        group=active_group,
        sort=active_sort,
        q=text,
        facets=facets,
        groups=groups,
        cards=shown,
        unavailable=list(unavailable),
        group_options=group_options,
        sort_options=list(SORT_OPTIONS),
        n_shown=len(shown),
        n_total=len(runnable),
        n_active=n_active,
        active_summary=summary,
        clear_url=gallery_url(group=active_group, sort=active_sort, taxonomy=taxonomy),
        filtering=bool(n_active or text),
        taxonomy_ok=not bool(getattr(taxonomy, "load_errors", ())),
    )


# ---------------------------------------------------------------------------
# Authoring: form -> draft, structural ops, editor context
# ---------------------------------------------------------------------------


def _form_str(form: Any, name: str, default: str = "") -> str:
    value = form.get(name)
    if value is None:
        return default
    return str(value)


def _form_list(form: Any, name: str) -> list[str]:
    getlist = getattr(form, "getlist", None)
    if getlist is None:
        value = form.get(name)
        return [] if value is None else [str(value)]
    return [str(v) for v in getlist(name)]


def _form_flag(form: Any, name: str) -> bool:
    return _form_str(form, name).strip().lower() in ("1", "true", "yes", "on")


def _form_keys(form: Any) -> list[str]:
    seen: list[str] = []
    try:
        candidates = list(form.keys())
    except Exception:  # noqa: BLE001 - a Mapping without keys() is still usable
        candidates = []
    for key in candidates:
        name = str(key)
        if name not in seen:
            seen.append(name)
    return seen


def _row_keys(form: Any, group: str) -> list[str]:
    """DOM order for one repeating group, from its hidden anchor field."""
    out: list[str] = []
    for raw in _form_list(form, f"{group}.k"):
        key = raw.strip()
        if key and _ROW_KEY_RE.match(key) and key not in out:
            out.append(key)
    return out


def parse_params_text(text: str) -> dict[str, str]:
    """One `name = value` per line (contract R12). Blank lines and `#` skipped."""
    out: dict[str, str] = {}
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, value = stripped.partition("=")
        if not sep:
            name, sep, value = stripped.partition(":")
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


def format_params_text(params: Mapping[str, str]) -> str:
    return "\n".join(f"{k} = {v}" for k, v in (params or {}).items())


def _split_list(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n]", str(text or "")) if p.strip()]


def _parse_passthrough(text: str) -> dict[str, Any]:
    if not str(text or "").strip():
        return {}
    import yaml

    try:
        loaded = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - a bad paste must not break the editor
        return {}
    return loaded if isinstance(loaded, dict) else {}


def tags_from_form(form: Any, taxonomy: Any) -> dict[str, list[str]]:
    """Read every `tags__*` / `tags_new__*` field, including facets that are
    not in the config — an unknown facet must survive a round trip."""
    raw: dict[str, list[str]] = {}
    for name in _form_keys(form):
        if name.startswith("tags__"):
            facet_id = name[len("tags__") :]
            raw.setdefault(facet_id, []).extend(_form_list(form, name))
        elif name.startswith("tags_new__"):
            facet_id = name[len("tags_new__") :]
            bucket = raw.setdefault(facet_id, [])
            for chunk in _split_list(" , ".join(_form_list(form, name))):
                slug = slugify_value(chunk)
                if slug:
                    bucket.append(slug)
    shaped = _shape_tag_map(raw)
    # Keep facets in config order first so the writer's front matter is stable.
    ordered: dict[str, list[str]] = {}
    for facet in taxonomy.facets:
        if facet.id in shaped:
            ordered[facet.id] = shaped.pop(facet.id)
    ordered.update(shaped)
    return {k: v for k, v in ordered.items() if v}


def draft_from_form(form: Any, *, taxonomy: Any = None) -> Any:
    """Rebuild the whole `TemplateDraft` from a submitted editor form.

    Row order comes from the repeated hidden `input.k` / `source.k` /
    `section.k` anchors, so nothing is ever renumbered between round trips.
    """
    taxonomy = taxonomy if taxonomy is not None else load_taxonomy()

    granularity = _form_str(form, "citation_granularity", "claim").strip().lower()
    if granularity not in ("claim", "paragraph", "section"):
        granularity = "claim"
    try:
        min_per_paragraph = int(str(_form_str(form, "citation_min", "1")).strip() or 1)
    except ValueError:
        min_per_paragraph = 1
    min_per_paragraph = max(0, min(min_per_paragraph, 10))

    draft = TemplateDraft(
        report_type=_form_str(form, "report_type").strip().lower(),
        title=" ".join(_form_str(form, "title").split()),
        description=" ".join(_form_str(form, "description").split()),
        version=_form_str(form, "version").strip() or "0.1.0",
        owner=_form_str(form, "owner").strip(),
        tags=tags_from_form(form, taxonomy),
        doc_heading=" ".join(_form_str(form, "doc_heading").split()),
        citation_required=_form_flag(form, "citation_required"),
        citation_granularity=granularity,
        citation_min_per_paragraph=min_per_paragraph,
        passthrough=_parse_passthrough(_form_str(form, "passthrough_yaml")),
    )

    for key in _row_keys(form, "input"):
        draft.inputs.append(
            DraftInput(
                key=key,
                id=_form_str(form, f"input.{key}.id").strip().lower(),
                prompt=" ".join(_form_str(form, f"input.{key}.prompt").split()),
                required=_form_flag(form, f"input.{key}.required"),
            )
        )

    for key in _row_keys(form, "source"):
        kind = _form_str(form, f"source.{key}.kind").strip().lower()
        if kind not in SOURCE_KINDS:
            kind = "bigquery"
        draft.sources.append(
            DraftSource(
                key=key,
                id=_form_str(form, f"source.{key}.id").strip().lower(),
                kind=kind,
                required=_form_flag(form, f"source.{key}.required"),
                dataset=_form_str(form, f"source.{key}.dataset").strip(),
                query_id=_form_str(form, f"source.{key}.query_id").strip(),
                sql=_form_str(form, f"source.{key}.sql").strip(),
                space=_form_str(form, f"source.{key}.space").strip(),
                cql=_form_str(form, f"source.{key}.cql").strip(),
                page_id=_form_str(form, f"source.{key}.page_id").strip(),
                filter_tags=_split_list(_form_str(form, f"source.{key}.filter_tags")),
                connector=_form_str(form, f"source.{key}.connector").strip(),
                endpoint=_form_str(form, f"source.{key}.endpoint").strip(),
                params=parse_params_text(_form_str(form, f"source.{key}.params")),
            )
        )

    for key in _row_keys(form, "section"):
        # Source references are kept VERBATIM, including keys with no matching
        # source row. Dropping them here would silently discard the user's
        # intent and leave a section with no evidence behind it, while
        # `validate_draft`'s `dangling_source` / `dangling_table` errors — the
        # rules written to catch exactly that — could never fire. The one
        # legitimate way a reference disappears is the `remove:` / `retype:`
        # cascade in `apply_structural_op`, which says so in `cascade_notes`.
        picked = [k for k in _form_list(form, f"section.{key}.sources") if k]
        draft.sections.append(
            DraftSection(
                key=key,
                heading=" ".join(_form_str(form, f"section.{key}.heading").split()),
                instruction=_form_str(form, f"section.{key}.instruction").strip(),
                source_keys=list(dict.fromkeys(picked)),
                table_key=_form_str(form, f"section.{key}.table").strip(),
                visual=_form_str(form, f"section.{key}.visual").strip(),
            )
        )
    return draft


def is_structural_op(op: str) -> bool:
    return str(op or "").startswith(("add:", "remove:", "move:", "retype:"))


def _new_row_key(prefix: str, existing: Iterable[str]) -> str:
    taken = set(existing)
    n = len(taken) + 1
    while f"{prefix}{n}" in taken:
        n += 1
    return f"{prefix}{n}"


def _source_label(draft: Any, key: str) -> str:
    for index, source in enumerate(draft.sources, start=1):
        if source.key == key:
            return source.id or f"source {index}"
    return key


def _drop_source_references(draft: Any, key: str) -> tuple[list[int], list[int]]:
    used_by = [i for i, s in enumerate(draft.sections, start=1) if key in s.source_keys]
    table_for = [i for i, s in enumerate(draft.sections, start=1) if s.table_key == key]
    for section in draft.sections:
        section.source_keys = [k for k in section.source_keys if k != key]
        if section.table_key == key:
            section.table_key = ""
    return used_by, table_for


def _sections_phrase(numbers: list[int]) -> str:
    return f"{_plural(len(numbers), 'section')} {_join_numbers(numbers)}"


def _cascade_sentence(lead: str, used_by: list[int], table_for: list[int]) -> str:
    if used_by and table_for:
        body = (
            f"used by {_sections_phrase(used_by)}, and was the table for "
            f"{_sections_phrase(table_for)}"
        )
    elif used_by:
        body = f"used by {_sections_phrase(used_by)}"
    elif table_for:
        body = f"the table for {_sections_phrase(table_for)}"
    else:
        return ""
    tail = (
        "That reference has been cleared."
        if len(used_by) + len(table_for) == 1
        else "Those references have been cleared."
    )
    return f"{lead} It was {body}. {tail} Nothing is written until you press Save."


def _join_numbers(numbers: list[int]) -> str:
    text = [str(n) for n in numbers]
    if len(text) == 1:
        return text[0]
    return ", ".join(text[:-1]) + " and " + text[-1]


def apply_structural_op(
    draft: Any, op: str, *, add_source_kind: str = "bigquery"
) -> list[str]:
    """Apply one add / remove / move / retype op IN MEMORY and cascade.

    Nothing here touches `report-templates/`: the draft is not the file, so
    the undo for any mistake is "do not press Save".
    """
    notes: list[str] = []
    parts = str(op or "").split(":")
    verb = parts[0] if parts else ""

    if verb == "add" and len(parts) >= 2:
        what = parts[1]
        if what == "input":
            draft.inputs.append(DraftInput(key=_new_row_key("i", [r.key for r in draft.inputs])))
        elif what == "source":
            kind = str(add_source_kind or "bigquery").strip().lower()
            if kind not in SOURCE_KINDS:
                kind = "bigquery"
            draft.sources.append(
                DraftSource(key=_new_row_key("s", [r.key for r in draft.sources]), kind=kind)
            )
        elif what == "section":
            draft.sections.append(
                DraftSection(key=_new_row_key("t", [r.key for r in draft.sections]))
            )
        return notes

    if verb == "remove" and len(parts) >= 3:
        what, key = parts[1], parts[2]
        if what == "input":
            draft.inputs = [r for r in draft.inputs if r.key != key]
        elif what == "source":
            label = _source_label(draft, key)
            draft.sources = [r for r in draft.sources if r.key != key]
            used_by, table_for = _drop_source_references(draft, key)
            sentence = _cascade_sentence(f"Removed source “{label}”.", used_by, table_for)
            if sentence:
                notes.append(sentence)
        elif what == "section":
            draft.sections = [r for r in draft.sections if r.key != key]
        return notes

    if verb == "move" and len(parts) >= 4:
        what, key, direction = parts[1], parts[2], parts[3]
        rows = {
            "input": draft.inputs,
            "source": draft.sources,
            "section": draft.sections,
        }.get(what)
        if rows is None:
            return notes
        index = next((i for i, r in enumerate(rows) if r.key == key), -1)
        if index < 0:
            return notes
        target = index - 1 if direction == "up" else index + 1
        if 0 <= target < len(rows):
            rows[index], rows[target] = rows[target], rows[index]
        return notes

    if verb == "retype" and len(parts) >= 2:
        key = parts[1]
        source = next((s for s in draft.sources if s.key == key), None)
        if source is not None and source.kind != "bigquery":
            table_for = [
                i for i, s in enumerate(draft.sections, start=1) if s.table_key == key
            ]
            for section in draft.sections:
                if section.table_key == key:
                    section.table_key = ""
            if table_for:
                notes.append(
                    f"Source “{source.id or key}” is no longer a BigQuery "
                    f"source, so it was cleared as the table for "
                    f"{_plural(len(table_for), 'section')} {_join_numbers(table_for)}. "
                    "Nothing is written until you press Save."
                )
        return notes

    return notes


def validate_template_draft(
    draft: Any,
    *,
    existing_keys: Sequence[str] = (),
    is_new: bool = True,
    taxonomy: Any = None,
    base_draft: Any = None,
) -> list[Any]:
    """`validate_draft` merged with the taxonomy's own view of the tags.

    Rules 4 and 5 of §3.1 (a required facet with no value; more than one value
    on a single-value facet) are the only tag problems that BLOCK a save.
    Everything else is a warning, so an unknown value typed by another team
    survives a round trip instead of being silently dropped.
    """
    taxonomy = taxonomy if taxonomy is not None else load_taxonomy()
    issues = list(
        validate_draft(
            draft,
            existing_keys=tuple(existing_keys),
            is_new=is_new,
            # The live registry, so a reference that resolves to nothing is
            # visible while authoring rather than at run-setup — which is to
            # say, after someone has picked a template, filled in a compound
            # and pressed go.
            known_query_ids=tuple(query_registry().ids()),
        )
    )

    for issue in taxonomy.validate(draft.tags):
        blocking = issue.code in ("missing_required", "cardinality")
        issues.append(
            DraftIssue(
                field=f"tags__{issue.facet}",
                severity="error" if blocking else "warning",
                code=issue.code,
                message=issue.message,
                fix_hint=(
                    "Choose a value before saving." if blocking else ""
                ),
            )
        )

    if base_draft is not None and _structure_changed(base_draft, draft):
        if base_draft.version == draft.version:
            issues.append(
                DraftIssue(
                    field="version",
                    severity="warning",
                    code="structure_changed_no_bump",
                    message=(
                        "The sections, inputs or sources changed but the version "
                        "is still " + str(draft.version) + "."
                    ),
                    fix_hint="Bump the version so a run can be traced to what it used.",
                )
            )
    return issues


def _structure_changed(before: Any, after: Any) -> bool:
    def shape(draft: Any) -> tuple:
        return (
            tuple((i.id, i.required) for i in draft.inputs),
            tuple((s.id, s.kind) for s in draft.sources),
            tuple(s.heading for s in draft.sections),
        )

    return shape(before) != shape(after)


def draft_has_errors(issues: Iterable[Any]) -> bool:
    return any(getattr(i, "severity", "") == "error" for i in issues)


# --- editor context --------------------------------------------------------


def _row_errors(field_errors: Mapping[str, str], prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, message in field_errors.items():
        if name.startswith(prefix):
            out[name[len(prefix) :]] = message
    return out


def _editor_tag_facets(
    tags: Mapping[str, list[str]], taxonomy: Any, field_errors: Mapping[str, str]
) -> list[EditorFacet]:
    facet_ids = [f.id for f in taxonomy.facets]
    for facet_id in tags:
        if facet_id not in facet_ids:
            facet_ids.append(facet_id)

    out: list[EditorFacet] = []
    for facet_id in facet_ids:
        facet = taxonomy.facet(facet_id)
        selected = list(tags.get(facet_id) or ())
        values: list[EditorFacetValue] = []
        configured = list(facet.values) if facet is not None else []
        for value in configured:
            if value.deprecated and value.id not in selected:
                continue
            values.append(
                EditorFacetValue(
                    id=value.id,
                    label=value.label,
                    description=value.description,
                    selected=value.id in selected,
                    deprecated=value.deprecated,
                    unknown=False,
                )
            )
        known = {v.id for v in configured}
        for value_id in selected:
            if value_id in known:
                continue
            values.append(
                EditorFacetValue(
                    id=value_id,
                    label=value_id,
                    description="",
                    selected=True,
                    deprecated=False,
                    unknown=True,
                )
            )
        out.append(
            EditorFacet(
                id=facet_id,
                label=taxonomy.facet_label(facet_id),
                description=(facet.description if facet is not None else ""),
                multi=(facet.is_multi if facet is not None else True),
                required=(facet.required if facet is not None else False),
                open_mode=(facet.mode == "open" if facet is not None else True),
                field_name=f"tags__{facet_id}",
                new_field_name=(
                    f"tags_new__{facet_id}"
                    if (facet is None or facet.mode == "open")
                    else ""
                ),
                selected=selected,
                values=values,
                error=field_errors.get(f"tags__{facet_id}", ""),
                new_value_text="",
            )
        )
    return out


def editor_context(
    draft: Any,
    *,
    mode: str,
    key: str = "",
    base_sha: str = "",
    issues: Iterable[Any] = (),
    banner: Mapping[str, str] | None = None,
    cascade_notes: Iterable[str] = (),
    preview: str = "",
    checked: bool = False,
    conflict: bool = False,
    delete_info: Mapping[str, Any] | None = None,
    taxonomy: Any = None,
) -> dict[str, Any]:
    """The whole §5.3 context. Every key is ALWAYS present."""
    taxonomy = taxonomy if taxonomy is not None else load_taxonomy()
    issue_list = list(issues)

    field_errors: dict[str, str] = {}
    for issue in issue_list:
        if issue.severity == "error" and issue.field:
            field_errors.setdefault(issue.field, issue.message)

    ordered = [i for i in issue_list if i.severity == "error"]
    ordered += [i for i in issue_list if i.severity != "error"]

    input_rows = [
        EditorInputRow(
            key=row.key,
            id=row.id,
            prompt=row.prompt,
            required=row.required,
            errors=_row_errors(field_errors, f"input.{row.key}."),
        )
        for row in draft.inputs
    ]

    source_rows: list[EditorSourceRow] = []
    for index, row in enumerate(draft.sources, start=1):
        fields = {name: "" for name in SOURCE_FIELD_NAMES}
        fields.update(
            {
                "dataset": row.dataset,
                "query_id": row.query_id,
                "sql": row.sql,
                "space": row.space,
                "cql": row.cql,
                "page_id": row.page_id,
                "filter_tags": ", ".join(row.filter_tags or []),
                # The Oracle and SharePoint fields. Without these the row was
                # seeded from SOURCE_FIELD_NAMES as empty strings and never
                # filled, so opening an Oracle source showed a blank Service
                # and saving wrote the blank back — a silent wipe of the one
                # setting that says which database a figure came from.
                "service": row.service,
                "site": row.site,
                "folder": row.folder,
                "file_types": row.file_types,
                "query": row.query,
                "connector": row.connector,
                "endpoint": row.endpoint,
                "params": format_params_text(row.params or {}),
            }
        )
        source_rows.append(
            EditorSourceRow(
                key=row.key,
                id=row.id,
                kind=row.kind,
                required=row.required,
                legend=f"Source {index} — {row.id or 'unnamed'}",
                fields=fields,
                errors=_row_errors(field_errors, f"source.{row.key}."),
            )
        )

    section_rows = [
        EditorSectionRow(
            key=row.key,
            number=index,
            heading=row.heading,
            instruction=row.instruction,
            source_keys=list(row.source_keys or []),
            table_key=row.table_key,
            visual=row.visual,
            errors=_row_errors(field_errors, f"section.{row.key}."),
        )
        for index, row in enumerate(draft.sections, start=1)
    ]

    source_choices = [
        {
            "key": row.key,
            "id": row.id,
            "kind": row.kind,
            "label": row.id or f"Source {index} (unnamed)",
            "is_bigquery": row.kind == "bigquery",
        }
        for index, row in enumerate(draft.sources, start=1)
    ]

    if mode == "new":
        page_title = "New report template"
        form_action = "/templates"
        cancel_url = "/"
    elif mode == "delete":
        page_title = f"Delete “{draft.title or key}”?"
        form_action = f"/templates/{key}/delete"
        cancel_url = f"/new/{key}"
    else:
        page_title = f"Edit — {draft.title or key}"
        form_action = f"/templates/{key}"
        cancel_url = f"/new/{key}"

    import yaml

    passthrough_yaml = ""
    if draft.passthrough:
        try:
            passthrough_yaml = yaml.safe_dump(
                dict(draft.passthrough), sort_keys=False, allow_unicode=True
            ).strip()
        except Exception:  # noqa: BLE001 - never block the editor on a dump
            passthrough_yaml = ""

    return {
        "mode": mode,
        "page_title": page_title,
        "form_action": form_action,
        "cancel_url": cancel_url,
        "key": key,
        "key_locked": mode != "new",
        "base_sha": base_sha,
        "identity": {
            "report_type": draft.report_type,
            "title": draft.title,
            "description": draft.description,
            "version": draft.version,
            "owner": draft.owner,
            "doc_heading": draft.doc_heading,
        },
        "citation": {
            "required": bool(draft.citation_required),
            "granularity": draft.citation_granularity,
            "min_per_paragraph": int(draft.citation_min_per_paragraph),
        },
        "granularity_options": list(GRANULARITY_OPTIONS),
        "tag_facets": _editor_tag_facets(draft.tags, taxonomy, field_errors),
        "input_rows": input_rows,
        "source_rows": source_rows,
        "section_rows": section_rows,
        "source_choices": source_choices,
        "source_kinds": list(SOURCE_KIND_OPTIONS),
        "placeholders": [f"{{{{inputs.{r.id}}}}}" for r in draft.inputs if r.id],
        "issues": [
            {
                "field": i.field,
                "severity": i.severity,
                "code": i.code,
                "message": i.message,
                "fix_hint": i.fix_hint,
            }
            for i in ordered
        ],
        "field_errors": field_errors,
        # Built from the same issue list the cards render, so a tab's badge and
        # its panel can never disagree about how many problems are in there.
        "tabs": editor_tabs(issue_list),
        "n_errors": sum(1 for i in issue_list if i.severity == "error"),
        "n_warnings": sum(1 for i in issue_list if i.severity != "error"),
        "banner": dict(banner) if banner else None,
        "cascade_notes": list(cascade_notes),
        "preview": preview,
        "passthrough_yaml": passthrough_yaml,
        "checked": bool(checked),
        "conflict": bool(conflict),
        "delete_info": dict(delete_info) if delete_info else None,
    }


def preview_text(draft: Any, *, taxonomy: Any = None) -> str:
    """The Markdown a save would write. Never raises — the preview is a
    courtesy, and a serialiser problem is reported as a validation issue."""
    taxonomy = taxonomy if taxonomy is not None else load_taxonomy()
    try:
        return serialize_draft(draft, facet_order=taxonomy.facet_order_ids())
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_STORE: RunStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> RunStore:
    """Process-wide `RunStore` singleton. Thread-safe and idempotent."""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = RunStore()
    return _STORE


__all__ = [
    "Anchor",
    "BandKind",
    "CitationRef",
    "DERIVED_GROUPS",
    "EditorFacet",
    "EditorFacetValue",
    "EditorInputRow",
    "EditorSectionRow",
    "EditorSourceRow",
    "FacetView",
    "FacetValueView",
    "GRANULARITY_OPTIONS",
    "GROUP_NONE",
    "GalleryView",
    "GroupView",
    "MAX_CARD_CHIPS",
    "SORT_OPTIONS",
    "SOURCE_KIND_OPTIONS",
    "TEMPLATE_BACKUPS_DIR",
    "TEMPLATE_KEY_RE",
    "TEMPLATE_TRASH_DIR",
    "TagChip",
    "TemplateConflict",
    "TemplateWriteError",
    "apply_structural_op",
    "blank_draft",
    "clone_draft",
    "draft_from_form",
    "draft_has_errors",
    "editor_context",
    "gallery_url",
    "is_structural_op",
    "load_taxonomy",
    "parse_params_text",
    "preview_text",
    "read_sha256",
    "tags_from_form",
    "validate_template_draft",
    "CitationView",
    "ClaimView",
    "CORPUS_DIR",
    "DataTableView",
    "DRAFT_NOTICE",
    "DRAFT_NOTICE_STUB",
    "DraftView",
    "EDC_SQLITE",
    "EventView",
    "FormField",
    "INPUT_DEFAULTS",
    "LedgerRow",
    "MAX_INPUT_LEN",
    "MAX_WORKERS",
    "NumberChip",
    "OutlineItem",
    "ParagraphView",
    "PreflightIssue",
    "PreflightReport",
    "QUERIES_DIR",
    "REPO_ROOT",
    "RUNS_ROOT",
    "Readiness",
    "RunCancelled",
    "RunError",
    "RunNotTerminal",
    "RunRecord",
    "RunStatus",
    "RunStore",
    "RunSummary",
    "SECTION_LABEL",
    "STATUS_LABEL",
    "STUB_LLM_WARNING",
    "SectionOutline",
    "SectionProgress",
    "SectionStatus",
    "SectionView",
    "Segment",
    "Severity",
    "SourceSpec",
    "TEMPLATES_DIR",
    "TERMINAL_STATUSES",
    "TemplateCard",
    "TolerantSqlSafetyGate",
    "TrustBar",
    "build_api_gate",
    "EngineInfo",
    "build_llm_client",
    "build_stub_client",
    "resolve_engine",
    "build_sql_gate",
    "get_store",
    "load_corpus",
    "query_registry",
]


def unwired_connector_statuses() -> list[ConnectorStatus]:
    """Sources implemented against the same protocols but not registered here.

    Built by asking the real classes, not by writing prose about them. A
    hand-maintained description drifts from the code it describes, and the
    thing this page exists to prevent is a confident claim nobody checked.
    """
    from services.api_integration.sharepoint import SharePointConnector
    from services.data_integration.oracle_executor import OracleQueryExecutor

    return [
        SharePointConnector().status(),
        OracleQueryExecutor(service="oracle").status(),
        ConnectorStatus(
            connector_id="bigquery",
            kind="bigquery",
            configured=False,
            reachable=None,
            detail=(
                "Needs a GCP project and Application Default Credentials. "
                "GSK's VPC-SC perimeter blocks self-service access, so this is "
                "a request to the cloud platform team rather than something "
                "this app can provision."
            ),
            missing=("gcp-project", "application-default-credentials"),
        ),
    ]


def probe_connection(conn: Any) -> ConnectorStatus:
    """Actually reach out to a configured connection.

    The only function in this module that touches a network, and it runs only
    when someone presses Test. Every `status()` above answers from configuration
    alone, because a probe on a page render is what made every page in this app
    take sixteen seconds — and because "not checked" has to mean nobody checked,
    rather than "we checked quietly and did not say".

    Credentials are read from the environment here, at the moment of use, using
    the variable names the connection stores. They are never held on the
    connection and never written to disk.
    """
    settings = dict(getattr(conn, "settings", {}) or {})
    kind = str(getattr(conn, "kind", ""))
    cid = str(getattr(conn, "id", ""))

    base = conn.status()
    if not base.configured:
        return base

    def env(name_key: str) -> str:
        return os.environ.get(settings.get(name_key, ""), "")

    # Check the variables THIS connection names, before handing off to an
    # executor that would otherwise fall back to its own defaults and tell the
    # reader to set a variable their connection does not use. A connection
    # pointing at LIMS_PROD_DSN should not be told to set
    # REPORTGEN_ORACLE_DSN.
    # Only the variables this connection's chosen auth mode actually uses. A
    # Kerberos Oracle connection has no username, so checking for one would
    # report a correct connection as unconfigured.
    from services.api_gateway.connections import fields_for

    applicable = {f.name for f in fields_for(kind, settings)}
    env_keys = [k for k in settings if k.endswith("_env") and k in applicable]
    empty = [settings[k] for k in sorted(env_keys) if not os.environ.get(settings[k], "")]
    if empty:
        return ConnectorStatus(
            connector_id=cid,
            kind=kind,
            configured=False,
            reachable=None,
            detail=(
                f"{', '.join(empty)} "
                f"{'is' if len(empty) == 1 else 'are'} not set in this "
                f"process's environment, so there is nothing to connect with. "
                f"The connection itself is configured correctly."
            ),
            missing=tuple(empty),
        )

    if kind == "oracle":
        from services.data_integration.oracle_executor import OracleQueryExecutor

        return OracleQueryExecutor(
            service=settings.get("service", cid),
            dsn=env("dsn_env"),
            user=env("user_env"),
            password=env("password_env"),
            auth_mode=settings.get("auth_mode", "kerberos"),
            wallet_dir=settings.get("wallet_dir", ""),
        ).probe()

    if kind == "sharepoint":
        from services.api_integration.sharepoint import SharePointConnector

        return SharePointConnector(
            tenant_id=env("tenant_env"),
            client_id=env("client_env"),
            client_secret=env("secret_env"),
            transport=HttpTransport.from_settings(settings),
        ).probe()

    if kind == "bigquery":
        # GSK signs in to GCP with Google SSO, so the question a probe answers
        # is whether Application Default Credentials are present — that is the
        # SSO session, not a key file. Checked without importing the BigQuery
        # client, which lives in an optional extra.
        return _probe_google_credentials(cid, settings)

    # Confluence has no probe of its own yet. Saying so is the honest answer;
    # inventing a green tick for an untested path is the exact failure this
    # vocabulary exists to prevent.
    return ConnectorStatus(
        connector_id=cid,
        kind=kind,
        configured=True,
        reachable=None,
        detail=(
            f"No connection test exists for a {kind} connection yet, so nothing "
            f"was checked. The settings look complete "
            f"({HttpTransport.from_settings(settings).describe()})."
        ),
    )


def _probe_google_credentials(cid: str, settings: dict[str, str]) -> ConnectorStatus:
    """Are there usable Google credentials for this project?

    Deliberately checks credentials rather than running a query. A SELECT would
    also be a bill and a permission surface; "can this app prove who it is to
    Google" is the question someone pressing Test is asking, and it is the one
    that fails first.
    """
    mode = settings.get("auth_mode", "adc")
    try:
        import google.auth  # type: ignore[import-not-found]
    except ImportError:
        return ConnectorStatus(
            connector_id=cid,
            kind="bigquery",
            configured=False,
            reachable=None,
            detail=(
                "The google-auth library is not installed. Install the "
                "project's [gcp] extra."
            ),
            missing=("google-auth",),
        )
    try:
        _credentials, project = google.auth.default()
    except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
        return ConnectorStatus(
            connector_id=cid,
            kind="bigquery",
            configured=True,
            reachable=False,
            detail=(
                f"No Google credentials on this machine: {str(exc).splitlines()[0][:150]} "
                f"Run `gcloud auth application-default login` to sign in with SSO."
            ),
        )
    named = settings.get("project", "")
    note = ""
    if named and project and named != project:
        # Worth saying rather than silently proceeding: the credentials work,
        # but they are for somewhere else, and the query would run against a
        # project nobody configured.
        note = f" Credentials default to {project!r}, not {named!r}."
    return ConnectorStatus(
        connector_id=cid,
        kind="bigquery",
        configured=True,
        reachable=True,
        detail=(
            f"Google credentials present ({mode}).{note} This confirms sign-in "
            f"only — VPC-SC may still refuse the query from this network."
        ),
    )


#: The editor's tabs, in order. Each entry is (id, label, field prefixes).
#:
#: The form was one column roughly two thousand pixels tall, so configuring a
#: section meant scrolling past every source, and checking a source meant
#: scrolling back. Tabs cut that, but they introduce a failure the single column
#: did not have: a validation error can land on a panel nobody is looking at.
#:
#: Hence the prefixes. They are what lets `editor_tabs` count problems per tab,
#: put those counts on the labels, and open the tab holding the first error —
#: so hiding a panel never hides the reason a save was refused.
EDITOR_TABS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "details",
        "Details",
        ("report_type", "title", "description", "version", "owner", "doc_heading", "tags"),
    ),
    ("inputs", "Inputs", ("input",)),
    ("sources", "Sources", ("source",)),
    ("sections", "Sections", ("section",)),
    ("output", "Output", ("citation", "passthrough")),
)

#: Where an issue goes when its field matches no prefix. Details rather than a
#: silent drop: an unrouted problem still has to be counted somewhere a reader
#: will look, or the tab bar would say everything is fine while the save fails.
_FALLBACK_TAB = "details"


def tab_for_field(field: str) -> str:
    """Which tab a validation issue belongs on."""
    head = str(field or "").split(".", 1)[0]
    head = head.split("__", 1)[0]
    for tab_id, _label, prefixes in EDITOR_TABS:
        # Prefix, not equality. `citation_min` and `passthrough_yaml` are whole
        # field names rather than dotted paths, and an exact match sent both to
        # Details — counting an Output problem against the wrong tab, which is
        # the one thing this mapping exists to get right.
        if any(head == p or head.startswith(p + "_") for p in prefixes):
            return tab_id
    return _FALLBACK_TAB


def editor_tabs(issues: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """The tab bar: label, counts, and which one starts open.

    The open tab is the first one carrying an error, falling back to the first
    tab. Re-rendering after a refused save onto a panel that hides the reason
    would make the app look broken rather than strict.
    """
    errors: dict[str, int] = {}
    warnings: dict[str, int] = {}
    for issue in issues or ():
        tab = tab_for_field(getattr(issue, "field", ""))
        bucket = errors if getattr(issue, "severity", "") == "error" else warnings
        bucket[tab] = bucket.get(tab, 0) + 1

    first_with_error = next(
        (tab_id for tab_id, _l, _p in EDITOR_TABS if errors.get(tab_id)), ""
    )
    active = first_with_error or EDITOR_TABS[0][0]

    return [
        {
            "id": tab_id,
            "label": label,
            "n_errors": errors.get(tab_id, 0),
            "n_warnings": warnings.get(tab_id, 0),
            "active": tab_id == active,
        }
        for tab_id, label, _prefixes in EDITOR_TABS
    ]


# ---------------------------------------------------------------------------
# Runs list: grouping, sorting, filtering
# ---------------------------------------------------------------------------
#
# This was one boolean — `group=1` meant "by compound" and nothing else — plus
# a text box. Fifty runs of the same report type read as fifty identical rows,
# which is what made the list hard to use: the thing you scan for is the report
# type, and it was the one axis you could not organise by.
#
# Shaped like `gallery_view` on purpose. The templates page already had
# grouping, sorting and facets with a settled vocabulary; a second, different
# mechanism for the same three ideas would be two things to learn and two
# places to fix.

RUN_GROUP_OPTIONS: list[tuple[str, str]] = [
    ("template", "Report type"),
    ("compound", "Compound"),
    ("status", "State"),
    ("day", "Day"),
    (GROUP_NONE, "Nothing (one flat list)"),
]

RUN_SORT_OPTIONS: list[tuple[str, str]] = [
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
    ("template", "Report type (A–Z)"),
    ("compound", "Compound (A–Z)"),
    ("duration", "Longest first"),
    ("cited", "Most citations"),
]

_RUN_GROUP_IDS = frozenset(k for k, _ in RUN_GROUP_OPTIONS)
_RUN_SORT_IDS = frozenset(k for k, _ in RUN_SORT_OPTIONS)

#: Grouping by report type is the default. It is what the list is scanned by,
#: and an ungrouped page of near-identical rows is the state this replaced.
DEFAULT_RUN_GROUP = "template"


@dataclass
class RunGroup(_Dict):
    key: str
    label: str
    sub: str
    count: int
    heading_id: str
    runs: list[Any]


@dataclass
class RunListView(_Dict):
    groups: list[RunGroup]
    runs: list[Any]
    n_total: int
    n_shown: int
    group: str
    sort: str
    q: str
    state: str
    group_options: list[tuple[str, str]]
    sort_options: list[tuple[str, str]]
    states: list[dict[str, Any]]
    grouped: bool


def _haystack(summary: Any) -> str:
    """Everything a text filter should match on for one run."""
    parts = (
        summary.title,
        summary.template_title,
        summary.primary_input,
        summary.status_label,
        summary.run_id,
        summary.created_human,
    )
    return " ".join(str(p or "") for p in parts).lower()


def _run_sort_key(sort: str) -> Any:
    if sort == "oldest":
        return lambda s: (s.created_at, s.template_title.casefold())
    if sort == "template":
        return lambda s: (s.template_title.casefold(), _desc(s.created_at))
    if sort == "compound":
        return lambda s: ((s.primary_input or "￿").casefold(), _desc(s.created_at))
    if sort == "duration":
        # Negated rather than reversed, so the tie-break stays ascending by
        # title instead of flipping with it.
        return lambda s: (-(s.duration_s or 0.0), s.template_title.casefold())
    if sort == "cited":
        return lambda s: (-(s.n_sections_cited or 0), s.template_title.casefold())
    return lambda s: (_desc(s.created_at), s.template_title.casefold())


def _desc(value: str) -> tuple[int, ...]:
    """A descending sort key for an ISO timestamp, without reverse=True.

    `reverse=True` would also flip every tie-break, so two runs started in the
    same second would come back in reverse alphabetical order. Inverting the
    codepoints inverts only this field.
    """
    return tuple(-ord(c) for c in value)


def _run_group_of(summary: Any, group: str) -> tuple[str, str, str]:
    """(key, label, sub) for the bucket this run belongs in."""
    if group == "template":
        return (
            summary.template_key or "untitled",
            summary.template_title or summary.template_key,
            f"v{summary.template_version}" if summary.template_version else "",
        )
    if group == "compound":
        value = summary.primary_input or ""
        return (value or UNTAGGED, value or "No compound", "")
    if group == "status":
        return (summary.status, summary.status_label or summary.status, "")
    if group == "day":
        day = (summary.created_at or "")[:10]
        return (day or UNTAGGED, day or "No date", "")
    return ("all", "All runs", "")


def run_list_view(
    summaries: Sequence[Any],
    *,
    group: str | None = None,
    sort: str | None = None,
    q: str = "",
    state: str = "",
) -> RunListView:
    """Everything `GET /runs` renders, filtered, grouped and sorted server-side.

    Non-matching runs are omitted rather than rendered-then-hidden, so there is
    one predicate in one language — the same rule the template gallery follows.
    """
    n_total = len(summaries)

    active_group = str(group or "").strip().lower()
    if active_group not in _RUN_GROUP_IDS:
        active_group = DEFAULT_RUN_GROUP
    active_sort = str(sort or "").strip().lower()
    if active_sort not in _RUN_SORT_IDS:
        active_sort = "newest"

    needle = q.strip().lower()
    wanted_state = str(state or "").strip().lower()

    # Counts are taken before the state filter so the chips can say how many
    # each one would show. A facet whose count reflects the filter it applies
    # always reads "1" once clicked, which tells nobody anything.
    state_counts: dict[str, int] = {}
    for summary in summaries:
        state_counts[summary.status] = state_counts.get(summary.status, 0) + 1

    shown = [
        s
        for s in summaries
        if (not needle or needle in _haystack(s))
        and (not wanted_state or s.status == wanted_state)
    ]
    shown = sorted(shown, key=_run_sort_key(active_sort))

    groups: list[RunGroup] = []
    if active_group != GROUP_NONE:
        buckets: dict[str, list[Any]] = {}
        meta: dict[str, tuple[str, str]] = {}
        for summary in shown:
            key, label, sub = _run_group_of(summary, active_group)
            buckets.setdefault(key, []).append(summary)
            meta.setdefault(key, (label, sub))
        used: set[str] = set()
        # Group order follows the chosen sort: whatever is first inside a
        # bucket decides where the bucket sits, so "newest first" puts the
        # report type with the newest run at the top rather than sorting the
        # headings alphabetically and burying it.
        for key in sorted(buckets, key=lambda k: _run_sort_key(active_sort)(buckets[k][0])):
            label, sub = meta[key]
            groups.append(
                RunGroup(
                    key=key,
                    label=label,
                    sub=sub,
                    count=len(buckets[key]),
                    heading_id=_heading_id("run", key, used),
                    runs=buckets[key],
                )
            )

    return RunListView(
        groups=groups,
        runs=shown,
        n_total=n_total,
        n_shown=len(shown),
        group=active_group,
        sort=active_sort,
        q=q,
        state=wanted_state,
        group_options=list(RUN_GROUP_OPTIONS),
        sort_options=list(RUN_SORT_OPTIONS),
        states=[
            {
                "id": status,
                "label": STATUS_LABEL.get(status, status),
                "count": count,
                "selected": status == wanted_state,
            }
            for status, count in sorted(state_counts.items(), key=lambda kv: -kv[1])
        ],
        grouped=active_group != GROUP_NONE,
    )
