"""No page may stretch to the viewport.

Hit twice in one pass, in two different rules:

  * `.ti-page` had `max-width: 1200px` and no auto margin, so four pages sat
    against the left edge of a wide window with the remainder empty.
  * `.ti-main` — the compound page's two-column body — had neither, so it ran
    1365px at a 1400px viewport and grew without limit beyond that, putting its
    reading column past any usable measure on a wide monitor.

Both are one missing declaration and neither is visible at the width a page is
built at, which is exactly why they are pinned here rather than trusted to a
review.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app

STATIC = Path(__file__).resolve().parents[1] / "services" / "api_gateway" / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "services" / "api_gateway" / "templates"


def _stylesheets() -> str:
    client = TestClient(app)
    return "\n".join(
        client.get(f"/static/{sheet.name}").text for sheet in sorted(STATIC.glob("*.css"))
    )


def _without_print(css: str) -> str:
    """Drop every `@media print` block, brace-matched.

    A naive regex cannot do this: the block contains nested rules, so a
    character class stopping at the first closing brace ends the match inside
    the block rather than after it.
    """
    out, i = [], 0
    while True:
        at = css.find("@media print", i)
        if at == -1:
            out.append(css[i:])
            return "".join(out)
        out.append(css[i:at])
        depth, j = 0, css.index("{", at)
        while j < len(css):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1


def _page_containers() -> set[str]:
    """The outermost element of every Titanium page's content block.

    Discovered from the templates rather than hardcoded, so a new page is
    covered the moment it exists.
    """
    found: set[str] = set()
    for path in sorted(TEMPLATES.glob("*.html")):
        source = path.read_text(encoding="utf-8")
        if "titanium_base.html" not in source:
            continue
        block = re.search(
            r"\{%\s*block content\s*%\}\s*(?:\{#.*?#\})?\s*<(\w+)[^>]*class=\"([^\"]+)\"",
            source,
            re.DOTALL,
        )
        if block:
            found.add(block.group(2).split()[0])
    return found


@pytest.fixture(scope="module")
def css() -> str:
    return _stylesheets()


def test_containers_were_actually_discovered():
    """A silent empty set would make every assertion below vacuous."""
    containers = _page_containers()
    assert len(containers) >= 3, f"only found {containers}"


@pytest.mark.parametrize("container", sorted(_page_containers()))
def test_every_page_container_is_bounded_and_centred(css: str, container: str):
    rules = [
        match.group(2)
        for match in re.finditer(r"([^{}]+)\{([^}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
        if any(sel.strip() in (f".{container}", f".ti .{container}")
               for sel in match.group(1).split(","))
    ]
    assert rules, f".{container} is used as a page container but has no rule"
    declarations = "\n".join(rules)
    assert "max-width" in declarations, (
        f".{container} has no max-width, so it stretches with the viewport"
    )
    assert "margin-inline: auto" in declarations or re.search(
        r"margin:[^;]*\bauto\b", declarations
    ), f".{container} has a max-width but no auto margin, so it is left-aligned"


def test_the_compound_page_matches_the_other_two_column_page(css: str):
    """1240px in two places, not two invented widths."""
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    widths = {}
    for name in ("ti-main", "ti-home2"):
        rule = re.search(r"\." + name + r"\s*\{([^}]*)\}", bare)
        assert rule, f".{name} rule missing"
        found = re.search(r"max-width:\s*(\d+)px", rule.group(1))
        assert found, f".{name} has no max-width"
        widths[name] = int(found.group(1))
    assert widths["ti-main"] == widths["ti-home2"], (
        f"the app's two two-column pages disagree on width: {widths}"
    )


#: Rules where a single-digit font-size sizes an ICON, not text. Icons here are
#: authored on a 16-unit grid and scale with `1em`, so font-size is the only
#: handle on them, and a 9px glyph beside 10px uppercase text is correct.
#: Listed explicitly so a new one has to be justified rather than assumed.
#: Empty, and that is the point. The one entry it started with,
#: `.ti-state__glyph`, turned out to be dead CSS — a leftover from before the
#: status icons became authored SVG — so it was deleted rather than exempted.
#: A new entry here has to argue for itself.
GLYPH_SIZING_RULES: set[str] = set()


def test_no_text_falls_below_the_scale(css: str):
    """10px is the smallest step in this system's type scale.

    The user badge was set at 9px, two capitals in a 22px circle, smaller than
    anything else in the app for no reason it could name.
    """
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    # The floor is a SCREEN floor. Print legitimately goes smaller — a 9px table
    # cell on paper is normal typography and is how a 3010px evidence table fits
    # a sheet at all — so the print block is excluded rather than exempted
    # rule by rule.
    bare = _without_print(bare)
    offenders = [
        f"{match.group(1).strip()[:60]} @ {match.group(2)}px"
        for match in re.finditer(r"([^{}]+)\{[^}]*font-size:\s*(\d)px", bare)
        if match.group(1).strip() not in GLYPH_SIZING_RULES
    ]
    assert not offenders, "screen text below the 10px floor:" + chr(10) + chr(10).join(offenders)


def test_the_glyph_exemptions_really_are_glyphs():
    """An exemption that stops being about icons is how the floor erodes."""
    corpus = chr(10).join(
        path.read_text(encoding="utf-8") for path in TEMPLATES.glob("*.html")
    )
    for rule in GLYPH_SIZING_RULES:
        assert rule.lstrip(".") in corpus, (
            f"{rule} is exempt from the type floor but is not used anywhere - "
            "drop the exemption rather than carrying it"
        )


# --- long lists ------------------------------------------------------------


def test_the_row_placeholder_matches_the_row(css: str):
    """`contain-intrinsic-size` is the height content-visibility hands a row it
    has not laid out yet. If it disagrees with the real row, it causes exactly
    the scrollbar jump it exists to prevent.

    It was a hard-coded 44px against rows measuring 61px compact and 69px
    comfortable — 28-40% short, and this route returns up to 200 rows, so four
    to six thousand pixels of drift as they realise.

    The measurement trap, recorded because it cost a wrong fix first: reading
    getBoundingClientRect on an off-screen content-visibility row returns the
    PLACEHOLDER, not the row. Measuring the normal way just reads this value
    back to you. The real heights come from re-measuring with containment
    switched off.
    """
    bare = _without_print(re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
    # ALL the rules named .ti-lrow, not the first. There are two — the layout
    # one near the top of the sheet and the virtualisation one at the bottom —
    # and taking the first match is the same trap that broke the .ti-outline
    # lookup when print styles landed.
    body = chr(10).join(
        m.group(1) for m in re.finditer(r"\.ti-lrow\s*\{([^}]*)\}", bare)
    )
    assert body, ".ti-lrow has no rule"
    assert "content-visibility: auto" in body, (
        "`hidden` would take these rows out of find-in-page; `auto` keeps them"
    )
    assert "contain-intrinsic-size" in body
    assert "var(--ti-lrow-h)" in body, (
        "the placeholder is a literal again; it has to follow the density that "
        "determines the row height"
    )


@pytest.mark.parametrize(
    ("density", "expected"), [("compact", 61), ("comfortable", 69)]
)
def test_each_density_declares_its_measured_row_height(
    css: str, density: str, expected: int
):
    """Both densities need their own value — the padding differs, so the row
    does too, and one number cannot serve both."""
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    if density == "compact":
        block = bare[: bare.index("[data-density='comfortable']")]
    else:
        block = bare[bare.index("[data-density='comfortable']") :][:400]
    found = re.search(r"--ti-lrow-h:\s*(\d+)px", block)
    assert found, f"{density} declares no --ti-lrow-h"
    assert int(found.group(1)) == expected, (
        f"{density} declares {found.group(1)}px; the row measures {expected}px"
    )


def test_the_draft_is_deliberately_not_virtualised(css: str):
    """Find-in-page across a whole report matters more than the paint saving,
    and a regulatory reader searching for a number is the point of the page."""
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for selector in (".ti-para", ".ti-draft", ".ti-section-head"):
        rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", bare)
        if rule:
            assert "content-visibility" not in rule.group(1), (
                f"{selector} is virtualised, which can hide draft text from "
                "find-in-page"
            )
