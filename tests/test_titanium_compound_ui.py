"""Titanium redesign: identity, the compound ledger, and the new routes.

Covers the code added for the compound-first information architecture:

  * `services/api_gateway/identity.py` — attribution (NOT authentication)
  * `services/api_gateway/compounds.py` — the compound-level evidence ledger
  * the `/`, `/compound/{id}` and `/templates` routes

The security-relevant test here is
`test_a_forged_proxy_header_cannot_impersonate_unless_the_deployment_opts_in`.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from services.api_gateway import compounds as compounds_module
from services.api_gateway import identity as identity_module
from services.api_gateway.main import app
from services.api_gateway.runs import get_store

IAP_HEADER = "x-goog-authenticated-user-email"
TRUST_ENV = "REPORTGEN_TRUST_PROXY_AUTH"
USER_ENV = "REPORTGEN_USER"


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(TRUST_ENV, raising=False)
    monkeypatch.delenv(USER_ENV, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# identity — attribution, not authentication
# ---------------------------------------------------------------------------


def test_a_forged_proxy_header_cannot_impersonate_unless_the_deployment_opts_in(
    clean_env,
):
    """The security property that makes header-based identity safe.

    Any client can send `X-Forwarded-Email`. It must be ignored unless the
    deployment asserts a proxy is genuinely in front of the app, otherwise the
    "My compounds" split is trivially forgeable.
    """
    headers = {"x-forwarded-email": "someone.else@gsk.com"}
    user = identity_module.resolve_user(headers)
    assert user.user_id != "someone.else@gsk.com"
    assert user.source != "proxy"

    clean_env.setenv(TRUST_ENV, "1")
    trusted = identity_module.resolve_user(headers)
    assert trusted.user_id == "someone.else@gsk.com"
    assert trusted.source == "proxy"


def test_iap_prefix_is_stripped_so_one_human_is_one_identity(clean_env):
    """IAP sends `accounts.google.com:Chad.L.Alburn@gsk.com`. Attribution must
    collapse that to one stable, lowercase key or the same person splits into
    two owners."""
    clean_env.setenv(TRUST_ENV, "1")
    a = identity_module.resolve_user({IAP_HEADER: "accounts.google.com:Chad.L.Alburn@gsk.com"})
    b = identity_module.resolve_user({"x-forwarded-email": "chad.l.alburn@gsk.com"})
    assert a.user_id == b.user_id == "chad.l.alburn@gsk.com"


def test_env_override_beats_the_os_user_but_not_a_trusted_proxy(clean_env):
    clean_env.setenv(USER_ENV, "demo.user@gsk.com")
    assert identity_module.resolve_user({}).source == "env"

    clean_env.setenv(TRUST_ENV, "1")
    user = identity_module.resolve_user({IAP_HEADER: "real.person@gsk.com"})
    assert user.user_id == "real.person@gsk.com"
    assert user.source == "proxy"


def test_identity_always_resolves_to_something_usable(clean_env):
    user = identity_module.resolve_user({})
    assert user.user_id
    assert user.display_name
    assert user.source in ("proxy", "env", "os", "fallback")


@pytest.mark.parametrize(
    "user_id,expected",
    [
        ("chad.l.alburn@gsk.com", "CA"),
        ("cla95835", "CL"),
        ("a", "A"),
    ],
)
def test_initials_for_the_header_badge(clean_env, user_id, expected):
    clean_env.setenv(USER_ENV, user_id)
    assert identity_module.resolve_user({}).initials == expected


# ---------------------------------------------------------------------------
# compound id heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,ok",
    [
        ("XYZ-001", True),
        ("GSK-OLIGO-42", True),
        ("Kinase Z", False),      # a target, and it has a space
        ("CRASH", False),         # no digit
        ("", False),
        ("x" * 40, False),        # too long to be an identifier
    ],
)
def test_only_plausible_compound_identifiers_reach_the_list(value, ok):
    """`primary_input` is whatever a template's first field happened to be, so
    the run history also holds target names and scratch values. Without this
    gate the compounds list and the Related rail fill with noise."""
    assert compounds_module.looks_like_compound_id(value) is ok


# ---------------------------------------------------------------------------
# identity-band text shortening
# ---------------------------------------------------------------------------


def test_the_subtitle_is_shortened_to_keep_the_band_on_one_baseline():
    """The full chemical class pushes the metadata row onto a second line and
    costs 32px of band height. The design's own example uses the short form."""
    assert (
        compounds_module._short_subtitle(
            "Small-molecule selective inhibitor of Kinase Z"
        )
        == "Kinase Z inhibitor"
    )


