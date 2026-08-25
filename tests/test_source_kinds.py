"""Source kinds: what they map to, and that the editor cannot lose them.

Evidence for a preclinical report is not all in one folder. A section may need a
warehouse query, a deck a scientist left in SharePoint, and a page in
Confluence, and each of those is reached a different way. This file pins the
mapping from an authored source to the binding the engine executes, and pins the
round trip — because the failure mode is silent: the editor reads a template,
writes it back, and a kind it did not understand is simply gone.

That is not hypothetical. The writer dropped `> Visual:` exactly that way, and
only the round-trip test noticed.
"""

from __future__ import annotations

import pytest

from services.template_service.report_doc import ReportDocError, load_report_doc
from services.template_service.report_doc_writer import (
    SOURCE_KINDS,
    draft_from_text,
    serialize_draft,
)
from shared.schemas.template import (
    ApiCallBinding,
    FileSetBinding,
    NamedQueryBinding,
    SqlQueryBinding,
)

TEMPLATE = """---
report_type: kinds_probe
title: Every source kind
version: 0.1.0
description: Exercises one source of each kind.
owner: test

inputs:
  - id: compound_id
    prompt: Compound
    required: true

sources:
  - id: warehouse
    type: bigquery
    dataset: nonclinical_safety
    query_id: pivotal_toxicology_summary_v2
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: lims
    type: oracle
    service: LIMSPRD
    query_id: invivo_pk_summary_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: lims_adhoc
    type: oracle
    service: LIMSPRD
    sql: |
      SELECT species, auc FROM pk WHERE compound_id = :compound_id
  - id: decks
    type: sharepoint
    site: Nonclinical-Safety
    folder: Programmes/XYZ-001/Tox
    file_types: pptx, docx
  - id: deck_search
    type: sharepoint
    site: Nonclinical-Safety
    query: "26-week dog"
    file_types: pptx
  - id: wiki
    type: confluence
    space: PSS
    cql: 'label = "toxicology"'
  - id: local_docs
    type: file
    filter_tags: [nonclinical, toxicology]

citation:
  required: true
  granularity: claim
  min_per_paragraph: 1

output:
  formats: [html]
---

# Every source kind

## 1. All of them

> Instruction: Summarise everything.
> Sources: warehouse, lims, lims_adhoc, decks, deck_search, wiki, local_docs
> Table: warehouse
"""


@pytest.fixture
def template(tmp_path):
    path = tmp_path / "kinds_probe.md"
    path.write_text(TEMPLATE, encoding="utf-8")
    return load_report_doc(path)


def _bindings(template) -> dict[str, object]:
    return {b.binding_id: b for b in template.sections[0].data_bindings}


# --- the mapping -----------------------------------------------------------


def test_oracle_with_a_named_query_becomes_a_named_query_binding(template):
    """Same binding shape as BigQuery, because the difference is the connection
    rather than anything the section cares about."""
    binding = _bindings(template)["lims"]
    assert isinstance(binding, NamedQueryBinding)
    assert binding.query_id == "invivo_pk_summary_v1"


def test_an_oracle_source_records_which_database_it_read(template):
    """`source` carries the service name so a citation says which database a
    figure came from. "The numbers came from a database" is not provenance —
    a reader checking a NOAEL needs to know which system to look in."""
    assert _bindings(template)["lims"].source == "LIMSPRD"
    assert _bindings(template)["lims_adhoc"].source == "LIMSPRD"


def test_oracle_inline_sql_takes_the_gated_path(template):
    """SqlQueryBinding, not NamedQueryBinding — the linter, dry-run and
    approval gate hang off that type, and routing inline SQL around them
    because it happens to be Oracle would be a hole in the same wall."""
    assert isinstance(_bindings(template)["lims_adhoc"], SqlQueryBinding)


