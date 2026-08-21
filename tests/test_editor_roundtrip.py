"""A structural edit must not throw away what you have typed.

The editor's repeaters — add, remove, move, and switching a source's type — all
round-trip through the server: each one posts the whole form and gets a fresh
one back. That is the right design for a page that must work with scripting off,
and it is also the design where a single dropped field silently loses work
someone has done. `test_ui_authoring.py` covers the outcomes (a create writes a
readable file, a stale base_sha is a conflict, removing a source clears its
references); nothing covered whether the values survive the trip.

The retype hint makes this a promise in the interface, not just an expectation:
"Changing the type swaps the fields below. Nothing you already typed is thrown
away."

A note on serialising the form, because getting it wrong looked exactly like a
data-loss bug. The editor posts one `source.k` field PER ROW, all sharing that
name, and the server rebuilds the row list from them. Collecting the form into a
dict keyed by field name collapses those five values into one, so the server
correctly rebuilds a single source and four appear to vanish. The body has to
keep duplicate names, in order, the way a browser sends them.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app

TEMPLATE = "target_assessment"
EDIT_URL = f"/templates/{TEMPLATE}/edit"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


class _FormReader(HTMLParser):
    """Serialise the editor form the way a browser would.

    Written on stdlib html.parser rather than a regex because a regex over the
    open tag cannot see two things a browser sends:

      * `<textarea name=x>the value</textarea>` — the value is the element's
        TEXT, not an attribute, so a tag-only match posts every textarea empty.
      * `<select name=x><option selected value=y>` — likewise, the value lives
        on the chosen option.

    Posting the form with every textarea and select blanked makes the server
    rebuild a much smaller draft, which looks exactly like rows being deleted.
    That was the second false alarm on this path; the first was submitting the
    `<template>` blueprints. Both are handled here, and `lxml` is deliberately
    not used — it is present in the venv only as a transitive dependency, and a
    data-preservation test that silently skips is no protection at all.
    """

    #: `<template>` content is inert: a browser never submits it, and the
    #: editor keeps three row blueprints in there full of `__KEY__` placeholders.
    _INERT = "template"

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pairs: list[tuple[str, str]] = []
        self._inert_depth = 0
        self._in_form = False
        self._textarea: str | None = None
        self._buf: list[str] = []
        self._select: str | None = None
        self._select_first: str | None = None
        self._select_chosen: str | None = None

    # -- structure ---------------------------------------------------------

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        a = dict(attrs)
        if tag == self._INERT:
            self._inert_depth += 1
            return
        if tag == "form" and "rg-editor" in (a.get("class") or ""):
            self._in_form = True
            return
        if self._inert_depth or not self._in_form:
            return
        if tag == "input":
            kind = (a.get("type") or "text").lower()
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            if a.get("name"):
                self.pairs.append((a["name"], a.get("value", "")))
        elif tag == "textarea" and a.get("name"):
            self._textarea, self._buf = a["name"], []
        elif tag == "select" and a.get("name"):
            self._select = a["name"]
            self._select_first = self._select_chosen = None
        elif tag == "option" and self._select is not None:
            value = a.get("value", "")
            if self._select_first is None:
                self._select_first = value
            if "selected" in a:
                self._select_chosen = value

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag == self._INERT:
            self._inert_depth = max(0, self._inert_depth - 1)
        elif tag == "form":
            self._in_form = False
        elif tag == "textarea" and self._textarea is not None:
            self.pairs.append((self._textarea, "".join(self._buf)))
            self._textarea = None
        elif tag == "select" and self._select is not None:
            # No `selected` attribute means the browser sends the first option.
            chosen = self._select_chosen
            if chosen is None:
                chosen = self._select_first or ""
            self.pairs.append((self._select, chosen))
            self._select = None

    def handle_data(self, data):  # noqa: ANN001
        if self._textarea is not None and not self._inert_depth:
            self._buf.append(data)


def _controls(html: str) -> list[tuple[str, str]]:
    """Every named control in the editor form, in order, duplicates kept.

    Buttons are excluded: a browser sends only the one that was pressed, and
    this form has 47 of them all named `op`.
    """
    reader = _FormReader()
    reader.feed(html)
    return reader.pairs


def _row_keys(html: str, kind: str) -> list[str]:
    """The row keys the form declares for `source` / `input` / `section`."""
    return [value for name, value in _controls(html) if name == f"{kind}.k"]


def _field(html: str, name: str) -> str | None:
    for field, value in _controls(html):
        if field == name:
            return value
    return None


def _post(client: TestClient, html: str, op: str, overrides: dict[str, str] | None = None):
    action = re.search(r'<form class="rg-editor"[^>]*action="([^"]+)"', html).group(1)
    pairs = _controls(html)
    if overrides:
        pairs = [(n, overrides.get(n, v)) for n, v in pairs]
    pairs.append(("op", op))
    # Encoded explicitly rather than handed to `data=`. This httpx version
    # treats a list of tuples as raw content — it warns "Use content=... to
    # upload raw bytes/text content" — so the server received a body it could
    # not parse and answered with an empty draft: no title, no rows. That looks
    # identical to the app deleting everything, and was the third false alarm on
    # this path. A dict cannot be used instead: duplicate names are the point.
    return client.post(
        action,
        content=urlencode(pairs),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


@pytest.fixture(scope="module")
def start(client: TestClient) -> str:
    page = client.get(EDIT_URL)
    assert page.status_code == 200
    return page.text


def test_the_serialiser_sees_every_row(start: str):
    """Guards the trap in this file's docstring: if this drops to one key, every
    assertion below would pass against a form that lost four sources."""
    keys = _row_keys(start, "source")
    assert len(keys) >= 3, f"only found source rows {keys}"
    assert len(set(keys)) == len(keys), "duplicate row keys"
    assert "__KEY__" not in keys, (
        "the <template> blueprint leaked into the submitted body; see "
        "_submittable"
    )


@pytest.mark.parametrize("kind", ["source", "input", "section"])
def test_adding_a_row_keeps_the_others_and_what_you_typed(
    client: TestClient, start: str, kind: str
):
    before = _row_keys(start, kind)
    response = _post(client, start, f"add:{kind}", {"title": "PROBE TITLE"})
    assert response.status_code in (200, 422)
    after = _row_keys(response.text, kind)
    assert len(after) == len(before) + 1, (
        f"adding a {kind} went from {len(before)} rows to {len(after)}"
    )
    assert _field(response.text, "title") == "PROBE TITLE", (
        f"adding a {kind} discarded an unrelated field"
    )


def test_moving_a_row_reorders_without_losing_one(client: TestClient, start: str):
    keys = _row_keys(start, "source")
    response = _post(client, start, f"move:source:{keys[1]}:up")
    assert response.status_code in (200, 422)
    after = _row_keys(response.text, "source")
    assert len(after) == len(keys), f"{len(keys)} sources became {len(after)}"
    assert after != keys, "the move did nothing"
    assert sorted(after) == sorted(keys), "a move invented or dropped a key"


def test_switching_a_source_type_keeps_what_you_typed(client: TestClient, start: str):
    """The hint promises exactly this."""
    keys = _row_keys(start, "source")
    first = keys[0]
    response = _post(
        client,
        start,
        f"retype:{first}",
        {f"source.{first}.id": "probe_marker_id", f"source.{first}.kind": "bigquery"},
    )
    assert response.status_code in (200, 422)
    body = response.text
    assert len(_row_keys(body, "source")) == len(keys), "a retype lost a source"
    assert _field(body, f"source.{first}.id") == "probe_marker_id", (
        "the hint promises nothing typed is thrown away, and the id was"
    )
    assert _field(body, "title") == _field(start, "title")


def test_the_retype_hint_still_makes_that_promise(start: str):
    """If the wording goes, the test above is checking a behaviour nobody
    claimed. If the behaviour goes, the wording is a lie. They travel together.
    """
    assert "Nothing you already typed is thrown away" in start


def test_an_unknown_source_kind_is_normalised_not_an_error(
    client: TestClient, start: str
):
    """A real <select> can only submit one of its own options, so this only
    arises from a hand-made POST. Normalising matches how the rest of the app
    treats unknown values — a stale bookmark widens rather than 500s.
    """
    keys = _row_keys(start, "source")
    response = _post(
        client, start, f"retype:{keys[0]}", {f"source.{keys[0]}.kind": "not_a_kind"}
    )
    assert response.status_code in (200, 422)
    assert "ti-errpage" not in response.text, "an unknown kind produced an error page"
    assert _field(response.text, f"source.{keys[0]}.kind") != "not_a_kind"


def test_a_structural_op_never_writes_to_disk(client: TestClient, start: str):
    """add/move/retype re-render the form; only `save` may touch the file. A
    structural op that wrote would make every keystroke a commit."""
    keys = _row_keys(start, "source")
    for op in (f"add:source", f"move:source:{keys[1]}:up", f"retype:{keys[0]}"):
        body = _post(client, start, op).text
        assert "Nothing is on disk yet" in body or "rg-editor" in body
        assert "Saved" not in body, f"{op} reported a save"
