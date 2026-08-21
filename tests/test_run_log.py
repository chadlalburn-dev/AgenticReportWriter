"""The run log is the provenance record, so its timestamps have to say something.

Its lede promises "every step this run took, in order". It rendered 46 rows all
stamped with the identical string "21 Aug 2026, 16:29" — the date repeated 46
times, carrying nothing, while the seconds that distinguish the steps were
truncated away by the shared minute-precision formatter. How long each step took
was the one thing a reader came for and the one thing not shown.

This matters more now than it did: on the stub engine a whole run finished in
half a second, so the missing precision was easy to overlook. On the local Claude
CLI a single section takes tens of seconds, and "where did this run spend its
time" becomes a real question about a real cost.

`_human_ts` is not changed — the run lists want minute precision and a date, and
one function trying to serve both surfaces is how this happened.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import runs as runs_module
from services.api_gateway.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def log_page(client: TestClient) -> str:
    store = runs_module.get_store()
    for summary in store.list_runs(limit=40):
        if not summary.terminal:
            continue
        view = store.draft_view(summary.run_id)
        if view and view.events:
            return client.get(f"/runs/{summary.run_id}?tab=log").text
    pytest.skip("no run with audit events")


# --- the formatters --------------------------------------------------------


def test_the_log_clock_carries_seconds_and_no_date():
    """Seconds because the rows differ by them; no date because it is the same
    on every row and belongs in the lede."""
    stamp = runs_module._clock_ts("2026-08-21T16:29:07+00:00")
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", stamp), stamp


def test_the_run_lists_keep_minute_precision():
    """The shared formatter is deliberately untouched: seconds in a run list are
    noise, and making one function serve both surfaces caused this bug."""
    stamp = runs_module._human_ts("2026-08-21T16:29:07+00:00")
    assert re.search(r"\d{2}:\d{2}$", stamp), stamp
    assert "Aug" in stamp, "the run lists still need the date"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "+0.0s"), (0.44, "+0.4s"), (9.9, "+9.9s"), (34, "+34s"), (900, "+15m")],
)
def test_the_elapsed_column_scales_with_the_gap(seconds: float, expected: str):
    """Sub-second at the bottom because a stub section completes in
    milliseconds; minutes at the top because a real model call does not."""
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 8, 21, 16, 29, tzinfo=timezone.utc)
    later = start + timedelta(seconds=seconds)
    assert runs_module._offset_human(later.isoformat(), start.isoformat()) == expected


def test_a_backwards_clock_says_nothing_rather_than_lying():
    """Clock skew is real and a negative elapsed time is worse than a blank."""
    assert runs_module._offset_human(
        "2026-08-21T16:29:00+00:00", "2026-08-21T16:29:05+00:00"
    ) == ""


def test_a_missing_timestamp_does_not_crash_the_log():
    assert runs_module._clock_ts("") == "—"
    assert runs_module._offset_human("", "") == ""


# --- the page --------------------------------------------------------------


def test_the_date_appears_once_not_on_every_row(log_page: str):
    """46 copies of one fact is what crowded out the seconds."""
    assert len(re.findall(r"\d{1,2} \w{3} 20\d\d", log_page)) <= 3, (
        "the date is being repeated per row again"
    )


def test_every_row_shows_a_time_with_seconds(log_page: str):
    rows = re.findall(r'class="ti-lrow ti-lrow--run"[^>]*>(.*?)</div>\s*</div>',
                      log_page, re.DOTALL)
    assert rows, "no log rows rendered"
    for row in rows:
        assert re.search(r"\d{2}:\d{2}:\d{2}", row), (
            "a log row shows no second-precision time"
        )


def test_the_elapsed_column_is_present_and_ordered(log_page: str):
    """It has to increase down the page, or it is not measuring elapsed."""
    offsets = re.findall(r'class="ti-log__offset">([^<]*)<', log_page)
    assert offsets, "no elapsed column"
    numeric = [
        float(o.strip().lstrip("+").rstrip("s"))
        for o in offsets
        if o.strip().endswith("s") and "m" not in o
    ]
    assert numeric == sorted(numeric), "the elapsed column is not monotonic"


def test_the_columns_are_labelled(log_page: str):
    """The middle column read as a bare word — "llm", "plan", "section" — with
    nothing saying what kind of thing that was. Both other tables on this run
    have a header row."""
    headers = re.findall(r'role="columnheader"[^>]*>([^<]*)<', log_page)
    assert headers, "the log has no header row"
    for expected in ("step", "kind", "elapsed", "time"):
        assert expected in [h.strip() for h in headers], f"no {expected!r} header"


def test_the_elapsed_figures_align(client: TestClient):
    """Comparing one step against another is the only reason to show this
    column, and ragged numerals defeat it."""
    css = client.get("/static/titanium.css").text
    rule = re.search(r"\.ti-log__offset\s*\{([^}]*)\}", css)
    assert rule, "the elapsed column has no styling"
    assert "tabular-nums" in rule.group(1)


# --- what the audit trail calls a call with no provider id -----------------


def test_a_call_with_no_provider_id_is_not_called_anonymous():
    """In an audit trail "anonymous" reads as an unattributed ACTOR.

    Someone auditing this record would reasonably take it as activity with no
    identity behind it. What it actually means is narrower and duller: the
    provider returned no request id to correlate against. The local Claude CLI
    never returns one, so every real generation would have carried that word.
    """
    from pathlib import Path

    raw = (
        Path(__file__).resolve().parents[1] / "services" / "audit" / "llm_audit.py"
    ).read_text(encoding="utf-8")
    # Comments stripped first. The fix is documented directly above the line it
    # fixes and quotes the old value, so an uncommented scan finds its own
    # explanation — the same trap that has now caught CSS, Jinja and Python
    # source scans in this suite.
    source = re.sub(r"^\s*#.*$", "", raw, flags=re.MULTILINE)
    assert '"anonymous"' not in source, (
        "an LLM call with no provider request id is labelled 'anonymous' in the "
        "audit trail, which describes an actor rather than a missing id"
    )
    assert '"no-provider-id"' in source


def test_no_request_id_is_invented_for_the_audit_trail():
    """`request_id` is documented as the PROVIDER's id. Generating one that
    looks like the provider's is precisely what a provenance record must not do,
    and the CLI client is the one with nothing to report."""
    from shared.llm.claude_cli import ClaudeCliLlmClient  # noqa: F401
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "shared" / "llm" / "claude_cli.py"
    ).read_text(encoding="utf-8")
    assert "request_id=" not in source, (
        "the CLI client sets a request_id; it has no provider id to report, so "
        "anything it sets there is fabricated"
    )


def test_the_audit_event_can_still_be_correlated(client: TestClient, log_page: str):
    """Dropping the fake id is only acceptable because correlation survives
    without it: each event carries its model version, its time, and its place in
    the hash chain."""
    import re as _re

    store = runs_module.get_store()
    for summary in store.list_runs(limit=40):
        if not summary.terminal:
            continue
        view = store.draft_view(summary.run_id)
        if not view or not view.events:
            continue
        llm = [e for e in view.events if e.group == "llm"]
        if not llm:
            continue
        for event in llm:
            assert event.ts_precise and event.ts_precise != "—", (
                "an LLM event with no usable timestamp cannot be correlated"
            )
        return
    pytest.skip("no run with model-call events")
