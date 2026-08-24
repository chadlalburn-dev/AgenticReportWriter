"""Serialise an editable draft back to the Markdown + front-matter format that
`load_report_doc` reads.

This is the inverse of `report_doc.load_report_doc`. It exists so templates can
be authored in the app: the editor edits a `TemplateDraft`, and this module turns
that draft back into a file that is indistinguishable from a hand-authored one.

Three invariants hold, and `write_template` enforces them at runtime rather than
only in tests:

  INV-1  A file written from a draft parsed out of an existing template loads
         back to an equivalent `ReportTemplate` (see `_comparable`).
  INV-2  `draft_from_text(serialize_draft(d)) == d` for any valid draft.
  INV-3  The writer never puts a file the loader rejects into report-templates/.
         Candidate text is proved against the real loader in a scratch directory
         first, and the final write is atomic.

Placeholders are emitted in the authored `{{inputs.X}}` form, never the
`{{report.X}}` form the loader rewrites them to, so a saved file matches a
hand-authored one byte-for-byte in that respect.

Imports: stdlib + pyyaml + `report_doc` (for the round-trip proof) only. This
module knows nothing about the tag taxonomy — facet ordering is injected.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

import yaml

from services.template_service.report_doc import (
    ReportDocError,
    _DIRECTIVE_RE,
    _FRONTMATTER_RE,
    _LEADING_NUM_RE,
    _SECTION_RE,
    load_report_doc,
)
from shared.schemas import ReportTemplate

# --- module constants -------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "report-templates"
VAR_DIR = REPO_ROOT / "var"
SCRATCH_DIR = VAR_DIR / "template-drafts"
BACKUP_DIR = VAR_DIR / "template-backups"
TRASH_DIR = VAR_DIR / "template-trash"

SourceKind = Literal[
    "bigquery", "oracle", "confluence", "sharepoint", "file", "api"
]
SOURCE_KINDS: tuple[SourceKind, ...] = (
    "bigquery",
    #: A relational source reached over a driver rather than an HTTP API.
    #: Separate from `bigquery` because the connection is configured
    #: differently and the citation should say which warehouse a figure came
    #: from — "the numbers came from a database" is not provenance.
    "oracle",
    "confluence",
    #: Files in SharePoint or OneDrive, decks included. Distinct from `file`,
    #: which reads a local folder: this one crosses a network and needs a
    #: configured connector, so it can be unreachable in a way a local folder
    #: cannot.
    "sharepoint",
    "file",
    "api",
)
Granularity = Literal["claim", "paragraph", "section"]
GRANULARITIES: tuple[Granularity, ...] = ("claim", "paragraph", "section")

REPORT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
PARAM_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*inputs\.([A-Za-z0-9_]+)\s*\}\}")
_FACET_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VALUE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

MAX_TITLE = 120
MAX_DESCRIPTION = 300

WRAP_WIDTH = 76
BACKUP_KEEP = 10

#: Front-matter keys this module models explicitly. Everything else is carried
#: through `TemplateDraft.passthrough` verbatim so hand-authored extras survive.
_MODELLED_KEYS = frozenset(
    {
        "report_type",
        "title",
        "version",
        "description",
        "owner",
        "updated",
        "tags",
        "inputs",
        "sources",
        "citation",
    }
)

#: `required:` is only written when it differs from what the loader assumes for
#: that source kind, which keeps hand-authored files byte-stable on re-save.
_KIND_REQUIRED_DEFAULT: dict[str, bool] = {
    "bigquery": True,
    #: Required like bigquery: a section built on a warehouse query has nothing
    #: to say without it, whereas supplementary documents merely add colour.
    "oracle": True,
    "confluence": False,
    "sharepoint": False,
    "file": False,
    "api": False,
}

#: Order in which kind-specific keys are emitted.
_KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "bigquery": ("dataset", "query_id", "sql"),
    "oracle": ("service", "query_id", "sql"),
    "confluence": ("space", "cql", "page_id"),
    "sharepoint": ("site", "folder", "file_types", "query"),
    "file": ("filter_tags",),
    "api": ("connector", "endpoint"),
}

#: Source kinds whose payload the filler renders as a table. `> Table:` may only
#: name one of these.
_TABULAR_KINDS = frozenset({"bigquery", "oracle", "api"})

#: A wrapped instruction line must never start with one of these, or the loader's
#: directive regex would swallow the rest of the instruction.
_DIRECTIVE_WORDS = ("Sources:", "Table:", "Instruction:", "Visual:")


# --- errors ----------------------------------------------------------------


class TemplateWriteError(ValueError):
    """Raised when a draft cannot be turned into a file the loader accepts."""

    def __init__(self, message: str, *, generated_text: str = "") -> None:
        super().__init__(message)
        self.generated_text = generated_text


class TemplateConflict(TemplateWriteError):
    """Raised by write_template when expected_sha256 does not match disk."""

    def __init__(self, path: Path, expected: str, actual: str) -> None:
        super().__init__(
            f"{path.name} changed on disk since it was opened "
            f"(expected {expected[:12] or '(new file)'}, found {actual[:12] or '(missing)'})"
        )
        self.path = Path(path)
        self.expected = expected
        self.actual = actual


# --- the draft -------------------------------------------------------------


@dataclass
class DraftInput:
    key: str = ""  # opaque editor row key; NOT serialised
    id: str = ""
    prompt: str = ""
    required: bool = True


@dataclass
class DraftSource:
    key: str = ""  # opaque editor row key; NOT serialised
    id: str = ""
    kind: str = "bigquery"
    required: bool = True
    # bigquery
    dataset: str = ""
    query_id: str = ""
    sql: str = ""
    # confluence
    space: str = ""
    cql: str = ""
    page_id: str = ""
    # file
    filter_tags: list[str] = field(default_factory=list)
    # oracle
    service: str = ""
    # sharepoint
    site: str = ""
    folder: str = ""
    file_types: str = ""
    query: str = ""
    # api
    connector: str = ""
    endpoint: str = ""
    # bigquery + api (also carried for other kinds when hand-authored)
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class DraftSection:
    key: str = ""  # opaque editor row key; NOT serialised
    heading: str = ""  # TITLE TEXT ONLY — the writer owns the "## N. " prefix
    instruction: str = ""
    source_keys: list[str] = field(default_factory=list)  # DraftSource.key values
    table_key: str = ""  # DraftSource.key or ""
    #: The `> Visual:` directive body, kept as the author typed it rather than
    #: parsed into fields. Two reasons: the round trip is exact, and the editor
    #: can expose it as one line the author already understands. Validation
    #: lives in the loader (`_parse_visual`), which is the only place that has
    #: to agree with the chart renderer.
    visual: str = ""
    #: Authored heading number, preserved so re-saving an existing file does not
    #: renumber it. `None` means "the writer numbers this 1..N"; `""` means the
    #: heading is deliberately unnumbered. See the deviation note in the module
    #: history: strict writer-owned numbering would rewrite ICH-mandated section
    #: numbers (e.g. "## 3.1 Nonclinical pharmacology") on a no-op save.
    number: str | None = None


@dataclass
class TemplateDraft:
    report_type: str = ""
    title: str = ""
    description: str = ""
    version: str = "0.1.0"
    owner: str = ""
    updated: str = ""  # ISO date; stamped by write_template
    tags: dict[str, list[str]] = field(default_factory=dict)
    doc_heading: str = ""  # the "# ..." line under the front matter
    citation_required: bool = True
    citation_granularity: str = "claim"
    citation_min_per_paragraph: int = 1
    inputs: list[DraftInput] = field(default_factory=list)
    sources: list[DraftSource] = field(default_factory=list)
    sections: list[DraftSection] = field(default_factory=list)
    #: Every top-level front-matter key the editor does not model (e.g.
    #: `output:`, `status:`), re-emitted verbatim after `citation:`.
    passthrough: dict[str, Any] = field(default_factory=dict)

    def source_by_key(self, key: str) -> DraftSource | None:
        for src in self.sources:
            if src.key == key:
                return src
        return None

    def source_ids(self) -> list[str]:
        return [s.id for s in self.sources]

    def input_ids(self) -> list[str]:
        return [i.id for i in self.inputs]

    def placeholders(self) -> list[str]:
        return ["{{inputs.%s}}" % i.id for i in self.inputs if i.id]


@dataclass(frozen=True, slots=True)
class DraftIssue:
    field: str  # form field name, e.g. "source.s4.query_id"; "" = whole form
    severity: Literal["error", "warning"]
    code: str
    message: str
    fix_hint: str = ""


@dataclass(frozen=True, slots=True)
class WriteResult:
    path: Path
    text: str
    sha256: str
    backup_path: Path | None
    created: bool


# --- YAML scalar emission ---------------------------------------------------


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _yaml_scalar(value: str) -> str:
    """Emit `value` as a single-line YAML scalar, quoting only when needed."""
    if _ISO_DATE_RE.match(value):
        # A plain ISO date loads as a `date`, whose str() is the same text, so
        # leaving it unquoted round-trips and reads better in the file.
        return value
    dumped = yaml.safe_dump(
        value, default_flow_style=True, width=10**6, allow_unicode=True
    )
    lines = [ln for ln in dumped.splitlines() if ln.strip() != "..."]
    return "\n".join(lines).strip()


def _flow_scalar(value: str) -> str:
    """Emit `value` inside a YAML *flow* collection (stricter than block)."""
    if (
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]*", value)
        and not re.fullmatch(r"\d+(\.\d+)?", value)
        and isinstance(yaml.safe_load(value), str)
    ):
        return value
    return json.dumps(value, ensure_ascii=False)


def _wrap(
    text: str,
    *,
    first_prefix: str,
    cont_prefix: str,
    width: int = WRAP_WIDTH,
    forbid_start: Sequence[str] = (),
) -> list[str]:
    """Greedy word wrap that refuses to start a continuation line with any of
    `forbid_start` — which is what keeps a wrapped instruction from being cut in
    half by the loader's `> Sources:` / `> Table:` directive regex."""
    words = text.split()
    if not words:
        return [first_prefix.rstrip()]

    def forbidden(word: str) -> bool:
        return any(word.startswith(w) for w in forbid_start)

    lines: list[str] = []
    i = 0
    while i < len(words):
        prefix = first_prefix if not lines else cont_prefix
        j = i + 1
        line = prefix + words[i]
        while j < len(words) and len(line) + 1 + len(words[j]) <= width:
            line += " " + words[j]
            j += 1
        # Back off a word at a time so the *next* line does not open with a
        # directive keyword. Never back off to an empty line.
        while j < len(words) and forbidden(words[j]) and j - i > 1:
            j -= 1
            line = prefix + " ".join(words[i:j])
        lines.append(line)
        i = j
    return lines


