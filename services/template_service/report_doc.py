"""Load a Markdown+front-matter report-template document into a runnable
`ReportTemplate` that the existing generation engine can execute.

This is the bridge between the human-authored `report-templates/*.md` files
(see report-templates/README.md) and the engine's strict `ReportTemplate`
schema. The mapping:

  front-matter inputs   -> FreeTextInputBinding (attached to every section)
  sources[bigquery]     -> NamedQueryBinding (or SqlQueryBinding if inline sql)
  sources[confluence]   -> ApiCallBinding (connector_id="confluence")
  sources[file]         -> FileSetBinding
  "## N. Title" heading -> TemplateSection (section_id = leading number/slug)
  "> Instruction: ..."  -> GenerationPolicy.prompt_template
  "> Sources: a, b"     -> which source bindings that section carries
  "> Table: x"          -> hint (the filler already renders query/api results
                            as tables verbatim, so this is advisory)
  front-matter tags     -> ReportTemplate.tags (classification only — the
                            engine never reads it; see report-templates/taxonomy.yaml)

So an authored .md runs through the *same* ReportGenerator, citation
enforcement, safety gate, and audit trail as everything else.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from shared.schemas import GenerationMode, ReportTemplate, TemplateSection
from shared.schemas.template import (
    ApiCallBinding,
    CitationPolicy,
    DataBinding,
    FileSetBinding,
    FreeTextInputBinding,
    GenerationPolicy,
    GlobalStyle,
    NamedQueryBinding,
    OutputShape,
    SqlQueryBinding,
    TemplateMetadata,
    TemplateStatus,
    ValidationRule,
    VisualKind,
    VisualSpec,
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_SECTION_RE = re.compile(r"^##\s+(.*)$", re.MULTILINE)
_LEADING_NUM_RE = re.compile(r"^([\d]+(?:\.\d+)*)[.\s]")
_DIRECTIVE_RE = re.compile(
    # Instruction runs to the next directive, whichever it is. Visual has to be
    # in this lookahead too, or a section with a Visual line swallows it into
    # the prompt and the model is handed chart syntax as guidance.
    r"Instruction:\s*(?P<instruction>.*?)"
    r"(?=\n\s*Sources:|\n\s*Table:|\n\s*Visual:|\Z)"
    r"|Sources:\s*(?P<sources>[^\n]*)"
    r"|Table:\s*(?P<table>[^\n]*)"
    r"|Visual:\s*(?P<visual>[^\n]*)",
    re.DOTALL,
)


class ReportDocError(ValueError):
    """Raised when a report-template document can't be parsed/validated."""


def load_report_doc(path: str | Path) -> ReportTemplate:
    text = Path(path).read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ReportDocError(f"{path}: missing YAML front-matter (--- ... ---)")
    fm_raw, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError as exc:
        raise ReportDocError(f"{path}: bad front-matter YAML: {exc}") from exc

    for required in ("report_type", "title"):
        if required not in fm:
            raise ReportDocError(f"{path}: front-matter missing '{required}'")

    sources = {s["id"]: s for s in fm.get("sources", [])}
    inputs = fm.get("inputs", [])
    citation_fm = fm.get("citation", {}) or {}

    input_bindings = [
        FreeTextInputBinding(
            binding_id=i["id"],
            prompt=i.get("prompt", i["id"]),
            required=bool(i.get("required", True)),
        )
        for i in inputs
    ]

    sections = _parse_sections(body, sources, input_bindings, citation_fm, str(path))
    if not sections:
        raise ReportDocError(f"{path}: no '## ' sections found in the body")

    return ReportTemplate(
        template_id=fm["report_type"],
        version=str(fm.get("version", "0.1.0")),
        status=TemplateStatus.DRAFT,
        report_type=fm["report_type"],
        title=fm["title"],
        # Same whitespace collapse the gallery applies, so the two never disagree.
        description=" ".join(str(fm.get("description", "") or "").split()),
        metadata=TemplateMetadata(
            authored_by=fm.get("owner", "report-doc"),
            authored_at=datetime.now(timezone.utc),
            source_origin="from_scratch",
        ),
        global_style=GlobalStyle(),
        sections=sections,
        # Handed over raw: shape normalisation is the schema's job, and taxonomy
        # conformance is decided at render time, not at load time.
        tags=fm.get("tags"),
    )


