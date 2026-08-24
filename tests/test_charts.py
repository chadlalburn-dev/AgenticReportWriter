"""Figures, and the rules that keep them honest.

A chart is the easiest place in this application to lie. It carries no
sentences, so nothing about it looks like a claim; it is read at a glance, so
nobody checks it against the table; and a plotting library will happily draw
whatever it is handed. The tests here exist because "the figure agreed with the
data" has to be a property rather than an assumption.
"""

from __future__ import annotations

import re

import pytest

from services.document_renderer.charts import ChartDataError, extract_points, render_chart
from shared.schemas.template import VisualKind, VisualSpec

# The real shape of exposure_margin_v1, so these tests break if the query
# changes under them rather than passing against a shape nothing produces.
COLUMNS = (
    "species",
    "duration_text",
    "study_label",
    "study_id",
    "noael_mg_per_kg_per_day",
    "noael_auc_24",
    "projected_auc_24_ng_h_per_ml",
    "exposure_margin_x",
)
ROWS = (
    ("Rat", "28-day", "Rat 28-day", "XYZ-NC-002", 60.0, 12000.0, 800.0, 15.0),
    ("Dog", "28-day", "Dog 28-day", "XYZ-NC-003", 12.0, 9600.0, 800.0, 12.0),
    ("Rat", "13-week", "Rat 13-week", "XYZ-NC-004", 30.0, 6200.0, 800.0, 7.8),
    ("Dog", "26-week", "Dog 26-week", "XYZ-NC-005", 6.0, 4800.0, 800.0, 6.0),
)


def _spec(**over) -> VisualSpec:
    fields = {
        "kind": VisualKind.MARGIN,
        "binding_id": "exposure_margins",
        "x": "study_label",
        "y": "exposure_margin_x",
        "unit": "x",
        "threshold": 10.0,
    }
    fields.update(over)
    return VisualSpec(**fields)


# --- the figure may not say anything the table does not --------------------


def test_every_plotted_value_comes_from_the_rows():
    """The whole provenance argument in one assertion. A figure whose numbers
    came from anywhere other than the resolved query would carry no citation
    and could not be checked against a source."""
    points = extract_points(_spec(), COLUMNS, ROWS)
    assert [p.value for p in points] == [15.0, 12.0, 7.8, 6.0]
    assert [p.label for p in points] == [
        "Rat 28-day",
        "Dog 28-day",
        "Rat 13-week",
        "Dog 26-week",
    ]


def test_a_non_numeric_cell_is_refused_not_coerced():
    """Reading 12 out of "12 uM" would plot a number the table does not state.
    The unit is not decoration — 12 uM and 12 nM differ by a thousandfold, and
    a bar cannot show which one it meant."""
    rows = (("Rat", "28-day", "Rat 28-day", "X", 60.0, 12000.0, 800.0, "12 uM"),)
    with pytest.raises(ChartDataError, match="not a number"):
        render_chart(_spec(), COLUMNS, rows)


def test_no_rows_is_refused_rather_than_drawn_empty():
    """An empty frame leaves the reader deciding whether the margins are zero
    or the query is broken. Those are opposite conclusions."""
    with pytest.raises(ChartDataError, match="no rows"):
        render_chart(_spec(), COLUMNS, ())


def test_a_missing_column_names_what_is_actually_there():
    """The author pointed at a column that does not exist. Saying which columns
    do is the difference between a fixable message and a puzzle."""
    with pytest.raises(ChartDataError) as caught:
        render_chart(_spec(y="margin_pct"), COLUMNS, ROWS)
    assert "margin_pct" in str(caught.value)
    assert "exposure_margin_x" in str(caught.value), "the real columns are not listed"


def test_a_boolean_is_not_a_measurement():
    """True would plot as 1.0 and look like a value. SQLite hands back integers
    for booleans, so this is reachable rather than theoretical."""
    rows = (("Rat", "28-day", "Rat 28-day", "X", 60.0, 12000.0, 800.0, True),)
    with pytest.raises(ChartDataError, match="boolean"):
        render_chart(_spec(), COLUMNS, rows)


