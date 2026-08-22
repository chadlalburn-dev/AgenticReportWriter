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
        points.append(
            ChartPoint(
                label="" if row[xi] is None else str(row[xi]),
                value=_as_number(row[yi], spec.y),
                series="" if si is None or row[si] is None else str(row[si]),
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
