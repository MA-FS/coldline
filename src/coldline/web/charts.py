"""Server-rendered SVG trend charts and sparklines (no JS chart library).

Charts are pure functions from :class:`~coldline.contracts.Trend` models to SVG markup. They use CSS classes
from ``static/css/components.css`` for colour, so they follow the theme (dark default, light optional).

Conventions (ISA-101 high-performance style):

* Normal series are drawn in grey. A series is only drawn in amber/red when the caller marks it abnormal.
* Stale samples (``Quality.STALE`` / ``BAD``) are drawn dashed and muted, with a ``STALE`` marker.
* Events (door open, alarm) are vertical dashed markers with a short label.
* Divergence between two series beyond a threshold is shaded (amber, low opacity).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

from markupsafe import Markup

from coldline.contracts import Quality, Reading, Trend


@dataclass
class ChartEvent:
    ts: datetime
    label: str
    tone: str = "neutral"  # neutral | amber | red


@dataclass
class Series:
    trend: Trend
    tone: str = "normal"  # normal | amber | red | ghost
    label: str | None = None


@dataclass
class _Frame:
    x0: float
    x1: float
    y0: float  # top
    y1: float  # bottom
    t0: float
    t1: float
    v0: float
    v1: float
    ticks_y: list[float] = field(default_factory=list)

    def x(self, ts: datetime) -> float:
        t = ts.timestamp()
        span = (self.t1 - self.t0) or 1.0
        return self.x0 + (t - self.t0) / span * (self.x1 - self.x0)

    def y(self, v: float) -> float:
        span = (self.v1 - self.v0) or 1.0
        return self.y1 - (v - self.v0) / span * (self.y1 - self.y0)


def _nice_step(span: float, target: int = 5) -> float:
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _y_range(values: Iterable[float], y_min: float | None, y_max: float | None) -> tuple[float, float, list]:
    vals = [v for v in values if v is not None]
    lo = min(vals) if vals else 0.0
    hi = max(vals) if vals else 1.0
    if y_min is not None:
        lo = min(lo, y_min)
    if y_max is not None:
        hi = max(hi, y_max)
    if hi - lo < 1.0:
        mid = (hi + lo) / 2
        lo, hi = mid - 0.5, mid + 0.5
    step = _nice_step(hi - lo, 5)
    lo = math.floor(lo / step) * step
    hi = math.ceil(hi / step) * step
    ticks = []
    v = lo
    while v <= hi + 1e-9:
        ticks.append(round(v, 6))
        v += step
    return lo, hi, ticks


def _fmt_tick(v: float) -> str:
    return f"{v:.1f}"


def _segments(points: Sequence[Reading]) -> list[tuple[bool, list[Reading]]]:
    """Split points into runs of (is_good, points). Each run starts at the previous run's last point."""
    runs: list[tuple[bool, list[Reading]]] = []
    prev: Reading | None = None
    for p in points:
        if p.value_c is None:
            continue
        good = p.quality == Quality.GOOD
        if not runs or runs[-1][0] != good:
            runs.append((good, [prev] if prev is not None else []))
        runs[-1][1].append(p)
        prev = p
    return runs


def _path(frame: _Frame, pts: Sequence[Reading]) -> str:
    return " ".join(
        f"{'M' if i == 0 else 'L'}{frame.x(p.ts):.1f},{frame.y(p.value_c):.1f}" for i, p in enumerate(pts)
    )


def _divergence_polys(frame: _Frame, a: Trend, b: Trend, threshold: float) -> list[str]:
    """Polygons covering the area between a and b wherever |a-b| > threshold (sample-aligned by timestamp)."""
    bmap = {p.ts: p for p in b.points if p.value_c is not None}
    pairs = [
        (p, bmap[p.ts])
        for p in a.points
        if p.value_c is not None
        and p.ts in bmap
        and p.quality == Quality.GOOD
        and bmap[p.ts].quality == Quality.GOOD
    ]
    polys: list[str] = []
    run: list[tuple[Reading, Reading]] = []
    for pa, pb in pairs + [(None, None)]:  # sentinel flush
        if pa is not None and abs(pa.value_c - pb.value_c) > threshold:
            run.append((pa, pb))
            continue
        if len(run) >= 2:
            top = [f"{frame.x(x.ts):.1f},{frame.y(x.value_c):.1f}" for x, _ in run]
            bot = [f"{frame.x(y.ts):.1f},{frame.y(y.value_c):.1f}" for _, y in reversed(run)]
            polys.append(" ".join(top + bot))
        run = []
    return polys