# --- bars start at zero ----------------------------------------------------


def test_bar_lengths_are_proportional_to_their_values():
    """A clipped baseline misrepresents magnitude, and these numbers are safety
    multiples — the figures a reader uses to judge whether a margin is
    adequate. So 15x must draw two and a half times the bar of 6x.
    """
    svg = render_chart(_spec(), COLUMNS, ROWS)
    widths = [float(w) for w in re.findall(r'<rect[^>]*width="([\d.]+)"', svg)]
    assert len(widths) == 4
    ratio = widths[0] / widths[3]
    expected = 15.0 / 6.0
    assert abs(ratio - expected) < 0.02, (
        f"the 15x bar is {ratio:.2f} times the 6x bar, not {expected:.2f} — "
        "the baseline is not at zero"
    )


# --- the threshold is the point of a margin plot ---------------------------


def test_only_the_bars_below_the_threshold_are_accented():
    """Highlighting everything says nothing. With a 10x threshold, the 7.8x and
    6.0x margins are the answer to the only question anyone asks of this
    figure."""
    svg = render_chart(_spec(), COLUMNS, ROWS)
    fills = re.findall(r'<rect[^>]*fill="var\(--ti-(\w+)\)"', svg)
    assert fills == ["sec", "sec", "accent", "accent"]


def test_with_no_threshold_no_bar_is_singled_out():
    svg = render_chart(_spec(threshold=None), COLUMNS, ROWS)
    fills = re.findall(r'<rect[^>]*fill="var\(--ti-(\w+)\)"', svg)
    assert set(fills) == {"sec"}
    assert "stroke-dasharray" not in svg, "a threshold line was drawn without one set"


def test_the_axis_reaches_the_threshold_even_when_no_bar_does():
    """Otherwise a threshold above every value falls off the right edge and the
    figure silently stops showing the thing it is measured against."""
    svg = render_chart(_spec(threshold=40.0), COLUMNS, ROWS)
    assert "stroke-dasharray" in svg
    ticks = [float(t.replace(",", "")) for t in re.findall(r'>([\d,]+)</text>', svg)]
    assert max(ticks) >= 40.0, f"axis tops out at {max(ticks)}, below the 40x threshold"


# --- colours come from the design system -----------------------------------