def _coerce_tags(raw: Any) -> dict[str, list[str]]:
    """Shape-normalise a front-matter `tags:` value. Lenient by design: reading a
    hand-authored file must never fail because of a malformed tag, so anything
    unusable is dropped rather than raised on."""
    if raw is None or raw == "" or raw == []:
        return {}
    pairs: dict[str, list[Any]] = {}
    if isinstance(raw, (list, tuple)):
        for element in raw:
            facet, sep, val = str(element).strip().partition(":")
            if sep:
                pairs.setdefault(facet.strip(), []).append(val.strip())
    elif isinstance(raw, dict):
        for facet_key, facet_values in raw.items():
            items = (
                list(facet_values)
                if isinstance(facet_values, (list, tuple))
                else [facet_values]
            )
            pairs.setdefault(str(facet_key).strip(), []).extend(items)
    else:
        return {}

    out: dict[str, list[str]] = {}
    for facet_key, items in pairs.items():
        facet = facet_key.strip().lower()
        if not _FACET_ID_RE.match(facet):
            continue
        seen: list[str] = []
        for item in items:
            if item is None:
                continue
            val = str(item).strip().lower()
            if not val or not _VALUE_ID_RE.match(val) or val in seen:
                continue
            seen.append(val)
        out[facet] = seen
    return out


# --- 3.3.1 serialise -------------------------------------------------------