def trend_svg(
    series: Sequence[Series | Trend],
    *,
    width: int = 360,
    height: int = 150,
    events: Sequence[ChartEvent] = (),
    divergence: tuple[str, str, float] | None = None,
    y_min: float | None = None,
    y_max: float | None = None,
    limit: tuple[float, str] | None = None,
    x_ticks: int = 4,
    title: str = "",
    tz=None,
) -> Markup:
    """Render a trend chart as inline SVG.

    ``divergence=("TT-401", "TT-402", 2.0)`` shades where the two tags differ by more than 2.0.
    ``limit=(4.0, "High 4.0")`` draws a dashed limit line.
    """
    ss = [s if isinstance(s, Series) else Series(s) for s in series]
    all_pts = [p for s in ss for p in s.trend.points if p.value_c is not None]
    if not all_pts:
        return Markup(
            f'<svg class="chart chart--empty" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="No data"><text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'class="chart-empty">No samples in window</text></svg>'
        )
    extra = [limit[0]] if limit else []
    lo, hi, ticks = _y_range([p.value_c for p in all_pts] + extra, y_min, y_max)
    t0 = min(p.ts for p in all_pts).timestamp()
    t1 = max(p.ts for p in all_pts).timestamp()
    pad_l, pad_r, pad_t, pad_b = 34, 10, 10, 22
    f = _Frame(pad_l, width - pad_r, pad_t, height - pad_b, t0, t1, lo, hi, ticks)

    o: list[str] = []
    label = escape(title or ", ".join(s.trend.tag for s in ss))
    o.append(
        f'<svg class="chart" viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{label}">'
    )
    # grid + y axis
    for tv in ticks:
        y = f.y(tv)
        o.append(f'<line class="chart-grid" x1="{f.x0}" x2="{f.x1}" y1="{y:.1f}" y2="{y:.1f}"/>')
        o.append(
            f'<text class="chart-tick" x="{f.x0 - 6}" y="{y + 3.5:.1f}" text-anchor="end">{_fmt_tick(tv)}</text>'
        )
    o.append(f'<line class="chart-axis" x1="{f.x0}" x2="{f.x0}" y1="{f.y0}" y2="{f.y1}"/>')
    o.append(f'<line class="chart-axis" x1="{f.x0}" x2="{f.x1}" y1="{f.y1}" y2="{f.y1}"/>')
    # x ticks
    n = max(x_ticks, 1)
    for i in range(n + 1):
        t = t0 + (t1 - t0) * i / n
        x = f.x0 + (f.x1 - f.x0) * i / n
        ts = datetime.fromtimestamp(t, tz=all_pts[0].ts.tzinfo if tz is None else tz)
        anchor = "start" if i == 0 else ("end" if i == n else "middle")
        o.append(f'<line class="chart-tickmark" x1="{x:.1f}" x2="{x:.1f}" y1="{f.y1}" y2="{f.y1 + 3}"/>')
        o.append(
            f'<text class="chart-tick" x="{x:.1f}" y="{f.y1 + 14}" text-anchor="{anchor}">{ts:%H:%M}</text>'
        )
    # limit line
    if limit:
        y = f.y(limit[0])
        o.append(f'<line class="chart-limit" x1="{f.x0}" x2="{f.x1}" y1="{y:.1f}" y2="{y:.1f}"/>')
        o.append(
            f'<text class="chart-limit-label" x="{f.x1 - 2}" y="{y - 3:.1f}" text-anchor="end">'
            f"{escape(limit[1])}</text>"
        )
    # divergence shading
    if divergence:
        by_tag = {s.trend.tag: s.trend for s in ss}
        a, b, thr = divergence
        if a in by_tag and b in by_tag:
            for poly in _divergence_polys(f, by_tag[a], by_tag[b], thr):
                o.append(f'<polygon class="chart-divergence" points="{poly}"/>')
    # events
    last_label_x = -1e9
    row = 0
    for ev in sorted(events, key=lambda e: e.ts):
        if not (t0 <= ev.ts.timestamp() <= t1):
            continue
        x = f.x(ev.ts)
        row = row + 1 if x - last_label_x < 48 else 0
        last_label_x = x
        o.append(
            f'<line class="chart-event chart-event--{ev.tone}" x1="{x:.1f}" x2="{x:.1f}" '
            f'y1="{f.y0}" y2="{f.y1}"/>'
        )
        o.append(
            f'<text class="chart-event-label chart-event-label--{ev.tone}" x="{x + 3:.1f}" '
            f'y="{f.y0 + 9 + row * 11}">{escape(ev.label)}</text>'
        )
    # series (ghosts first so the primary sits on top)
    for s in sorted(ss, key=lambda s: 0 if s.tone == "ghost" else 1):
        for good, pts in _segments(s.trend.points):
            if len(pts) < 2 and good:
                continue
            cls = f"chart-line chart-line--{s.tone}" if good else "chart-line chart-line--stale"
            if len(pts) >= 2:
                o.append(f'<path class="{cls}" d="{_path(f, pts)}"/>')
            if not good:
                x = f.x(pts[-1].ts)
                y = f.y(pts[-1].value_c)
                o.append(
                    f'<text class="chart-stale-label" x="{x - 2:.1f}" y="{y - 6:.1f}" '
                    f'text-anchor="end">STALE</text>'
                )
        last = next((p for p in reversed(s.trend.points) if p.value_c is not None), None)
        if last is not None and s.tone != "ghost":
            dot_cls = "stale" if last.quality != Quality.GOOD else s.tone
            o.append(
                f'<circle class="chart-dot chart-dot--{dot_cls}" cx="{f.x(last.ts):.1f}" '
                f'cy="{f.y(last.value_c):.1f}" r="2.6"/>'
            )
    o.append("</svg>")
    return Markup("".join(o))


def sparkline_svg(
    values: Sequence[float | None], *, width: int = 96, height: int = 24, tone: str = "normal"
) -> Markup:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return Markup(f'<svg class="spark" viewBox="0 0 {width} {height}" aria-hidden="true"></svg>')
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = width / (len(values) - 1)
    pts = []
    for i, v in enumerate(values):
        if v is None:
            continue
        pts.append(f"{i * step:.1f},{height - 2 - (v - lo) / span * (height - 4):.1f}")
    last_x, last_y = pts[-1].split(",")
    return Markup(
        f'<svg class="spark spark--{tone}" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'aria-hidden="true"><polyline points="{" ".join(pts)}"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="2"/></svg>'
    )
