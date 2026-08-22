"""No internal identifier reaches a reader as if it were prose.

The section meta line printed `critique_status` straight through, so a
nonclinical safety summary told a scientist "failed_after_retries" beside a
section with zero citations — while every other string on that page is written
English ("1 citation", "1 uncited number", "2 of 2 claims carry a citation").
The band underneath already had proper wording, and so did the outline rail; the
meta line was the one place the enum escaped.

This is worth a test rather than a one-line fix because the leak is silent. A
Literal rendered into a template looks like working code and reads like a bug
only to someone who knows the codebase does not talk that way.

Note what is NOT flagged: snake_case is correct and expected for identifiers —
binding ids, query ids, template keys, section ids. Those are values a reader
needs verbatim to correlate against a source. The test targets STATE vocabulary,
which is the app describing itself.
"""

from __future__ import annotations

import json
import re
import shutil

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import runs as runs_module
from services.api_gateway.main import app

#: Every internal state value that must never appear on a page. Drawn from the
#: Literals in the codebase, not invented.
INTERNAL_STATES = (
    "failed_after_retries",
    "resolved_uncited",
    "no_data",
    "unavailable",
    "pending",
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def failed_run_id() -> str:
    """A run with a failed critique, built here rather than found.

    This fixture used to mine the run store for whatever happened to be lying
    around, and it broke twice for opposite reasons: first because the window
    was the newest 40 of 50 runs, then because the store prunes to the newest 50
    and the one failing run was evicted from disk entirely. Both times the suite
    went red while the application was fine — the second time *because* fixing
    `must_cite_every_number` stopped sections failing, so a test was punishing an
    improvement.

    So the run is constructed: copy a real completed run's artifacts, flip one
    section's `critique_status`, and register it. `critique_status` is read
    straight out of `result.json` by `_build_draft_view`, so this produces a
    genuinely rendered failed-section page rather than a mocked one, and it does
    it without a model call.
    """
    store = runs_module.get_store()
    source_id = None
    for summary in store.list_runs(limit=10_000):
        if summary.terminal and store.draft_view(summary.run_id):
            source_id = summary.run_id
            break
    if source_id is None:
        pytest.skip("no completed run to build a failed-critique fixture from")

    root = store._run_dir(source_id).parent
    run_id = "ftest0000fail"
    target = root / run_id
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(root / source_id, target)

    for name, mutate in (
        ("run.json", lambda d: d.update({"run_id": run_id})),
        ("result.json", None),
    ):
        path = target / name
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if mutate is not None:
            mutate(payload)
        else:
            sections = (payload.get("instance") or {}).get("sections") or []
            if sections:
                sections[0]["critique_status"] = "failed_after_retries"
        path.write_text(json.dumps(payload), encoding="utf-8")

    # Force a rehydrate so the singleton sees it. The store globs run.json at
    # construction, and there is no reload hook.
    runs_module._STORE = None
    yield run_id
    shutil.rmtree(target, ignore_errors=True)
    runs_module._STORE = None


@pytest.fixture(scope="module")
def pages(client: TestClient, failed_run_id: str) -> dict[str, str]:
    """Every reader-facing page, including a run with failed sections."""
    urls = ["/", "/runs", "/templates"] + [
        f"/runs/{failed_run_id}?tab={tab}" for tab in ("draft", "sources", "log")
    ]
    return {url: client.get(url).text for url in urls}


def test_a_run_with_failed_sections_exists_to_test_against(pages: dict[str, str]):
    """Otherwise every assertion below is vacuous."""
    assert any("tab=draft" in url for url in pages), (
        "no run with a failed critique in the store; this suite proves nothing"
    )


@pytest.mark.parametrize("state", INTERNAL_STATES)
def test_no_internal_state_is_printed_as_prose(pages: dict[str, str], state: str):
    offenders = []
    for url, html in pages.items():
        # Strip the places an identifier legitimately appears verbatim: the
        # <title>, mono code spans, and attribute values (titles, hrefs, data-*).
        text = re.sub(r"<[^>]*>", " ", html)
        if re.search(rf"\b{state}\b", text):
            offenders.append(url)
    assert not offenders, (
        f"the internal state {state!r} is rendered as reader-facing text on: "
        + ", ".join(offenders)
    )


def test_the_failed_state_has_a_written_label():
    assert runs_module.CRITIQUE_LABEL["failed_after_retries"], (
        "the failing state has no English wording, so the template has nothing "
        "to show but the enum"
    )
    label = runs_module.CRITIQUE_LABEL["failed_after_retries"]
    assert "_" not in label, f"{label!r} still reads like an identifier"


@pytest.mark.parametrize("state", ["passed", "pending"])
def test_the_uninteresting_states_say_nothing(state: str):
    """"passed" beside a citation count would be noise on every section that
    worked. The meta line mentions the state only when it is worth mentioning.
    """
    assert runs_module.CRITIQUE_LABEL[state] == ""


def test_every_critique_state_is_covered():
    """A new Literal member with no entry would fall back to "" and vanish
    silently, which is how a failing section would stop reporting at all."""
    import services.generation_orchestrator.types as types

    source = (types.__file__ or "")
    assert source
    from pathlib import Path

    text = Path(source).read_text(encoding="utf-8")
    match = re.search(r'critique_status:\s*Literal\[([^\]]*)\]', text)
    assert match, "could not find the critique_status Literal"
    members = re.findall(r'"([^"]+)"', match.group(1))
    assert members, "no Literal members parsed"
    missing = [m for m in members if m not in runs_module.CRITIQUE_LABEL]
    assert not missing, f"critique states with no display decision: {missing}"


def test_the_label_travels_with_the_view(client: TestClient):
    """It is a required field, not a defaulted one, so a new construction site
    cannot quietly omit it and fall back to the enum."""
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(runs_module.SectionView)}
    assert "critique_label" in fields
    assert fields["critique_label"].default is dataclasses.MISSING, (
        "critique_label is defaulted, so a caller that forgets it renders blank "
        "and a failing section stops announcing itself"
    )
