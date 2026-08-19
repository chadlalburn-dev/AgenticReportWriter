"""End-to-end tests for in-app template authoring (contract §4, §8.3).

Everything here drives the real FastAPI app through `TestClient` with the
process-wide `RunStore` singleton re-pointed at a throwaway templates
directory, so no test can write into `report-templates/`.

The centre of gravity is the save pipeline: what reaches disk, what is
refused, and — the part that is easiest to get wrong — what is refused
*loudly* rather than silently dropped.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import runs as runs_module
from services.api_gateway.main import app
from services.template_service.report_doc import load_report_doc

FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path):
    """A store whose templates/backups/trash all live under a temp dir."""
    templates = tmp_path / "report-templates"
    templates.mkdir()
    replacement = runs_module.RunStore(
        root=tmp_path / "runs",
        templates_dir=templates,
        backups_dir=tmp_path / "backups",
        trash_dir=tmp_path / "trash",
    )
    previous = runs_module._STORE
    runs_module._STORE = replacement
    try:
        yield replacement
    finally:
        runs_module._STORE = previous
        replacement.shutdown(wait=True)


@pytest.fixture()
def client(store):
    return TestClient(app)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def draft_form(key: str = "probe_template", **overrides: str) -> list[tuple[str, str]]:
    """A minimal but genuinely valid editor submission.

    Two sources of different kinds and two sections, so the source/section
    cross-references are actually exercised rather than assumed.
    """
    pairs = [
        ("base_sha", ""),
        ("report_type", key),
        ("title", "Probe Template"),
        ("description", "A probe template with enough description to satisfy the gallery card."),
        ("version", "0.1.0"),
        ("owner", "dmpk"),
        ("doc_heading", "Probe Template"),
        ("tags__domain", "dmpk"),
        ("tags__compliance", "non_gxp"),
        ("input.k", "i1"),
        ("input.i1.id", "compound_id"),
        ("input.i1.prompt", "Compound identifier"),
        ("input.i1.required", "1"),
        ("source.k", "s1"),
        ("source.s1.id", "assays"),
        ("source.s1.kind", "bigquery"),
        ("source.s1.dataset", "preclin"),
        ("source.s1.query_id", "assays_v1"),
        ("source.k", "s2"),
        ("source.s2.id", "method_notes"),
        ("source.s2.kind", "confluence"),
        ("source.s2.space", "DMPK"),
        ("section.k", "t1"),
        ("section.t1.heading", "Overview"),
        ("section.t1.instruction", "Summarise the assay results and cite every value."),
        ("section.t1.sources", "s1"),
        ("section.t1.table", "s1"),
        ("section.k", "t2"),
        ("section.t2.heading", "Method"),
        ("section.t2.instruction", "Describe the assay method and where it is documented."),
        ("section.t2.sources", "s2"),
        ("section.t2.table", ""),
        ("citation_required", "1"),
        ("citation_granularity", "claim"),
        ("citation_min", "1"),
    ]
    if not overrides:
        return pairs
    out: list[tuple[str, str]] = []
    for name, value in pairs:
        if name in overrides:
            replacement = overrides[name]
            if replacement is None:  # type: ignore[comparison-overlap]
                continue
            out.append((name, replacement))
        else:
            out.append((name, value))
    # An override naming a field the baseline does not carry ADDS it. Silently
    # ignoring it would let a test think it exercised something it did not.
    present = {name for name, _ in pairs}
    for name, value in overrides.items():
        if name not in present and value is not None:
            out.append((name, value))
    return out


def submit(client: TestClient, path: str, pairs, op: str = "save"):
    body = urllib.parse.urlencode(list(pairs) + [("op", op)])
    return client.post(path, content=body, headers=FORM_HEADERS, follow_redirects=False)


def issue_text(page: str) -> str:
    rows = re.findall(r'<li class="rg-issues__row[^"]*">(.*?)</li>', page, re.S)
    joined = " ".join(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r)) for r in rows)
    return html.unescape(joined)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_writes_a_file_the_loader_can_read_back(client, store):
    response = submit(client, "/templates", draft_form())
    assert response.status_code == 303
    assert response.headers["location"] == "/?saved=probe_template"

    path = store.templates_dir / "probe_template.md"
    assert path.exists()

    template = load_report_doc(path)
    assert template.report_type == "probe_template"
    assert template.title == "Probe Template"
    assert template.tags == {"domain": ["dmpk"], "compliance": ["non_gxp"]}
    assert [s.title for s in template.all_sections()] == ["Overview", "Method"]


def test_a_created_template_shows_up_in_its_group_in_the_gallery(client):
    submit(client, "/templates", draft_form())
    html = client.get("/?group=domain").text
    section = re.search(
        r'id="g-domain-dmpk".*?(?=<h3 class="rg-h3" id="g-|</main)', html, re.S
    )
    assert section is not None, "no DMPK group rendered"
    assert "/new/probe_template" in section.group(0)


def test_a_created_template_is_reachable_by_each_of_its_tags(client):
    submit(client, "/templates", draft_form())
    assert "/new/probe_template" in client.get("/?tag=domain:dmpk").text
    assert "/new/probe_template" in client.get("/?tag=compliance:non_gxp").text
    assert "/new/probe_template" not in client.get("/?tag=domain:clinical").text


# ---------------------------------------------------------------------------
# edit / clone / delete
# ---------------------------------------------------------------------------


def test_edit_persists_a_changed_tag_and_a_changed_section(client, store):
    submit(client, "/templates", draft_form())
    edited = draft_form(
        **{
            "tags__domain": "clinical",
            "section.t2.heading": "Method and limitations",
        }
    )
    response = submit(client, "/templates/probe_template", edited)
    assert response.status_code == 303

    template = load_report_doc(store.templates_dir / "probe_template.md")
    assert template.tags["domain"] == ["clinical"]
    assert template.all_sections()[1].title == "Method and limitations"


def test_the_key_is_locked_while_editing(client, store):
    submit(client, "/templates", draft_form())
    response = submit(
        client, "/templates/probe_template", draft_form(report_type="something_else")
    )
    assert response.status_code == 303
    assert (store.templates_dir / "probe_template.md").exists()
    assert not (store.templates_dir / "something_else.md").exists()


def test_delete_moves_the_file_to_trash_and_undelete_puts_it_back(client, store):
    submit(client, "/templates", draft_form())
    path = store.templates_dir / "probe_template.md"

    response = client.post("/templates/probe_template/delete", follow_redirects=False)
    assert response.status_code == 303
    assert not path.exists()
    assert "/new/probe_template" not in client.get("/").text

    response = client.post("/templates/probe_template/undelete", follow_redirects=False)
    assert response.status_code == 303
    assert path.exists()
    assert "/new/probe_template" in client.get("/").text


def test_a_stale_base_sha_is_a_conflict_not_a_silent_overwrite(client, store):
    submit(client, "/templates", draft_form())

    stale = draft_form(title="Mine") + [("base_sha", "0" * 64)]
    response = submit(client, "/templates/probe_template", stale)
    assert response.status_code == 409
    assert load_report_doc(store.templates_dir / "probe_template.md").title != "Mine"

    forced = stale + [("force", "1")]
    response = submit(client, "/templates/probe_template", forced)
    assert response.status_code == 303
    assert load_report_doc(store.templates_dir / "probe_template.md").title == "Mine"


# ---------------------------------------------------------------------------
# refusals — nothing reaches disk, and nothing is dropped quietly
# ---------------------------------------------------------------------------


def test_a_section_pointing_at_a_source_that_does_not_exist_is_refused(client, store):
    """The reference must be REPORTED, never quietly discarded.

    Dropping it while parsing the form would save a template whose section has
    no evidence behind it — and would make the `dangling_source` rule
    unreachable. This is the regression guard for exactly that.
    """
    broken = draft_form(
        **{"section.t1.sources": "s404", "section.t1.table": "s404"}
    )
    response = submit(client, "/templates", broken)

    assert response.status_code == 422
    assert not (store.templates_dir / "probe_template.md").exists()
    text = issue_text(response.text)
    assert "source that no longer exists" in text
    assert "table refers to a source that no longer exists" in text


def test_removing_a_source_clears_its_references_and_says_so(client, store):
    """The one legitimate way a reference disappears — and it is announced."""
    response = submit(client, "/templates", draft_form(), op="remove:source:s1")

    assert response.status_code == 200
    assert not (store.templates_dir / "probe_template.md").exists()
    body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response.text))
    assert "Removed source" in body
    assert "Nothing is written until you press Save" in body


@pytest.mark.parametrize(
    "overrides, expect",
    [
        ({"report_type": "dmpk_adme_summary"}, "already exists"),
        ({"report_type": "Bad-Key!"}, "not a usable key"),
        ({"title": ""}, "needs a title"),
        ({"owner": ""}, "Name the team"),
        ({"version": "one"}, "not a version number"),
        ({"tags__compliance": ""}, "Compliance has not been set"),
        ({"tags__domain": ""}, "Domain area has not been set"),
        ({"source.s1.query_id": ""}, "named query or inline SQL"),
        ({"section.t1.instruction": ""}, "needs an instruction"),
        ({"section.t1.table": "s2"}, "does not return rows"),
        ({"source.s1.id": "compound_id"}, "already the id of an input"),
    ],
    ids=lambda v: "" if isinstance(v, str) else "-".join(sorted(v)),
)
def test_invalid_drafts_are_refused_with_a_useful_message(client, store, overrides, expect):
    if overrides.get("report_type") == "dmpk_adme_summary":
        (store.templates_dir / "dmpk_adme_summary.md").write_text("x", encoding="utf-8")

    response = submit(client, "/templates", draft_form(**overrides))

    assert response.status_code == 422, response.status_code
    assert expect in issue_text(response.text)
    assert not (store.templates_dir / "probe_template.md").exists()


def test_a_section_referencing_an_undeclared_input_is_refused(client, store):
    response = submit(
        client, "/templates", draft_form(**{"source.s1.params": "c = {{inputs.nope}}"})
    )
    assert response.status_code == 422
    assert "not one of this template's inputs" in issue_text(response.text)
    assert not (store.templates_dir / "probe_template.md").exists()


# ---------------------------------------------------------------------------
# GxP is a label and nothing else (contract §9)
# ---------------------------------------------------------------------------


def test_the_new_template_form_defaults_to_non_gxp(client):
    html = client.get("/templates/new").text
    select = re.search(
        r'<select[^>]*name="tags__compliance".*?</select>', html, re.S
    ).group(0)
    assert re.search(r'value="non_gxp"[^>]*\bselected\b', select)
    assert not re.search(r'value="gxp"[^>]*\bselected\b', select)


def test_gxp_changes_nothing_about_a_template_card_except_its_tags(client, store):
    submit(client, "/templates", draft_form("probe_non_gxp"))
    submit(client, "/templates", draft_form("probe_gxp", **{"tags__compliance": "gxp"}))

    a = store.get_template("probe_non_gxp").to_dict()
    b = store.get_template("probe_gxp").to_dict()
    tag_derived = {
        "tags", "tag_tokens", "chips", "n_more_tags", "tag_aria",
        "key", "path", "template_id", "title", "search",
    }
    assert {k: v for k, v in a.items() if k not in tag_derived} == {
        k: v for k, v in b.items() if k not in tag_derived
    }
    # and both compliance values reach a screen reader
    assert "Non-GxP" in a["tag_aria"]
    assert "GxP" in b["tag_aria"]


def test_the_gxp_chip_is_visually_identical_to_every_other_tag_chip(client):
    submit(client, "/templates", draft_form("probe_gxp", **{"tags__compliance": "gxp"}))
    html = client.get("/?group=none").text
    card = re.search(
        r"<li[^>]*>(?:(?!</li>).)*?/new/probe_gxp\"(?:(?!</li>).)*?</li>", html, re.S
    ).group(0)
    classes = re.findall(r'<span class="([^"]*rg-badge--tag[^"]*)"[^>]*>([^<]*)', card)
    gxp = [c for c, label in classes if label.strip() == "GxP"]
    others = [c for c, label in classes if label.strip() not in ("GxP", "")]
    assert gxp, "no GxP chip rendered"
    assert all(c == gxp[0] for c in others), "GxP chip styled differently to its peers"


# ---------------------------------------------------------------------------
# unavailable files stay out of the classified view
# ---------------------------------------------------------------------------


def test_unparseable_files_are_excluded_from_counts_groups_and_facets(client, store):
    (store.templates_dir / "not_a_template.md").write_text(
        "# just some notes\n", encoding="utf-8"
    )
    submit(client, "/templates", draft_form())

    view = store.gallery_view(group="domain", sort="name", tags=[], q="")
    assert [c.key for c in view.cards] == ["probe_template"]
    assert view.n_total == 1
    assert [u.key for u in view.unavailable] == ["not_a_template"]
    assert "not_a_template" not in {
        value.id for facet in view.facets for value in facet.values
    }
    assert sum(g.count for g in view.groups) == 1