def serialize_draft(
    draft: TemplateDraft, *, facet_order: Sequence[str] | None = None
) -> str:
    """Render `draft` as the complete .md file text.

    Pure: never touches the filesystem, never mutates `draft`, never reads a
    clock. Identical input produces byte-identical output.
    """
    out: list[str] = ["---"]

    out.append(f"report_type: {_yaml_scalar(draft.report_type)}")
    out.append(f"title: {_yaml_scalar(draft.title)}")
    out.append(f"version: {_yaml_scalar(draft.version)}")
    if draft.description:
        out.append("description: >")
        out.extend(_wrap(draft.description, first_prefix="  ", cont_prefix="  "))
    if draft.owner:
        out.append(f"owner: {_yaml_scalar(draft.owner)}")
    if draft.updated:
        out.append(f"updated: {_yaml_scalar(draft.updated)}")

    out.extend(_serialize_tags(draft.tags, facet_order))
    out.extend(_serialize_inputs(draft.inputs))
    out.extend(_serialize_sources(draft.sources))

    out.append("")
    out.append("citation:")
    out.append(f"  required: {_bool(draft.citation_required)}")
    out.append(f"  granularity: {_yaml_scalar(draft.citation_granularity)}")
    out.append(f"  min_per_paragraph: {int(draft.citation_min_per_paragraph)}")

    if draft.passthrough:
        out.append("")
        dumped = yaml.safe_dump(
            dict(draft.passthrough),
            sort_keys=False,
            # `None` keeps leaf collections inline (`formats: [docx, html]`),
            # which is how these keys are written by hand.
            default_flow_style=None,
            allow_unicode=True,
            width=10**6,
        )
        out.extend(dumped.rstrip("\n").splitlines())

    out.append("---")
    out.append("")

    if draft.doc_heading:
        out.append(f"# {draft.doc_heading}")
        out.append("")

    out.extend(_serialize_sections(draft))

    return "\n".join(out).rstrip("\n") + "\n"


def _bool(value: object) -> str:
    return "true" if value else "false"


def _serialize_tags(
    tags: dict[str, list[str]], facet_order: Sequence[str] | None
) -> list[str]:
    ordered = _ordered_facets(tags, facet_order)
    if not ordered:
        return []
    out = ["", "tags:"]
    for facet in ordered:
        values = tags.get(facet) or []
        if not values:
            continue
        if len(values) == 1:
            out.append(f"  {facet}: {_yaml_scalar(values[0])}")
        else:
            rendered = ", ".join(_flow_scalar(v) for v in values)
            out.append(f"  {facet}: [{rendered}]")
    if len(out) == 2:  # every facet was empty
        return []
    return out


def _ordered_facets(
    tags: dict[str, list[str]], facet_order: Sequence[str] | None
) -> list[str]:
    if not tags:
        return []
    if not facet_order:
        return [f for f, v in tags.items() if v]
    seen = list(facet_order)
    ordered = [f for f in seen if tags.get(f)]
    ordered.extend(f for f, v in tags.items() if v and f not in seen)
    return ordered


def _serialize_inputs(inputs: Sequence[DraftInput]) -> list[str]:
    if not inputs:
        return []
    out = ["", "inputs:"]
    for item in inputs:
        out.append(f"  - id: {_yaml_scalar(item.id)}")
        out.append(f"    prompt: {_yaml_scalar(item.prompt)}")
        out.append(f"    required: {_bool(item.required)}")
    return out


def _serialize_sources(sources: Sequence[DraftSource]) -> list[str]:
    if not sources:
        return []
    out = ["", "sources:"]
    for src in sources:
        out.append(f"  - id: {_yaml_scalar(src.id)}")
        out.append(f"    type: {_yaml_scalar(src.kind)}")
        for name in _KIND_FIELDS.get(src.kind, ()):
            value = getattr(src, name, "")
            if name == "filter_tags":
                if value:
                    rendered = ", ".join(_flow_scalar(str(t)) for t in value)
                    out.append(f"    filter_tags: [{rendered}]")
                continue
            if name == "sql":
                if value:
                    out.append("    sql: |")
                    body = textwrap.dedent(str(value)).strip("\n")
                    for line in body.splitlines():
                        out.append(("        " + line).rstrip() if line.strip() else "")
                continue
            if value:
                out.append(f"    {name}: {_yaml_scalar(str(value))}")
        if bool(src.required) != _KIND_REQUIRED_DEFAULT.get(src.kind, True):
            out.append(f"    required: {_bool(src.required)}")
        if src.params:
            rendered = ", ".join(
                f"{k}: {json.dumps(str(v), ensure_ascii=False)}"
                for k, v in src.params.items()
            )
            out.append(f"    params: {{ {rendered} }}")
    return out


def _serialize_sections(draft: TemplateDraft) -> list[str]:
    by_key = {s.key: s for s in draft.sources if s.key}
    out: list[str] = []
    for index, section in enumerate(draft.sections, start=1):
        number = str(index) if section.number is None else section.number
        prefix = f"{number}. " if number else ""
        out.append(f"## {prefix}{section.heading}".rstrip())
        out.append("")
        out.extend(
            _wrap(
                section.instruction,
                first_prefix="> Instruction: ",
                cont_prefix="> ",
                forbid_start=_DIRECTIVE_WORDS,
            )
        )
        source_ids = [
            by_key[k].id for k in section.source_keys if k in by_key and by_key[k].id
        ]
        out.append("> Sources: " + (", ".join(source_ids) if source_ids else "(none)"))
        table = by_key.get(section.table_key)
        out.append("> Table: " + (table.id if table and table.id else "(none)"))
        if section.visual.strip():
            out.append("> Visual: " + section.visual.strip())
        out.append("")
    return out


# --- 3.3.2 parse -----------------------------------------------------------


