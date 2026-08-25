"""Every class a template names is defined in a stylesheet.

Why this exists
---------------
The connections form shipped using `ti-input`, `ti-field__hint`, `ti-facets`,
`ti-group__title` and four more names that no stylesheet defined. The design
system already had `.ti-field__input`, `.ti-field__msg` and `.ti-facet`; a
parallel set was invented instead of read for.

The page rendered as unstyled inline text — labels beside inputs, hints running
into the next field, a "not used by this mode" note overlapping its own label —
and every check made of it passed. Substring assertions on the HTML found
`name="dsn_env"` and `Google SSO` exactly as expected, because the markup was
right and only the styling was absent. Nothing that greps HTML can catch this.

So this is the guard: a class in a template with no rule anywhere is a failure,
and the failure names the file.

What it cannot catch
--------------------
A class that exists but is wrong for the job, and a rule that exists but does
not do what its name suggests. Those still need eyes on the rendered page. This
catches the specific, silent, entirely mechanical mistake of naming something
that is not there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "services" / "api_gateway"
TEMPLATE_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"

#: A sentinel standing in for a Jinja expression, so `ti-state--{{ x }}` yields
#: one token containing it rather than a bare `ti-state--` that no stylesheet
#: could ever define. Split on whitespace would otherwise invent a class.
_EXPR = "\x00"

#: Classes that come from outside these stylesheets or carry no styling.
_IGNORE = {
    "ti",          # the theme root, set on <body>
    "has-gaps",    # state hooks, styled via a compound selector
    "is-on",
    "is-current",
    "is-error",
}

#: `base.html` is a macro library, not a page. Nothing extends it and nothing
#: renders it — `template_editor.html` imports it `without context` for its
#: macros, and that is the only reference in the app. Its page-level markup
#: (topbar, shell, tables, error page) is dead code naming 41 classes from
#: `gsk.css`, which was deleted when the app moved to Titanium.
#:
#: Excluded rather than allowlisted, because listing 41 dead names would bury
#: the four that actually reach a rendered page. Deleting that markup is a
#: separate job with its own risk: the macros the editor depends on live in the
#: same file.
_EXCLUDE_FILES = {"base.html"}

#: Undefined classes that DO reach a rendered page, recorded so this test could
#: be introduced without first rewriting the editor's banners. The point of the
#: file is that this set cannot grow.
_KNOWN_UNDEFINED = {
    # Emitted by base.html's banner macro into the template editor.
    "rg-banner__body",
    "rg-banner__list",
    "rg-banner__title",
    "rg-editor__rail",
    # A wrapper on the header search form with no rule. Harmless — the header
    # lays out correctly without it — but it is a hook that styles nothing.
    "ti-search-form",
}


def _defined_classes() -> set[str]:
    found: set[str] = set()
    for sheet in sorted(STATIC_DIR.glob("*.css")):
        text = sheet.read_text(encoding="utf-8")
        # Comments stripped: a class named only in prose is not defined.
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        found |= set(re.findall(r"\.([A-Za-z_][\w-]*)", text))
    return found


def _used_classes() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for page in sorted(TEMPLATE_DIR.glob("*.html")):
        if page.name in _EXCLUDE_FILES:
            continue
        body = page.read_text(encoding="utf-8")
        body = re.sub(r"\{#.*?#\}", " ", body, flags=re.S)
        for match in re.finditer(r'class="([^"]*)"', body):
            raw = re.sub(r"\{\{.*?\}\}", _EXPR, match.group(1))
            raw = re.sub(r"\{%.*?%\}", _EXPR, raw)
            for token in raw.split():
                if _EXPR in token or token in _IGNORE:
                    continue
                used.setdefault(token, set()).add(page.name)
    return used


@pytest.fixture(scope="module")
def undefined() -> dict[str, set[str]]:
    defined = _defined_classes()
    return {
        name: pages
        for name, pages in _used_classes().items()
        if name not in defined
    }


def test_no_template_names_a_class_that_does_not_exist(undefined):
    """The whole point. A name with no rule renders as nothing and every
    substring check on the HTML still passes."""
    new = {n: sorted(p) for n, p in undefined.items() if n not in _KNOWN_UNDEFINED}
    assert not new, (
        "these classes are used but defined in no stylesheet:\n"
        + "\n".join(f"  {n:28} {', '.join(p)}" for n, p in sorted(new.items()))
    )


def test_the_recorded_debt_does_not_outlive_its_fix(undefined):
    """If one of the known-undefined classes gets a rule, it should leave this
    list. A permanent allowlist stops being a record of debt and becomes a
    place where new problems can hide."""
    stale = _KNOWN_UNDEFINED - set(undefined)
    assert not stale, (
        f"these are now defined and should be removed from _KNOWN_UNDEFINED: "
        f"{sorted(stale)}"
    )


def test_the_scan_finds_a_realistic_number_of_classes():
    """Guards the extraction itself. If the regex stops matching, every
    assertion above passes against an empty set — the failure mode this whole
    file exists to prevent, one level up."""
    used = _used_classes()
    assert len(used) > 200, f"only {len(used)} classes found; the scan is broken"
    assert "ti-lrow" in used, "a class known to be everywhere was not found"


def test_interpolated_classes_are_not_mistaken_for_names():
    """`class="ti-state--{{ x }}"` is one interpolated name, not a class called
    `ti-state--`. Treating it as the latter reports a failure that cannot be
    fixed, and an unfixable failure gets the test deleted."""
    used = _used_classes()
    for token in used:
        assert not token.endswith("--"), (
            f"{token!r} looks like a truncated interpolation rather than a class"
        )
