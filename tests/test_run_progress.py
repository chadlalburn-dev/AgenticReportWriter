"""The screen someone watches while a run is working.

On the stub engine this state flickered past, so one line — "N of 6 sections
done." — was enough. The local Claude CLI changes that: a six-section report
takes minutes, and for those minutes the only thing on screen was a number that
might be stuck. There was no way to tell "working on section 4" from "hung".

The states below cannot be reached by driving the app, because the stub
finishes faster than a poll completes. So the template is rendered directly
against synthetic records — the only way to exercise `retrying` and `failed`
at all.
"""

from __future__ import annotations

import re

import pytest

from services.api_gateway import runs as runs_module
from services.api_gateway.ui import TEMPLATES

ALL_STATUSES = (
    "pending",
    "running",
    "retrying",
    "passed",
    "failed",
    "skipped",
    "cancelled",
)


def _section(status: str, index: int) -> runs_module.SectionProgress:
    return runs_module.SectionProgress(
        section_id=f"s{index}",
        title=f"Section {index}",
        level=2,
        status=status,  # type: ignore[arg-type]
        status_label=status.title(),
        attempts=2 if status == "retrying" else 1,
        n_paragraphs=3 if status == "passed" else 0,
        n_citations=4 if status == "passed" else 0,
    )


def _render(sections: list, status_label: str = "Drafting") -> str:
    """Render the in-progress branch of the run page."""

    class _Run:
        terminal = False
        template_title = "Candidate Selection Dossier"
        primary_input = "XYZ-001"
        created_human = "21 Aug 2026, 11:05"
        run_id = "deadbeefcafe"
        status_state = "busy"

    run = _Run()
    run.status_label = status_label

    class _Record:
        pass

    record = _Record()
    record.sections = sections

    template = TEMPLATES.get_template("run_titanium.html")
    return template.render(
        request=None,
        run=run,
        record=record,
        poll_url="/runs/deadbeefcafe/status",
        draft=None,
        tab="draft",
        tabs=[],
        partial=False,
        engine=runs_module.resolve_engine(),
        user=type("U", (), {"is_known": False, "display_name": "tester"})(),
        nav_active="runs",
        app_version="0.2.0",
    )


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_every_section_status_renders(status: str):
    """Including the three the stub engine can never produce fast enough to see."""
    html = _render([_section(status, 1)])
    assert f"ti-progress__item--{status}" in html


def test_the_list_says_which_section_is_being_written():
    """"Working on section 4" versus "hung" is the whole point."""
    sections = [
        _section("passed", 1),
        _section("passed", 2),
        _section("running", 3),
        _section("pending", 4),
    ]
    html = _render(sections)
    for index in (1, 2, 3, 4):
        assert f"Section {index}" in html
    assert "ti-progress__item--running" in html


def test_progress_is_reported_by_a_real_progress_element():
    """A native <progress> announces its value to assistive tech and needs no
    script; a styled div would need both."""
    sections = [_section("passed", 1), _section("passed", 2), _section("pending", 3)]
    html = _render(sections)
    tag = re.search(r"<progress[^>]*>", html)
    assert tag, "no <progress> element"
    assert 'max="3"' in tag.group(0)
    assert 'value="2"' in tag.group(0)
    assert "aria-label=" in tag.group(0)


def test_a_failed_section_counts_as_finished_not_as_pending():
    """A run that failed half its sections must not look like it is still
    working on them."""
    html = _render([_section("failed", 1), _section("passed", 2), _section("pending", 3)])
    tag = re.search(r"<progress[^>]*>", html).group(0)
    assert 'value="2"' in tag, "a failed section is still a finished section"


def test_a_retry_is_visible():
    """Silent retries are how a run looks stuck when it is not."""
    html = _render([_section("retrying", 1)])
    assert "attempt 2" in html


def test_the_run_phase_is_named_not_hardcoded():
    """The old copy said "Drafting…" during preflight and ingestion too."""
    html = _render([_section("pending", 1)], status_label="Reading evidence folder")
    assert "Reading evidence folder" in html
    assert "Drafting…" not in html


def test_the_engine_is_disclosed_while_waiting():
    """Minutes of waiting deserve to say what is doing the work."""
    html = _render([_section("running", 1)])
    engine = runs_module.resolve_engine()
    assert engine.detail[:40] in html


def test_it_still_works_with_scripting_off():
    """The poller is an enhancement. Without it the page must say how to see
    progress rather than appearing frozen."""
    html = _render([_section("running", 1)])
    # The base layout has its own <noscript> (it hides js-only chrome), so look
    # inside the progress panel rather than at the first one in the document.
    panel = html[html.index("ti-progress") :]
    panel = panel[: panel.index("</div>", panel.index("ti-progress__foot"))]
    assert "<noscript>" in panel, "the progress panel has no scriptless fallback"
    fallback = panel[panel.index("<noscript>") :]
    assert "href=" in fallback, "no way to refresh without scripting"
