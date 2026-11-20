"""Unit tests for view helpers: mimic state rules, charts and the safe markdown subset.

Plant states come from the simulator itself (:func:`coldline.systems.create_plant`), so the rules are checked
against the same snapshots and trends the pages render.
"""

from __future__ import annotations

import re

from coldline.agent.evidence import DIVERGENCE_LIMIT_C, EXCURSION_LIMIT_C
from coldline.contracts import AlarmPriority, AssetKind, Quality, Scenario, Trend
from coldline.systems import Sandbox, create_plant
from coldline.web import context
from coldline.web.charts import PROBE_LIMIT_LINE, ChartEvent, Series, sparkline_svg, trend_svg
from coldline.web.views import (
    PlantView,
    alarm_badge,
    cite_chips,
    mini_markdown,
    plant_view,
    probe_tags,
    trend_direction,
)


def _plant(scenario: Scenario | None = None, stale: str | None = None) -> Sandbox:
    p = create_plant()
    if scenario:
        p.inject(scenario)
    if stale:
        p.make_stale(stale)
    return p


def _view(p: Sandbox) -> PlantView:
    snap = p.snapshot()
    tags = [t for a in snap.assets if a.kind == AssetKind.CHILLER for t in probe_tags(a)]
    return plant_view(snap, p.get_trend(tags, 60))


def _chiller(view, cid):
    return next(c for c in view.chillers if c.id == cid)


def test_probe_mismatch_colours_only_the_abnormal_probe() -> None:
    view = _view(_plant(Scenario.PROBE_MISMATCH))
    ch4 = _chiller(view, "CH4")
    assert ch4.alarm == "amber"
    assert ch4.badge == "PROBE MISMATCH"
    tones = {t.tag: t.tone for t in ch4.tags}
    assert tones == {"TT-401": "amber", "TT-402": "normal"}
    assert [t.tag for t in ch4.tags] == ["TT-401", "TT-402"]  # evaporator tags excluded
    assert view.evap_supply.value.startswith("\u2212")  # a true minus sign, not a hyphen
    assert all(c.alarm == "" for c in view.chillers if c.id != "CH4")


def test_a_stale_reference_probe_is_greyed_not_coloured() -> None:
    view = _view(_plant(Scenario.PROBE_MISMATCH, stale="TT-402"))
    ch4 = _chiller(view, "CH4")
    assert ch4.badge == "PROBE MISMATCH"
    assert ch4.stale
    assert {t.tag: t.tone for t in ch4.tags}["TT-402"] == "stale"


def test_low_priority_alarm_never_colours_the_mimic() -> None:
    snap = _plant(Scenario.PROBE_MISMATCH).snapshot()
    advisory = [a.model_copy(update={"priority": AlarmPriority.LOW}) for a in snap.alarms]
    view = plant_view(snap.model_copy(update={"alarms": advisory}))
    assert advisory and all(c.alarm == "" for c in view.chillers)


def test_trip_marks_compressor_tripped_and_standby_stays_grey() -> None:
    view = _view(_plant(Scenario.COMPRESSOR_TRIP))
    states = {c.id: c.state for c in view.compressors}
    assert states == {"C1": "run", "C2": "tripped", "C3": "standby"}


def test_trend_direction() -> None:
    a, b = _plant(Scenario.PROBE_MISMATCH).get_trend(["TT-401", "TT-402"], 60)
    assert trend_direction(a) == "up"
    assert trend_direction(b) == "steady"


def test_alarm_badge_short_label() -> None:
    assert alarm_badge(_plant(Scenario.PROBE_MISMATCH).list_alarms()[0]) == "PROBE MISMATCH"