def _parse_sections(
    body: str,
    sources: dict[str, dict[str, Any]],
    input_bindings: list[FreeTextInputBinding],
    citation_fm: dict[str, Any],
    path: str,
) -> list[TemplateSection]:
    matches = list(_SECTION_RE.finditer(body))
    sections: list[TemplateSection] = []
    for idx, mt in enumerate(matches):
        heading = mt.group(1).strip()
        start = mt.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        block = body[start:end]

        instruction, source_ids, _table, visual = _parse_directives(
            block, heading, path
        )
        section_id = _section_id_from_heading(heading, idx + 1)
        title = _LEADING_NUM_RE.sub("", heading).strip() or heading

        bindings: list[DataBinding] = list(input_bindings)
        for sid in source_ids:
            if sid not in sources:
                raise ReportDocError(
                    f"{path}: section {heading!r} references unknown source {sid!r}"
                )
            bindings.append(_source_to_binding(sources[sid], path))

        sections.append(
            TemplateSection(
                section_id=section_id,
                title=title,
                level=2,
                generation=GenerationPolicy(
                    mode=GenerationMode.HYBRID if source_ids else GenerationMode.LLM,
                    prompt_template=instruction or f"Write the {title!r} section.",
                    style_directives=["formal", "factual_only"],
                    output_shape=OutputShape.PROSE,
                ),
                data_bindings=bindings,
                citation_policy=CitationPolicy(
                    required=bool(citation_fm.get("required", True)),
                    granularity=citation_fm.get("granularity", "claim"),
                    min_citations_per_paragraph=int(citation_fm.get("min_per_paragraph", 1)),
                ),
                validation_rules=[
                    ValidationRule(rule="must_cite_every_number", severity="error"),
                    ValidationRule(rule="no_unbound_claims", severity="error"),
                ],
                visual=visual,
            )
        )
    return sections


def _parse_directives(
    block: str, heading: str = "", path: str = ""
) -> tuple[str, list[str], str | None, VisualSpec | None]:
    # Strip blockquote markers so the directive regex sees clean text.
    cleaned = "\n".join(
        re.sub(r"^\s*>\s?", "", line) for line in block.splitlines()
    )
    instruction = ""
    sources: list[str] = []
    table: str | None = None
    visual: VisualSpec | None = None
    for m in _DIRECTIVE_RE.finditer(cleaned):
        if m.group("instruction") is not None:
            instruction = " ".join(m.group("instruction").split())
        elif m.group("sources") is not None:
            raw = m.group("sources").strip()
            # Authors write parenthetical notes here, e.g. "(none)" or
            # "(synthesises the above)". Treat any parenthetical as prose,
            # not a source id.
            if raw and not raw.startswith("("):
                sources = [
                    s.strip()
                    for s in raw.split(",")
                    if s.strip() and not s.strip().startswith("(")
                ]
        elif m.group("table") is not None:
            raw = m.group("table").strip()
            table = None if raw.lower() in ("(none)", "none", "") else raw
        elif m.group("visual") is not None:
            visual = _parse_visual(m.group("visual"), heading, path)
    return instruction, sources, table, visual


_VISUAL_TOKEN_RE = re.compile(r'(\w+)=("[^"]*"|\S+)')