def test_an_unrecognised_chemical_class_is_left_alone():
    text = "Something we cannot parse"
    assert compounds_module._short_subtitle(text) == text


def test_the_metadata_row_uses_a_short_date():
    assert compounds_module._short_date("19 Aug 2026, 12:20") == "19 Aug"
    assert compounds_module._short_date("") == ""


# ---------------------------------------------------------------------------
# the compound ledger
# ---------------------------------------------------------------------------


def test_the_ledger_unions_bindings_across_templates_and_counts_users():
    """Bindings are declared per template; the compound page shows one ledger.
    A binding several templates declare must appear ONCE, with `used_by`
    counting the templates."""
    view = compounds_module.build_compound_view("XYZ-001")
    ids = [b.binding_id for b in view.bindings]
    assert ids, "expected a non-empty evidence ledger"
    assert len(ids) == len(set(ids)), "a binding was listed twice"
    assert all(b.used_by_count >= 1 for b in view.bindings)
    assert any(b.used_by_count > 1 for b in view.bindings), (
        "expected at least one binding shared by two templates"
    )


def test_coverage_totals_agree_with_the_rows():
    view = compounds_module.build_compound_view("XYZ-001")
    assert view.bindings_total == len(view.bindings)
    assert view.bindings_ready == sum(1 for b in view.bindings if b.resolved)
    assert view.gaps_count == view.bindings_total - view.bindings_ready


def test_resolved_bindings_sort_before_gaps():
    view = compounds_module.build_compound_view("XYZ-001")
    flags = [b.resolved for b in view.bindings]
    assert flags == sorted(flags, reverse=True), "gaps must not interleave"


def test_reports_are_ranked_by_coverage_and_only_the_top_is_lead():
    view = compounds_module.build_compound_view("XYZ-001")
    assert view.reports
    fractions = [
        (r.sources_ready / r.sources_total) if r.sources_total else 0
        for r in view.reports
    ]
    assert fractions == sorted(fractions, reverse=True)
    assert view.reports[0].lead is True
    assert sum(1 for r in view.reports if r.lead) == 1
    assert view.primary_report is view.reports[0]


def test_each_report_tick_track_matches_its_ratio():
    view = compounds_module.build_compound_view("XYZ-001")
    for r in view.reports:
        assert len(r.tick_flags) == r.sources_total
        assert sum(1 for f in r.tick_flags if f) == r.sources_ready
        assert r.ratio_text == f"{r.sources_ready}/{r.sources_total}"


def test_an_unknown_compound_does_not_explode():
    """Readiness is derived from binding targets, not from compound data, so an
    unrun compound still has a meaningful ledger. It must not 500."""
    view = compounds_module.build_compound_view("NOPE-999")
    assert view.compound_id == "NOPE-999"
    assert view.runs == []


def test_a_missing_query_is_reported_once_not_per_template():
    """Audit item 8: the original stated the same warning three times. The
    footnote count must be a count of distinct bindings, not of mentions."""
    view = compounds_module.build_compound_view("XYZ-001")
    not_registered = [b for b in view.bindings if b.returns_text == "not reg."]
    assert view.missing_query_count == len(not_registered)


# ---------------------------------------------------------------------------
# owner scoping
# ---------------------------------------------------------------------------


def test_owners_text_reads_naturally():
    f = compounds_module._owners_text
    assert f(["me@x"], "me@x") == "you"
    assert f(["me@x", "b@x"], "me@x") == "you +1 other"
    assert f(["me@x", "b@x", "c@x"], "me@x") == "you +2 others"
    assert f(["b@x", "c@x"], "me@x") == "2 people"
    assert f([], "me@x") == "unattributed", "runs predating attribution"


