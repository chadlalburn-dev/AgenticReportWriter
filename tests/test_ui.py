"""End-to-end tests for the UI layer (services.api_gateway).

Everything here drives the real FastAPI app through `TestClient`, with the
process-wide `RunStore` singleton re-pointed at a throwaway directory so the
suite never touches `var/runs/`. Generation itself is already offline — the
store builds a `StubLlmClient` and reads the bundled synthetic corpus — so
these tests need no network, no cloud and no keys.
"""

from __future__ import annotations

import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from services.api_gateway import runs as runs_module
from services.api_gateway.main import app
from services.template_service import load_report_doc
from shared.schemas.template import FreeTextInputBinding

#: The template every "drive a run to completion" test uses. Chosen because it
#: is small (4 sections), takes more than one input, and its draft ends up
#: citing both a local .docx and a SQL query — so one completed run exercises
#: the citation markers, the source-file route and the sources ledger.
RUN_TEMPLATE = "ib_nonclinical_sections"

#: Generation is stub-LLM fast (~2-3 s), but the pool is 2 workers wide.
RUN_TIMEOUT_S = 120.0


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory):
    """Swap the module-level singleton for a store rooted in a temp dir."""
    root = tmp_path_factory.mktemp("ui-runs")
    replacement = runs_module.RunStore(root=root)

    previous = runs_module._STORE
    runs_module._STORE = replacement
    try:
        yield replacement
    finally:
        runs_module._STORE = previous
        replacement.shutdown(wait=True)


@pytest.fixture(scope="module")
def client(store):
    # Deliberately NOT used as a context manager: the app's lifespan shutdown
    # hook calls get_store().shutdown(), which would kill the fixture's store
    # halfway through the module.
    return TestClient(app)


@pytest.fixture(scope="module")
def runnable_keys(store) -> list[str]:
    runnable, _ = store.list_templates()
    return [card.key for card in runnable]


