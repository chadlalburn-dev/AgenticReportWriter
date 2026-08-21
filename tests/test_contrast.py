"""The contrast the tokens claim, computed rather than trusted.

`titanium.css` documents a ratio next to several colours — "6.65 panel / 5.57
field", "lightest legal 5.12:1 on panel", "7.31 panel / 6.13 field — safe
everywhere". Every one of those claims measured true. What had drifted was
USAGE: two tokens were being set on a surface their comment does not cover.

  * `--ti-ok` is documented "connected status dot". A dot is a non-text
    indicator and needs 3:1 (WCAG 1.4.11), which it clears. `.ti-state--ok`
    then set 10px TEXT in it, needing 4.5:1, and got 3.80 on the page
    background.
  * `--ti-dim` is "the lightest grey that is legal on a panel" — true, and
    4.29 on the field, which is not. Six classes used it for text sitting on
    the field: the draft outline's head and labels, section meta, the editor's
    captions, its `.rg-subtle` utility and its breadcrumb.

A browser sweep after the fix found zero failures across 15 pages and 2,026
text nodes. This file is the part of that which can run without a browser: the
arithmetic on the tokens themselves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (
    Path(__file__).resolve().parents[1]
    / "services" / "api_gateway" / "static" / "titanium.css"
)

#: The two surfaces text can land on in this app.
SURFACES = {"field": "#D9DDE0", "panel": "#EDF0F1"}

#: Tokens used to set TEXT. Each must clear 4.5:1 on BOTH surfaces, because a
#: utility class cannot know which one it will land on. --ti-dim is absent by
#: design: it is panel-only and the classes that used it on the field were the
#: bug this file records.
TEXT_TOKENS = ("--ti-ink", "--ti-sec", "--ti-mut", "--ti-accent-dark", "--ti-ok-dark")

#: Tokens used for non-text indicators, which need 3:1 rather than 4.5:1.
INDICATOR_TOKENS = ("--ti-ok", "--ti-accent")


def _hex(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(raw: int) -> float:
        c = raw / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(fg: str, bg: str) -> float:
    a, b = _luminance(_hex(fg)), _luminance(_hex(bg))
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


@pytest.fixture(scope="module")
def tokens() -> dict[str, str]:
    source = CSS.read_text(encoding="utf-8")
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r"(--ti-[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", source)
    }


def test_the_surfaces_are_still_what_this_file_assumes(tokens: dict[str, str]):
    """If the page or panel colour changes, every number below moves."""
    assert tokens["--ti-field"].upper() == SURFACES["field"]
    assert tokens["--ti-panel"].upper() == SURFACES["panel"]


@pytest.mark.parametrize("token", TEXT_TOKENS)
@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_text_tokens_clear_45_on_both_surfaces(
    tokens: dict[str, str], token: str, surface: str
):
    """A colour used for text has to be legal wherever the text lands.

    This is the assertion that was missing: --ti-dim passes on the panel and
    fails on the field, and nothing stopped six classes using it on the field.
    """
    assert token in tokens, f"{token} is not declared"
    ratio = _ratio(tokens[token], SURFACES[surface])
    assert ratio >= 4.5, f"{token} on the {surface} is {ratio:.2f}:1, under 4.5"


@pytest.mark.parametrize("token", INDICATOR_TOKENS)
@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_indicator_tokens_clear_3_on_both_surfaces(
    tokens: dict[str, str], token: str, surface: str
):
    """Non-text UI components need 3:1 (WCAG 1.4.11), not 4.5."""
    ratio = _ratio(tokens[token], SURFACES[surface])
    assert ratio >= 3.0, f"{token} on the {surface} is {ratio:.2f}:1, under 3.0"


def test_the_panel_only_grey_is_honestly_labelled(tokens: dict[str, str]):
    """--ti-dim earns its keep as the lightest legal grey on a panel. The point
    is that its comment must keep saying so, because that is the only thing
    stopping it being used on the field again."""
    assert _ratio(tokens["--ti-dim"], SURFACES["panel"]) >= 4.5
    assert _ratio(tokens["--ti-dim"], SURFACES["field"]) < 4.5, (
        "--ti-dim now passes on the field too; if that is deliberate, fold it "
        "into TEXT_TOKENS and delete this test"
    )
    source = CSS.read_text(encoding="utf-8")
    note = source[source.index("--ti-dim:") : source.index("--ti-dim:") + 500]
    assert "PANEL" in note or "panel" in note


def test_the_status_text_green_is_not_the_dot_green():
    """They are different jobs with different thresholds, and collapsing them is
    what put 10px text at 3.80:1."""
    source = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.ti-state--ok\s*\{([^}]*)\}", source)
    assert rule, ".ti-state--ok has no rule"
    assert "--ti-ok-dark" in rule.group(1), (
        "status text uses the dot green, which is only rated for 3:1"
    )


def test_the_dot_may_keep_the_lighter_green():
    """The dot is aria-hidden and paired with words, so 3:1 is the bar it has to
    meet — this records that the lighter green there is a decision, not an
    oversight."""
    source = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.ti-dot--on\s*\{([^}]*)\}", source)
    assert rule and "var(--ti-ok)" in rule.group(1)


@pytest.mark.parametrize("token", TEXT_TOKENS + INDICATOR_TOKENS)
def test_every_ratio_a_comment_claims_is_true(tokens: dict[str, str], token: str):
    """The comments carry numbers. Numbers in comments rot silently."""
    source = CSS.read_text(encoding="utf-8")
    at = source.index(token + ":")
    note = source[at : at + 420]
    note = note[: note.index("--ti-", 5)] if "--ti-" in note[5:] else note
    for value, surface in re.findall(r"([0-9]\.[0-9]{1,2})[:\s]*[01]?\s*(panel|field)", note):
        actual = _ratio(tokens[token], SURFACES[surface])
        assert abs(actual - float(value)) < 0.06, (
            f"{token} claims {value} on the {surface} but measures {actual:.2f}"
        )
