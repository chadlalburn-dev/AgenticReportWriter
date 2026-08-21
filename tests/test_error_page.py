"""The error page is part of the app, not a different one.

Every 404, 405, 409, 422, 500 and 503 rendered `base.html` — the last route on
the legacy gsk.css shell. So the app was one application until you mistyped a
URL and then a visibly different one: white canvas instead of the Titanium
field, "Report types" instead of "Templates", no search, no engine chip, and a
footer hard-coding "Runs locally against a stub model" — a claim that goes false
the moment the local Claude CLI is signed in.

That last one is the reason this file exists rather than being a style note. An
error page that states the engine wrongly is the same provenance failure as a
draft that does, just on a screen nobody thought to look at.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import runs as runs_module
from services.api_gateway.main import app

#: URLs that must produce an error page, and the status each should carry.
ERROR_URLS = [
    ("/no-such-page-at-all", 404),
    ("/runs/deadbeefdeadbeef", 404),
    ("/new/not-a-real-template", 404),
]


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize(("url", "status"), ERROR_URLS)
def test_an_error_page_is_still_this_app(client: TestClient, url: str, status: int):
    response = client.get(url)
    assert response.status_code == status
    body = response.text
    assert "/static/titanium.css" in body, f"{url} is not on the Titanium shell"
    assert "gsk.css" not in body, f"{url} still loads the deleted legacy sheet"
    assert "ti-errpage" in body


@pytest.mark.parametrize(("url", "status"), ERROR_URLS)
def test_the_error_page_keeps_the_app_chrome(client: TestClient, url: str, status: int):
    """Losing a URL should not also lose the search box and the nav."""
    body = client.get(url).text
    assert "ti-status__engine" in body, "no engine chip"
    assert "ti-search__input" in body, "no search — the fastest way back"
    for label in ("Compounds", "Runs", "Templates"):
        assert label in body, f"nav is missing {label}"


@pytest.mark.parametrize(("url", "status"), ERROR_URLS)
def test_the_error_page_makes_no_claim_about_the_engine(
    client: TestClient, url: str, status: int
):
    """The legacy footer said "Runs locally against a stub model" as a literal.

    Any statement about the engine has to come from `resolve_engine()`, or it
    becomes a lie the day the engine changes — and nobody re-reads the 404 page
    to check.
    """
    body = client.get(url).text
    assert "Runs locally against a stub model" not in body
    engine = runs_module.resolve_engine()
    assert engine.label in body, (
        "the chip is present but does not name the engine the app resolved"
    )


def test_the_links_use_the_words_that_are_on_screen(client: TestClient):
    """They said "Back to report types" and "Run history".

    Neither phrase appears anywhere in the nav any more — "report types" is what
    Templates was called before the port, and "/" is Compounds now, not the
    gallery. Pointing a lost reader at names that no longer exist is worse than
    pointing nowhere.
    """
    body = client.get("/no-such-page-at-all").text
    links = re.findall(r'class="ti-(?:btn-primary|seg__btn)" href="([^"]+)">([^<]+)<', body)
    assert links, "the error page offers no way out"
    labels = {label.strip() for _href, label in links}
    hrefs = {href for href, _label in links}
    assert labels <= {"Compounds", "Runs", "Templates"}, labels
    assert hrefs == {"/", "/runs", "/templates"}
    # and they resolve
    for href in hrefs:
        assert client.get(href).status_code == 200, href


def test_the_server_message_is_passed_through_verbatim(client: TestClient):
    """On a 422 this is the validation reason. Prettifying it would lose the
    thing that tells you what to change."""
    response = client.get("/runs/deadbeefdeadbeef")
    assert "deadbeefdeadbeef" in response.text, (
        "the error page dropped the detail that identifies what was not found"
    )


def test_a_technical_detail_is_collapsed_not_hidden(client: TestClient):
    """A traceback is noise to most readers and the only useful thing on the
    page to whoever is debugging the run that produced it."""
    css = client.get("/static/titanium.css").text
    assert ".ti-errpage__detail" in css
    block = re.search(r"\.ti-errpage__detail pre \{([^}]*)\}", css)
    assert block, "no rule for the detail block"
    assert "overflow-x: auto" in block.group(1), (
        "wrapped stack frames are unreadable; this is the one place a "
        "horizontal scroller is right"
    )


def test_the_error_page_never_fails(client: TestClient, monkeypatch):
    """The fallback matters more here than anywhere: if rendering the error page
    raises, the user gets a bare 500 from the framework with no message at all.
    """
    from services.api_gateway import ui as ui_module

    def explode(*_args, **_kwargs):
        raise RuntimeError("template blew up")

    monkeypatch.setattr(ui_module, "_render", explode)
    response = client.get("/no-such-page-at-all")
    assert response.status_code == 404
    assert "404" in response.text, "the fallback lost the status"
