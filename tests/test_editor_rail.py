"""The template editor's Save button must stay reachable on a 9,149px form.

`position: sticky` was already on `.rg-rail-sticky` — the div inside the rail —
and it never stuck once. A sticky box can only travel inside its containing
block, and that div's containing block is `.rg-cols__rail`, which
`align-items: start` sizes to its content. 424px of rail inside a 424px parent
is zero travel, so the declaration was inert.

The cost was not cosmetic: Save scrolled off after the first screen and stayed
gone for the next seven, and it is the ONLY save control on the page — the foot
cluster holds "Check without saving" and "Cancel". Someone editing a template
at the bottom of the form had no way to commit it without scrolling back up.

The fix is to move sticky one level up, onto the grid item, whose containing
block is its grid area — full row height regardless of align-items. That is the
pattern `.ti-outline` already uses on the draft page, where it demonstrably
works, so this makes the two rails agree rather than inventing anything.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app

STATIC = Path(__file__).resolve().parents[1] / "services" / "api_gateway" / "static"
BRIDGE = STATIC / "editor-titanium.css"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def bridge() -> str:
    return re.sub(r"/\*.*?\*/", "", BRIDGE.read_text(encoding="utf-8"), flags=re.DOTALL)


def _rule(css: str, selector: str) -> str:
    found = [
        m.group(2)
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css)
        if selector in [s.strip() for s in m.group(1).split(",")]
    ]
    assert found, f"no rule for {selector}"
    return "\n".join(found)


def test_the_sticky_sits_on_the_grid_item(bridge: str):
    """Not on its child, which has nowhere to travel."""
    rail = _rule(bridge, ".ti .rg-cols__rail")
    assert "position: sticky" in rail, (
        "the rail is not sticky, so Save leaves the screen on a 9000px form"
    )
    assert "top:" in rail, "a sticky box with no offset never pins"


def test_the_inner_wrapper_is_not_also_sticky(bridge: str):
    """Two nested stickies is how the inert one hid for so long: the inner
    declaration looked like the feature working."""
    inner = _rule(bridge, ".ti .rg-rail-sticky")
    assert "position: static" in inner


def test_the_rail_matches_the_draft_pages_offset():
    """Both rails clear the same 64px header. Two different offsets would be two
    different apps."""
    titanium = re.sub(
        r"/\*.*?\*/", "", (STATIC / "titanium.css").read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    outline = re.search(r"\.ti-outline\s*\{([^}]*)\}", titanium)
    assert outline, ".ti-outline rule missing"
    draft_top = re.search(r"top:\s*(\d+)px", outline.group(1))
    bridge = re.sub(
        r"/\*.*?\*/", "", BRIDGE.read_text(encoding="utf-8"), flags=re.DOTALL
    )
    editor_top = re.search(r"top:\s*(\d+)px", _rule(bridge, ".ti .rg-cols__rail"))
    assert draft_top and editor_top
    assert draft_top.group(1) == editor_top.group(1), (
        f"draft rail pins at {draft_top.group(1)}px, editor rail at "
        f"{editor_top.group(1)}px"
    )


def test_stacked_narrow_the_rail_comes_first(bridge: str):
    """In DOM order the rail follows the form, so at narrow widths leaving it
    alone put the only Save control at the foot of a 9,593px page. Reordered
    above, which is what .ti-outline does at the same breakpoint.

    Honest limit: this makes Save one screen away instead of nine. It is not
    reachable from the middle of the form on a narrow window — the editor is a
    desktop surface and a sticky action bar there would need new markup.
    """
    narrow = bridge[bridge.index("@media (max-width: 980px)") :][:400]
    assert "grid-row: 1" in narrow, "the rail is not moved above the form"
    assert ".ti .rg-cols__main" in narrow and "grid-row: 2" in narrow


def test_save_is_still_the_only_primary_on_the_page(client: TestClient):
    """The accent budget: one orange element per view. If a second Save ever
    appears in the foot cluster it must not be a second primary."""
    body = client.get("/templates/target_assessment/edit").text
    primaries = re.findall(r'class="[^"]*rg-btn--primary[^"]*"', body)
    assert len(primaries) <= 1, f"{len(primaries)} primary buttons on one view"


def test_the_editor_page_has_exactly_one_save(client: TestClient):
    """Pins the premise of this whole file. If a second Save is added at the
    foot, the sticky rail stops being load-bearing and this test should be the
    thing that says so.
    """
    body = client.get("/templates/target_assessment/edit").text
    saves = re.findall(r">\s*Save template\s*<", body)
    assert len(saves) == 1, (
        f"{len(saves)} Save controls — revisit whether the rail still needs to "
        "be sticky"
    )