def draft_from_text(text: str, *, report_type: str = "") -> TemplateDraft:
    """Inverse of `serialize_draft`. Also accepts hand-authored files.

    Row keys are assigned deterministically as i1..iN / s1..sN / t1..tN in
    document order. Raises `TemplateWriteError` only when there is no front
    matter at all.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise TemplateWriteError("missing YAML front-matter (--- ... ---)")
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise TemplateWriteError(f"bad front-matter YAML: {exc}") from exc
    if not isinstance(fm, dict):
        raise TemplateWriteError("front-matter must be a mapping")
    body = match.group(2)

    draft = TemplateDraft(
        report_type=str(fm.get("report_type", "") or report_type or ""),
        title=str(fm.get("title", "") or ""),
        description=" ".join(str(fm.get("description", "") or "").split()),
        version=str(fm.get("version", "0.1.0") or "0.1.0"),
        owner=str(fm.get("owner", "") or ""),
        updated=str(fm.get("updated", "") or ""),
        tags=_coerce_tags(fm.get("tags")),
    )

    citation = fm.get("citation") or {}
    if isinstance(citation, dict):
        draft.citation_required = bool(citation.get("required", True))
        draft.citation_granularity = str(citation.get("granularity", "claim") or "claim")
        try:
            draft.citation_min_per_paragraph = int(citation.get("min_per_paragraph", 1))
        except (TypeError, ValueError):
            draft.citation_min_per_paragraph = 1

    for index, item in enumerate(fm.get("inputs") or [], start=1):
        if not isinstance(item, dict):
            continue
        draft.inputs.append(
            DraftInput(
                key=f"i{index}",
                id=str(item.get("id", "") or ""),
                prompt=str(item.get("prompt", "") or ""),
                required=bool(item.get("required", True)),
            )
        )

    id_to_key: dict[str, str] = {}
    for index, item in enumerate(fm.get("sources") or [], start=1):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", "") or "")
        src = DraftSource(
            key=f"s{index}",
            id=str(item.get("id", "") or ""),
            kind=kind,
            required=bool(
                item.get("required", _KIND_REQUIRED_DEFAULT.get(kind, True))
            ),
            dataset=str(item.get("dataset", "") or ""),
            query_id=str(item.get("query_id", "") or ""),
            sql=str(item.get("sql", "") or "").rstrip("\n"),
            space=str(item.get("space", "") or ""),
            cql=str(item.get("cql", "") or ""),
            page_id=str(item.get("page_id", "") or ""),
            filter_tags=[str(t) for t in (item.get("filter_tags") or [])],
            connector=str(item.get("connector", "") or ""),
            endpoint=str(item.get("endpoint", "") or ""),
            params={
                str(k): str(v) for k, v in (item.get("params") or {}).items()
            },
        )
        draft.sources.append(src)
        if src.id:
            id_to_key.setdefault(src.id, src.key)

    draft.passthrough = {
        k: v for k, v in fm.items() if k not in _MODELLED_KEYS
    }

    draft.doc_heading = _parse_doc_heading(body)
    draft.sections = _parse_draft_sections(body, id_to_key)
    return draft


def draft_from_path(path: str | Path) -> TemplateDraft:
    p = Path(path)
    return draft_from_text(p.read_text(encoding="utf-8"), report_type=p.stem)


def _parse_doc_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return ""
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _parse_draft_sections(body: str, id_to_key: dict[str, str]) -> list[DraftSection]:
    matches = list(_SECTION_RE.finditer(body))
    sections: list[DraftSection] = []
    for index, match in enumerate(matches):
        raw_heading = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        instruction, source_ids, table_id, visual = _parse_block(body[start:end])

        number_match = _LEADING_NUM_RE.match(raw_heading)
        number = number_match.group(1) if number_match else ""
        heading = _LEADING_NUM_RE.sub("", raw_heading).strip() or raw_heading

        sections.append(
            DraftSection(
                key=f"t{index + 1}",
                heading=heading,
                instruction=instruction,
                source_keys=[id_to_key.get(sid, f"!{sid}") for sid in source_ids],
                table_key=id_to_key.get(table_id, f"!{table_id}") if table_id else "",
                visual=visual,
                number=number,
            )
        )
    return sections


def _parse_block(block: str) -> tuple[str, list[str], str, str]:
    """Re-implements the loader's directive parsing over one section body."""
    cleaned = "\n".join(re.sub(r"^\s*>\s?", "", line) for line in block.splitlines())
    instruction = ""
    sources: list[str] = []
    table = ""
    visual = ""
    for match in _DIRECTIVE_RE.finditer(cleaned):
        if match.group("instruction") is not None:
            instruction = " ".join(match.group("instruction").split())
        elif match.group("sources") is not None:
            raw = match.group("sources").strip()
            if raw and not raw.startswith("("):
                sources = [
                    s.strip()
                    for s in raw.split(",")
                    if s.strip() and not s.strip().startswith("(")
                ]
        elif match.group("table") is not None:
            raw = match.group("table").strip()
            table = "" if raw.lower() in ("(none)", "none", "") else raw
        elif match.group("visual") is not None:
            raw = match.group("visual").strip()
            visual = "" if raw.lower() in ("(none)", "none", "") else raw
    return instruction, sources, table, visual


def blank_draft(
    *, report_type: str = "", tags: dict[str, list[str]] | None = None
) -> TemplateDraft:
    """One empty input row, one empty bigquery source row, one empty section row.

    `tags` is copied in verbatim — the caller supplies the taxonomy defaults, so
    this module never has an opinion about which compliance label is the default.
    """
    return TemplateDraft(
        report_type=report_type,
        version="0.1.0",
        tags={k: list(v) for k, v in (tags or {}).items()},
        inputs=[DraftInput(key="i1")],
        sources=[DraftSource(key="s1", kind="bigquery", required=True)],
        sections=[DraftSection(key="t1")],
    )


def clone_draft(
    draft: TemplateDraft, *, report_type: str, title: str = ""
) -> TemplateDraft:
    """Deep copy with a new key and a reset version.

    Tags are carried over unchanged — cloning keeps whatever labels the source
    carried; the taxonomy default only applies to a blank create.
    """
    clone = _copy_draft(draft)
    clone.report_type = report_type
    clone.version = "0.1.0"
    clone.title = title or f"{draft.title} (copy)"
    clone.updated = ""
    return clone


