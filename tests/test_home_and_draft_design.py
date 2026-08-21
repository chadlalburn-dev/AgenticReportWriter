"""Contracts for the home page, the draft outline, and the empty states.

These are design decisions with a reason, so they are pinned rather than left
to drift:

  * The home page shows only REAL per-compound facts. Source coverage is the
    obvious thing to put there and is deliberately absent, because preflight
    resolves against the shared corpus and returns the same figure for every
    compound — signal-shaped noise.
  * A multi-section draft offers an outline. `draft.outline` existed in the
    view model from the start and rendered nowhere, so a six-section report
    had no way to show its own structure.
  * An empty state names a next action. A dead end is a bug.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import compounds as compounds_module
from services.api_gateway import runs as runs_module
from services.api_gateway.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def terminal_run() -> str:
    store = runs_module.get_store()
    for summary in store.list_runs(limit=40):
        if summary.terminal:
            return summary.run_id
    pytest.skip("no terminal run in the shared store")


# --- home: real facts only --------------------------------------------------


def test_compound_cards_carry_only_per_compound_evidence():
    """Every number on a card comes from that compound's own runs."""
    cards = compounds_module.compound_cards()
    assert cards
    for card in cards:
        # the rollup is internally consistent
        assert card.claims_cited <= card.claims_total
        assert card.has_gaps == (
            bool(card.claims_total) and card.claims_cited < card.claims_total
        )
        assert card.reports_drafted <= card.run_count or card.run_count == 0
        if card.claims_total:
            assert card.cited_text, "a compound with claims must state its ratio"


def test_compound_cards_actually_differ_between_compounds():
    """The guard against re-introducing signal-shaped noise.

    Source coverage was rejected for the home page precisely because it was
    identical for every compound. If the facts shown here ever collapse to one
    value across a varied portfolio, they have stopped being per-compound.
    """
    cards = compounds_module.compound_cards()
    if len(cards) < 2:
        pytest.skip("needs at least two compounds to compare")
    signatures = {(c.reports_drafted, c.claims_total, c.run_count) for c in cards}
    assert len(signatures) > 1, (
        "every compound reports identical figures — that is not per-compound signal"
    )


def test_the_home_page_states_the_evidence_ratio(client: TestClient):
    body = client.get("/?scope=all").text
    assert "claims cited" in body
    assert 'class="ti-cmp' in body


def test_the_home_page_offers_recent_activity_and_a_portfolio_rollup(client):
    """Continuity: a returning user should not need the Runs tab to see what
    happened, and the rollup is what makes a three-compound page feel like a
    portfolio rather than an empty list."""
    body = client.get("/?scope=all").text
    assert "ti-rrun" in body, "no recent-runs rail"
    assert "Portfolio" in body
    assert "ti-fact__n" in body


def test_the_home_page_does_not_show_source_coverage(client: TestClient):
    """Explicitly asserted, because it is the tempting wrong answer.

    `build_compound_view` returns the same bindings_ready/bindings_total for
    every compound, so a coverage meter on this page would look like a
    per-compound measure and be none.
    """
    body = client.get("/?scope=all").text
    assert "sources ready" not in body, (
        "source coverage is not per-compound and must not appear on the "
        "compounds list — see CompoundCard's docstring"
    )


# --- draft: document structure ---------------------------------------------


def test_a_multi_section_draft_renders_an_outline(client, terminal_run: str):
    body = client.get(f"/runs/{terminal_run}?tab=draft").text
    draft = runs_module.get_store().draft_view(terminal_run)
    if len(draft.outline) <= 1:
        pytest.skip("single-section draft: the outline is suppressed by design")
    assert "ti-outline__list" in body
    assert "ti-docgrid" in body


def test_every_outline_link_resolves_to_a_section_on_the_page(client, terminal_run):
    """A table of contents with dead links is worse than none."""
    body = client.get(f"/runs/{terminal_run}?tab=draft").text
    hrefs = set(re.findall(r'href="#(sec-[^"]+)"', body))
    ids = set(re.findall(r'id="(sec-[^"]+)"', body))
    if not hrefs:
        pytest.skip("no outline rendered for this run")
    assert hrefs <= ids, f"dead outline links: {sorted(hrefs - ids)}"