def test_mine_is_a_subset_of_all():
    counts = compounds_module.compound_scope_counts(current_user="cla95835")
    assert counts["mine"] <= counts["all"]
    mine = compounds_module.compound_cards(current_user="cla95835", scope="mine")
    every = compounds_module.compound_cards(current_user="cla95835", scope="all")
    assert {c.compound_id for c in mine} <= {c.compound_id for c in every}
    assert all(c.mine for c in mine)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app)


def test_the_front_door_is_compounds_not_the_report_gallery(client):
    """Audit item 1. The gallery's six controls for six cards must not be the
    first thing a user meets."""
    body = client.get("/").text
    assert "/static/titanium.css" in body
    assert "compounds" in body.lower()
    # the gallery's controls must not be on the front page
    assert 'name="group"' not in body
    assert 'name="sort"' not in body


def test_the_template_library_still_exists_at_templates(client):
    """`/templates` is now a plain Titanium list, not the old faceted gallery.

    The six-control bank was the largest complexity finding in the UI audit, so
    the controls moved into a <details> that opens on demand. The CAPABILITY is
    unchanged — the taxonomy is what keeps a growing library navigable — and
    every runnable template stays reachable on the Titanium shell.
    """
    r = client.get("/templates")
    assert r.status_code == 200
    assert "/static/titanium.css" in r.text
    assert "gsk.css" not in r.text
    # The tag faceting stays: the taxonomy is what keeps a growing library
    # navigable. What changed is its form — the controls now live in a
    # <details> instead of a permanent six-control bank.
    assert 'name="group"' in r.text
    assert 'name="sort"' in r.text
    assert 'name="tag"' in r.text

    runnable, _ = get_store().list_templates()
    assert runnable
    for card in runnable:
        assert str(escape(card.title)) in r.text, card.key


def test_searching_a_known_compound_redirects_to_its_page(client):
    r = client.get("/?q=XYZ-001", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/compound/XYZ-001")


def test_searching_nonsense_re_renders_with_a_note_instead_of_guessing(client):
    r = client.get("/?q=definitely-not-a-compound-zzz", follow_redirects=False)
    assert r.status_code == 200
    assert "Nothing matches" in r.text


@pytest.mark.parametrize("scope", ["mine", "all", "bogus", ""])
def test_every_scope_value_renders(client, scope):
    r = client.get(f"/?scope={scope}")
    assert r.status_code == 200


def test_the_compound_page_renders_the_ledger_and_the_report_rail(client):
    body = client.get("/compound/XYZ-001").text
    assert "XYZ-001" in body
    assert "Evidence bindings" in body
    assert "Draft a report" in body
    assert "ti-brow" in body, "expected binding rows"


@pytest.mark.parametrize("flt,expect_resolved", [("ready", True), ("gaps", False)])
def test_the_binding_filter_actually_filters(client, flt, expect_resolved):
    view = compounds_module.build_compound_view("XYZ-001")
    expected = sum(1 for b in view.bindings if b.resolved is expect_resolved)
    body = client.get(f"/compound/XYZ-001?filter={flt}").text
    assert body.count('class="ti-brow"') == expected


def test_an_unknown_binding_filter_widens_instead_of_erroring(client):
    """A stale bookmark should still work — same rule the gallery follows."""
    all_rows = client.get("/compound/XYZ-001?filter=all").text.count('class="ti-brow"')
    junk = client.get("/compound/XYZ-001?filter=nonsense").text
    assert junk.count('class="ti-brow"') == all_rows


def test_the_titanium_shell_loads_no_off_origin_assets(client):
    """This app runs offline against local data; fonts are self-hosted."""
    for path in ("/", "/compound/XYZ-001"):
        body = client.get(path).text
        assert "fonts.googleapis.com" not in body
        assert "fonts.gstatic.com" not in body
        assert "/static/fonts/chivo-latin-var.woff2" in body


def test_the_self_hosted_fonts_are_actually_on_disk(client):
    for name in ("chivo-latin-var.woff2", "chivo-mono-latin-var.woff2"):
        r = client.get(f"/static/fonts/{name}")
        assert r.status_code == 200
        assert r.content[:4] == b"wOF2", "expected a real WOFF2 payload"
