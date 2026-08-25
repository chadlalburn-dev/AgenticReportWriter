"""Every page against a store with nothing in it.

Degenerate data is where layouts break and empty states get skipped, and none of
it occurs naturally here: the sample corpus always has 50 runs and six
well-formed templates, so the first-run experience is the one state nobody
develops against. These tests point the app at an empty store and walk it.

What that turned up was not a crash but an inconsistency: the compounds home and
a compound with no runs both offer a primary button, while `/runs` asked the
reader to find the word "Compounds" in the middle of a sentence. Same situation,
three different affordances.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import compounds as compounds_module
from services.api_gateway import runs as runs_module
from services.api_gateway.main import app


@pytest.fixture
def empty(tmp_path, monkeypatch) -> TestClient:
    """The app with an empty run store and no seeded compound."""
    replacement = runs_module.RunStore(root=tmp_path / "empty")
    monkeypatch.setattr(runs_module, "_STORE", replacement)
    monkeypatch.setattr(compounds_module, "_seed_compound", lambda: None)
    try:
        yield TestClient(app)
    finally:
        replacement.shutdown(wait=True)


#: Every page a first-time user can reach before drafting anything.
FIRST_RUN_PAGES = ["/", "/runs", "/templates", "/templates/new", "/compound/XYZ-001"]


@pytest.mark.parametrize("url", FIRST_RUN_PAGES)
def test_no_page_breaks_on_an_empty_store(empty: TestClient, url: str):
    response = empty.get(url)
    assert response.status_code == 200, url
    assert "Traceback" not in response.text, f"{url} leaked a traceback"


@pytest.mark.parametrize("url", FIRST_RUN_PAGES)
def test_every_first_run_page_offers_a_way_forward(empty: TestClient, url: str):
    """A first-time user must never reach a page with nothing to do on it."""
    body = empty.get(url).text
    # Two primary-button classes exist: `ti-btn-primary` on the Titanium pages
    # and `rg-btn--primary` on the template editor, which rides the bridge
    # sheet. Checking only the first reported the editor as a dead end when its
    # primary action is "Save template" in the rail.
    links = re.findall(r'class="ti-btn-primary" href="([^"]*)"', body)
    buttons = re.findall(r'class="[^"]*rg-btn--primary[^"]*"', body)
    assert links or buttons, f"{url} has no primary action when the app is empty"
    for href in links:
        assert empty.get(href).status_code in (200, 303), f"{url} CTA {href} is dead"


def test_an_empty_list_and_a_failed_search_are_different_states(empty: TestClient):
    """They deserve different affordances.

    An empty list means "start here" — a primary button. A failed search means
    "your filter is too narrow" — a link back, because clearing a filter is a
    lateral move and not a commitment. Giving both the same treatment loses that.
    """
    fresh = empty.get("/runs").text
    assert "No runs yet" in fresh
    assert 'class="ti-btn-primary"' in fresh

    missed = empty.get("/runs?q=zzz-no-such-run").text
    assert "Nothing matches" in missed
    assert 'class="ti-btn-primary"' not in missed, (
        "a search miss should not push a primary action; the way out is to "
        "clear the filter"
    )
    assert "Clear the filter" in missed


def test_clearing_a_filter_keeps_the_grouping(empty: TestClient):
    """The same asymmetry that broke the segment links: clearing the search must
    not silently reset the view."""
    # `group` is a named axis now, not a boolean flag. Same property: the empty
    # state's clear link has to rebuild the arrangement, not reset it.
    body = empty.get("/runs?q=zz&group=status&sort=oldest").text
    clear = re.search(r'ti-empty__body">\s*<a href="([^"]*)"', body)
    assert clear, "no clear link in the no-match state"
    target = clear.group(1).replace("&amp;", "&")
    assert "group=status" in target and "sort=oldest" in target, (
        f"clearing the filter dropped the arrangement: {target}"
    )
    assert "q=" not in target, f"clearing the filter kept the search: {target}"


def test_the_compound_page_survives_a_compound_with_no_runs(empty: TestClient):
    """Reachable in normal use — a compound is a directory, not a run."""
    body = empty.get("/compound/XYZ-001").text
    assert "No runs yet for XYZ-001" in body
    assert 'class="ti-btn-primary"' in body


def test_an_unknown_run_is_an_error_page_not_a_crash(empty: TestClient):
    response = empty.get("/runs/deadbeefnosuchrun")
    assert response.status_code == 404
    assert "ti-errpage" in response.text
    assert "deadbeefnosuchrun" in response.text, (
        "the error page dropped the identifier that says what was not found"
    )


def test_the_engine_chip_still_renders_with_no_runs(empty: TestClient):
    """The disclosure is app chrome, not a property of a run, so it has to be
    there before anything has been drafted."""
    for url in ("/", "/runs", "/templates"):
        assert "ti-status__engine" in empty.get(url).text, url


def test_stray_files_in_the_template_folder_are_explained(empty: TestClient):
    """README and SKILL.template live beside the templates. They are surfaced as
    not-runnable, which is right — but it has to say that is expected, or it
    reads as two broken templates."""
    body = empty.get("/templates").text
    if "not runnable" not in body:
        pytest.skip("no stray files in the template folder")
    assert "expected" in body, (
        "stray files are listed as unusable with no note that this is normal"
    )
