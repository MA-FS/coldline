"""Unit tests for view helpers: mimic state rules, charts and the safe markdown subset."""

from __future__ import annotations

from coldline.contracts import Quality
from coldline.web import fixtures as fx
from coldline.web.charts import ChartEvent, Series, sparkline_svg, trend_svg
from coldline.web.views import alarm_badge, cite_chips, mini_markdown, plant_view, trend_direction


def _chiller(view, cid):
    return next(c for c in view.chillers if c.id == cid)


def test_hero_alarm_colours_only_the_abnormal_probe() -> None:
    view = plant_view(fx.snapshot("hero"), fx.hero_trends())
    ch4 = _chiller(view, "CH4")
    assert ch4.alarm == "amber"
    assert ch4.badge == "PROBE MISMATCH"
    tones = {t.tag: t.tone for t in ch4.tags}
    assert tones == {"TT-401": "amber", "TT-402": "normal"}
    assert [t.tag for t in ch4.tags] == ["TT-401", "TT-402"]  # evaporator tags excluded
    assert view.evap_supply.value == "−6.1"
    assert all(c.alarm == "" for c in view.chillers if c.id != "CH4")


def test_low_priority_alarm_never_colours_the_mimic() -> None:
    view = plant_view(fx.snapshot("stale"), fx.hero_trends(stale=True))
    ch4 = _chiller(view, "CH4")
    assert ch4.badge == "PROBE MISMATCH"
    assert ch4.stale
    assert {t.tag: t.tone for t in ch4.tags}["TT-402"] == "stale"


def test_trip_marks_compressor_tripped_and_standby_stays_grey() -> None:
    view = plant_view(fx.snapshot("trip"))
    states = {c.id: c.state for c in view.compressors}
    assert states == {"C1": "run", "C2": "tripped", "C3": "standby"}


def test_trend_direction() -> None:
    a, b = fx.hero_trends()
    assert trend_direction(a) == "up"
    assert trend_direction(b) == "steady"


def test_alarm_badge_short_label() -> None:
    assert alarm_badge(fx.hero_alarm()) == "PROBE MISMATCH"


def test_trend_svg_draws_divergence_events_and_stale() -> None:
    a, b = fx.hero_trends(stale=True)
    svg = str(
        trend_svg(
            [Series(a, "amber"), Series(b)],
            events=[ChartEvent(fx.DOOR_OPEN, "Door")],
            divergence=("TT-401", "TT-402", 2.0),
        )
    )
    assert svg.startswith("<svg")
    assert "chart-divergence" in svg
    assert "chart-line--stale" in svg
    assert "STALE" in svg
    assert "chart-event" in svg
    assert any(p.quality == Quality.STALE for p in b.points)


def test_trend_svg_empty_and_sparkline() -> None:
    assert "No samples" in str(trend_svg([]))
    assert "<polyline" in str(sparkline_svg([1.0, 2.0, 1.5]))


def test_markdown_escapes_html_and_renders_citations() -> None:
    html = str(mini_markdown("## Title\n\n- <script>x</script> [WO-1042]\n1. **bold** `code`"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'class="cite cite--wo"' in html
    assert "<h3>Title</h3>" in html
    assert "<strong>bold</strong>" in html and "<code>code</code>" in html
    assert "<ol>" in html and "<ul>" in html


def test_cite_chips_escape() -> None:
    assert "&lt;b&gt;" in str(cite_chips("<b> [L-2231]"))
