"""Ten errors on a 9,100px form need more than a count.

The editor's validation was already the strongest in the app: a 422 that stays
on the form, every failed field carrying `aria-invalid` and an `e-<name>`
message, specific human wording ("'not-a-version' is not a version number"),
and a `role="alert"` summary at the top. Two things were missing, and both only
bite at this page's scale.

  * The summary listed messages as plain text. `DraftIssue` has carried a
    `field` attribute the whole time — the form field name — so the data for
    links was there and unused. On a page this tall, ten messages and a count
    describes a search rather than offering a way to fix anything.
  * Nothing took the caret. The run-setup form autofocuses its first failed
    field; this form, fifteen times taller, dropped you at the top with nothing
    selected.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _submit(client: TestClient, **overrides: str):
    """Post the editor's own form back, with deliberate breakage."""
    page = client.get("/templates/new")
    assert page.status_code == 200
    body = page.text
    action = re.search(r'<form class="rg-editor"[^>]*action="([^"]+)"', body).group(1)
    fields: dict[str, str] = {}
    for tag in re.findall(r"<(?:input|textarea|select)[^>]*>", body):
        name = re.search(r'name="([^"]+)"', tag)
        if not name or 'type="checkbox"' in tag:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        fields[name.group(1)] = value.group(1) if value else ""
    fields["op"] = "save"
    fields.update(overrides)
    return client.post(action, data=fields, follow_redirects=False)


@pytest.mark.parametrize(
    ("overrides", "expected_field", "expected_words"),
    [
        ({"report_type": ""}, "report_type", "needs a key"),
        ({"report_type": "Has Spaces"}, "report_type", "not a usable key"),
        ({"version": "not-a-version"}, "version", "not a version number"),
        ({"title": ""}, "title", "needs a title"),
    ],
)
def test_each_broken_field_gets_its_own_message(
    client: TestClient, overrides: dict, expected_field: str, expected_words: str
):
    """The message has to name what is wrong with THAT field, not just fail."""
    response = _submit(client, **overrides)
    assert response.status_code == 422
    body = response.text
    # The whole element, tags stripped. ed_error renders an icon and a <span>
    # inside the <p id="e-...">, so the message is not the first child text.
    at = body.find(f'id="e-{expected_field}"')
    assert at != -1, f"no error rendered for {expected_field}"
    element = body[at : body.index("</p>", at)]
    text = re.sub(r"<[^>]*>", " ", element)
    assert expected_words in text, text.strip()[:160]


def test_nothing_is_saved_and_the_page_says_so(client: TestClient):
    """The worst outcome on this form is a partial write you cannot see."""
    body = _submit(client, report_type="").text
    assert "Nothing was saved" in body
    assert "template file was not touched" in body


def test_the_summary_is_announced(client: TestClient):
    """A count that only appears visually leaves a screen reader user on a
    9,100px form with no idea the submit failed."""
    body = _submit(client, report_type="").text
    assert re.search(r'class="rg-banner rg-banner--error" role="alert"', body)


def test_every_summary_item_links_to_its_field(client: TestClient):
    """The fix this file is mostly about. An issue with no `field` is a
    whole-form problem and correctly stays plain text."""
    body = _submit(client, version="not-a-version").text
    block = body[body.index('class="rg-banner rg-banner--error"') :]
    block = block[: block.index("</ul>")]
    items = re.findall(r"<li>(.*?)</li>", block, re.DOTALL)
    assert items, "the summary lists nothing"
    linked = [i for i in items if "<a href=" in i]
    assert len(linked) >= len(items) - 1, (
        f"only {len(linked)} of {len(items)} summary items link to a field"
    )
    for href in re.findall(r'href="#(f-[^"]+)"', block):
        assert f'id="{href}"' in body, f"summary links to a missing field: {href}"


def test_exactly_one_field_takes_the_caret(client: TestClient):
    """Two autofocus attributes is undefined behaviour; zero is a hunt."""
    body = _submit(client, version="not-a-version").text
    autofocused = re.findall(r'<input[^>]*\bautofocus\b[^>]*>', body)
    assert len(autofocused) == 1, f"{len(autofocused)} autofocused fields"


def test_the_caret_lands_on_the_first_failure_in_form_order(client: TestClient):
    """Landing on the last error would scroll past everything you need to fix."""
    body = _submit(client, version="not-a-version").text
    focused = re.search(r'<input[^>]*\bautofocus\b[^>]*>', body).group(0)
    name = re.search(r'name="([^"]+)"', focused).group(1)
    # report_type is the first field on the form and also fails here
    assert name == "report_type", f"the caret landed on {name}"


def test_a_clean_form_has_no_autofocus(client: TestClient):
    """Stealing the caret on first load moves the viewport for no reason."""
    body = client.get("/templates/new").text
    assert "autofocus" not in body, "the editor grabs focus before anything failed"
