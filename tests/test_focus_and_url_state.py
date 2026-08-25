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


#: Rules allowed to suppress the focus ring because a NAMED partner shows focus
#: instead. The partner is stated so it can be checked, not just asserted.
SUPPRESSORS_WITH_PARTNERS = {
    # The input is borderless inside a pill, so its own outline would draw a
    # rectangle inside a 17px radius. The pill rings instead.
    ".ti-search__input": ".ti-search:focus-within",
}


def test_nothing_suppresses_a_focus_ring_without_replacing_it(css: str):
    """`outline: none` is allowed only where something else shows focus.

    The first version of this listed the one permitted selector, which made it a
    whitelist rather than a test: adding the anchor landing marker — which
    suppresses the ring precisely so it can draw a better one — failed it even
    though that rule replaces what it removes. What matters is the replacement,
    so that is what is checked: either a named partner rule, or a box-shadow in
    the same declaration.
    """
    offenders = []
    for match in re.finditer(r"([^{}]+)\{([^}]*outline:\s*(?:0|none)[^}]*)\}", css):
        selectors = match.group(1).strip()
        body = match.group(2)
        if "box-shadow" in body:
            continue                      # replaced in place
        partner = SUPPRESSORS_WITH_PARTNERS.get(selectors)
        if partner and partner in css:
            continue                      # replaced by a named partner rule
        offenders.append(selectors.replace(chr(10), " ")[:80])
    assert not offenders, (
        "focus rings suppressed with nothing shown instead:" + chr(10)
        + chr(10).join(offenders)
    )


def test_the_named_partners_actually_exist(css: str):
    """A partner that gets renamed turns the exemption above into a hole."""
    for suppressor, partner in SUPPRESSORS_WITH_PARTNERS.items():
        assert suppressor in css, f"{suppressor} no longer exists — drop the entry"
        assert partner in css, (
            f"{suppressor} is exempt because {partner} shows focus, and that "
            "rule is gone"
        )


def test_every_page_offers_a_skip_link(client: TestClient):
    """The first tab stop on a page with a full nav should be a way past it."""
    body = client.get("/runs").text
    assert 'class="ti-skip"' in body
    assert 'href="#ti-main"' in body
    assert 'id="ti-main"' in body, "the skip link points at nothing"


# --- URL state -------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "/runs"),
        ({"group": "template"}, "/runs?group=template"),
        ({"q": "tox"}, "/runs?q=tox"),
        ({"q": "tox", "group": "compound"}, "/runs?q=tox&group=compound"),
        (
            {"q": "tox", "group": "template", "sort": "duration", "state": "failed"},
            "/runs?q=tox&group=template&sort=duration&state=failed",
        ),
        # queries holding the characters that break naive concatenation
        ({"q": "a & b", "group": "template"}, "/runs?q=a+%26+b&group=template"),
        ({"q": "100%"}, "/runs?q=100%25"),
    ],
)
def test_the_url_builder_keeps_every_axis(kwargs: dict, expected: str):
    """Grouping used to be a boolean and the links were the literals "/runs"
    and "/runs?group=1", so switching it discarded the search. There are four
    controls now — text, grouping, sorting and the state facet — and a
    hand-built link would lose three at a time."""
    assert _runs_url(**kwargs) == expected


@pytest.mark.parametrize("axis", ["group", "sort"])
def test_changing_one_control_keeps_the_search(client: TestClient, axis: str):
    """The select carries the others through the form; this checks the round
    trip rather than the markup."""
    landed = client.get(f"/runs?q=nonclinical&{axis}=" + ("status" if axis == "group" else "oldest"))
    assert landed.status_code == 200
    value = re.search(r'id="ti-runs-q"[^>]*value="([^"]*)"', landed.text)
    assert value and value.group(1) == "nonclinical", (
        f"changing {axis} discarded the search"
    )


def test_a_state_chip_keeps_the_search_and_the_arrangement(client: TestClient):
    """Each chip is a link, so it has to rebuild the whole query itself.

    Asked for with a state already selected. The facet hides when every run
    shares one state — "All 49 / Completed 49" is two ways of saying the same
    number — so a request without one renders no chips and this would pass
    against an empty list.
    """
    body = client.get(
        "/runs?q=nonclinical&group=status&sort=oldest&state=completed"
    ).text
    hrefs = re.findall(r'class="ti-tag" href="([^"]*)"', body)
    assert hrefs, "no state chips rendered"
    for href in hrefs:
        assert "q=nonclinical" in href, f"{href} dropped the search"
        assert "group=status" in href, f"{href} dropped the grouping"
        assert "sort=oldest" in href, f"{href} dropped the sort"


def test_clear_drops_the_search_and_keeps_the_arrangement(client: TestClient):
    """Clearing a search means clearing the search, not resetting the view."""
    body = client.get("/runs?q=nonclinical&group=status&sort=oldest").text
    clear = re.search(r'href="([^"]*)"[^>]*>\s*clear\s*<', body)
    assert clear, "no clear link when a search is active"
    target = clear.group(1).replace("&amp;", "&")
    assert "q=" not in target
    assert "group=status" in target and "sort=oldest" in target


def test_the_search_form_carries_the_arrangement(client: TestClient):
    """Submitting the search must not reset grouping or sorting. They are real
    selects inside the same form, which is also what makes the whole toolbar
    work with scripting off."""
    body = client.get("/runs?q=nonclinical&group=status&sort=oldest").text
    form = re.search(r'<form[^>]*action="/runs"[^>]*>(.*?)</form>', body, re.S)
    assert form, "no /runs search form"
    inner = form.group(1)
    assert re.search(r'name="group"[^>]*>.*?value="status"[^>]*selected', inner, re.S), (
        "the form does not carry the current grouping"
    )
    assert re.search(r'name="sort"[^>]*>.*?value="oldest"[^>]*selected', inner, re.S), (
        "the form does not carry the current sort"
    )
def test_an_unknown_query_param_is_ignored_not_an_error(client: TestClient):
    """A stale bookmark must still open. /runs takes q and group; anything else
    widens rather than 500s."""
    for url in ("/runs?sort=oldest", "/runs?scope=mine", "/runs?group=maybe"):
        assert client.get(url).status_code == 200, url
