"""Truncation must be legible, and it must be recoverable.

Two bugs sat behind these tests, both invisible at the width the app was built
at and both found by measuring rather than looking:

  * `.ti-brow__name` / `.ti-brow__detail` carry the ellipsis pattern
    (overflow + text-overflow + nowrap), but two of the six usage sites are
    `<span>`. An inline box ignores overflow, text-overflow and max-width
    entirely, so those spans measured 845px inside a 641px parent and the
    panel clipped them mid-word — no ellipsis, reading as a rendering fault.
  * A truncated binding id with no `title` is simply gone. The detail lines
    always had one; the identifier — the more important token — did not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app

TEMPLATES = Path(__file__).resolve().parents[1] / "services" / "api_gateway" / "templates"

#: Classes that promise an ellipsis. Anything wearing one must be a block box
#: and must be capped to its container, or the promise is not kept.
ELLIPSIS_CLASSES = ("ti-brow__name", "ti-brow__detail")


@pytest.fixture(scope="module")
def css() -> str:
    return TestClient(app).get("/static/titanium.css").text


def _rule(css: str, selector: str) -> str:
    """Every declaration that reaches `selector`, from all rules naming it.

    These classes are deliberately styled by two rules — one shared truncation
    rule and one per-class typographic rule — so reading only the first match
    misses half the declarations.
    """
    # Comments must go first: this sheet documents its own reasoning in prose
    # that contains commas, so splitting an uncommented rule leaves the comment
    # terminator glued to the first selector and the rule is silently missed.
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    found = [
        match.group(2)
        for match in re.finditer(r"([^{}]+)\{([^}]*)\}", bare)
        if selector in [s.strip() for s in match.group(1).split(",")]
    ]
    assert found, f"no rule for {selector}"
    return chr(10).join(found)


@pytest.mark.parametrize("cls", ELLIPSIS_CLASSES)
def test_an_ellipsis_promise_needs_a_block_box(css: str, cls: str):
    """max-width, overflow and text-overflow are all no-ops on an inline box."""
    body = _rule(css, f".{cls}")
    assert "text-overflow: ellipsis" in body
    assert "display: block" in body, (
        f".{cls} promises an ellipsis but is not forced to a block box, so it "
        "silently does nothing on the <span> usages"
    )
    assert "max-width: 100%" in body, (
        f".{cls} is not capped to its container, so an ancestor clips it "
        "instead of the ellipsis firing"
    )


@pytest.mark.parametrize("cls", ELLIPSIS_CLASSES)
def test_everything_that_can_truncate_carries_its_full_text(cls: str):
    """A truncated identifier with no `title` is unrecoverable."""
    offenders: list[str] = []
    for path in sorted(TEMPLATES.glob("*.html")):
        source = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<(?:p|span|div)\s[^>]*" + cls + r"[^>]*>", source):
            if "title=" not in tag:
                offenders.append(f"{path.name}: {tag[:90]}")
    assert not offenders, "truncatable text with no recoverable full value:\n" + "\n".join(
        offenders
    )


def test_the_binding_row_stops_squeezing_the_identifier_when_narrow(css: str):
    """At 375px the four-column row gave the identifier 81px while two status
    columns kept 76 and 80. Stacking gives the name the whole line."""
    assert re.search(
        r"@media\s*\(max-width:\s*5\d\dpx\)\s*\{[^@]*\.ti-brow\s*\{[^}]*grid-template-columns",
        css,
        re.DOTALL,
    ), "no narrow-width rule for .ti-brow"


def test_the_truncation_classes_are_documented_as_needing_a_block_box():
    """A note for the next person, enforced.

    A sweep of every `text-overflow` rule in the sheet was tried first and was
    useless: 18 classes matched and 14 usages were `<span>`, but a span that is
    a grid or flex item is blockified, so all but one were already fine.
    Measuring computed display in a browser found exactly one real break —
    `.ti-brow__detail`, whose parent is a plain block div. Tag names cannot
    answer this, so what is pinned instead is the explanation, next to the fix.
    """
    css_path = (
        Path(__file__).resolve().parents[1]
        / "services" / "api_gateway" / "static" / "titanium.css"
    )
    source = css_path.read_text(encoding="utf-8")
    block = source[source.index(".ti-brow__name,") : source.index(".ti-brow__name,") + 900]
    assert "inline" in block, (
        "the reason display:block is load-bearing here is not written down, so "
        "the next person will delete it"
    )
