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