def test_oracle_needs_a_query_of_some_kind(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text(
        TEMPLATE.replace("    query_id: invivo_pk_summary_v1\n", ""), encoding="utf-8"
    )
    with pytest.raises(ReportDocError, match="needs query_id or sql"):
        load_report_doc(path)


def test_sharepoint_becomes_an_api_call_through_a_connector(template):
    """Not a FileSetBinding. A local folder cannot be unreachable; a SharePoint
    site can, and it needs a configured connector and an audited call to get
    there. Collapsing the two would hide a network dependency behind something
    that looks like reading a directory."""
    binding = _bindings(template)["decks"]
    assert isinstance(binding, ApiCallBinding)
    assert binding.connector_id == "sharepoint"
    assert binding.parameters["site"] == "Nonclinical-Safety"
    assert binding.parameters["file_types"] == "pptx, docx"


def test_a_sharepoint_search_and_a_folder_fetch_are_different_operations(template):
    """The gate authorises per operation, so "fetch this folder" and "search the
    site" cannot arrive as the same call."""
    bindings = _bindings(template)
    assert bindings["decks"].endpoint == "get_file"
    assert bindings["deck_search"].endpoint == "search_files"


def test_the_local_and_remote_document_kinds_stay_distinct(template):
    bindings = _bindings(template)
    assert isinstance(bindings["local_docs"], FileSetBinding)
    assert isinstance(bindings["decks"], ApiCallBinding)


# --- the round trip --------------------------------------------------------


def test_every_kind_survives_a_pass_through_the_editor():
    """Read the template, write it back, read it again. The editor does exactly
    this on every save, so a kind that does not survive here is a kind that
    gets deleted by opening a template and pressing save."""
    once = draft_from_text(TEMPLATE, report_type="kinds_probe")
    twice = draft_from_text(serialize_draft(once), report_type="kinds_probe")

    assert [s.kind for s in twice.sources] == [s.kind for s in once.sources]

    # Assert the VALUES, not just that the two passes agree.
    #
    # This test passed while the reader silently dropped every Oracle and
    # SharePoint setting, because both sides were empty and empty equals empty.
    # A round-trip check that only compares before with after certifies a
    # pipeline that loses the same data twice.
    expected = {
        "lims": {"service": "LIMSPRD", "query_id": "invivo_pk_summary_v1"},
        "lims_adhoc": {"service": "LIMSPRD"},
        "decks": {
            "site": "Nonclinical-Safety",
            "folder": "Programmes/XYZ-001/Tox",
            "file_types": "pptx, docx",
        },
        "deck_search": {
            "site": "Nonclinical-Safety",
            "query": "26-week dog",
            "file_types": "pptx",
        },
    }
    by_id = {s.id: s for s in twice.sources}
    for source_id, fields in expected.items():
        for field, value in fields.items():
            actual = getattr(by_id[source_id], field)
            assert actual == value, (
                f"source {source_id!r} lost {field!r}: expected {value!r}, "
                f"got {actual!r}"
            )

    for before, after in zip(once.sources, twice.sources):
        assert after.id == before.id
        for field in ("service", "site", "folder", "file_types", "query", "query_id"):
            assert getattr(after, field) == getattr(before, field), (
                f"source {before.id!r} lost {field!r} on the round trip: "
                f"{getattr(before, field)!r} -> {getattr(after, field)!r}"
            )


def test_the_editor_offers_every_kind_the_loader_understands():
    """The two lists drifting apart is how you get a kind that can be authored
    by hand but not seen in the app, or offered in the app and rejected on
    load."""
    from services.api_gateway.runs import SOURCE_KIND_OPTIONS

    offered = {kind for kind, _label in SOURCE_KIND_OPTIONS}
    assert offered == set(SOURCE_KINDS), (
        f"editor offers {sorted(offered)}, loader knows {sorted(SOURCE_KINDS)}"
    )


def test_a_tabular_kind_can_back_a_table_directive():
    """`> Table:` may only name a source that produces rows. Oracle does, so it
    belongs in that set — otherwise an Oracle-backed section could never show
    its own numbers."""
    from services.template_service.report_doc_writer import _TABULAR_KINDS

    assert "oracle" in _TABULAR_KINDS
    assert "sharepoint" not in _TABULAR_KINDS, (
        "a deck is not a table; treating it as one would put a document listing "
        "where a reader expects measurements"
    )


# --- a reference that resolves to nothing ----------------------------------
#
# Sixteen `query_id` references across the shipped template library point at
# queries the registry does not have, and until this check existed the only
# place that said so was a run's preflight — after someone had chosen a
# template, filled in a compound and pressed go.


def _sql_draft(**source_fields):
    from services.template_service.report_doc_writer import DraftSource, blank_draft

    draft = blank_draft(report_type="probe")
    draft.title = "Probe"
    draft.version = "0.1.0"
    draft.description = "Fixture."
    draft.owner = "test-team"
    draft.inputs[0].id = "compound_id"
    draft.inputs[0].prompt = "Compound"
    draft.sections[0].heading = "Only section"
    draft.sections[0].instruction = "Write something factual."
    fields = {"key": "s1", "id": "src", "kind": "bigquery", "dataset": "ds"}
    fields.update(source_fields)
    draft.sources = [DraftSource(**fields)]
    return draft


def _unknown_query_issues(draft, known):
    from services.template_service.report_doc_writer import validate_draft

    return [
        i
        for i in validate_draft(draft, known_query_ids=known)
        if i.code == "unknown_named_query"
    ]


KNOWN = ("pivotal_toxicology_summary_v2", "exposure_margin_v1")


def test_an_unregistered_query_is_flagged_while_authoring():
    issues = _unknown_query_issues(_sql_draft(query_id="no_such_query_v1"), KNOWN)
    assert len(issues) == 1
    assert "no_such_query_v1" in issues[0].message


def test_a_registered_query_is_not_flagged():
    assert _unknown_query_issues(_sql_draft(query_id="exposure_margin_v1"), KNOWN) == []


def test_a_near_miss_gets_the_name_it_probably_meant():
    """The live failure was `pivotal_tox_summary_v2` against a registry holding
    `pivotal_toxicology_summary_v2` — a typo at a glance, a mystery without the
    candidate spelled out."""
    issues = _unknown_query_issues(_sql_draft(query_id="pivotal_tox_summary_v2"), KNOWN)
    assert "pivotal_toxicology_summary_v2" in issues[0].fix_hint


def test_an_unrelated_name_gets_no_invented_suggestion():
    """Suggesting `exposure_margin_v1` for `physchem_formulation_v1` is worse
    than suggesting nothing: a confident wrong answer sends someone off to
    check it."""
    issues = _unknown_query_issues(_sql_draft(query_id="physchem_formulation_v1"), KNOWN)
    assert "Did you mean" not in issues[0].fix_hint
    assert "registry" in issues[0].fix_hint


def test_no_registry_means_no_check_not_everything_is_broken():
    """An empty sequence is "nothing to compare against", not "nothing exists".
    The other reading turns every query in every template into a finding the
    moment a caller forgets the argument — and a validator that cries wolf gets
    switched off, after which it catches nothing."""
    from services.template_service.report_doc_writer import validate_draft

    draft = _sql_draft(query_id="anything_at_all_v1")
    assert not [
        i for i in validate_draft(draft) if i.code == "unknown_named_query"
    ]


def test_it_is_a_warning_so_the_template_still_saves():
    """Errors block the save. Thirteen references in the shipped library point
    at queries nobody has written yet; a template naming a planned query is a
    legitimate artifact and the registry is what is incomplete. Blocking would
    make every existing template unsavable."""
    issues = _unknown_query_issues(_sql_draft(query_id="not_yet_written_v1"), KNOWN)
    assert issues[0].severity == "warning"


def test_oracle_sources_are_checked_too():
    """Same registry, same rule. An Oracle-backed section referencing a
    nonexistent query fails exactly as quietly."""
    draft = _sql_draft(kind="oracle", service="LIMSPRD", dataset="", query_id="ghost_v1")
    assert len(_unknown_query_issues(draft, KNOWN)) == 1
