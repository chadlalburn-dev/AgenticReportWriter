"""Deterministic inline-SVG charts for report sections.

Why SVG generated in Python
---------------------------
The app is server-rendered Jinja with no build step, no npm assets and no CDN,
and core flows must work with JavaScript disabled. That rules out every charting
library. It is not much of a loss: the charts a preclinical summary needs are
bars, lines and margin plots, and those are a few hundred lines of arithmetic.
Inline SVG also prints, which a canvas would not.

Two rules the rest of this module exists to enforce
--------------------------------------------------
**A chart may not say anything the table does not.** Every plotted value comes
from the resolved query result — the same rows the reader sees immediately
below, carrying the same citation. Nothing here reads model output. A chart
drawn from prose would be a figure with no provenance, which is worse than no
figure at all.

**Bars start at zero.** Not a stylistic preference. A bar chart with a clipped
baseline misrepresents magnitude, and the numbers going through here are
exposure margins and NOAELs — the figures a reader uses to judge whether a
safety multiple is adequate.

Determinism
-----------
The same spec and the same rows yield byte-identical output: no timestamps, no
randomness, coordinates rounded to two decimals. That is what makes "this kind
of section always looks the same" a property rather than an intention, and it is
what lets a test assert on the markup.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from shared.schemas.template import VisualKind, VisualSpec

#: Plot geometry, fixed so two charts of the same kind are the same size in
#: every report. A figure that changes shape per section reads as an accident.
_W = 720
_H = 300
_PAD_L = 132  # category labels are things like "Rat 13-week"
_PAD_R = 56   # the value label on the longest bar
_PAD_T = 34
_PAD_B = 46

#: Titanium tokens, never literals. The chart inherits the page theme and the
#: print overrides for free, and a palette change cannot leave figures behind.
_INK = "var(--ti-ink)"
_SEC = "var(--ti-sec)"
_MUT = "var(--ti-mut)"
_LINE = "var(--ti-line)"
_RULE = "var(--ti-rule)"
_ACCENT = "var(--ti-accent)"

_MAX_CATEGORIES = 24


class ChartDataError(ValueError):
    """This spec cannot be drawn from these rows.

    Raised rather than papered over. A chart that silently drops the rows it
    could not parse is a chart that misstates the data; the caller renders this
    message instead, so the reader learns the figure is absent and why.
    """


@dataclass(frozen=True)
class ChartPoint:
    label: str
    value: float
    series: str = ""
    #: The x column as a number, when it is one. `None` for a categorical x —
    #: "Rat 13-week" has a position in a list but no position on an axis.
    #: Scatter needs this and refuses without it; bar and line do not.
    x_number: float | None = None


def _column_index(columns: tuple[str, ...], name: str, role: str) -> int:
    try:
        return columns.index(name)
    except ValueError:
        raise ChartDataError(
            f"the {role} column {name!r} is not in this table. "
            f"Columns present: {', '.join(columns) or '(none)'}"
        ) from None


def _as_number(raw: object, column: str) -> float:
    """A number, or a refusal.

    Coercing "12 uM" to 12, or letting a NaN through as a zero-length bar, both
    plot something the table does not say. A non-numeric value in a column
    someone pointed a chart at is an authoring mistake worth surfacing.
    """
    if isinstance(raw, bool):
        raise ChartDataError(f"column {column!r} holds a boolean, not a measurement")
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            raise ChartDataError(f"column {column!r} holds a non-finite value")
        return value
    raise ChartDataError(
        f"column {column!r} holds {type(raw).__name__} {raw!r}, which is not a "
        "number. Point the chart at a numeric column, or add one to the query."
    )


def extract_points(
    spec: VisualSpec, columns: tuple[str, ...], rows: tuple[tuple[object, ...], ...]
) -> list[ChartPoint]:
    """The plotted values, taken straight out of the query result."""
    if not rows:
        raise ChartDataError("the query returned no rows, so there is nothing to plot")

    xi = _column_index(columns, spec.x, "x")
    yi = _column_index(columns, spec.y, "y")
    si = _column_index(columns, spec.series, "series") if spec.series else None

    points: list[ChartPoint] = []
    for row in rows[:_MAX_CATEGORIES]:
        raw_x = row[xi]
        points.append(
            ChartPoint(
                label="" if raw_x is None else str(raw_x),
                value=_as_number(row[yi], spec.y),
                series="" if si is None or row[si] is None else str(row[si]),
                # Read, never coerced from text. A string that happens to look
                # numeric is still a label as far as the table is concerned.
                x_number=(
                    float(raw_x)
                    if isinstance(raw_x, (int, float)) and not isinstance(raw_x, bool)
                    else None
                ),
            )
        )
    return points


def _fmt(value: float) -> str:
    """A tick or bar label. Trailing zeros dropped, so 15.0 reads as 15."""
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}"
    return f"{value:,.1f}"


def _nice_max(highest: float) -> float:
    """A round upper bound at or above the tallest bar.

    Deterministic rather than pretty: the same data always yields the same axis,
    so the same section renders identically from one run to the next.
    """
    if highest <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(highest))) - 1)
    for step in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        candidate = magnitude * step
        if candidate >= highest:
            return float(candidate)
    return float(magnitude * 10)


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _n(value: float) -> str:
    """Two decimals, and never a negative zero. Both matter for byte-identical
    output, which is the whole basis of the determinism claim above."""
    return f"{round(value + 0.0, 2):g}"


def render_chart(
    spec: VisualSpec,
    columns: tuple[str, ...],
    rows: tuple[tuple[object, ...], ...],
) -> str:
    """One `<svg>` element, or a ChartDataError explaining why there is none."""
    points = extract_points(spec, columns, rows)
    if spec.kind in (VisualKind.BAR, VisualKind.MARGIN):
        return _render_bars(spec, points)
    if spec.kind is VisualKind.LINE:
        return _render_line(spec, points)
    if spec.kind is VisualKind.SCATTER:
        return _render_scatter(spec, points)
    raise ChartDataError(f"visual kind {spec.kind.value!r} is not implemented yet")


def _render_bars(spec: VisualSpec, points: list[ChartPoint]) -> str:
    """One horizontal bar per row.

    Horizontal rather than vertical because the categories here read like
    "Rat 13-week" and "Dog 26-week". Rotated x-axis labels are hostile; these
    sit left of the baseline at full size.
    """
    highest = max(p.value for p in points)
    axis_max = _nice_max(max(highest, spec.threshold or 0))
    plot_w = _W - _PAD_L - _PAD_R
    band = (_H - _PAD_T - _PAD_B) / len(points)
    bar_h = min(band * 0.62, 26.0)
    unit = f" {spec.unit}" if spec.unit else ""
    title = spec.title or f"{spec.y} by {spec.x}"
    ident = _esc(spec.binding_id)

    parts: list[str] = [
        f'<svg class="ti-chart" viewBox="0 0 {_W} {_H}" width="100%" '
        f'preserveAspectRatio="xMinYMin meet" role="img" '
        f'aria-labelledby="{ident}-cht-t {ident}-cht-d" '
        f'xmlns="http://www.w3.org/2000/svg">',
        f'<title id="{ident}-cht-t">{_esc(title)}</title>',
        # The description names the range rather than every bar. The full
        # numbers are in the table directly below, which is the accessible copy
        # that actually matters; repeating them here would be two sources of
        # truth for the same figures.
        f'<desc id="{ident}-cht-d">{len(points)} values from {_esc(spec.x)}, '
        f"ranging {_esc(_fmt(min(p.value for p in points)))}{_esc(unit)} to "
        f"{_esc(_fmt(highest))}{_esc(unit)}. The same figures appear in the "
        f"table below.</desc>",
    ]

    for i in range(5):
        gx = _PAD_L + plot_w * i / 4
        parts.append(
            f'<line x1="{_n(gx)}" y1="{_PAD_T}" x2="{_n(gx)}" '
            f'y2="{_n(_H - _PAD_B)}" stroke="{_RULE}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{_n(gx)}" y="{_H - _PAD_B + 18}" fill="{_MUT}" '
            f'font-size="11" text-anchor="middle" font-family="var(--ti-mono)">'
            f"{_esc(_fmt(axis_max * i / 4))}</text>"
        )

    for index, point in enumerate(points):
        y = _PAD_T + band * index + (band - bar_h) / 2
        width = 0.0 if axis_max == 0 else plot_w * point.value / axis_max
        # Accent marks the row that falls short of the threshold — the one thing
        # a reader is scanning for. With no threshold set, every bar is the same
        # colour, because highlighting everything says nothing.
        breach = spec.threshold is not None and point.value < spec.threshold
        parts.append(
            f'<text x="{_PAD_L - 10}" y="{_n(y + bar_h / 2 + 4)}" fill="{_INK}" '
            f'font-size="12" text-anchor="end">{_esc(point.label)}</text>'
        )
        parts.append(
            f'<rect x="{_PAD_L}" y="{_n(y)}" width="{_n(max(width, 1.0))}" '
            f'height="{_n(bar_h)}" fill="{_ACCENT if breach else _SEC}" rx="1"/>'
        )
        parts.append(
            f'<text x="{_n(_PAD_L + max(width, 1.0) + 8)}" '
            f'y="{_n(y + bar_h / 2 + 4)}" fill="{_MUT}" font-size="11" '
            f'font-family="var(--ti-mono)">{_esc(_fmt(point.value))}'
            f"{_esc(unit)}</text>"
        )

    if spec.threshold is not None and axis_max > 0:
        tx = _PAD_L + plot_w * spec.threshold / axis_max
        parts.append(
            f'<line x1="{_n(tx)}" y1="{_PAD_T - 8}" x2="{_n(tx)}" '
            f'y2="{_n(_H - _PAD_B)}" stroke="{_ACCENT}" stroke-width="1.5" '
            f'stroke-dasharray="4 3"/>'
        )
        parts.append(
            f'<text x="{_n(tx)}" y="{_PAD_T - 14}" fill="{_ACCENT}" '
            f'font-size="11" text-anchor="middle" font-family="var(--ti-mono)">'
            f"{_esc(_fmt(spec.threshold))}{_esc(unit)}</text>"
        )

    # The baseline goes on last so bars that touch it do not paint over it.
    parts.append(
        f'<line x1="{_PAD_L}" y1="{_PAD_T}" x2="{_PAD_L}" '
        f'y2="{_n(_H - _PAD_B)}" stroke="{_LINE}" stroke-width="1.5"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


# --- line and scatter -------------------------------------------------------
#
# These share a plotting frame with each other and nothing with the bars. The
# bars are horizontal with categories down the left; these are conventional x/y
# plots, because what they show is a relationship rather than a set of
# magnitudes to compare side by side.
#
# **These may crop the y axis, and bars may not.** Not an inconsistency. A bar
# encodes its value as a length, so a clipped baseline makes a 6x margin look
# like a third of a 15x one. A line or a scatter encodes position against a
# labelled axis that the reader actually reads — and forcing zero onto a
# steady-state dose-response curve flattens the whole thing against the top.
# Different encodings, different rules.

_PLOT_L = 72   # room for y tick labels; far less than the bars need
_PLOT_B = 52   # room for x tick labels


def _series_of(points: list[ChartPoint]) -> list[str]:
    """Series names in first-appearance order.

    First-appearance rather than sorted, so the legend follows the query's own
    ORDER BY. A query that returns rat before dog means it, and re-sorting here
    would quietly overrule the author.
    """
    seen: list[str] = []
    for point in points:
        if point.series not in seen:
            seen.append(point.series)
    return seen


def _bounds(values: list[float]) -> tuple[float, float]:
    """A (low, high) that always has width.

    A single point, or a column where every value is identical, would otherwise
    divide by zero. Padding it draws a flat line across the middle, which is
    exactly what the data says.
    """
    low, high = min(values), max(values)
    if low == high:
        pad = abs(low) * 0.1 or 1.0
        return low - pad, high + pad
    span = high - low
    return low - span * 0.08, high + span * 0.08


def _ticks(low: float, high: float, count: int = 4) -> list[float]:
    return [low + (high - low) * i / count for i in range(count + 1)]


def _frame(
    spec: VisualSpec, points: list[ChartPoint], y_low: float, y_high: float
) -> list[str]:
    """Axes, gridlines and labels. Shared, so line and scatter cannot drift."""
    unit = f" {spec.unit}" if spec.unit else ""
    title = spec.title or f"{spec.y} against {spec.x}"
    ident = _esc(spec.binding_id)
    plot_h = _H - _PAD_T - _PLOT_B

    parts = [
        f'<svg class="ti-chart" viewBox="0 0 {_W} {_H}" width="100%" '
        f'preserveAspectRatio="xMinYMin meet" role="img" '
        f'aria-labelledby="{ident}-cht-t {ident}-cht-d" '
        f'xmlns="http://www.w3.org/2000/svg">',
        f'<title id="{ident}-cht-t">{_esc(title)}</title>',
        f'<desc id="{ident}-cht-d">{len(points)} points. {_esc(spec.y)} ranges '
        f"{_esc(_fmt(min(p.value for p in points)))}{_esc(unit)} to "
        f"{_esc(_fmt(max(p.value for p in points)))}{_esc(unit)}. The same "
        f"figures appear in the table below.</desc>",
    ]

    for tick in _ticks(y_low, y_high):
        y = _PAD_T + plot_h * (1 - (tick - y_low) / (y_high - y_low))
        parts.append(
            f'<line x1="{_PLOT_L}" y1="{_n(y)}" x2="{_W - _PAD_R}" '
            f'y2="{_n(y)}" stroke="{_RULE}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{_PLOT_L - 8}" y="{_n(y + 4)}" fill="{_MUT}" '
            f'font-size="11" text-anchor="end" font-family="var(--ti-mono)">'
            f"{_esc(_fmt(tick))}</text>"
        )
    return parts


def _close(parts: list[str]) -> str:
    """The two axis rules, drawn last so marks touching them do not paint over."""
    plot_h = _H - _PAD_T - _PLOT_B
    parts.append(
        f'<line x1="{_PLOT_L}" y1="{_PAD_T}" x2="{_PLOT_L}" '
        f'y2="{_n(_PAD_T + plot_h)}" stroke="{_LINE}" stroke-width="1.5"/>'
    )
    parts.append(
        f'<line x1="{_PLOT_L}" y1="{_n(_PAD_T + plot_h)}" x2="{_W - _PAD_R}" '
        f'y2="{_n(_PAD_T + plot_h)}" stroke="{_LINE}" stroke-width="1.5"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _stroke_for(index: int) -> str:
    """Two strokes, alternating.

    A third series would need a third visual channel, and this palette has no
    third neutral that clears contrast on the recessed surface. Rather than
    inventing one, the label on the last point carries the identity — which is
    also more readable than a legend the eye has to travel to.
    """
    return _SEC if index % 2 == 0 else _ACCENT


def _render_line(spec: VisualSpec, points: list[ChartPoint]) -> str:
    """A line per series, with x taken as the row order.

    Row order rather than a sort, because x here is often a category whose
    sequence the values do not encode: "28-day", "13-week", "26-week" sorts
    alphabetically into nonsense. The query's ORDER BY is the author's statement
    about sequence, and this respects it.
    """
    y_low, y_high = _bounds([p.value for p in points])
    plot_w = _W - _PLOT_L - _PAD_R
    plot_h = _H - _PAD_T - _PLOT_B
    parts = _frame(spec, points, y_low, y_high)

    for index, name in enumerate(_series_of(points)):
        members = [p for p in points if p.series == name]
        if not members:
            continue
        step = plot_w / max(len(members) - 1, 1)
        coords = [
            (
                _PLOT_L + step * i,
                _PAD_T + plot_h * (1 - (p.value - y_low) / (y_high - y_low)),
            )
            for i, p in enumerate(members)
        ]
        stroke = _stroke_for(index)
        path = " ".join(
            ("M" if i == 0 else "L") + f"{_n(x)} {_n(y)}"
            for i, (x, y) in enumerate(coords)
        )
        parts.append(
            f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for x, y in coords:
            parts.append(f'<circle cx="{_n(x)}" cy="{_n(y)}" r="3" fill="{stroke}"/>')
        if name:
            last_x, last_y = coords[-1]
            parts.append(
                f'<text x="{_n(last_x + 6)}" y="{_n(last_y + 4)}" fill="{stroke}" '
                f'font-size="11">{_esc(name)}</text>'
            )
        if index == 0:
            for i, point in enumerate(members):
                parts.append(
                    f'<text x="{_n(_PLOT_L + step * i)}" y="{_H - _PLOT_B + 18}" '
                    f'fill="{_MUT}" font-size="11" text-anchor="middle">'
                    f"{_esc(point.label)}</text>"
                )
    return _close(parts)


def _render_scatter(spec: VisualSpec, points: list[ChartPoint]) -> str:
    """One mark per row, positioned on both axes.

    Refuses a categorical x rather than falling back to row order. A scatter's
    whole claim is that horizontal distance means something; spacing categories
    evenly and calling it a scatter would invent a relationship the table does
    not contain.
    """
    missing = [p.label for p in points if p.x_number is None]
    if missing:
        raise ChartDataError(
            f"a scatter needs a numeric x, and column {spec.x!r} holds "
            f"{missing[0]!r}. Use a line for a categorical x, or point this at "
            f"a numeric column."
        )

    xs = [p.x_number for p in points if p.x_number is not None]
    x_low, x_high = _bounds(xs)
    y_low, y_high = _bounds([p.value for p in points])
    plot_w = _W - _PLOT_L - _PAD_R
    plot_h = _H - _PAD_T - _PLOT_B
    parts = _frame(spec, points, y_low, y_high)

    for tick in _ticks(x_low, x_high):
        x = _PLOT_L + plot_w * (tick - x_low) / (x_high - x_low)
        parts.append(
            f'<text x="{_n(x)}" y="{_H - _PLOT_B + 18}" fill="{_MUT}" '
            f'font-size="11" text-anchor="middle" font-family="var(--ti-mono)">'
            f"{_esc(_fmt(tick))}</text>"
        )

    series = _series_of(points)
    for point in points:
        if point.x_number is None:  # guarded above; kept for the type checker
            continue
        x = _PLOT_L + plot_w * (point.x_number - x_low) / (x_high - x_low)
        y = _PAD_T + plot_h * (1 - (point.value - y_low) / (y_high - y_low))
        parts.append(
            f'<circle cx="{_n(x)}" cy="{_n(y)}" r="4" '
            f'fill="{_stroke_for(series.index(point.series))}"/>'
        )
    return _close(parts)