def test_no_hardcoded_colours():
    """Every fill and stroke is a Titanium token, so the figure inherits the
    page theme and the print overrides instead of drifting from them."""
    svg = render_chart(_spec(), COLUMNS, ROWS)
    literals = re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{3,8}|rgb[^"]*)"', svg)
    assert not literals, f"hardcoded colours in the chart: {literals}"


# --- determinism -----------------------------------------------------------


def test_the_same_spec_and_rows_render_identically():
    """This is what makes "this kind of section always looks the same" a
    property rather than an intention. Two reports of the same type put their
    figures side by side, and a reader comparing compounds should be comparing
    the data."""
    first = render_chart(_spec(), COLUMNS, ROWS)
    second = render_chart(_spec(), COLUMNS, ROWS)
    assert first == second


# --- accessibility ---------------------------------------------------------


def test_the_figure_is_announced_and_described():
    """An SVG with no accessible name is an unlabelled graphic. The description
    names the range rather than every bar, because the full numbers are in the
    table below — that is the accessible copy that matters, and repeating them
    would create two sources of truth for the same figures."""
    svg = render_chart(_spec(), COLUMNS, ROWS)
    assert 'role="img"' in svg
    assert "aria-labelledby=" in svg
    title = re.search(r"<title[^>]*>(.*?)</title>", svg)
    desc = re.search(r"<desc[^>]*>(.*?)</desc>", svg)
    assert title and desc
    assert "6 x" in desc.group(1) and "15 x" in desc.group(1)
    assert "table below" in desc.group(1)


def test_labels_are_escaped():
    """Category labels are data, and data reaches this from a database. An
    unescaped label would inject markup into the page."""
    rows = (
        ("Rat", "28-day", '</svg><script>alert(1)</script>', "X", 1.0, 2.0, 3.0, 5.0),
    )
    svg = render_chart(_spec(), COLUMNS, rows)
    assert "<script>" not in svg
    assert "&lt;/svg&gt;" in svg or "&lt;script&gt;" in svg


# --- the rehydration path --------------------------------------------------


def test_numbers_survive_the_trip_through_stored_sources():
    """The gap thirteen passing tests left open.

    Every test above calls `render_chart` directly with typed rows, and they all
    passed while the app rendered "No figure: column 'exposure_margin_x' holds
    str '15.0', which is not a number." The draft view does not build charts from
    live objects — it rebuilds them from `sources.json`, and the loader was
    reading the stringified `rows` key into `typed_rows`.

    So this test goes through the loader. A chart that works in a unit test and
    not on the page is worth nothing.
    """
    from services.api_gateway.runs import _ledger_from_dicts

    stored = [
        {
            "binding_id": "exposure_margins",
            "kind": "named_query",
            "label": "Registered query",
            "status": "cited",
            "columns": list(COLUMNS),
            # What the writer persists: display strings and the real values.
            "rows": [[str(c) for c in row] for row in ROWS],
            "typed_rows": [list(row) for row in ROWS],
        }
    ]
    row = _ledger_from_dicts(stored)[0]

    assert [type(c).__name__ for c in row.typed_rows[0]] == [
        "str", "str", "str", "str", "float", "float", "float", "float"
    ], f"types lost on load: {row.typed_rows[0]}"

    svg = render_chart(
        _spec(), tuple(row.columns), tuple(tuple(c) for c in row.typed_rows)
    )
    assert svg.count("<rect") == 4


def test_a_run_recorded_before_typed_rows_existed_does_not_crash():
    """Older runs have only the strings. The chart refuses them — which is the
    correct outcome, and specifically not a traceback on someone's report."""
    from services.api_gateway.runs import _ledger_from_dicts

    stored = [
        {
            "binding_id": "exposure_margins",
            "kind": "named_query",
            "columns": list(COLUMNS),
            "rows": [[str(c) for c in row] for row in ROWS],
        }
    ]
    row = _ledger_from_dicts(stored)[0]
    assert row.typed_rows, "the fallback to rows did not happen"
    with pytest.raises(ChartDataError, match="not a number"):
        render_chart(_spec(), tuple(row.columns), tuple(tuple(c) for c in row.typed_rows))


# --- line and scatter ------------------------------------------------------
#
# The shape of `nonclinical_pk_summary_v1`: numeric dose, numeric exposure, two
# species. Real columns, so these break if the query changes under them.

PK_COLUMNS = ("species", "dose_mg_per_kg", "auc_inf_ng_h_per_ml")
PK_ROWS = (
    ("Rat", 5.0, 580.0),
    ("Rat", 25.0, 2900.0),
    ("Rat", 100.0, 11400.0),
    ("Dog", 1.0, 340.0),
    ("Dog", 5.0, 1700.0),
    ("Dog", 25.0, 8600.0),
)


def _pk(kind, **over):
    fields = {
        "kind": kind,
        "binding_id": "pk",
        "x": "dose_mg_per_kg",
        "y": "auc_inf_ng_h_per_ml",
        "series": "species",
    }
    fields.update(over)
    return VisualSpec(**fields)


def test_a_line_draws_one_path_per_series():
    svg = render_chart(_pk(VisualKind.LINE), PK_COLUMNS, PK_ROWS)
    assert svg.count("<path") == 2
    assert svg.count("<circle") == 6


def test_series_keep_the_order_the_query_returned_them_in():
    """First-appearance order, not sorted. A query returning rat before dog
    means it, and re-sorting here would quietly overrule the author — the same
    reason a line takes x from row order rather than sorting it."""
    svg = render_chart(_pk(VisualKind.LINE), PK_COLUMNS, PK_ROWS)
    labels = re.findall(r'font-size="11">(Rat|Dog)</text>', svg)
    assert labels == ["Rat", "Dog"]


def test_a_scatter_refuses_a_categorical_x():
    """A scatter's whole claim is that horizontal distance means something.
    Spacing categories evenly and calling it a scatter would invent a
    relationship the table does not contain."""
    with pytest.raises(ChartDataError) as caught:
        render_chart(_pk(VisualKind.SCATTER, x="species"), PK_COLUMNS, PK_ROWS)
    message = str(caught.value)
    assert "numeric x" in message
    assert "Use a line" in message, "the message does not say what to do instead"


def test_a_line_accepts_a_categorical_x():
    """The counterpart. A line plots a sequence, and "28-day, 13-week, 26-week"
    is a real sequence even though it is not a number."""
    svg = render_chart(_pk(VisualKind.LINE, x="species", series=None), PK_COLUMNS, PK_ROWS)
    assert "<path" in svg


def test_a_scatter_positions_marks_on_both_axes():
    """Distinct x positions, which is what separates a scatter from a line: if
    every mark shared an x the plot would be saying nothing about dose."""
    svg = render_chart(_pk(VisualKind.SCATTER), PK_COLUMNS, PK_ROWS)
    xs = {x for x in re.findall(r'<circle cx="([\d.]+)"', svg)}
    assert svg.count("<circle") == 6
    assert len(xs) >= 4, f"marks collapsed onto {len(xs)} x positions"


def test_identical_values_do_not_divide_by_zero():
    """A column where every value is the same, or a single row. Draws a flat
    line across the middle, which is exactly what the data says."""
    flat = (("Rat", 1.0, 7.0), ("Rat", 2.0, 7.0))
    assert "<path" in render_chart(_pk(VisualKind.LINE), PK_COLUMNS, flat)
    assert "<circle" in render_chart(_pk(VisualKind.SCATTER), PK_COLUMNS, flat)


def test_line_and_scatter_are_deterministic_too():
    for kind in (VisualKind.LINE, VisualKind.SCATTER):
        first = render_chart(_pk(kind), PK_COLUMNS, PK_ROWS)
        assert first == render_chart(_pk(kind), PK_COLUMNS, PK_ROWS)


def test_these_kinds_use_no_hardcoded_colours_either():
    for kind in (VisualKind.LINE, VisualKind.SCATTER):
        svg = render_chart(_pk(kind), PK_COLUMNS, PK_ROWS)
        literals = re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{3,8}|rgb[^"]*)"', svg)
        assert not literals, f"{kind.value}: hardcoded colours {literals}"


def test_they_are_announced_and_described_as_well():
    for kind in (VisualKind.LINE, VisualKind.SCATTER):
        svg = render_chart(_pk(kind), PK_COLUMNS, PK_ROWS)
        assert 'role="img"' in svg
        desc = re.search(r"<desc[^>]*>(.*?)</desc>", svg)
        assert desc and "table below" in desc.group(1)


def test_a_cropped_y_axis_is_allowed_here_but_never_on_bars():
    """Not an inconsistency, and worth pinning so nobody "fixes" it.

    A bar encodes its value as a length, so a clipped baseline makes a 6x margin
    look like a third of a 15x one. A line encodes position against a labelled
    axis the reader actually reads, and forcing zero onto a steady-state curve
    flattens it against the top. Different encodings, different rules.
    """
    # `[\d,.]` — the line chart's padded bounds produce fractional ticks like
    # "329.6", which a digits-and-commas pattern silently matches nothing of.
    tick_re = r'>([\d,]+\.?\d*)</text>'

    svg = render_chart(_pk(VisualKind.LINE), PK_COLUMNS, PK_ROWS)
    ticks = [float(t.replace(",", "")) for t in re.findall(tick_re, svg)]
    assert ticks, "no ticks found; the pattern no longer matches the markup"
    assert min(ticks) > 0, "the line chart forced a zero baseline"

    bars = render_chart(_spec(), COLUMNS, ROWS)
    bar_ticks = [float(t.replace(",", "")) for t in re.findall(tick_re, bars)]
    assert bar_ticks
    assert min(bar_ticks) == 0, "the bar chart lost its zero baseline"
