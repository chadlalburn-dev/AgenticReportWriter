"""Two defects a keyboard-and-URL walk of the app turned up.

**The search had no focus indicator.** `.ti-search__input` sets `outline: 0`,
which is right on its own terms — the input is borderless inside a pill, so its
outline would draw a rectangle inside a 17px radius. But nothing compensated:
there was no `:focus-within` anywhere in the sheet. So tabbing to the search,
the most-used control in the app and present on every page, produced nothing
visible at all.

**Switching the runs grouping threw away the search.** The two segment links
were the literals "/runs" and "/runs?group=1". The form already carried `group`
in a hidden input the other way, and that asymmetry is what let this survive:
one direction was handled, and nobody clicked the other.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app
from services.api_gateway.ui import _runs_url


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def css(client: TestClient) -> str:
    return re.sub(
        r"/\*.*?\*/", "", client.get("/static/titanium.css").text, flags=re.DOTALL
    )


def _rule(css: str, selector: str) -> str:
    found = [
        m.group(2)
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css)
        if selector in [s.strip() for s in m.group(1).split(",")]
    ]
    assert found, f"no rule for {selector}"
    return "\n".join(found)


# --- focus visibility ------------------------------------------------------


def test_the_search_pill_shows_focus(css: str):
    """The ring lives on the pill because the input cannot carry it."""
    for pill in (".ti-search:focus-within", ".ti-home__search:focus-within"):
        body = _rule(css, pill)
        assert "outline" in body, f"{pill} has no focus ring"
        assert "2px solid" in body, f"{pill} does not match the app-wide ring"


def test_the_pill_keeps_its_radius_when_focused(css: str):
    """The global `.ti :focus-visible` rule sets `border-radius: var(--ti-r-focus)`
    — 4px, which would square off a pill whose radius is half its height. The
    outline follows the element's own radius, so the fix is to not set one."""
    for pill in (".ti-search:focus-within", ".ti-home__search:focus-within"):
        assert "border-radius" not in _rule(css, pill), (
            f"{pill} overrides its own radius and stops looking like a pill"
        )


def test_nothing_suppresses_a_focus_ring_without_replacing_it(css: str):
    """`outline: 0` is allowed only where something else shows focus.

    Today that is exactly one rule — .ti-search__input, compensated by the
    :focus-within above. A second one appearing here without a partner is the
    bug this test exists to catch.
    """
    suppressors = [
        m.group(1).strip()
        for m in re.finditer(r"([^{}]+)\{[^}]*outline:\s*(?:0|none)", css)
    ]
    assert suppressors == [".ti-search__input"], (
        f"focus rings suppressed without a documented replacement: {suppressors}"
    )


def test_every_page_offers_a_skip_link(client: TestClient):
    """The first tab stop on a page with a full nav should be a way past it."""
    body = client.get("/runs").text
    assert 'class="ti-skip"' in body
    assert 'href="#ti-main"' in body
    assert 'id="ti-main"' in body, "the skip link points at nothing"


# --- URL state -------------------------------------------------------------


@pytest.mark.parametrize(
    ("q", "group", "expected"),
    [
        ("", False, "/runs"),
        ("", True, "/runs?group=1"),
        ("tox", False, "/runs?q=tox"),
        ("tox", True, "/runs?q=tox&group=1"),
        # a query holding the characters that break naive concatenation
        ("a & b", True, "/runs?q=a+%26+b&group=1"),
        ("100%", False, "/runs?q=100%25"),
    ],
)
def test_the_url_builder_encodes_and_keeps_both_axes(q: str, group: bool, expected: str):
    assert _runs_url(q=q, group=group) == expected


@pytest.mark.parametrize("label", ["by compound", "newest"])
def test_switching_the_grouping_keeps_the_search(client: TestClient, label: str):
    """The round trip a user actually makes: search, then change the grouping."""
    start = "/runs?q=nonclinical" + ("&group=1" if label == "newest" else "")
    body = client.get(start).text
    links = dict(
        (text.strip(), href)
        for href, text in re.findall(
            r'class="ti-seg__btn" href="([^"]*)"[^>]*>([^<]*)<', body
        )
    )
    target = next((h for t, h in links.items() if label in t), None)
    assert target, f"no {label!r} link on {start}: {list(links)}"
    landed = client.get(target.replace("&amp;", "&"))
    assert landed.status_code == 200
    assert 'id="ti-runs-q"' in landed.text
    value = re.search(r'id="ti-runs-q"[^>]*value="([^"]*)"', landed.text)
    assert value and value.group(1) == "nonclinical", (
        f"clicking {label!r} discarded the search"
    )


def test_clear_drops_the_search_and_keeps_the_grouping(client: TestClient):
    """Clearing a search means clearing the search, not resetting the view."""
    body = client.get("/runs?q=nonclinical&group=1").text
    clear = re.search(r'class="ti-seg__btn" href="([^"]*)"[^>]*>\s*clear\s*<', body)
    assert clear, "no clear link when a search is active"
    assert clear.group(1) == "/runs?group=1"


def test_the_search_form_still_carries_the_grouping(client: TestClient):
    """The direction that already worked. Kept under test so fixing the links
    does not quietly break the form."""
    body = client.get("/runs?group=1").text
    # `ti-toolbar`, not `role="search"`: the header carries a global search form
    # with the same role on every page, and grabbing that one instead is the
    # same trap that once broke six form-scraping tests at once.
    form = body[body.index('class="ti-toolbar"') :][:600]
    assert 'type="hidden"' in form and 'name="group"' in form


def test_an_unknown_query_param_is_ignored_not_an_error(client: TestClient):
    """A stale bookmark must still open. /runs takes q and group; anything else
    widens rather than 500s."""
    for url in ("/runs?sort=oldest", "/runs?scope=mine", "/runs?group=maybe"):
        assert client.get(url).status_code == 200, url
