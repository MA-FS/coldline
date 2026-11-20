from datetime import timedelta

import pytest

from coldline.contracts import AlarmPriority, Quality, Scenario
from coldline.sim.analysis import FRESHNESS_WINDOW, divergence, freshness, is_comparison_valid, is_diverging
from coldline.systems import create_plant

PAIR = ["TT-401", "TT-402"]


def test_seed_cadence_and_prehistory() -> None:
    a, b = create_plant(22), create_plant(22)
    assert a.now().hour == 5 and a.now().minute == 0
    assert len(a.get_trend(PAIR, 90)[0].points) == 91
    for _ in range(10):
        a.tick(12)
    b.tick(120)
    assert a.snapshot() == b.snapshot()
    assert a.get_trend(PAIR) == b.get_trend(PAIR)
    assert create_plant(21).latest(PAIR) != create_plant(22).latest(PAIR)


def test_hero_evidence_and_reinjection() -> None:
    p = create_plant()
    p.inject(Scenario.PROBE_MISMATCH)
    a, b = p.get_trend(PAIR)
    assert is_diverging(a, b, p.now()) is True
    assert divergence(a, b, 15) > 2
    assert [a.points[-1].value_c, b.points[-1].value_c] == pytest.approx([6.8, 2.1], abs=0.06)
    assert b.points[41].value_c > 2.8
    assert max(abs(r.value_c - 2.1) for r in b.points[-10:]) < 0.1
    (alarm,) = p.list_alarms()
    assert alarm.ts.strftime("%H:%M") == "05:42"
    assert alarm.priority == AlarmPriority.AMBER
    p.tick(0.5)
    p.inject(Scenario.PROBE_MISMATCH)
    assert p.now().strftime("%H:%M") == "07:01"
    assert len(p.list_alarms()) == 1
    assert len(p.list_alarms(False)) == 2


def test_trip_and_door() -> None:
    p = create_plant()
    p.inject(Scenario.COMPRESSOR_TRIP)
    assert not p.get_asset("C2").running and not p.get_asset("C3").running
    assert p.get_asset("C1").running
    assert p.list_alarms()[0].priority == AlarmPriority.RED
    for tag in PAIR + ["TT-501", "TT-502"]:
        (trend,) = p.get_trend([tag], 20)
        assert trend.points[-1].value_c - trend.points[0].value_c > 4
    p = create_plant()
    p.inject(Scenario.DOOR_OPEN)
    for t in p.get_trend(PAIR, 12):
        assert max(r.value_c for r in t.points) > 4
        assert t.points[-1].value_c == pytest.approx(2.1, abs=0.12)
    assert not p.list_alarms()


def test_stopped_probe_invalidates_comparison() -> None:
    p = create_plant()
    fresh = p.latest(PAIR)[PAIR[1]]
    assert freshness(fresh, fresh.ts + FRESHNESS_WINDOW) == Quality.GOOD
    p.make_stale(PAIR[1])
    p.tick(180)
    a, b = p.latest(PAIR).values()
    assert b.ts == fresh.ts and b.value_c == fresh.value_c
    assert b.quality == Quality.STALE and a.quality == Quality.GOOD
    assert not is_comparison_valid(a, b, p.now())
    assert divergence(*p.get_trend(PAIR), 5) is None
    assert is_diverging(*p.get_trend(PAIR), p.now()) is None
    assert freshness(a, a.ts - timedelta(seconds=1)) == Quality.BAD