def test_trend_svg_draws_divergence_events_and_stale() -> None:
    p = _plant(Scenario.PROBE_MISMATCH, stale="TT-402")
    a, b = p.get_trend(["TT-401", "TT-402"], 60)
    svg = str(
        trend_svg(
            [Series(a, "amber"), Series(b)],
            events=[ChartEvent(p.list_alarms()[0].ts, "Alarm")],
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


def test_dock_follows_the_lot_under_investigation() -> None:
    from datetime import timedelta

    from coldline.contracts import Scenario
    from coldline.systems import create_plant

    p = create_plant()
    assert plant_view(p.snapshot()).due_lot.id == "L-2231"  # nothing under investigation: next load due
    p.inject(Scenario.PROBE_MISMATCH)
    p.change_dispatch("L-2231", p.get_lot("L-2231").dispatch_due + timedelta(hours=3), "Shift Supervisor", "Hold")
    snap = p.snapshot()
    # L-2228 (CH1) is now due first, but the alarm is on CH4: the dock keeps showing the lot in question.
    assert plant_view(snap).due_lot.id == "L-2231"
    p.inject(Scenario.DOOR_OPEN)  # no alarm any more: the latest investigation's chiller decides
    assert plant_view(p.snapshot(), run_zones=["CH4"]).due_lot.id == "L-2231"
    assert plant_view(p.snapshot()).due_lot.id == "L-2228"


def _shaded_samples(svg: str) -> int:
    """Samples inside the chart's divergence shading (each polygon runs along one probe and back the other)."""
    polys = re.findall(r'<polygon class="chart-divergence" points="([^"]+)"', svg)
    return sum(len(points.split()) // 2 for points in polys)


def _samples_beyond(a: Trend, b: Trend, limit_c: float) -> int:
    """Samples where both probes read good and differ by more than ``limit_c``, in runs of two or more."""
    other = {p.ts: p for p in b.points}
    flags = [p.ts in other and p.quality == other[p.ts].quality == Quality.GOOD
             and p.value_c is not None and other[p.ts].value_c is not None
             and abs(p.value_c - other[p.ts].value_c) > limit_c for p in a.points]
    total = run = 0
    for beyond in [*flags, False]:
        if beyond:
            run += 1
            continue
        total += run if run >= 2 else 0
        run = 0
    return total


def _diverging_plant():
    """CH4 just after its probes start to drift apart: the gap passes through the divergence limit."""
    plant = create_plant()
    plant.inject(Scenario.PROBE_MISMATCH)
    a, b = plant.get_trend(["TT-401", "TT-402"], 60)
    expected = _samples_beyond(a, b, DIVERGENCE_LIMIT_C)
    # The chart can only tell the limit apart from a nearby one if the gap crosses both.
    assert 0 < expected and expected not in {_samples_beyond(a, b, DIVERGENCE_LIMIT_C + d) for d in (-0.5, 0.5)}
    return plant, expected


def test_the_run_chart_draws_the_limits_the_evidence_checks_use() -> None:
    from coldline.contracts import RunTrace

    plant, expected = _diverging_plant()
    run = RunTrace(run_id="R-1", session_id="s", scenario=Scenario.PROBE_MISMATCH, started_at=plant.now(),
                   extra={"evidence": [{"asset_id": "CH4"}]})
    svg = str(context.run_chart(plant, run))
    assert PROBE_LIMIT_LINE == (EXCURSION_LIMIT_C, f"High {EXCURSION_LIMIT_C:.1f} °C")
    assert svg.count('class="chart-limit"') == 1
    assert re.findall(r'class="chart-limit-label"[^>]*>([^<]+)<', svg) == [PROBE_LIMIT_LINE[1]]
    assert _shaded_samples(svg) == expected


def test_the_chiller_detail_charts_shade_the_divergence_and_draw_the_limit_the_evidence_checks_use() -> None:
    plant, expected = _diverging_plant()
    _, detail = context.detail_context(plant, "CH4")
    primary, reference = (str(chart) for chart in detail["charts"])
    assert _shaded_samples(primary) == expected
    assert _shaded_samples(reference) == 0  # shaded once per chiller, on the primary probe's chart
    for svg in (primary, reference):
        assert svg.count('class="chart-limit"') == 1
        assert re.findall(r'class="chart-limit-label"[^>]*>([^<]+)<', svg) == [PROBE_LIMIT_LINE[1]]