def _copy_draft(draft: TemplateDraft) -> TemplateDraft:
    return TemplateDraft(
        report_type=draft.report_type,
        title=draft.title,
        description=draft.description,
        version=draft.version,
        owner=draft.owner,
        updated=draft.updated,
        tags={k: list(v) for k, v in draft.tags.items()},
        doc_heading=draft.doc_heading,
        citation_required=draft.citation_required,
        citation_granularity=draft.citation_granularity,
        citation_min_per_paragraph=draft.citation_min_per_paragraph,
        inputs=[dataclasses.replace(i) for i in draft.inputs],
        sources=[
            dataclasses.replace(
                s, filter_tags=list(s.filter_tags), params=dict(s.params)
            )
            for s in draft.sources
        ],
        sections=[
            dataclasses.replace(t, source_keys=list(t.source_keys))
            for t in draft.sections
        ],
        passthrough=dict(draft.passthrough),
    )


# --- 3.3.3 validate --------------------------------------------------------


def validate_draft(
    draft: TemplateDraft,
    *,
    existing_keys: Sequence[str] = (),
    is_new: bool = True,
    known_query_ids: Sequence[str] = (),
) -> list[DraftIssue]:
    """Pure. `existing_keys` are report_type stems already on disk, compared
    case-insensitively. Never raises. Performs no taxonomy checks — tag
    conformance is decided against the taxonomy by the caller.

    `known_query_ids` is the named-query registry's contents, passed in rather
    than looked up so this module stays pure and testable. An empty sequence
    means "do not check", not "nothing is registered" — a caller with no
    registry to hand must not turn every query in a template into an error.

    Why the check is here at all: sixteen `query_id` references across the
    shipped template library resolve to nothing, and until now the only place
    that said so was a run's preflight — which is to say, after someone had
    chosen a template, filled in a compound and pressed go. Reporting it while
    authoring is the difference between a typo and a wasted run.
    """
    issues: list[DraftIssue] = []
    add = issues.append

    # --- identity ---
    key = draft.report_type.strip()
    if not key:
        add(
            DraftIssue(
                "report_type",
                "error",
                "key_required",
                "This template needs a key.",
                "Lowercase letters, digits and underscores, e.g. dmpk_adme_summary.",
            )
        )
    elif not REPORT_TYPE_RE.match(key):
        add(
            DraftIssue(
                "report_type",
                "error",
                "key_charset",
                f"{key!r} is not a usable key.",
                "Start with a lowercase letter; use only lowercase letters, "
                "digits and underscores; 3–64 characters.",
            )
        )
    elif is_new and key.lower() in {str(k).lower() for k in existing_keys}:
        add(
            DraftIssue(
                "report_type",
                "error",
                "key_taken",
                f"A template with the key {key!r} already exists.",
                "Pick a different key, or edit the existing template.",
            )
        )

    if not draft.title.strip():
        add(DraftIssue("title", "error", "title_required", "This template needs a title."))
    elif len(draft.title) > MAX_TITLE:
        add(
            DraftIssue(
                "title",
                "error",
                "title_length",
                f"The title is {len(draft.title)} characters; the limit is {MAX_TITLE}.",
            )
        )

    if not draft.description.strip():
        add(
            DraftIssue(
                "description",
                "warning",
                "description_missing",
                "Without a description the gallery card has nothing to say.",
            )
        )
    elif len(draft.description) > MAX_DESCRIPTION:
        add(
            DraftIssue(
                "description",
                "error",
                "description_length",
                f"The description is {len(draft.description)} characters; "
                f"the limit is {MAX_DESCRIPTION}.",
            )
        )

    if not VERSION_RE.match(draft.version.strip()):
        add(
            DraftIssue(
                "version",
                "error",
                "version_format",
                f"{draft.version!r} is not a version number.",
                "Use three numbers separated by dots, e.g. 0.1.0.",
            )
        )

    if not draft.owner.strip():
        add(
            DraftIssue(
                "owner",
                "error",
                "owner_required",
                "Name the team that owns this template.",
            )
        )

    if draft.citation_granularity not in GRANULARITIES:
        add(
            DraftIssue(
                "citation_granularity",
                "error",
                "granularity_unknown",
                f"{draft.citation_granularity!r} is not a citation granularity.",
                "Choose per claim, per paragraph, or per section.",
            )
        )

    # --- inputs ---
    input_ids: list[str] = []
    for item in draft.inputs:
        base = f"input.{item.key}"
        ident = item.id.strip()
        if not ID_RE.match(ident):
            add(
                DraftIssue(
                    f"{base}.id",
                    "error",
                    "input_id_charset",
                    f"{ident or '(blank)'} is not a usable input id.",
                    "Start with a lowercase letter; lowercase letters, digits and "
                    "underscores only.",
                )
            )
        elif ident in input_ids:
            add(
                DraftIssue(
                    f"{base}.id",
                    "error",
                    "input_id_duplicate",
                    f"Two inputs are both called {ident!r}.",
                )
            )
        if ident:
            input_ids.append(ident)
        if not item.prompt.strip():
            add(
                DraftIssue(
                    f"{base}.prompt",
                    "error",
                    "input_prompt_required",
                    "This input needs a question to show the person running the report.",
                )
            )

    # --- sources ---
    source_ids: list[str] = []
    live_keys = {s.key for s in draft.sources if s.key}
    for src in draft.sources:
        issues.extend(
            _validate_source(src, source_ids, input_ids, known_query_ids)
        )
        if src.id.strip():
            source_ids.append(src.id.strip())

    # --- sections ---
    if not draft.sections:
        add(
            DraftIssue(
                "section",
                "error",
                "no_sections",
                "A template needs at least one section.",
            )
        )
    headings: list[str] = []
    for section in draft.sections:
        issues.extend(_validate_section(section, headings, live_keys, draft))

    return issues


