"""A draft is labelled by what wrote it, not by what the app would use now.

These are two different questions and the draft page was answering the wrong
one. `resolve_engine()` describes the app at this moment; a draft was written
at some point in the past, possibly by a different engine. Rendering the
ambient answer meant a report drafted by Claude read "PLACEHOLDER text from an
offline stub".

The other direction is the one that matters. Once the CLI is signed in,
`resolve_engine()` returns the real engine — and the same code would then have
labelled every previously stub-drafted report as the model's own words. That is
invented prose sitting behind a real provenance claim, in a product whose only
claim is provenance.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import runs as runs_module
from services.api_gateway.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize(
    ("model_version", "expect_real"),
    [
        ("claude-code-cli", True),
        ("stub", False),
        ("stub-claude-sonnet-4-6@stub", False),
        ("", False),
        ("claude-sonnet-5@vertex", True),
    ],
)
def test_the_run_engine_is_read_from_the_run(model_version: str, expect_real: bool):
    engine = runs_module.engine_for_run(model_version)
    assert engine.real is expect_real, f"{model_version!r} -> real={engine.real}"
    assert engine.label and engine.detail


def test_an_unrecorded_engine_under_claims_rather_than_over_claims():
    """Runs written before model_version existed must not be credited to a
    model. Under-claiming costs a reader nothing; over-claiming voids the
    provenance claim."""
    engine = runs_module.engine_for_run("")
    assert engine.real is False
    assert "PLACEHOLDER" in engine.detail.upper()


def test_the_stub_and_the_cli_do_not_share_a_description():
    stub = runs_module.engine_for_run("stub")
    cli = runs_module.engine_for_run("claude-code-cli")
    assert stub.detail != cli.detail
    assert "PLACEHOLDER" in stub.detail.upper()
    assert "PLACEHOLDER" not in cli.detail.upper()


def test_every_stored_draft_is_labelled_by_its_own_engine(client: TestClient):
    """The end-to-end invariant, over whatever is in the store.

    A stub-drafted run must carry the stub notice and a model-drafted one must
    not, regardless of which engine the app is currently on.
    """
    store = runs_module.get_store()
    checked = 0
    for summary in store.list_runs(limit=40):
        if not summary.terminal:
            continue
        record = store.get(summary.run_id)
        body = client.get(f"/runs/{summary.run_id}?tab=draft").text
        if "ti-notice" not in body:
            continue
        checked += 1
        expected_stub = not runs_module.engine_for_run(record.model_version).real
        actually_stub = "ti-notice--stub" in body
        assert actually_stub == expected_stub, (
            f"{summary.run_id} was drafted by {record.model_version!r} but the "
            f"page {'claims' if actually_stub else 'does not claim'} placeholder"
        )
    assert checked, "no terminal run with a draft notice to check"


def test_the_header_chip_still_describes_the_app_not_the_run(client: TestClient):
    """The two disclosures answer different questions and must stay separate.

    The chip is app chrome — "what would happen if you ran something now" — and
    it appears on pages that have no run at all.
    """
    body = client.get("/runs").text
    assert "ti-status__engine" in body, "the chip is missing from a page with no run"
    ambient = runs_module.resolve_engine()
    assert ambient.label in body


def test_the_notice_survives_scripting_being_off(client: TestClient):
    """It is part of the deliverable, so it cannot be a script-rendered banner."""
    store = runs_module.get_store()
    run_id = next(
        (s.run_id for s in store.list_runs(limit=40) if s.terminal), None
    )
    if run_id is None:
        pytest.skip("no terminal run")
    body = client.get(f"/runs/{run_id}?tab=draft").text
    if "ti-notice" not in body:
        pytest.skip("this run produced no draft")
    notice = body[body.index("ti-notice") : body.index("ti-notice") + 700]
    assert "AI-generated draft for human review" in notice
    assert "<script" not in notice


# --- the artifact that leaves the building ---------------------------------


def test_the_export_names_the_engine_that_drafted_it(client: TestClient):
    """The markdown export always read the run's own model_version, so it never
    had the page's bug. Pinned so it stays that way."""
    store = runs_module.get_store()
    checked = 0
    for summary in store.list_runs(limit=40):
        if not summary.terminal:
            continue
        response = client.get(f"/runs/{summary.run_id}/export.md")
        if response.status_code != 200:
            continue
        notice = next(
            (line for line in response.text.splitlines() if "AI-generated" in line),
            None,
        )
        assert notice, f"{summary.run_id} exported with no notice at all"
        record = store.get(summary.run_id)
        if "produced no draft" in notice:
            continue  # hollow run: a different, equally explicit branch
        checked += 1
        assert record.model_version in notice, (
            f"{summary.run_id} was drafted by {record.model_version!r} and the "
            f"export does not say so"
        )
    assert checked, "no exportable run to check"