def _parse_visual(raw: str, heading: str, path: str) -> VisualSpec | None:
    """`> Visual: margin binding=exposure_margins x=species y=margin_x ...`

    Authored as a directive rather than picked per run, because the value of
    declaring the figure in the template is that every report of this type
    renders the same one. A reader comparing two compounds should be comparing
    the data, not working out whether the chart changed shape.

    A malformed directive raises. The alternative — dropping the figure and
    carrying on — produces a report that is quietly missing something the
    template asked for, and nobody reads the log to find out why.
    """
    raw = raw.strip()
    if not raw or raw.lower() in ("(none)", "none"):
        return None

    kind_token, _, rest = raw.partition(" ")
    try:
        kind = VisualKind(kind_token.strip().lower())
    except ValueError:
        raise ReportDocError(
            f"{path}: section {heading!r} asks for visual kind "
            f"{kind_token.strip()!r}. Known kinds: "
            f"{', '.join(k.value for k in VisualKind)}"
        ) from None

    fields = {
        key: value.strip('"')
        for key, value in _VISUAL_TOKEN_RE.findall(rest)
    }
    missing = [k for k in ("binding", "x", "y") if not fields.get(k)]
    if missing:
        raise ReportDocError(
            f"{path}: section {heading!r} visual is missing "
            f"{', '.join(missing)}. A chart needs to know which binding "
            f"supplies the rows and which columns to plot — it is never "
            f"inferred, because guessing a column would put numbers on a "
            f"figure that nobody chose."
        )

    threshold = fields.get("threshold")
    try:
        threshold_value = float(threshold) if threshold else None
    except ValueError:
        raise ReportDocError(
            f"{path}: section {heading!r} visual threshold {threshold!r} is "
            f"not a number"
        ) from None

    return VisualSpec(
        kind=kind,
        binding_id=fields["binding"],
        x=fields["x"],
        y=fields["y"],
        series=fields.get("series") or None,
        title=fields.get("title") or None,
        unit=fields.get("unit") or None,
        threshold=threshold_value,
    )


def _section_id_from_heading(heading: str, ordinal: int) -> str:
    m = _LEADING_NUM_RE.match(heading)
    if m:
        return m.group(1)
    slug = re.sub(r"[^a-z0-9]+", "_", heading.lower()).strip("_")
    return slug or f"s{ordinal}"


def _sub_inputs(value: Any) -> str:
    """Rewrite `{{inputs.X}}` -> `{{report.X}}` so the engine's binding
    resolver (which substitutes `{{report.X}}` from the run inputs) resolves
    template placeholders without any change to the resolver."""
    return re.sub(r"\{\{\s*inputs\.", "{{report.", str(value))


def _source_to_binding(src: dict[str, Any], path: str) -> DataBinding:
    stype = src.get("type")
    sid = src["id"]
    params = {str(k): _sub_inputs(v) for k, v in (src.get("params") or {}).items()}
    if stype == "bigquery":
        if src.get("query_id"):
            return NamedQueryBinding(
                binding_id=sid,
                source=str(src.get("dataset", "bigquery")),
                query_id=src["query_id"],
                parameters=params,
                required=bool(src.get("required", True)),
            )
        if src.get("sql"):
            return SqlQueryBinding(
                binding_id=sid,
                source=str(src.get("dataset", "bigquery")),
                sql=src["sql"],
                parameters=params,
                required=bool(src.get("required", True)),
            )
        raise ReportDocError(f"{path}: bigquery source {sid!r} needs query_id or sql")
    if stype == "confluence":
        endpoint = "get_page" if src.get("page_id") else "search_pages"
        api_params = dict(params)
        for key in ("space", "cql", "page_id"):
            if src.get(key) is not None:
                api_params[key] = _sub_inputs(src[key])
        return ApiCallBinding(
            binding_id=sid,
            connector_id="confluence",
            endpoint=endpoint,
            parameters=api_params,
            required=bool(src.get("required", False)),
        )
    if stype == "file":
        return FileSetBinding(
            binding_id=sid,
            filter_tags=[str(t) for t in (src.get("filter_tags") or [])],
            required=bool(src.get("required", False)),
        )
    if stype == "api":
        if not src.get("connector") or not src.get("endpoint"):
            raise ReportDocError(
                f"{path}: api source {sid!r} needs 'connector' and 'endpoint'"
            )
        return ApiCallBinding(
            binding_id=sid,
            connector_id=str(src["connector"]),
            endpoint=str(src["endpoint"]),
            parameters=params,
            required=bool(src.get("required", False)),
        )
    raise ReportDocError(f"{path}: source {sid!r} has unknown type {stype!r}")