def _unknown_query_issue(
    src: DraftSource, base: str, known_query_ids: Sequence[str]
) -> DraftIssue | None:
    """Flag a `query_id` the registry does not have.

    An empty registry means "nothing to check against", not "nothing exists".
    Treating it as the latter would turn every query in every template into an
    error the moment a caller forgot to pass the registry — a validator that
    cries wolf gets switched off, and then it catches nothing.

    The near-miss suggestion is what makes this actionable rather than
    annoying: the live failure that motivated it was `pivotal_tox_summary_v2`
    against a registry holding `pivotal_toxicology_summary_v2`, which is a typo
    at a glance and a mystery without the candidate spelled out.
    """
    query_id = src.query_id.strip()
    if not query_id or not known_query_ids:
        return None
    known = list(known_query_ids)
    if query_id in known:
        return None

    suggestion = _closest(query_id, known)
    fix = (
        f"Did you mean {suggestion!r}?"
        if suggestion
        else "Add a YAML file for it to the named-query registry, or use inline SQL."
    )
    return DraftIssue(
        f"{base}.query_id",
        # A warning, not an error, and the distinction is load-bearing: errors
        # block the save. Thirteen references across the shipped library point
        # at queries nobody has written yet, and a template that names a
        # planned query is a legitimate authored artifact — the registry is
        # what is incomplete. Blocking the save would make every existing
        # template unsavable and punish an author for a data gap they may not
        # own. The section will still report having no data, which is the
        # honest outcome at run time.
        "warning",
        "unknown_named_query",
        f"No registered query is called {query_id!r}.",
        fix,
    )


def _closest(needle: str, candidates: Sequence[str]) -> str:
    """The nearest registered id, or "" when nothing is close.

    Thresholded rather than always returning a best match: suggesting
    `ae_summary_by_soc_v3` for `physchem_formulation_v1` is worse than
    suggesting nothing, because a confident wrong answer sends someone off to
    check it.
    """
    import difflib

    hits = difflib.get_close_matches(needle, list(candidates), n=1, cutoff=0.6)
    return hits[0] if hits else ""


def _validate_source(
    src: DraftSource,
    seen_ids: Sequence[str],
    input_ids: Sequence[str],
    known_query_ids: Sequence[str] = (),
) -> list[DraftIssue]:
    base = f"source.{src.key}"
    issues: list[DraftIssue] = []
    add = issues.append

    ident = src.id.strip()
    if not ID_RE.match(ident):
        add(
            DraftIssue(
                f"{base}.id",
                "error",
                "source_id_charset",
                f"{ident or '(blank)'} is not a usable source id.",
                "Start with a lowercase letter; lowercase letters, digits and "
                "underscores only.",
            )
        )
    elif ident in seen_ids:
        add(
            DraftIssue(
                f"{base}.id",
                "error",
                "source_id_duplicate",
                f"Two sources are both called {ident!r}.",
            )
        )
    elif ident in input_ids:
        add(
            DraftIssue(
                f"{base}.id",
                "error",
                "source_id_collides_input",
                f"{ident!r} is already the id of an input.",
                "Sources and inputs share one namespace inside a section.",
            )
        )

    if src.kind not in SOURCE_KINDS:
        add(
            DraftIssue(
                f"{base}.kind",
                "error",
                "source_kind_unknown",
                f"{src.kind or '(blank)'} is not a source kind.",
                "Choose BigQuery, Oracle, Confluence, SharePoint, local "
                "documents, or an API connector.",
            )
        )
        return issues

    if src.kind == "bigquery":
        if not src.query_id.strip() and not src.sql.strip():
            add(
                DraftIssue(
                    f"{base}.query_id",
                    "error",
                    "bq_needs_query",
                    "A BigQuery source needs either a named query or inline SQL.",
                )
            )
        if src.query_id.strip() and src.sql.strip():
            add(
                DraftIssue(
                    f"{base}.query_id",
                    "warning",
                    "bq_both",
                    "This source has both a named query and inline SQL; "
                    "the named query wins and the SQL is ignored.",
                )
            )
        if not src.dataset.strip():
            add(
                DraftIssue(
                    f"{base}.dataset",
                    "warning",
                    "bq_no_dataset",
                    "Without a dataset the citation just says 'bigquery'.",
                )
            )
        if src.sql.strip():
            add(
                DraftIssue(
                    f"{base}.sql",
                    "warning",
                    "bq_inline_sql",
                    "Inline SQL needs per-run approval; a named query does not.",
                )
            )
        unknown = _unknown_query_issue(src, base, known_query_ids)
        if unknown is not None:
            add(unknown)
    elif src.kind == "oracle":
        if not src.query_id.strip() and not src.sql.strip():
            add(
                DraftIssue(
                    f"{base}.query_id",
                    "error",
                    "or_needs_query",
                    "An Oracle source needs either a named query or inline SQL.",
                )
            )
        if not src.service.strip():
            add(
                DraftIssue(
                    f"{base}.service",
                    "warning",
                    "or_no_service",
                    "Without a service name the citation just says 'oracle', "
                    "which does not tell a reader which database a figure "
                    "came from.",
                )
            )
        if src.sql.strip():
            add(
                DraftIssue(
                    f"{base}.sql",
                    "warning",
                    "or_inline_sql",
                    "Inline SQL needs per-run approval; a named query does not.",
                )
            )
        unknown = _unknown_query_issue(src, base, known_query_ids)
        if unknown is not None:
            add(unknown)
    elif src.kind == "sharepoint":
        if not (src.site.strip() or src.folder.strip() or src.query.strip()):
            add(
                DraftIssue(
                    f"{base}.site",
                    "error",
                    "sp_needs_locator",
                    "A SharePoint source needs a site, a folder, or a search.",
                )
            )
        if not src.file_types.strip():
            add(
                DraftIssue(
                    f"{base}.file_types",
                    "warning",
                    "sp_no_types",
                    "Without file types this pulls every document in scope, "
                    "including ones nobody meant to cite.",
                )
            )
    elif src.kind == "confluence":
        if not (src.space.strip() or src.cql.strip() or src.page_id.strip()):
            add(
                DraftIssue(
                    f"{base}.space",
                    "error",
                    "cf_needs_locator",
                    "A Confluence source needs a space, a search, or a page id.",
                )
            )
        if src.cql.strip() and src.page_id.strip():
            add(
                DraftIssue(
                    f"{base}.page_id",
                    "warning",
                    "cf_both",
                    "This source has both a search and a page id; "
                    "the page id decides what is fetched.",
                )
            )
    elif src.kind == "file":
        if not src.filter_tags:
            add(
                DraftIssue(
                    f"{base}.filter_tags",
                    "warning",
                    "fs_no_tags",
                    "Without tags this source matches nothing.",
                )
            )
    elif src.kind == "api":
        if not src.connector.strip():
            add(
                DraftIssue(
                    f"{base}.connector",
                    "error",
                    "api_needs_connector",
                    "An API source needs a connector.",
                )
            )
        if not src.endpoint.strip():
            add(
                DraftIssue(
                    f"{base}.endpoint",
                    "error",
                    "api_needs_endpoint",
                    "An API source needs an endpoint.",
                )
            )

    for name in src.params:
        if not PARAM_KEY_RE.match(str(name)):
            add(
                DraftIssue(
                    f"{base}.params",
                    "error",
                    "param_key_charset",
                    f"{name!r} is not a usable parameter name.",
                    "Letters, digits and underscores only; do not start with a digit.",
                )
            )

    declared = set(input_ids)
    for field_name, value in _source_text_fields(src):
        for used in _PLACEHOLDER_RE.findall(value):
            if used not in declared:
                add(
                    DraftIssue(
                        f"{base}.{field_name}",
                        "error",
                        "unknown_placeholder",
                        f"{{{{inputs.{used}}}}} is not one of this template's inputs.",
                        "Declared inputs: "
                        + (", ".join(sorted(declared)) if declared else "(none yet)"),
                    )
                )
    return issues


