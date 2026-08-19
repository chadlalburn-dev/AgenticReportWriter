"""The round-trip guarantee for the template writer.

INV-1  A file written from a draft parsed out of an existing template loads back
       to an equivalent ReportTemplate.
INV-2  draft_from_text(serialize_draft(d)) == d.
INV-3  The writer never puts a file the loader rejects into the templates folder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.template_service.report_doc import ReportDocError, load_report_doc
from services.template_service.report_doc_writer import (
    DraftInput,
    DraftSection,
    DraftSource,
    TemplateConflict,
    TemplateDraft,
    TemplateWriteError,
    _comparable,
    _normalise_draft,
    backup_template,
    blank_draft,
    clone_draft,
    draft_from_path,
    draft_from_text,
    find_trashed,
    has_errors,
    read_sha256,
    restore_trashed,
    serialize_draft,
    trash_template,
    validate_draft,
    write_template,
)

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "report-templates"
REAL_TEMPLATES = sorted(TEMPLATES_DIR.glob("*.md"))


def codes(issues, severity=None):
    return {i.code for i in issues if severity is None or i.severity == severity}


def a_draft(**overrides) -> TemplateDraft:
    """A minimal draft that validates clean."""
    draft = TemplateDraft(
        report_type="unit_widget",
        title="Unit Widget",
        description="A template used by the writer tests.",
        version="0.1.0",
        owner="platform",
        doc_heading="Unit Widget — {{inputs.compound_id}}",
        inputs=[
            DraftInput(key="i1", id="compound_id", prompt="Compound id", required=True)
        ],
        sources=[
            DraftSource(
                key="s1",
                id="potency",
                kind="bigquery",
                dataset="assays",
                query_id="potency_v1",
                params={"compound_id": "{{inputs.compound_id}}"},
            )
        ],
        sections=[
            DraftSection(
                key="t1",
                heading="Summary",
                instruction="Summarise the potency data.",
                source_keys=["s1"],
                table_key="s1",
            )
        ],
    )
    for name, value in overrides.items():
        setattr(draft, name, value)
    return draft


# --- INV-1 / INV-2 over the real corpus ------------------------------------


@pytest.mark.parametrize("path", REAL_TEMPLATES, ids=lambda p: p.name)
def test_round_trip_every_real_template(path: Path, tmp_path: Path) -> None:
    try:
        original = load_report_doc(path)
    except (ReportDocError, ValueError):
        pytest.skip("not a runnable report template")

    draft = draft_from_path(path)
    text = serialize_draft(draft)

    scratch = tmp_path / path.name
    scratch.write_text(text, encoding="utf-8")

    assert _comparable(load_report_doc(scratch)) == _comparable(original)  # INV-1
    assert _normalise_draft(draft_from_text(text)) == _normalise_draft(draft)  # INV-2


@pytest.mark.parametrize("path", REAL_TEMPLATES, ids=lambda p: p.name)
def test_serialize_is_idempotent(path: Path) -> None:
    try:
        load_report_doc(path)
    except (ReportDocError, ValueError):
        pytest.skip("not a runnable report template")
    once = serialize_draft(draft_from_path(path))
    assert serialize_draft(draft_from_text(once)) == once


@pytest.mark.parametrize("path", REAL_TEMPLATES, ids=lambda p: p.name)
def test_every_real_template_validates_clean(path: Path) -> None:
    """A user must be able to open an existing template and press Save."""
    try:
        load_report_doc(path)
    except (ReportDocError, ValueError):
        pytest.skip("not a runnable report template")
    issues = validate_draft(draft_from_path(path), is_new=False)
    assert not has_errors(issues), sorted(codes(issues, "error"))


@pytest.mark.parametrize("path", REAL_TEMPLATES, ids=lambda p: p.name)
def test_authored_section_numbering_survives_a_no_op_save(path: Path) -> None:
    """Re-saving must not renumber ICH-mandated or unnumbered headings."""
    try:
        load_report_doc(path)
    except (ReportDocError, ValueError):
        pytest.skip("not a runnable report template")
    before = [s.section_id for s in load_report_doc(path).all_sections()]
    draft = draft_from_path(path)
    text = serialize_draft(draft)
    after = [s.section_id for s in _load_text(text)]
    assert after == before


def _load_text(text: str):
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.md"
        p.write_text(text, encoding="utf-8")
        return load_report_doc(p).all_sections()


# --- placeholders ----------------------------------------------------------


def test_placeholders_are_written_in_the_authored_inputs_form() -> None:
    text = serialize_draft(a_draft())
    assert '"{{inputs.compound_id}}"' in text
    assert "{{report." not in text


def test_loader_still_rewrites_placeholders_for_the_engine(tmp_path: Path) -> None:
    path = tmp_path / "unit_widget.md"
    path.write_text(serialize_draft(a_draft()), encoding="utf-8")
    binding = [
        b
        for b in load_report_doc(path).sections[0].data_bindings
        if b.binding_id == "potency"
    ][0]
    assert binding.parameters == {"compound_id": "{{report.compound_id}}"}


# --- every source kind survives -------------------------------------------


def test_all_four_source_kinds_round_trip(tmp_path: Path) -> None:
    draft = a_draft(
        sources=[
            DraftSource(
                key="s1",
                id="bq",
                kind="bigquery",
                dataset="assays",
                query_id="q_v1",
                params={"compound_id": "{{inputs.compound_id}}"},
            ),
            DraftSource(
                key="s2",
                id="wiki",
                kind="confluence",
                space="PSS",
                cql='label = "{{inputs.compound_id}}" and label = "background"',
                required=False,
            ),
            DraftSource(
                key="s3",
                id="docs",
                kind="file",
                filter_tags=["nonclinical", "prior_report"],
                required=False,
            ),
            DraftSource(
                key="s4",
                id="ext",
                kind="api",
                connector="mock_chembl",
                endpoint="target_search",
                params={"q": "{{inputs.compound_id}}"},
                required=False,
            ),
        ],
        sections=[
            DraftSection(
                key="t1",
                heading="Summary",
                instruction="Summarise everything.",
                source_keys=["s1", "s2", "s3", "s4"],
                table_key="s1",
            )
        ],
    )
    text = serialize_draft(draft)
    assert _normalise_draft(draft_from_text(text)) == _normalise_draft(draft)

    path = tmp_path / "unit_widget.md"
    path.write_text(text, encoding="utf-8")
    bindings = {b.binding_id: b for b in load_report_doc(path).sections[0].data_bindings}
    assert set(bindings) == {"compound_id", "bq", "wiki", "docs", "ext"}
    assert bindings["bq"].query_id == "q_v1"
    assert bindings["wiki"].connector_id == "confluence"
    assert bindings["wiki"].parameters["space"] == "PSS"
    assert bindings["docs"].filter_tags == ["nonclinical", "prior_report"]
    assert bindings["ext"].connector_id == "mock_chembl"


def test_inline_sql_round_trips_as_a_block_scalar(tmp_path: Path) -> None:
    sql = "SELECT compound_id, ic50\nFROM assays.potency\nWHERE compound_id = @compound_id"
    draft = a_draft(
        sources=[
            DraftSource(key="s1", id="potency", kind="bigquery", dataset="a", sql=sql)
        ]
    )
    text = serialize_draft(draft)
    assert "    sql: |" in text
    assert _normalise_draft(draft_from_text(text)).sources[0].sql == sql

    path = tmp_path / "unit_widget.md"
    path.write_text(text, encoding="utf-8")
    binding = [
        b
        for b in load_report_doc(path).sections[0].data_bindings
        if b.binding_id == "potency"
    ][0]
    assert binding.sql.strip() == sql


def test_source_required_is_only_written_when_it_differs_from_the_default() -> None:
    text = serialize_draft(
        a_draft(
            sources=[
                DraftSource(key="s1", id="bq", kind="bigquery", dataset="d", query_id="q"),
                DraftSource(key="s2", id="w", kind="confluence", space="PSS", required=False),
            ],
            sections=[
                DraftSection(
                    key="t1",
                    heading="S",
                    instruction="Write it.",
                    source_keys=["s1", "s2"],
                )
            ],
        )
    )
    assert "required:" not in text.split("citation:")[0].split("sources:")[1]


# --- citation, output formats, passthrough ---------------------------------


def test_citation_policy_round_trips(tmp_path: Path) -> None:
    draft = a_draft(
        citation_required=False,
        citation_granularity="paragraph",
        citation_min_per_paragraph=3,
    )
    path = tmp_path / "unit_widget.md"
    path.write_text(serialize_draft(draft), encoding="utf-8")
    policy = load_report_doc(path).sections[0].citation_policy
    assert policy.required is False
    assert policy.granularity == "paragraph"
    assert policy.min_citations_per_paragraph == 3


def test_output_formats_are_carried_through_as_passthrough() -> None:
    draft = draft_from_path(TEMPLATES_DIR / "dmpk_adme_summary.md")
    assert draft.passthrough["output"] == {"formats": ["docx", "html"]}
    assert "formats: [docx, html]" in serialize_draft(draft)


def test_unmodelled_front_matter_keys_survive() -> None:
    draft = a_draft(passthrough={"status": "draft", "output": {"formats": ["html"]}})
    assert draft_from_text(serialize_draft(draft)).passthrough == draft.passthrough


# --- tags ------------------------------------------------------------------


def test_tags_round_trip_with_both_cardinalities() -> None:
    draft = a_draft(
        tags={
            "domain": ["dmpk"],
            "compliance": ["non_gxp"],
            "modality": ["small_molecule", "biologic"],
        }
    )
    text = serialize_draft(draft, facet_order=("compliance", "domain", "modality"))
    assert "  compliance: non_gxp" in text
    assert "  domain: dmpk" in text
    assert "  modality: [small_molecule, biologic]" in text
    assert text.index("compliance:") < text.index("domain:") < text.index("modality:")
    assert draft_from_text(text).tags == draft.tags


def test_the_tags_key_is_omitted_when_there_are_no_tags() -> None:
    assert "tags:" not in serialize_draft(a_draft(tags={}))
    assert "tags:" not in serialize_draft(a_draft(tags={"domain": []}))


def test_a_bare_token_list_is_tolerated_on_read() -> None:
    text = serialize_draft(a_draft()).replace(
        "version: 0.1.0", "version: 0.1.0\ntags: [domain:dmpk, compliance:gxp]"
    )
    assert draft_from_text(text).tags == {"domain": ["dmpk"], "compliance": ["gxp"]}


def test_a_malformed_tag_never_stops_a_file_being_opened() -> None:
    text = serialize_draft(a_draft()).replace(
        "version: 0.1.0", "version: 0.1.0\ntags:\n  domain: DMPK / ADME\n  ok: fine"
    )
    assert draft_from_text(text).tags == {"domain": [], "ok": ["fine"]}


def test_the_writer_has_no_opinion_about_compliance_values() -> None:
    """Two drafts differing only in the compliance value serialise identically
    apart from that one token."""
    non_gxp = serialize_draft(a_draft(tags={"compliance": ["non_gxp"]}))
    gxp = serialize_draft(a_draft(tags={"compliance": ["gxp"]}))
    assert non_gxp.replace("compliance: non_gxp", "compliance: gxp") == gxp


# --- instruction wrapping --------------------------------------------------


def test_a_wrapped_instruction_survives_the_loaders_directive_regex(
    tmp_path: Path,
) -> None:
    instruction = (
        "Summarise pivotal toxicology by species and duration, reporting NOAELs, "
        "target organs of toxicity, and reversibility, and cite every value to "
        "the study it came from without exception or paraphrase."
    )
    draft = a_draft(
        sections=[
            DraftSection(
                key="t1",
                heading="Toxicology",
                instruction=instruction,
                source_keys=["s1"],
                table_key="s1",
            )
        ]
    )
    text = serialize_draft(draft)
    assert max(len(line) for line in text.splitlines()) <= 78

    path = tmp_path / "unit_widget.md"
    path.write_text(text, encoding="utf-8")
    assert load_report_doc(path).sections[0].generation.prompt_template == instruction


def test_a_wrapped_line_never_opens_with_a_directive_keyword(tmp_path: Path) -> None:
    """'Sources:' at the start of a continuation line would truncate the
    instruction and swallow the rest as a source list."""
    instruction = (
        "Name the studies and then list them exactly as written in the appendix "
        "under the heading aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
        "Sources: alpha, beta, gamma and stop there."
    )
    draft = a_draft(
        sections=[
            DraftSection(
                key="t1",
                heading="Studies",
                instruction=instruction,
                source_keys=["s1"],
                table_key="s1",
            )
        ]
    )
    text = serialize_draft(draft)
    body = text.split("---", 2)[2]
    for line in body.splitlines():
        assert not line.startswith("> Sources: alpha")

    path = tmp_path / "unit_widget.md"
    path.write_text(text, encoding="utf-8")
    section = load_report_doc(path).sections[0]
    assert section.generation.prompt_template == instruction
    assert [b.binding_id for b in section.data_bindings] == ["compound_id", "potency"]


def test_an_empty_source_list_is_written_as_none(tmp_path: Path) -> None:
    draft = a_draft(
        sections=[
            DraftSection(key="t1", heading="Wrap up", instruction="Synthesise the above.")
        ]
    )
    text = serialize_draft(draft)
    assert "> Sources: (none)" in text
    assert "> Table: (none)" in text

    path = tmp_path / "unit_widget.md"
    path.write_text(text, encoding="utf-8")
    section = load_report_doc(path).sections[0]
    assert [b.binding_id for b in section.data_bindings] == ["compound_id"]


# --- validation ------------------------------------------------------------


def test_a_clean_draft_has_no_errors() -> None:
    assert not has_errors(validate_draft(a_draft()))


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("report_type", "", "key_required"),
        ("report_type", "Not A Key", "key_charset"),
        ("report_type", "README", "key_charset"),
        ("title", "", "title_required"),
        ("title", "x" * 121, "title_length"),
        ("description", "x" * 301, "description_length"),
        ("version", "1.0", "version_format"),
        ("owner", "", "owner_required"),
        ("sections", [], "no_sections"),
    ],
)
def test_identity_validation(field: str, value: object, code: str) -> None:
    assert code in codes(validate_draft(a_draft(**{field: value})), "error")


def test_a_taken_key_is_only_an_error_for_a_new_template() -> None:
    draft = a_draft()
    assert "key_taken" in codes(
        validate_draft(draft, existing_keys=["UNIT_WIDGET"], is_new=True), "error"
    )
    assert "key_taken" not in codes(
        validate_draft(draft, existing_keys=["unit_widget"], is_new=False), "error"
    )


def test_source_and_input_ids_share_one_namespace() -> None:
    draft = a_draft()
    draft.sources[0].id = "compound_id"
    assert "source_id_collides_input" in codes(validate_draft(draft), "error")


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda s: setattr(s, "query_id", ""), "bq_needs_query"),
        (lambda s: setattr(s, "kind", "nonsense"), "source_kind_unknown"),
        (lambda s: s.params.update({"1bad": "x"}), "param_key_charset"),
        (
            lambda s: setattr(s, "params", {"c": "{{inputs.nope}}"}),
            "unknown_placeholder",
        ),
    ],
)
def test_source_validation(mutate, code: str) -> None:
    draft = a_draft()
    mutate(draft.sources[0])
    assert code in codes(validate_draft(draft), "error")


def test_confluence_and_api_sources_need_their_locators() -> None:
    draft = a_draft(
        sources=[DraftSource(key="s1", id="w", kind="confluence")],
        sections=[DraftSection(key="t1", heading="S", instruction="Write.")],
    )
    assert "cf_needs_locator" in codes(validate_draft(draft), "error")
    draft.sources[0] = DraftSource(key="s1", id="w", kind="api")
    found = codes(validate_draft(draft), "error")
    assert {"api_needs_connector", "api_needs_endpoint"} <= found


def test_a_table_must_be_one_of_the_sections_sources() -> None:
    draft = a_draft()
    draft.sections[0].source_keys = []
    assert "table_not_selected" in codes(validate_draft(draft), "error")


def test_a_table_must_name_a_source_that_returns_rows() -> None:
    draft = a_draft(
        sources=[
            DraftSource(key="s1", id="docs", kind="file", filter_tags=["x"]),
        ],
        sections=[
            DraftSection(
                key="t1",
                heading="S",
                instruction="Write.",
                source_keys=["s1"],
                table_key="s1",
            )
        ],
    )
    assert "table_not_bigquery" in codes(validate_draft(draft), "error")


def test_an_api_source_may_be_a_section_table() -> None:
    """target_assessment.md does exactly this, and the filler renders API
    payloads as tables."""
    draft = a_draft(
        sources=[
            DraftSource(key="s1", id="ext", kind="api", connector="c", endpoint="e"),
        ],
        sections=[
            DraftSection(
                key="t1",
                heading="S",
                instruction="Write.",
                source_keys=["s1"],
                table_key="s1",
            )
        ],
    )
    assert "table_not_bigquery" not in codes(validate_draft(draft), "error")


def test_dangling_references_are_errors() -> None:
    draft = a_draft()
    draft.sections[0].source_keys = ["gone"]
    draft.sections[0].table_key = "gone"
    found = codes(validate_draft(draft), "error")
    assert {"dangling_source", "dangling_table"} <= found


def test_warnings_do_not_block_a_save() -> None:
    draft = a_draft(description="")
    issues = validate_draft(draft)
    assert "description_missing" in codes(issues, "warning")
    assert not has_errors(issues)


# --- blank / clone ---------------------------------------------------------


def test_blank_draft_takes_its_tags_from_the_caller() -> None:
    draft = blank_draft(report_type="x_new", tags={"compliance": ["non_gxp"]})
    assert draft.tags == {"compliance": ["non_gxp"]}
    assert [i.key for i in draft.inputs] == ["i1"]
    assert [s.key for s in draft.sources] == ["s1"]
    assert draft.sources[0].kind == "bigquery"
    assert [t.key for t in draft.sections] == ["t1"]
    assert draft.version == "0.1.0"


def test_clone_carries_tags_over_and_resets_the_version() -> None:
    source = a_draft(tags={"compliance": ["gxp"], "domain": ["dmpk"]}, version="2.4.1")
    clone = clone_draft(source, report_type="unit_widget_copy")
    assert clone.tags == {"compliance": ["gxp"], "domain": ["dmpk"]}
    assert clone.report_type == "unit_widget_copy"
    assert clone.version == "0.1.0"
    assert clone.title == "Unit Widget (copy)"
    clone.tags["domain"].append("cmc")
    clone.sources[0].id = "changed"
    assert source.tags["domain"] == ["dmpk"]
    assert source.sources[0].id == "potency"


# --- write_template --------------------------------------------------------


def test_write_creates_a_loadable_file(tmp_path: Path) -> None:
    target = tmp_path / "unit_widget.md"
    result = write_template(a_draft(), target, create=True)
    assert result.created is True
    assert result.path == target
    assert result.sha256 == read_sha256(target)
    assert load_report_doc(target).report_type == "unit_widget"
    assert "updated: " in target.read_text(encoding="utf-8")


def test_writer_never_writes_a_file_the_loader_rejects(tmp_path: Path) -> None:
    bad = blank_draft(report_type="x_bad")  # no title, owner, sections filled in
    with pytest.raises(TemplateWriteError):
        write_template(bad, tmp_path / "x_bad.md", create=True)
    assert not (tmp_path / "x_bad.md").exists()  # INV-3
    assert not list(tmp_path.glob(".*.tmp"))
    assert not list(tmp_path.iterdir())


def test_a_draft_the_loader_would_reject_is_caught_before_it_lands(
    tmp_path: Path,
) -> None:
    """Validation and the loader disagree about nothing — but if they ever did,
    the round-trip proof is the backstop."""
    target = tmp_path / "unit_widget.md"
    write_template(a_draft(), target, create=True)
    before = target.read_text(encoding="utf-8")

    broken = a_draft(title="")
    with pytest.raises(TemplateWriteError):
        write_template(broken, target, create=False)
    assert target.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_create_refuses_to_clobber_and_update_refuses_to_invent(
    tmp_path: Path,
) -> None:
    target = tmp_path / "unit_widget.md"
    write_template(a_draft(), target, create=True)
    with pytest.raises(TemplateWriteError):
        write_template(a_draft(), target, create=True)
    with pytest.raises(TemplateWriteError):
        write_template(a_draft(), tmp_path / "not_here.md", create=False)


def test_a_stale_editor_cannot_silently_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "unit_widget.md"
    write_template(a_draft(), target, create=True)
    opened_at = read_sha256(target)

    write_template(a_draft(title="Changed By Someone Else"), target, create=False)

    with pytest.raises(TemplateConflict) as excinfo:
        write_template(a_draft(title="Mine"), target, expected_sha256=opened_at)
    assert excinfo.value.expected == opened_at
    assert "Changed By Someone Else" in target.read_text(encoding="utf-8")

    write_template(a_draft(title="Mine"), target, expected_sha256=None)
    assert load_report_doc(target).title == "Mine"


def test_overwriting_takes_a_backup_and_prunes(tmp_path: Path) -> None:
    target = tmp_path / "unit_widget.md"
    backups = tmp_path / "backups"
    write_template(a_draft(), target, create=True)
    for n in range(13):
        write_template(
            a_draft(title=f"Version {n}"), target, backup_dir=backups, create=False
        )
    kept = sorted((backups / "unit_widget").glob("*.md"))
    assert 0 < len(kept) <= 10


def test_backup_of_a_missing_file_is_a_no_op(tmp_path: Path) -> None:
    assert backup_template(tmp_path / "nope.md", tmp_path / "b") is None


def test_delete_is_undoable_through_the_trash(tmp_path: Path) -> None:
    target = tmp_path / "unit_widget.md"
    trash = tmp_path / "trash"
    write_template(a_draft(), target, create=True)

    trashed = trash_template(target, trash)
    assert not target.exists()
    assert find_trashed("unit_widget", trash) == trashed

    restore_trashed(trashed, target)
    assert load_report_doc(target).report_type == "unit_widget"
    assert find_trashed("unit_widget", trash) is None

    with pytest.raises(TemplateWriteError):
        trash_template(tmp_path / "gone.md", trash)


def test_restore_refuses_to_overwrite_a_live_template(tmp_path: Path) -> None:
    target = tmp_path / "unit_widget.md"
    trash = tmp_path / "trash"
    write_template(a_draft(), target, create=True)
    trashed = trash_template(target, trash)
    write_template(a_draft(title="Rebuilt"), target, create=True)
    with pytest.raises(TemplateWriteError):
        restore_trashed(trashed, target)


def test_read_sha256_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_sha256(tmp_path / "nope.md") == ""


def test_draft_from_text_needs_front_matter() -> None:
    with pytest.raises(TemplateWriteError):
        draft_from_text("# Just a heading\n\n## 1. Section\n")


def test_serialize_is_pure(tmp_path: Path) -> None:
    draft = a_draft()
    before = _normalise_draft(draft)
    first = serialize_draft(draft)
    second = serialize_draft(draft)
    assert first == second
    assert _normalise_draft(draft) == before


def test_written_files_use_unix_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "unit_widget.md"
    write_template(a_draft(), target, create=True)
    raw = target.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
