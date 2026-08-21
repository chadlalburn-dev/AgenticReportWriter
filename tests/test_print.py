"""A draft is a document, and documents get printed.

The print block hid the chrome and flattened the panels, which is the easy half.
What it did not do was rescue the evidence: a retrieved table lives in an
`overflow-x: auto` scroller on screen — one of them is 3010px wide inside a
712px column — and paper has no scrollbar. Printing clipped it to the first
quarter and said nothing about the rest.

That is the same defect as the keyboard-unreachable scroller, on a different
output. On screen the fix was to make the box focusable; on paper the box has to
stop being a box. A reflowed table is harder to read than a scrolled one and
infinitely better than a silently truncated one, in a document whose entire
claim is that every number traces to its source.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (
    Path(__file__).resolve().parents[1]
    / "services" / "api_gateway" / "static" / "titanium.css"
)


@pytest.fixture(scope="module")
def print_block() -> str:
    source = CSS.read_text(encoding="utf-8")
    start = source.index("@media print {")
    depth, i = 0, start
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError("unterminated @media print block")


def test_a_print_block_exists(print_block: str):
    assert len(print_block) > 200, "the print block is a stub"


def test_evidence_tables_are_not_clipped_on_paper(print_block: str):
    """The defect this file is about."""
    rule = re.search(r"\.ti-retrieved__scroll\s*\{([^}]*)\}", print_block)
    assert rule, "the table scroller has no print rule, so paper cuts it off"
    assert "overflow: visible" in rule.group(1)


def test_table_cells_may_wrap_on_paper(print_block: str):
    """On screen the cells are `white-space: nowrap`, which is what makes the
    table wide. Keeping that in print keeps the clipping."""
    rule = re.search(r"\.ti-table th,\s*\.ti-table td\s*\{([^}]*)\}", print_block)
    assert rule, "table cells keep their screen nowrap when printed"
    assert "white-space: normal" in rule.group(1)


def test_the_ai_notice_survives_printing(print_block: str):
    """It is part of the deliverable, and a printed draft that has lost it is a
    document with no warning on it."""
    rule = re.search(r"\.ti-notice\s*\{([^}]*)\}", print_block)
    assert rule, "the notice has no print rule"
    assert "display: block !important" in rule.group(1)


def test_the_notice_is_not_hidden_by_the_chrome_sweep(print_block: str):
    """A wildcard hide would take the notice with it."""
    hides = re.findall(r"([^{}]*)\{[^}]*display:\s*none", print_block)
    for selectors in hides:
        assert "ti-notice" not in selectors, f"the notice is hidden by: {selectors}"


def test_the_provenance_marks_are_forced_to_print(print_block: str):
    """Browsers strip backgrounds by default. The gutter is a border today, so
    it survives, but stating it means a future background treatment cannot
    silently vanish on paper."""
    assert "print-color-adjust: exact" in print_block
    block = re.search(r"([^{}]*)\{[^}]*print-color-adjust", print_block)
    for needed in ("ti-para__body", "ti-cite"):
        assert needed in block.group(1), f"{needed} is not forced to print"


def test_headings_do_not_split_from_their_sections(print_block: str):
    assert re.search(r"\.ti-section-head\s*\{[^}]*break-after:\s*avoid", print_block)
    inside = re.search(r"([^{}]*)\{[^}]*break-inside:\s*avoid", print_block)
    assert inside, "nothing is protected from splitting across sheets"
    for needed in ("ti-retrieved", "ti-table tr"):
        assert needed in inside.group(1)


def test_screen_only_navigation_is_dropped(print_block: str):
    """The outline is a way to move around a scrolling window; on paper the
    document's own headings do that."""
    assert re.search(r"\.ti-outline\s*\{[^}]*display:\s*none", print_block)
    assert re.search(r"\.ti-docgrid\s*\{[^}]*display:\s*block", print_block), (
        "the two-column grid is kept, so the prose column stays narrow on paper"
    )


def test_no_dead_class_survives_in_a_media_query():
    """The earlier dead-CSS sweep only removed top-level rules whose whole
    selector list was dead, so `.ti-crow:hover` and the copies nested in media
    queries outlived it. Anything referencing them now is a leftover."""
    source = CSS.read_text(encoding="utf-8")
    assert "ti-crow" not in source, (
        "the compound-row class replaced by .ti-cmp is still referenced"
    )