def test_outline_state_is_never_colour_alone(client, terminal_run: str):
    """Each outline entry carries a written label beside its dot."""
    body = client.get(f"/runs/{terminal_run}?tab=draft").text
    if "ti-outline__list" not in body:
        pytest.skip("no outline rendered")
    dots = len(re.findall(r"ti-outline__dot ", body))
    labels = len(re.findall(r"ti-outline__label", body))
    assert labels >= dots, "a state dot without a written label is colour-only"


def test_section_headings_outrank_body_text(client: TestClient):
    """The type scale must separate structure from prose.

    Section titles were 17px against 14px prose — a 3px step, so they read as
    bold paragraphs rather than as document structure.
    """
    css = client.get("/static/titanium.css").text
    block = re.search(r"\.ti-section-head__title\s*\{([^}]*)\}", css)
    assert block, "section title rule missing"
    size = re.search(r"font-size:\s*(\d+)px", block.group(1))
    assert size and int(size.group(1)) >= 19, (
        f"section titles at {size.group(1) if size else '?'}px do not separate "
        "from 14px prose"
    )


def test_anchored_sections_clear_the_sticky_header(client: TestClient):
    """Jumping from the outline must not land the heading under the header."""
    css = client.get("/static/titanium.css").text
    block = re.search(r"\.ti-section-head\s*\{([^}]*)\}", css)
    assert block and "scroll-margin-top" in block.group(1)


# --- empty states -----------------------------------------------------------


def test_the_first_run_state_explains_the_product_and_offers_a_first_step(
    tmp_path, monkeypatch
):
    """The one screen where explaining the product is right: there is nothing
    else to show, and a bare "No compounds yet" is a dead end."""
    replacement = runs_module.RunStore(root=tmp_path / "empty")
    monkeypatch.setattr(runs_module, "_STORE", replacement)
    monkeypatch.setattr(compounds_module, "_seed_compound", lambda: None)
    try:
        http = TestClient(app)
        body = http.get("/?scope=all").text
        assert "ti-onboard" in body
        assert body.count('class="ti-step"') == 3, "the three steps to a first draft"
        assert "/templates" in body, "no way to act on it"
    finally:
        replacement.shutdown(wait=True)


#: Searches, not facet filters. `gallery_view` deliberately widens an unknown
#: tag rather than emptying the list, so a stale bookmark still works — see its
#: docstring. A no-match search is the real empty state.
@pytest.mark.parametrize(
    "url",
    [
        "/runs?q=zzz-definitely-no-match",
        "/templates?q=zzz-definitely-no-match",
    ],
)
def test_no_empty_state_is_a_dead_end(client: TestClient, url: str):
    """Every empty state offers a link out. A user must never be stranded."""
    response = client.get(url)
    assert response.status_code == 200
    body = response.text
    if "ti-empty" not in body:
        pytest.skip(f"{url} was not empty in this fixture state")
    empty = body[body.index("ti-empty") :][:1400]
    assert "href=" in empty, f"{url} empty state offers no way forward"


# --- reachability of the evidence ------------------------------------------


def test_retrieved_tables_are_keyboard_reachable(client, terminal_run: str):
    """The overflow of these tables IS the evidence a citation points at.

    A retrieved table runs about four times the width of the column holding
    it, so an `overflow-x: auto` box with no tab stop hides roughly three
    quarters of the data from anyone not using a mouse.
    """
    body = client.get(f"/runs/{terminal_run}?tab=sources").text
    if "ti-retrieved__scroll" not in body:
        body = client.get(f"/runs/{terminal_run}?tab=draft").text
    if "ti-retrieved__scroll" not in body:
        pytest.skip("this run retrieved no tables")
    for scroller in re.findall(r"<div class=\"ti-retrieved__scroll\"[^>]*>", body):
        assert 'tabindex="0"' in scroller, f"not focusable: {scroller[:120]}"
        assert 'role="region"' in scroller
        assert "aria-label=" in scroller, "a focus stop with no name"


