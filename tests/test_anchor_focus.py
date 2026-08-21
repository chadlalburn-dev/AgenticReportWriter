"""Following a citation must move focus, not just the viewport.

Clicking a citation marker is the interaction this whole screen exists for: it
is how a reader gets from a number in the prose to the row of data it came from.
The anchors resolved correctly — no dead links — but the targets were a `<div>`
and a `<details>`, neither of which can take focus. So the browser scrolled and
the reading cursor stayed behind: a screen reader user who followed a citation
got no confirmation they had arrived anywhere.

`tabindex="-1"` is the fix, and this codebase already knew it — `<main
id="ti-main" tabindex="-1">` carries it so the skip link works. This applies the
same pattern to the outline and citation anchors, which had been missed.

Note what `tabindex="-1"` does NOT do: it does not add a tab stop. The tabbable
count on the draft page is unchanged at 28.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import runs as runs_module
from services.api_gateway.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def draft(client: TestClient) -> str:
    """A rendered draft that actually has citations to follow."""
    store = runs_module.get_store()
    for summary in store.list_runs(limit=40):
        if not summary.terminal:
            continue
        body = client.get(f"/runs/{summary.run_id}?tab=draft").text
        if 'href="#cite-' in body:
            return body
    pytest.skip("no run with citation anchors")


def test_no_anchor_points_at_nothing(draft: str):
    """A citation marker that scrolls nowhere is worse than no marker."""
    for prefix in ("cite", "sec"):
        hrefs = set(re.findall(rf'href="#({prefix}-[^"]+)"', draft))
        ids = set(re.findall(rf'id="({prefix}-[^"]+)"', draft))
        assert hrefs, f"no {prefix} anchors rendered"
        assert hrefs <= ids, f"dead {prefix} links: {sorted(hrefs - ids)}"


def test_section_anchors_can_receive_focus(draft: str):
    """Otherwise an outline jump moves the scroll position and nothing else."""
    heads = re.findall(r'<div class="ti-section-head" id="sec-[^"]+"([^>]*)>', draft)
    assert heads, "no section anchors rendered"
    for attrs in heads:
        assert 'tabindex="-1"' in attrs, (
            "a section anchor cannot take focus, so following the outline "
            "announces nothing"
        )


def test_citation_anchors_can_receive_focus(draft: str):
    """A <details> is not focusable — only its <summary> is — so the anchor
    target needs its own tabindex."""
    rows = re.findall(r'<details id="cite-[^"]+"([^>]*)>', draft)
    assert rows, "no citation anchors rendered"
    for attrs in rows:
        assert 'tabindex="-1"' in attrs


def test_the_anchors_add_no_tab_stops(draft: str):
    """`-1` means programmatically focusable, NOT in the tab order. A positive
    value here would put every section and citation in the tab sequence."""
    for value in re.findall(r'id="(?:sec|cite)-[^"]*"[^>]*tabindex="([^"]*)"', draft):
        assert value == "-1", f"anchor target has tabindex={value}"
    assert 'tabindex="0"' not in draft or "ti-retrieved__scroll" in draft, (
        "an unexpected tab stop was added"
    )


def test_the_shell_already_used_this_pattern(client: TestClient):
    """The skip-link target is the precedent. If it ever loses its tabindex the
    reasoning above stops being true, so it is pinned here too."""
    body = client.get("/runs").text
    assert re.search(r'<main id="ti-main"[^>]*tabindex="-1"', body), (
        "the skip-link target is no longer focusable"
    )


def test_landing_on_an_anchor_is_visible_but_not_shouted(client: TestClient):
    """The app-wide ring is a 2px box, which around a whole section is far too
    loud. A left-edge marker says the same thing at the right scale, and in the
    same grammar as the provenance gutter two columns over."""
    css = client.get("/static/titanium.css").text
    rule = re.search(
        r"\.ti-section-head:focus-visible[^{]*\{([^}]*)\}", css, re.DOTALL
    )
    assert rule, "landing on a section shows nothing"
    body = rule.group(1)
    assert "outline: none" in body, "the full-box ring was not replaced"
    assert "box-shadow" in body and "--ti-accent" in body, (
        "the landing marker does not use the accent, so it is not the same "
        "signal as the rest of the app"
    )