def test_a_stub_export_says_placeholder_in_plain_words(client: TestClient):
    """"using stub-claude-sonnet-4-6@stub" reads like a real Sonnet build to
    anyone who has not seen the codebase — and this line travels: it is the
    first thing in every exported file, read by people who never opened the app.
    """
    store = runs_module.get_store()
    checked = 0
    for summary in store.list_runs(limit=40):
        if not summary.terminal:
            continue
        record = store.get(summary.run_id)
        if runs_module.engine_for_run(record.model_version).real:
            continue
        response = client.get(f"/runs/{summary.run_id}/export.md")
        if response.status_code != 200:
            continue
        notice = next(
            (line for line in response.text.splitlines() if "AI-generated" in line), ""
        )
        if "produced no draft" in notice:
            continue
        checked += 1
        assert "PLACEHOLDER" in notice, (
            f"{summary.run_id} exported stub prose without saying so: {notice[:160]}"
        )
    if not checked:
        pytest.skip("no stub-drafted export in the store")


def test_a_real_export_is_not_labelled_placeholder(client: TestClient):
    """The dangerous direction: never call a model's words placeholder, and
    never call placeholder a model's words."""
    store = runs_module.get_store()
    checked = 0
    for summary in store.list_runs(limit=40):
        if not summary.terminal:
            continue
        record = store.get(summary.run_id)
        if not runs_module.engine_for_run(record.model_version).real:
            continue
        response = client.get(f"/runs/{summary.run_id}/export.md")
        if response.status_code != 200:
            continue
        notice = next(
            (line for line in response.text.splitlines() if "AI-generated" in line), ""
        )
        checked += 1
        assert "PLACEHOLDER" not in notice, (
            f"{summary.run_id} was drafted by {record.model_version!r} and the "
            "export calls it placeholder"
        )
    if not checked:
        pytest.skip("no model-drafted export in the store")


def test_the_runs_list_marks_placeholder_drafts_only(client: TestClient):
    """The list is where you choose what to open, and the store can hold a mix.

    Only the exception is marked. Labelling every real draft "local Claude"
    would spend a column saying the same thing on every row in normal
    operation, while the caveated case is the one that changes a reader's plans.
    """
    import re as _re

    store = runs_module.get_store()
    body = client.get("/runs").text
    rows = _re.findall(
        r'<a class="ti-lrow ti-lrow--run" href="/runs/([a-z0-9]+)"(.*?)</a>',
        body,
        _re.DOTALL,
    )
    assert rows, "no run rows rendered"
    checked = 0
    for run_id, markup in rows:
        record = store.get(run_id)
        expected = not runs_module.engine_for_run(record.model_version).real
        marked = "ti-lrow__stub" in markup
        assert marked == expected, (
            f"{run_id} drafted by {record.model_version!r} is "
            f"{'marked' if marked else 'unmarked'} in the list"
        )
        checked += 1
    assert checked, "no rows compared"


def test_the_list_marker_says_what_it_means_on_hover(client: TestClient):
    """A two-word chip needs somewhere to explain itself."""
    import re as _re

    body = client.get("/runs").text
    if "ti-lrow__stub" not in body:
        pytest.skip("no placeholder-drafted run in the store")
    chip = _re.search(r"<span class=\"ti-lrow__stub\"[^>]*>", body).group(0)
    assert "title=" in chip
    assert "not a model" in chip


# --- the chip must not cost a subprocess per click -------------------------


def test_forced_cli_probes_once_not_once_per_page(monkeypatch):
    """Every HTML page discloses the engine, so `resolve_engine()` runs on every
    render — including 404s, which is how this was found: static files and the
    JSON API returned in 10ms while every HTML page, error pages included, took
    15 to 19 seconds.

    The `cli` branch wrote its answer to the cache and never read it back, so
    each click booted a ~330MB binary to ask whether it was signed in. The first
    answer is still synchronous, because an operator who forced `cli` wants the
    failure and not a placeholder — but that is one answer, not one per click.
    """
    from services.api_gateway import runs as runs_module

    probes = 0

    def counting_probe(choice: str):
        nonlocal probes
        probes += 1
        return runs_module._stub_engine(hint="", choice=choice)

    monkeypatch.setenv(runs_module.ENGINE_ENV, "cli")
    monkeypatch.setattr(runs_module, "_probe_engine", counting_probe)
    runs_module.reset_engine_cache()

    for _ in range(12):
        runs_module.resolve_engine()

    assert probes == 1, (
        f"twelve page renders launched {probes} engine probes; at ~16s each "
        "that is the load time the app was showing"
    )


def test_a_forced_cli_failure_is_not_hidden_by_the_cache(monkeypatch):
    """The reason this branch was synchronous in the first place. Caching must
    not turn "the CLI is not signed in" into a quiet stub claim — the cached
    answer has to be the real one, including when it is bad news."""
    from services.api_gateway import runs as runs_module

    def failing_probe(choice: str):
        return runs_module._stub_engine(hint="not signed in", choice=choice)

    monkeypatch.setenv(runs_module.ENGINE_ENV, "cli")
    monkeypatch.setattr(runs_module, "_probe_engine", failing_probe)
    runs_module.reset_engine_cache()

    first = runs_module.resolve_engine()
    second = runs_module.resolve_engine()
    assert second.kind == first.kind
    assert second.hint == first.hint == "not signed in"
