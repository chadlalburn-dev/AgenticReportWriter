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
    """The light tokens, from the `:root` block only.

    Scoped rather than regexed over the whole file. It used to read everything,
    which was harmless while `:root` was the only place tokens were defined —
    and the moment a dark block was added, the dict comprehension kept the LAST
    match and every light assertion below started measuring dark colours. They
    failed loudly, which was lucky; a palette close enough to pass would have
    left this suite quietly testing the wrong theme.
    """
    source = CSS.read_text(encoding="utf-8")
    start = source.index(":root {")
    end = source.index(chr(10) + chr(125), start)
    return {
        m.group(1): m.group(2)
        for m in re.finditer(
            r"(--ti-[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", source[start:end]
        )
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


# --- the dark theme, held to the same arithmetic ---------------------------
#
# The light tokens document a ratio beside almost every colour and the tests
# above prove each claim. A dark theme picked by eye would be the one part of
# this stylesheet where the numbers were guessed, so it gets the same treatment.


DARK_SURFACES = {"field": "#15181B", "panel": "#1F2429"}


@pytest.fixture(scope="module")
def dark_tokens() -> dict[str, str]:
    """Tokens from the `[data-theme='dark']` block only.

    Parsed from that block rather than the whole file, or the light values
    above would win and this suite would silently re-test the light theme —
    a test that passes while measuring the wrong thing.
    """
    source = CSS.read_text(encoding="utf-8")
    start = source.index("[data-theme='dark'] {")
    end = source.index("}", start)
    block = source[start:end]
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r"(--ti-[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", block)
    }


def test_the_dark_block_actually_defines_the_tokens(dark_tokens: dict[str, str]):
    """Guards the fixture. If the block moves or is renamed, everything below
    would pass against an empty dict."""
    for token in TEXT_TOKENS + INDICATOR_TOKENS + ("--ti-field", "--ti-panel"):
        assert token in dark_tokens, f"{token} is not defined in the dark block"


def test_the_dark_surfaces_are_what_this_file_assumes(dark_tokens: dict[str, str]):
    assert dark_tokens["--ti-field"].upper() == DARK_SURFACES["field"]
    assert dark_tokens["--ti-panel"].upper() == DARK_SURFACES["panel"]


@pytest.mark.parametrize("token", TEXT_TOKENS)
@pytest.mark.parametrize("surface", sorted(DARK_SURFACES))
def test_dark_text_tokens_clear_45_on_both_surfaces(
    dark_tokens: dict[str, str], token: str, surface: str
):
    got = _ratio(dark_tokens[token], DARK_SURFACES[surface])
    assert got >= 4.5, f"{token} on the dark {surface} is {got:.2f}:1"


@pytest.mark.parametrize("token", INDICATOR_TOKENS)
@pytest.mark.parametrize("surface", sorted(DARK_SURFACES))
def test_dark_indicator_tokens_clear_3_on_both_surfaces(
    dark_tokens: dict[str, str], token: str, surface: str
):
    got = _ratio(dark_tokens[token], DARK_SURFACES[surface])
    assert got >= 3.0, f"{token} on the dark {surface} is {got:.2f}:1"


def test_the_dark_text_accent_is_lighter_than_the_indicator_accent(
    dark_tokens: dict[str, str],
):
    """`--ti-accent-dark` names a role, not a lightness. In the light theme the
    text accent is darker than the indicator; on a dark surface it has to be
    lighter, and inverting the hue steps mechanically would have produced an
    illegible one. Same for the greens."""
    panel = DARK_SURFACES["panel"]
    assert _ratio(dark_tokens["--ti-accent-dark"], panel) > _ratio(
        dark_tokens["--ti-accent"], panel
    )
    assert _ratio(dark_tokens["--ti-ok-dark"], panel) > _ratio(
        dark_tokens["--ti-ok"], panel
    )


def test_the_dark_greys_are_legal_on_both_surfaces(dark_tokens: dict[str, str]):
    """A deliberate difference from light, recorded rather than assumed.

    There, `--ti-dim` is panel-only — 4.29 on the field, which fails, and six
    classes using it for text on the field were a real bug. Here it clears 4.5
    on both. That is a property of this palette, not a rule to reproduce, so
    this asserts the fact instead of importing the light theme's constraint.
    """
    for surface in DARK_SURFACES.values():
        got = _ratio(dark_tokens["--ti-dim"], surface)
        assert got >= 4.5, f"--ti-dim on dark {surface} is {got:.2f}:1"