def _source_text_fields(src: DraftSource) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [
        ("dataset", src.dataset),
        ("query_id", src.query_id),
        ("sql", src.sql),
        ("space", src.space),
        ("cql", src.cql),
        ("page_id", src.page_id),
        ("connector", src.connector),
        ("endpoint", src.endpoint),
    ]
    out.extend(("filter_tags", str(t)) for t in src.filter_tags)
    out.extend(("params", str(v)) for v in src.params.values())
    return out


def _validate_section(
    section: DraftSection,
    seen_headings: list[str],
    live_keys: set[str],
    draft: TemplateDraft,
) -> list[DraftIssue]:
    base = f"section.{section.key}"
    issues: list[DraftIssue] = []
    add = issues.append

    heading = section.heading.strip()
    if not heading:
        add(
            DraftIssue(
                f"{base}.heading",
                "error",
                "heading_required",
                "This section needs a heading.",
            )
        )
    else:
        slug = re.sub(r"[^a-z0-9]+", "_", heading.lower()).strip("_")
        if slug in seen_headings:
            add(
                DraftIssue(
                    f"{base}.heading",
                    "error",
                    "heading_duplicate",
                    f"Another section is already called {heading!r}.",
                )
            )
        seen_headings.append(slug)

    instruction = section.instruction.strip()
    if not instruction:
        add(
            DraftIssue(
                f"{base}.instruction",
                "error",
                "instruction_required",
                "This section needs an instruction — it is what the section is "
                "written from.",
            )
        )
    else:
        for line in instruction.splitlines():
            stripped = re.sub(r"^\s*>\s?", "", line).strip()
            if stripped.startswith(("Sources:", "Table:", "Visual:")):
                add(
                    DraftIssue(
                        f"{base}.instruction",
                        "error",
                        "instruction_directive",
                        "An instruction line cannot start with 'Sources:', "
                        "'Table:' or 'Visual:' — those are reserved.",
                    )
                )
                break

    for source_key in section.source_keys:
        if source_key not in live_keys:
            add(
                DraftIssue(
                    f"{base}.sources",
                    "error",
                    "dangling_source",
                    "This section refers to a source that no longer exists.",
                    "Untick it, or add the source back.",
                )
            )
            break

    if section.table_key:
        table = draft.source_by_key(section.table_key)
        if table is None:
            add(
                DraftIssue(
                    f"{base}.table",
                    "error",
                    "dangling_table",
                    "This section's table refers to a source that no longer exists.",
                )
            )
        else:
            if table.kind not in _TABULAR_KINDS:
                add(
                    DraftIssue(
                        f"{base}.table",
                        "error",
                        "table_not_bigquery",
                        f"{table.id or 'This source'} does not return rows, so it "
                        "cannot be the section's table.",
                        "Choose a BigQuery or API connector source.",
                    )
                )
            if section.table_key not in section.source_keys:
                add(
                    DraftIssue(
                        f"{base}.table",
                        "error",
                        "table_not_selected",
                        f"{table.id or 'The table source'} has to be one of the "
                        "section's sources as well.",
                        "Tick it in Sources.",
                    )
                )
    return issues


def has_errors(issues: Sequence[DraftIssue]) -> bool:
    return any(i.severity == "error" for i in issues)


# --- 3.3.4 write -----------------------------------------------------------


