"""The density toggle: a labelled group that says which option is on.

Two problems, both quiet:

  * It was two loose buttons — no `role="group"`, no label — so a screen reader
    announced "compact button, comfortable button" with no indication of what
    they control. A sighted reader gets that context from the footer position;
    an AT user got nothing.
  * It reported state with `aria-current`. That attribute means "the current
    item in a set" and belongs on the navigation links which share this same
    `.ti-seg__btn` class — which is exactly where the confusion came from. For a
    button that turns a setting on, `aria-pressed` is the attribute, and
    `aria-current="false"` is discouraged outright: the spec says omit it rather
    than assert it.

The trap in fixing it: the active-pill styling hung off `[aria-current='true']`
alone, so correcting the attribute would have silently dropped the visual state.
The CSS now covers both, because the class genuinely serves both kinds of
control.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app

ROOT = Path(__file__).resolve().parents[1] / "services" / "api_gateway"
SHELL = ROOT / "templates" / "titanium_base.html"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def shell() -> str:
    return SHELL.read_text(encoding="utf-8")


def test_the_toggle_is_a_labelled_group(client: TestClient):
    body = client.get("/runs").text
    group = re.search(r'<span class="ti-density"[^>]*>', body)
    assert group, "no density group rendered"
    assert 'role="group"' in group.group(0)
    assert "aria-label=" in group.group(0), (
        "an unlabelled group announces two buttons with no context"
    )


def test_the_server_ships_a_pressed_state(client: TestClient):
    """It has to be right before JS boots, or the first paint disagrees with
    what an AT reads."""
    body = client.get("/runs").text
    block = body[body.index('class="ti-density"') :][:500]
    pressed = re.findall(r'aria-pressed="(true|false)"', block)
    assert pressed == ["true", "false"], (
        f"expected compact pressed and comfortable not, got {pressed}"
    )


def test_it_uses_pressed_not_current(shell: str):
    """aria-current is for the nav links that share this class."""
    block = shell[shell.index('class="ti-density"') :][:600]
    assert "aria-pressed" in block
    assert "aria-current" not in block, (
        "aria-current on a toggle; that attribute means current-item-in-a-set"
    )


def test_the_script_stops_asserting_aria_current(shell: str):
    """Leaving a stale aria-current behind would have two attributes claiming
    state, and the CSS matches both."""
    paint = shell[shell.index("function paintDensity") :][:700]
    assert "aria-pressed" in paint
    assert "removeAttribute('aria-current')" in paint, (
        "the old attribute is never cleared, so a stale one can outlive the fix"
    )


def test_the_active_pill_covers_both_attributes(client: TestClient):
    """The trap. The pill hung off [aria-current='true'] only, so switching the
    density buttons to aria-pressed would have removed their visual state while
    every ARIA check still passed."""
    css = client.get("/static/titanium.css").text
    rule = re.search(r"([^{}]*\[aria-pressed='true'\][^{}]*)\{([^}]*)\}", css)
    assert rule, ".ti-seg__btn[aria-pressed='true'] has no styling"
    assert "aria-current" in rule.group(1), (
        "the two selectors were split; the nav links and the toggle must share "
        "one appearance"
    )
    assert "background" in rule.group(2)


def test_the_toggle_disappears_without_scripting(client: TestClient):
    """It is a JS-only control, and a dead button is worse than no button.
    `[data-js-only]` plus the noscript style is the app's existing mechanism."""
    body = client.get("/runs").text
    group = re.search(r'<span class="ti-density"[^>]*>', body)
    assert "data-js-only" in group.group(0)
    assert "<noscript><style>[data-js-only]{display:none" in body.replace(" ", ""), (
        "nothing hides js-only controls when scripting is off"
    )


def test_density_actually_changes_something(client: TestClient):
    """A preference that alters no rule is decoration."""
    css = client.get("/static/titanium.css").text
    assert re.search(r"\[data-density='comfortable'\]\s*\{[^}]*--ti-row-pad", css), (
        "comfortable density sets no row padding, so the toggle does nothing"
    )
