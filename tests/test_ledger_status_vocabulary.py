"""The sources tab must not call a cited source a failure.

Two status vocabularies exist and they answer different questions:

  * `SourceSpec.status` — `ready` / `gap` / `unavailable`. Before a run: can
    this binding resolve?
  * `LedgerRow.status` — `cited` / `resolved_uncited` / `unavailable`. After a
    run: did it resolve, and did the draft use what came back?

The sources tab tested `status == "ready"`, a value the ledger never produces.
So it was always false and every row rendered in the failure treatment —
hollow dot, accent-coloured value — including the sources that resolved and
were cited. In a product whose whole claim is provenance, reporting a cited
source as failed is the worst error on offer.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import runs as runs_module
from services.api_gateway.main import app

LEDGER_STATUSES = {"cited", "resolved_uncited", "unavailable"}
PREFLIGHT_STATUSES = {"ready", "gap", "unavailable"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def ledger() -> list:
    store = runs_module.get_store()
    for summary in store.list_runs(limit=40):
        if not summary.terminal:
            continue
        view = store.draft_view(summary.run_id)
        if view and view.ledger:
            return list(view.ledger)
    pytest.skip("no terminal run with a source ledger")


def test_the_two_vocabularies_stay_distinct(ledger: list):
    """If they ever merge, this test is the place to say so deliberately."""
    seen = {row.status for row in ledger}
    assert seen <= LEDGER_STATUSES, f"unexpected ledger status: {seen - LEDGER_STATUSES}"
    assert not seen & (PREFLIGHT_STATUSES - {"unavailable"}), (
        "a preflight status leaked into the ledger"
    )


def test_derived_state_agrees_with_the_status(ledger: list):
    for row in ledger:
        assert row.resolved == (row.status != "unavailable")
        assert row.used == (row.status == "cited")
        if row.used:
            assert row.resolved, "cited but not resolved is incoherent"


def test_the_returned_value_names_the_right_unit(ledger: list):
    """A file set returns extracts, not rows. The template hardcoded "rows"
    for every kind, so document sources were mislabelled."""
    for row in ledger:
        if row.row_count is None:
            assert row.returned_text == "nothing"
            continue
        assert str(row.row_count) in row.returned_text
        expected = "extract" if row.kind in ("file_set", "file_ref") else "row"
        assert expected in row.returned_text, (
            f"{row.binding_id} is a {row.kind} but reports {row.returned_text!r}"
        )


def test_no_template_hardcodes_a_ledger_status_string(client: TestClient):
    """The vocabulary is interpreted in LedgerRow, once.

    Templates compare against `resolved` / `used`. A raw status comparison here
    is how the original bug got in: the string was plausible and always false.
    """
    from pathlib import Path

    templates = (
        Path(__file__).resolve().parents[1] / "services" / "api_gateway" / "templates"
    )
    offenders = []
    for path in sorted(templates.glob("*.html")):
        # Jinja comments go first: the fix for this bug is documented beside
        # it and quotes the offending expression, so an uncommented scan finds
        # its own explanation.
        source = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        if "draft.ledger" not in source:
            continue
        block = source[source.index("draft.ledger") :][:2600]
        for match in re.finditer(r"\br\.status\s*==", block):
            offenders.append(f"{path.name}: {block[match.start() - 20:match.start() + 40]}")
    assert not offenders, (
        "a ledger row's status compared as a raw string:\n" + "\n".join(offenders)
    )


def test_a_cited_source_renders_as_resolved(client: TestClient, ledger: list):
    """End to end: the page itself, not just the view model."""
    if not any(row.used for row in ledger):
        pytest.skip("this run cited nothing")
    store = runs_module.get_store()
    run_id = next(
        s.run_id
        for s in store.list_runs(limit=40)
        if s.terminal and (store.draft_view(s.run_id) or None) and store.draft_view(s.run_id).ledger
    )
    body = client.get(f"/runs/{run_id}?tab=sources").text
    rows = re.findall(r'<div class="ti-brow" role="row">(.*?)</div>\s*</div>', body, re.DOTALL)
    assert rows, "no binding rows rendered"
    assert 'ti-dot--on' in body, "not one source rendered as resolved"
    assert 'ti-brow__returns--on' in body, (
        "no source rendered its returned count in the resolved treatment"
    )


def test_the_unused_state_is_visually_distinct(client: TestClient):
    """Resolved-but-uncited is neither success nor failure and must not borrow
    the colour of either."""
    css = client.get("/static/titanium.css").text
    assert ".ti-brow__returns--unused" in css
    for variant in ("--on", "--off", "--unused"):
        assert re.search(
            r"\.ti-brow__returns" + re.escape(variant) + r"\s*\{[^}]*color:", css
        ), f"{variant} has no colour of its own"


def test_the_summary_accounts_for_every_source(ledger: list):
    """The arithmetic has to close.

    The summary named only "cited" and "could not be resolved", so a source
    that WAS pulled and then went unused by the draft vanished: 8 bound, 3
    cited, 4 unresolved, one unaccounted for. That omission is the interesting
    case — retrieval worked and the draft ignored the result — and it was the
    one silently dropped.
    """
    import re as _re

    store = runs_module.get_store()
    checked = 0
    for summary in store.list_runs(limit=40):
        if not summary.terminal:
            continue
        view = store.draft_view(summary.run_id)
        if not view or not view.ledger:
            continue
        checked += 1
        counts = {"cited": 0, "resolved_uncited": 0, "unavailable": 0}
        for row in view.ledger:
            counts[row.status] += 1
        numbers = [int(n) for n in _re.findall(r"\b(\d+)\b", view.ledger_summary)]
        assert numbers, f"no figures in {view.ledger_summary!r}"
        total, *stated = numbers
        assert total == len(view.ledger)
        assert sum(stated) == total, (
            f"{view.ledger_summary!r} accounts for {sum(stated)} of {total} sources"
        )
    assert checked, "no run with a ledger to check"


def test_a_state_with_no_members_is_not_mentioned(ledger: list):
    """A summary reading "0 pulled but unused" is noise, not information."""
    store = runs_module.get_store()
    for summary in store.list_runs(limit=40):
        if not summary.terminal:
            continue
        view = store.draft_view(summary.run_id)
        if not view or not view.ledger:
            continue
        assert " 0 " not in view.ledger_summary, view.ledger_summary
        if not any(row.status == "resolved_uncited" for row in view.ledger):
            assert "unused" not in view.ledger_summary, view.ledger_summary


# --- the status dot ---------------------------------------------------------


def test_the_resolved_dot_uses_the_systems_own_ok_colour(client: TestClient):
    """It was the rust accent, so a source that resolved got a solid red dot
    while one that failed got a hollow grey ring — inverted for anyone reading
    a safety document, where red means look here.

    The fix is consistency, not a new colour: .ti-state--ok and
    .ti-state--bad already establish green/rust, and the dot was the one status
    signal not following them.
    """
    import re as _re

    css = client.get("/static/titanium.css").text
    bare = _re.sub(r"/\*.*?\*/", "", css, flags=_re.DOTALL)

    def value(selector: str, prop: str) -> str:
        rule = _re.search(_re.escape(selector) + r"\s*\{([^}]*)\}", bare)
        assert rule, f"{selector} has no rule"
        found = _re.search(prop + r":\s*([^;]+);", rule.group(1))
        assert found, f"{selector} sets no {prop}"
        return found.group(1).strip()

    dot = value(".ti-dot--on", "background")
    text = value(".ti-state--ok", "color")
    # Deliberately NOT the same token. A dot is a non-text indicator and needs
    # 3:1 (WCAG 1.4.11); 10px status text needs 4.5:1, and the dot green is 3.80
    # on the page background. So they are two greens from one family: --ti-ok
    # and --ti-ok-dark. tests/test_contrast.py owns the ratios.
    assert dot == "var(--ti-ok)", dot
    assert text == "var(--ti-ok-dark)", text
    for used in (dot, text):
        assert "accent" not in used, (
            "the resolved state must not wear the accent — that is the failure "
            "colour, and reading it as one is the bug this file exists for"
        )


def test_absence_is_shown_as_absence_not_as_alarm(client: TestClient):
    """A hollow ring, not a red one.

    The paired text already names the specific failure ("nothing", "gap") in
    accent-dark, so the alarm is stated once, in words — and the accent stays
    as scarce as the handoff's accent budget asks.
    """
    import re as _re

    css = client.get("/static/titanium.css").text
    bare = _re.sub(r"/\*.*?\*/", "", css, flags=_re.DOTALL)
    rule = _re.search(r"\.ti-dot--off\s*\{([^}]*)\}", bare)
    assert rule
    body = rule.group(1)
    assert "background: transparent" in body, "the absent state must stay hollow"
    assert "accent" not in body, (
        "two accent signals for one gap — the value column already carries it"
    )


def test_the_dot_is_never_the_only_signal(client: TestClient, ledger: list):
    """Colour-blind readers and greyscale printouts both need the words."""
    store = runs_module.get_store()
    run_id = next(
        s.run_id
        for s in store.list_runs(limit=40)
        if s.terminal and (store.draft_view(s.run_id) or None)
        and store.draft_view(s.run_id).ledger
    )
    body = client.get(f"/runs/{run_id}?tab=sources").text
    assert 'aria-hidden="true"' in body, "the decorative dot is not hidden from AT"
    # every row states its outcome in text as well
    for row in store.draft_view(run_id).ledger:
        assert row.returned_text, f"{row.binding_id} has no written outcome"