def test_the_scroller_focus_ring_is_inset(client: TestClient):
    """`.ti-retrieved` clips with overflow:hidden, so an outset ring on the
    scroller inside it would be sliced off on three sides."""
    css = client.get("/static/titanium.css").text
    block = re.search(r"\.ti-retrieved__scroll:focus-visible\s*\{([^}]*)\}", css)
    assert block, "no focus style for the scrollable region"
    assert "outline-offset: -" in block.group(1), block.group(1)


def test_the_scroll_cue_needs_no_javascript(client: TestClient):
    """`background-attachment: local` is what makes the shadow appear only
    while there is more to the right — a scroll listener would not survive
    scripting being off."""
    css = client.get("/static/titanium.css").text
    block = re.search(r"\.ti-retrieved__scroll\s*\{([^}]*)\}", css)
    assert block and "local" in block.group(1) and "scroll" in block.group(1)


# --- measure and layout ----------------------------------------------------


def test_prose_line_length_is_capped_in_measured_characters(client: TestClient):
    """`ch` is the width of the "0" glyph, not of an average letter.

    In this sans, `72ch` rendered 94 characters per line — past the 45-75 the
    eye tracks without losing its place. The cap is stated in ch because CSS
    has no character unit, but the number was chosen by measuring.
    """
    css = client.get("/static/titanium.css").text
    block = re.search(r"\.ti-para__body\s*\{([^}]*)\}", css)
    assert block
    cap = re.search(r"max-width:\s*(\d+)ch", block.group(1))
    assert cap, "report prose has no line-length cap"
    assert int(cap.group(1)) <= 60, (
        f"{cap.group(1)}ch renders well over 75 characters in this font"
    )


def test_the_page_container_is_centred(client: TestClient):
    """A max-width with no auto margin pinned four pages to the left edge and
    left the rest of a wide viewport empty."""
    css = client.get("/static/titanium.css").text
    block = re.search(r"\n\.ti-page\s*\{([^}]*)\}", css)
    assert block, ".ti-page rule missing"
    body = block.group(1)
    assert "max-width" in body
    assert "margin-inline: auto" in body or re.search(r"margin:[^;]*auto", body), (
        "max-width without an auto margin is a left-aligned page"
    )


def test_the_run_setup_action_stays_reachable(client: TestClient):
    """The button was 994px down a 900px viewport, pushed there by the
    preflight table you have to read in order to press it."""
    css = client.get("/static/titanium.css").text
    block = re.search(r"\.ti-actions\s*\{([^}]*)\}", css)
    assert block, "no action row rule"
    assert "position: sticky" in block.group(1)
    assert "background" in block.group(1), (
        "a sticky bar with no background lets rows scroll through it"
    )


def test_identifier_fields_are_not_the_width_of_the_page(client: TestClient):
    """Field width is a hint about the expected answer. An 826px box for a
    compound ID reads as an invitation to write prose."""
    css = client.get("/static/titanium.css").text
    block = re.search(r"\.ti-field__input\s*\{([^}]*)\}", css)
    assert block
    cap = re.search(r"max-width:\s*(\d+)ch", block.group(1))
    assert cap and int(cap.group(1)) <= 50, block.group(1)
    assert ".ti-field__input--wide" in css, (
        "paths and URLs need a documented way back to the full measure"
    )


def test_a_failed_search_on_the_home_page_keeps_the_list(client: TestClient):
    """The home page answers a miss with a note, not an empty state.

    Emptying the page would throw away the one thing that helps — the list of
    compounds that do exist. So the miss is stated and the list stays.
    """
    body = client.get("/?q=zzz-definitely-no-match&scope=all").text
    assert "ti-home2__miss" in body, "the miss is not stated"
    assert "Nothing matches" in body
    assert "ti-cmp__id" in body, (
        "the compound list was thrown away on a failed search"
    )
    assert "ti-empty" not in body, (
        "an empty state here would hide the only useful content on the page"
    )
