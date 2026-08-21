"""One measure, one token.

The line-length cap was fixed per-class, which is how it stayed broken. When
every prose class carries its own number, correcting one teaches you nothing
about the others: `.ti-para__body` was brought to 58ch while `.ti-footnote` sat
at 88ch (96 real characters), `.ti-page__lede` and `.ti-notice` at 72ch (85),
and `.ti-field__msg` and `.rg-caption` at no cap at all — 115 and 124
characters, on a compound page and a template editor nobody had measured.

`--ti-measure` replaces all of them. `ch` inside a custom property resolves
against the element that USES it, so a single value holds at 11px and 14px
alike — which is the property that makes one token possible at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "services" / "api_gateway" / "static"

#: Classes that carry running prose. Each must be capped, and capped by the
#: token rather than by a number of its own.
PROSE_CLASSES = (
    "ti-para__body",
    "ti-page__lede",
    "ti-notice",
    "ti-footnote",
    "ti-field__msg",
    "ti-enginebox",
    "ti-errpage",
    "ti-progress__foot",
    "ti-progress__why",
    "ti-step__body",
    "rg-caption",
    "rg-body",
    "rg-card__sub",
    "rg-issues__row",
)


def _sheets() -> str:
    return "\n".join(
        re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        for path in sorted(STATIC.glob("*.css"))
    )


@pytest.fixture(scope="module")
def css() -> str:
    return _sheets()


def test_the_token_exists_and_is_a_ch_value(css: str):
    found = re.search(r"--ti-measure:\s*(\d+)ch", css)
    assert found, "--ti-measure is not declared, or is not in ch"
    value = int(found.group(1))
    assert 50 <= value <= 62, (
        f"{value}ch — in this face `ch` is much wider than the average letter, "
        "so anything near 72 renders past 90 characters"
    )


def test_the_reasoning_is_written_down():
    """Without the note, 58 looks like an arbitrarily narrow column and the next
    person widens it back to 72."""
    source = (STATIC / "titanium.css").read_text(encoding="utf-8")
    at = source.index("--ti-measure")
    note = source[max(0, at - 1200) : at]
    assert "0" in note and "average" in note, (
        "the ch-versus-average-letter reasoning is not recorded next to the token"
    )


@pytest.mark.parametrize("cls", PROSE_CLASSES)
def test_every_prose_class_is_capped(css: str, cls: str):
    rules = [
        m.group(2)
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css)
        if any(sel.strip().endswith("." + cls) for sel in m.group(1).split(","))
    ]
    assert rules, f".{cls} has no rule at all"
    body = "\n".join(rules)
    assert "max-width" in body, f".{cls} carries prose with no line-length cap"


@pytest.mark.parametrize("cls", PROSE_CLASSES)
def test_no_prose_class_invents_its_own_number(css: str, cls: str):
    rules = [
        m.group(2)
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css)
        if any(sel.strip().endswith("." + cls) for sel in m.group(1).split(","))
    ]
    body = "\n".join(rules)
    literal = re.search(r"max-width:\s*(\d+)ch", body)
    assert not literal, (
        f".{cls} sets max-width: {literal.group(1)}ch instead of "
        "var(--ti-measure) — a per-class number is how this drifted"
    )


def test_the_token_is_actually_used(css: str):
    """A token nothing references is worse than a literal: it reads as covered."""
    uses = len(re.findall(r"max-width:\s*var\(--ti-measure\)", css))
    assert uses >= len(PROSE_CLASSES) - 2, (
        f"only {uses} rules use the measure token, for {len(PROSE_CLASSES)} "
        "prose classes"
    )


def test_wide_by_nature_content_still_opts_out(css: str):
    """Paths, URLs, SQL and stack traces are long by nature and must not be
    squeezed into a reading measure. They have documented escapes; this checks
    the escapes survived the sweep."""
    for escape in ("ti-field__input--wide", "rg-input--wide"):
        assert escape in css, f"{escape} was removed — long values have no way out"