def read_sha256(path: str | Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _comparable(t: ReportTemplate) -> dict:
    """The equality used by INV-1. `metadata.authored_at` is a wall clock and is
    deliberately excluded."""
    return {
        "template_id": t.template_id,
        "report_type": t.report_type,
        "title": t.title,
        "version": t.version,
        "description": t.description,
        "status": t.status,
        "tags": t.tags,
        "global_style": t.global_style.model_dump(),
        "sections": [s.model_dump() for s in t.all_sections()],
    }


def _normalise_draft(draft: TemplateDraft) -> TemplateDraft:
    """Canonical form for the fidelity proof: deterministic row keys, resolved
    section numbers, dangling references dropped, SQL trailing newlines stripped.

    Row keys are opaque and regenerated on every parse, so they are the one thing
    a draft and its re-parsed self are allowed to disagree about."""
    copy = _copy_draft(draft)

    input_map = {i.key: f"i{n}" for n, i in enumerate(copy.inputs, start=1)}
    source_map = {s.key: f"s{n}" for n, s in enumerate(copy.sources, start=1)}

    for item in copy.inputs:
        item.key = input_map[item.key]
    for src in copy.sources:
        src.key = source_map[src.key]
        src.sql = src.sql.rstrip("\n")
        src.required = bool(src.required)
    for index, section in enumerate(copy.sections, start=1):
        section.key = f"t{index}"
        section.number = str(index) if section.number is None else section.number
        section.instruction = " ".join(section.instruction.split())
        section.heading = section.heading.strip()
        section.source_keys = [
            source_map[k] for k in section.source_keys if k in source_map
        ]
        section.table_key = source_map.get(section.table_key, "")
    return copy


def write_template(
    draft: TemplateDraft,
    path: str | Path,
    *,
    facet_order: Sequence[str] | None = None,
    backup_dir: str | Path | None = None,
    expected_sha256: str | None = None,
    create: bool = False,
) -> WriteResult:
    """The only function in the codebase that writes into report-templates/.

    Nothing reaches `path` until the generated text has been parsed by the real
    loader and re-parsed by `draft_from_text`. On any failure nothing is written
    and every scratch file is removed.
    """
    target = Path(path)

    stamped = _copy_draft(draft)
    stamped.updated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    existing_keys: list[str] = []
    if create and target.parent.exists():
        existing_keys = [p.stem for p in target.parent.glob("*.md")]
    issues = validate_draft(stamped, existing_keys=existing_keys, is_new=create)
    if has_errors(issues):
        first = next(i for i in issues if i.severity == "error")
        raise TemplateWriteError(
            f"{first.field or 'template'}: {first.message}"
            if first.field
            else first.message
        )

    text = serialize_draft(stamped, facet_order=facet_order)

    _prove_round_trip(stamped, text)

    exists = _existing_sibling(target)
    if create and exists is not None:
        raise TemplateWriteError(
            f"{exists.name} already exists.", generated_text=text
        )
    if not create and exists is None:
        raise TemplateWriteError(
            f"{target.name} does not exist, so there is nothing to update.",
            generated_text=text,
        )

    if expected_sha256 is not None:
        actual = read_sha256(target)
        if actual != expected_sha256:
            raise TemplateConflict(target, expected_sha256, actual)

    backup_path: Path | None = None
    if backup_dir is not None:
        backup_path = backup_template(target, backup_dir)

    created = not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    # The .tmp name does not match the *.md glob, so a half-written file is
    # invisible to the gallery; os.replace is atomic for a same-directory rename.
    tmp = target.parent / f".{target.stem}.md.tmp"
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        _atomic_replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    return WriteResult(
        path=target,
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        backup_path=backup_path,
        created=created,
    )


def _atomic_replace(tmp: Path, target: Path) -> None:
    """`os.replace`, retried.

    On Windows the rename intermittently fails with a sharing violation when a
    virus scanner or the search indexer momentarily holds a handle to the
    destination — reproducible roughly once every ten overwrites. Retrying keeps
    the write atomic (the rename either happens or it does not); falling back to
    a copy would not.
    """
    delay = 0.02
    for attempt in range(6):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == 5:
                raise TemplateWriteError(
                    f"{target.name} is open in another program, so it could not "
                    "be replaced. Close it and save again."
                ) from None
            time.sleep(delay)
            delay *= 2


def _existing_sibling(target: Path) -> Path | None:
    """Case-insensitive existence check — NTFS treats X.md and x.md as one file."""
    if target.exists():
        return target
    parent = target.parent
    if not parent.exists():
        return None
    wanted = target.name.lower()
    for candidate in parent.glob("*.md"):
        if candidate.name.lower() == wanted:
            return candidate
    return None


def _prove_round_trip(draft: TemplateDraft, text: str) -> None:
    """Steps 4 and 5 of the write pipeline. Not test-only: a file the loader
    would reject must never appear inside report-templates/, not even briefly."""
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    scratch = SCRATCH_DIR / f"{draft.report_type or 'draft'}.{stamp}.{os.getpid()}.md"
    try:
        scratch.write_text(text, encoding="utf-8", newline="\n")
        try:
            load_report_doc(scratch)
        except ReportDocError as exc:
            raise TemplateWriteError(
                _strip_path(str(exc), scratch), generated_text=text
            ) from exc
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError et al.
            raise TemplateWriteError(
                _strip_path(str(exc), scratch), generated_text=text
            ) from exc
    finally:
        scratch.unlink(missing_ok=True)

    reparsed = _normalise_draft(draft_from_text(text, report_type=draft.report_type))
    expected = _normalise_draft(draft)
    if reparsed != expected:
        raise TemplateWriteError(
            "This template did not survive being written and read back: "
            + _first_difference(expected, reparsed),
            generated_text=text,
        )


def _strip_path(message: str, scratch: Path) -> str:
    return message.replace(str(scratch), scratch.name).replace(f"{scratch.name}: ", "")


def _first_difference(expected: TemplateDraft, actual: TemplateDraft) -> str:
    for name in (f.name for f in dataclasses.fields(TemplateDraft)):
        want = getattr(expected, name)
        got = getattr(actual, name)
        if want != got:
            return f"{name} became {got!r} instead of {want!r}"
    return "(no field-level difference found)"


def backup_template(path: str | Path, backup_dir: str | Path) -> Path | None:
    """Copy `path` into the backup folder and prune to the newest 10.

    A failed backup must never block a save, so I/O problems are swallowed."""
    source = Path(path)
    if not source.exists():
        return None
    try:
        folder = Path(backup_dir) / source.stem
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = folder / f"{stamp}.md"
        shutil.copy2(source, destination)
        keep = sorted(folder.glob("*.md"), reverse=True)[BACKUP_KEEP:]
        for stale in keep:
            stale.unlink(missing_ok=True)
        return destination
    except OSError:
        return None


def trash_template(path: str | Path, trash_dir: str | Path) -> Path:
    source = Path(path)
    if not source.exists():
        raise TemplateWriteError(f"{source.name} does not exist.")
    folder = Path(trash_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = folder / f"{source.stem}.{stamp}.md"
    os.replace(source, destination)
    return destination


def find_trashed(report_type: str, trash_dir: str | Path) -> Path | None:
    folder = Path(trash_dir)
    if not folder.exists():
        return None
    matches = sorted(folder.glob(f"{report_type}.*.md"), reverse=True)
    return matches[0] if matches else None


def restore_trashed(trash_path: str | Path, dest_path: str | Path) -> None:
    source = Path(trash_path)
    destination = Path(dest_path)
    if not source.exists():
        raise TemplateWriteError(f"{source.name} is no longer in the trash.")
    if _existing_sibling(destination) is not None:
        raise TemplateWriteError(f"{destination.name} already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