@pytest.fixture(scope="module")
def completed_run(client, store) -> str:
    """One run, started through the browser form, driven to a terminal state."""
    run_id = _start_run(client, RUN_TEMPLATE)
    payload = _wait_for_terminal(client, run_id)
    assert payload["status"] == "completed", payload.get("error")
    return run_id


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FormScraper(HTMLParser):
    """Collect the first <form>'s method/action/controls, plus every <label for>.

    Deliberately attribute-exact: asserting on raw substrings let a mutation of
    `name="x"` into `data-name="x"` slip through, because the substring is still
    present. Parsing gives us the real submittable field set.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.action = ""
        self.method = ""
        self.fields: dict[str, str] = {}
        self.ids: set[str] = set()
        self.label_for: set[str] = set()
        self._depth = 0
        self._done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        if tag == "label" and attr.get("for"):
            self.label_for.add(attr["for"])
        if tag == "form" and not self._done:
            self._depth = 1
            self.action = attr.get("action", "")
            self.method = attr.get("method", "").lower()
            return
        if self._depth and tag in ("input", "textarea", "select"):
            name = attr.get("name")
            if name and attr.get("type") != "submit":
                self.fields[name] = attr.get("value", "")
                if attr.get("id"):
                    self.ids.add(attr["id"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._depth:
            self._depth = 0
            self._done = True


def _scrape_form(body: str) -> _FormScraper:
    scraper = _FormScraper()
    scraper.feed(body)
    return scraper


def _free_text_binding_ids(template_key: str) -> list[str]:
    """Read the run inputs straight from the engine, not from the view model.

    This is the whole point of the new-run page test: the form must offer an
    input for every `FreeTextInputBinding` the authored template declares.
    """
    template = load_report_doc(runs_module.TEMPLATES_DIR / f"{template_key}.md")
    seen: list[str] = []
    for section in template.all_sections():
        for binding in section.data_bindings:
            if isinstance(binding, FreeTextInputBinding):
                if binding.binding_id not in seen:
                    seen.append(binding.binding_id)
    return seen


def _start_run(client: TestClient, template_key: str) -> str:
    """Start a run the way a browser does: GET the page, submit its own form."""
    page = client.get(f"/new/{template_key}")
    assert page.status_code == 200, page.text[:2000]
    form = _scrape_form(page.text)

    # Submit exactly what the page rendered — nothing injected. The page
    # pre-fills each input with its sample value, so this is a real round-trip.
    response = client.post(form.action, data=form.fields, follow_redirects=False)
    assert response.status_code == 303, response.text[:2000]
    return response.headers["location"].rstrip("/").rsplit("/", 1)[-1]


def _wait_for_terminal(
    client: TestClient, run_id: str, timeout: float = RUN_TIMEOUT_S
) -> dict:
    deadline = time.monotonic() + timeout
    payload: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}/progress")
        assert response.status_code == 200, response.text[:2000]
        payload = response.json()
        if payload["terminal"]:
            return payload
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s: {payload}")


# ---------------------------------------------------------------------------
# /health — no regression
# ---------------------------------------------------------------------------


def test_health_still_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "api-gateway",
        "mode": "dev/stub",
    }


# ---------------------------------------------------------------------------
# gallery
# ---------------------------------------------------------------------------


def test_gallery_lists_every_runnable_template_by_title(
    client: TestClient, store
) -> None:
    runnable, _ = store.list_templates()
    assert runnable, "expected the authored report templates to be discoverable"

    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    body = response.text
    for card in runnable:
        assert str(escape(card.title)) in body, f"{card.key} title missing from gallery"
        assert f"/new/{card.key}" in body, f"{card.key} has no set-up link"


def test_gallery_flags_non_template_markdown_as_unavailable(
    client: TestClient, store
) -> None:
    """README.md / SKILL.template.md live in the same folder and must not 500."""
    _, unavailable = store.list_templates()
    assert {c.key for c in unavailable} >= {"README", "SKILL.template"}

    body = client.get("/").text
    for card in unavailable:
        assert str(escape(card.key)) in body


# ---------------------------------------------------------------------------
# new-run page
# ---------------------------------------------------------------------------


def test_new_run_page_renders_an_input_for_every_free_text_binding(
    client: TestClient, runnable_keys: list[str]
) -> None:
    for template_key in runnable_keys:
        expected = _free_text_binding_ids(template_key)
        assert expected, f"{template_key} declares no free-text inputs"

        response = client.get(f"/new/{template_key}")
        assert response.status_code == 200, template_key
        form = _scrape_form(response.text)

        # exactly the template's inputs, plus the two the form itself needs
        assert set(form.fields) == set(expected) | {
            "template_key",
            "evidence_folder",
        }, template_key
        assert form.fields["template_key"] == template_key

        for binding_id in expected:
            assert f"f-{binding_id}" in form.ids, f"{template_key}/{binding_id}"
            assert f"f-{binding_id}" in form.label_for, f"{template_key}/{binding_id}"

        # the form must post back to the run-creation endpoint
        assert form.method == "post", template_key
        assert form.action.endswith("/runs"), template_key


def test_new_run_page_shows_the_authored_prompt_as_the_field_label(
    client: TestClient, store
) -> None:
    card = store.get_template(RUN_TEMPLATE)
    body = client.get(f"/new/{RUN_TEMPLATE}").text
    for field in card.form_fields:
        assert str(escape(field.prompt)) in body


# ---------------------------------------------------------------------------
# creating a run
# ---------------------------------------------------------------------------


def test_create_run_redirects_to_the_run_page_and_registers_the_run(
    client: TestClient, store
) -> None:
    page = client.get(f"/new/{RUN_TEMPLATE}")
    form = _scrape_form(page.text)

    response = client.post(form.action, data=form.fields, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    run_id = location.rstrip("/").rsplit("/", 1)[-1]
    assert location.endswith(f"/runs/{run_id}")

    record = store.get(run_id)  # KeyError here == the run never landed
    assert record.template_key == RUN_TEMPLATE
    assert record.inputs == store.default_inputs(RUN_TEMPLATE)

    assert client.get(location).status_code == 200
    assert run_id in client.get("/runs").text

    _wait_for_terminal(client, run_id)


def test_create_run_with_a_missing_required_input_is_422_not_500(
    client: TestClient, store
) -> None:
    card = store.get_template(RUN_TEMPLATE)
    required = [f for f in card.form_fields if f.required]
    assert required

    form = {"template_key": RUN_TEMPLATE}
    form.update({f.binding_id: "" for f in card.form_fields})

    before = {s.run_id for s in store.list_runs()}
    response = client.post("/runs", data=form, follow_redirects=False)

    assert response.status_code == 422
    assert f'id="e-{required[0].binding_id}"' in response.text
    assert {s.run_id for s in store.list_runs()} == before


def test_api_create_run_returns_201_with_poll_urls(client: TestClient, store) -> None:
    response = client.post(
        "/api/runs",
        json={
            "template_key": RUN_TEMPLATE,
            "inputs": store.default_inputs(RUN_TEMPLATE),
        },
    )
    assert response.status_code == 201
    body = response.json()
    run_id = body["run_id"]
    assert body["poll_url"] == f"/api/runs/{run_id}/progress"
    assert body["html_url"] == f"/runs/{run_id}"
    assert store.get(run_id).template_key == RUN_TEMPLATE

    _wait_for_terminal(client, run_id)


# ---------------------------------------------------------------------------
# progress JSON
# ---------------------------------------------------------------------------


def test_progress_endpoint_matches_the_contract_payload_shape(
    client: TestClient, completed_run: str
) -> None:
    response = client.get(f"/api/runs/{completed_run}/progress")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["etag"]

    payload = response.json()
    assert set(payload) == {
        "run_id",
        "version",
        "status",
        "status_label",
        "terminal",
        "poll_after_ms",
        "template_key",
        "template_title",
        "template_version",
        "title",
        "inputs",
        "created_at",
        "started_at",
        "finished_at",
        "phase",
        "progress",
        "sections",
        "preflight",
        "totals",
        "instance_id",
        "model_version",
        "error",
        "result_url",
        "html_url",
    }

    assert payload["run_id"] == completed_run
    assert payload["template_key"] == RUN_TEMPLATE
    assert payload["terminal"] is True
    assert payload["poll_after_ms"] == 0
    assert payload["error"] is None
    assert payload["result_url"] == f"/api/runs/{completed_run}"
    assert payload["html_url"] == f"/runs/{completed_run}"
    assert isinstance(payload["version"], int)
    assert isinstance(payload["inputs"], dict)
    assert isinstance(payload["preflight"], list)

    assert set(payload["progress"]) == {
        "sections_total",
        "sections_done",
        "sections_failed",
        "sections_skipped",
        "percent",
    }
    assert payload["progress"]["percent"] == 100
    assert payload["progress"]["sections_total"] > 0
    assert payload["progress"]["sections_failed"] == 0

    assert set(payload["totals"]) == {
        "documents",
        "chunks",
        "citations",
        "audit_events",
    }
    assert payload["totals"]["citations"] > 0

    assert len(payload["sections"]) == payload["progress"]["sections_total"]
    for section in payload["sections"]:
        assert set(section) == {
            "section_id",
            "title",
            "level",
            "status",
            "status_label",
            "attempts",
            "n_paragraphs",
            "n_citations",
            "notes",
        }
        assert section["status"] in {
            "pending",
            "running",
            "retrying",
            "passed",
            "failed",
            "skipped",
            "cancelled",
        }


def test_progress_endpoint_honours_if_none_match(
    client: TestClient, completed_run: str
) -> None:
    first = client.get(f"/api/runs/{completed_run}/progress")
    etag = first.headers["etag"]

    second = client.get(
        f"/api/runs/{completed_run}/progress", headers={"If-None-Match": etag}
    )
    assert second.status_code == 304
    assert second.headers["etag"] == etag


# ---------------------------------------------------------------------------
# the cited draft actually renders
# ---------------------------------------------------------------------------


def test_completed_run_page_renders_section_titles_and_citation_markers(
    client: TestClient, store, completed_run: str
) -> None:
    response = client.get(f"/runs/{completed_run}")
    assert response.status_code == 200
    body = response.text

    # every section the authored template declares must appear in the draft
    template = load_report_doc(runs_module.TEMPLATES_DIR / f"{RUN_TEMPLATE}.md")
    titles = [s.title for s in template.all_sections()]
    assert titles
    for title in titles:
        assert str(escape(title)) in body, f"section {title!r} missing from draft"

    # ...and the draft must be cited: inline markers pointing at real
    # <article id="ref-N"> records in the source list.
    draft = store.draft_view(completed_run)
    assert draft is not None
    assert draft.citations, "the run produced no citations to render"

    for citation in draft.citations:
        assert f'id="ref-{citation.n}"' in body
        assert f'href="#ref-{citation.n}"' in body

    assert 'class="rg-cite"' in body
    assert 'data-citation-id="' in body

    # the non-dismissible human-review notice is part of the deliverable
    assert "AI-generated draft for human review" in body
    assert "verified against its citation" in body


def test_completed_run_exports_carry_the_draft(
    client: TestClient, completed_run: str
) -> None:
    md = client.get(f"/runs/{completed_run}/export.md")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert f'filename="report-{completed_run}.md"' in md.headers["content-disposition"]
    assert "AI-generated draft for human review" in md.text

    csv_response = client.get(f"/runs/{completed_run}/citations.csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert csv_response.text.strip(), "citations.csv came back empty"


def test_run_result_json_is_available_once_terminal(
    client: TestClient, completed_run: str
) -> None:
    response = client.get(f"/api/runs/{completed_run}")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) >= {"run", "instance", "citations", "audit_events"}
    assert payload["run"]["run_id"] == completed_run
    assert payload["instance"], "no report instance was persisted"
    assert payload["citations"], "no citations were persisted"


# ---------------------------------------------------------------------------
# static assets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "content_type", "needle"),
    [
        ("gsk.css", "text/css", ".rg-btn"),
        ("app.js", "javascript", "rg-progress"),
    ],
)
def test_static_assets_are_served(
    client: TestClient, name: str, content_type: str, needle: str
) -> None:
    response = client.get(f"/static/{name}")
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert needle in response.text

    on_disk = Path(runs_module.REPO_ROOT) / "services/api_gateway/static" / name
    assert on_disk.is_file()


def test_pages_reference_the_local_static_assets(client: TestClient) -> None:
    body = client.get("/").text
    assert "/static/gsk.css" in body
    assert "/static/app.js" in body


# ---------------------------------------------------------------------------
# 404s stay 404s
# ---------------------------------------------------------------------------


def test_unknown_template_returns_a_clean_404_page(client: TestClient) -> None:
    response = client.get("/new/not-a-real-template")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "not-a-real-template" in response.text
    assert "Traceback" not in response.text


def test_unknown_run_returns_a_clean_404_page(client: TestClient) -> None:
    response = client.get("/runs/deadbeefcafe")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "deadbeefcafe" in response.text
    assert "Traceback" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/templates/not-a-real-template",
        "/api/runs/deadbeefcafe",
        "/api/runs/deadbeefcafe/progress",
    ],
)
def test_unknown_ids_on_the_json_surface_return_404_json(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == 404


def test_posting_a_run_for_an_unknown_template_is_404(client: TestClient) -> None:
    response = client.post(
        "/runs", data={"template_key": "not-a-real-template"}, follow_redirects=False
    )
    assert response.status_code == 404


def test_exports_for_an_unknown_run_are_404(client: TestClient) -> None:
    assert client.get("/runs/deadbeefcafe/export.md").status_code == 404
    assert client.get("/runs/deadbeefcafe/citations.csv").status_code == 404


def test_source_outside_the_evidence_folder_is_404(
    client: TestClient, store, completed_run: str
) -> None:
    # Guard against a vacuous assertion: the route must serve a real source
    # file from this run's evidence folder before we can claim the 404 below
    # is a containment check rather than a routing miss.
    draft = store.draft_view(completed_run)
    open_urls = [c.open_url for c in draft.citations if c.open_url]
    assert open_urls, "no citation exposed a source-file link"
    served = client.get(open_urls[0].split("#", 1)[0])
    assert served.status_code == 200
    assert served.content

    response = client.get(f"/runs/{completed_run}/source/local::C:/Windows/win.ini")
    assert response.status_code == 404
