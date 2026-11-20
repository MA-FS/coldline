"""Every evidence predicate, probe by probe: limit, recovery, freshness, agreement and the release gate.

Each case builds real minute-by-minute historian trends for a chiller's two probes and runs the production
checks on them. Cases are written once and run twice, with the probes swapped, so a check that looks only at
the primary, only at the reference, or at an aggregate (the lower reading, the larger peak) fails one of the
two runs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from coldline.agent.evidence import (
    CLEARANCE_WINDOW_MIN,
    DIVERGENCE_LIMIT_C,
    EXCURSION_LIMIT_C,
    NEEDS_QA,
    UNCERTAIN,
    Verdict,
    assess_chiller,
    code_conclusion,
    gate_conclusion,
    release_clearance,
)
from coldline.agent.handover import lot_line, required_followups
from coldline.contracts import Asset, AssetKind, Lot, LotStatus, QAHold, Quality, Reading, Trend
from coldline.sim.analysis import FRESHNESS_WINDOW

NOW = datetime.fromisoformat("2026-11-21T05:40:00+08:00")
TAGS = ("TT-401", "TT-402")
WINDOW = 60  # minutes of history each probe has

Profile = Callable[[int], float]  # minutes before now -> °C


def flat(value: float) -> Profile:
    return lambda _m: value


def excursion(base: float, peak: float, *, start: int, end: int, ramp: int = 3) -> Profile:
    """``base``, rising to ``peak`` between ``start`` and ``end`` minutes ago, then back to ``base``."""
    def value(m: int) -> float:
        if m > start or m < end:
            return base
        edge = min(start - m, m - end)
        return base + (peak - base) * min(1.0, edge / ramp)
    return value


def split(early: Profile, late: Profile, at: int) -> Profile:
    """``early`` until ``at`` minutes ago, ``late`` after."""
    return lambda m: early(m) if m > at else late(m)


def trend(tag: str, profile: Profile, *, age_min: int = 0, quality: Quality = Quality.GOOD) -> Trend:
    """One reading a minute for ``WINDOW`` minutes, the latest ``age_min`` minutes old."""
    points = [Reading(tag=tag, ts=NOW - timedelta(minutes=m), value_c=round(profile(m), 3),
                      quality=quality if m == age_min else Quality.GOOD)
              for m in range(WINDOW, age_min - 1, -1)]
    return Trend(tag=tag, description=f"Chiller 4 air temp ({tag})", points=points)


@dataclass
class Systems:
    """The read side of the plant the checks use, serving fixed trends."""

    trends: dict[str, Trend]
    lot: Lot = field(default_factory=lambda: Lot(
        id="L-2231", zone_asset_id="CH4", product="Beef carcases, chilled", carcases=120, weight_kg=36000,
        customer="Synthetic Export Co", status=LotStatus.HOLD, dispatch_due=NOW + timedelta(minutes=35)))

    def now(self) -> datetime:
        return NOW

    def get_asset(self, asset_id: str) -> Asset:
        if asset_id != "CH4":
            raise KeyError(asset_id)
        return Asset(id="CH4", kind=AssetKind.CHILLER, name="Chiller 4", zone="carcase_chillers", tags=list(TAGS))

    def get_trend(self, tags: list[str], window_min: int = 60) -> list[Trend]:
        cut = NOW - timedelta(minutes=window_min)
        return [t.model_copy(update={"points": [p for p in t.points if p.ts >= cut]}) for t in
                (self.trends[tag] for tag in tags)]

    def get_lot(self, lot_id: str) -> Lot:
        return self.lot


def systems(profiles: Sequence[Profile], swap: bool, **kw) -> Systems:
    """The two probes' trends; with ``swap`` the reference gets the first profile."""
    a, b = (profiles[1], profiles[0]) if swap else (profiles[0], profiles[1])
    first = {k: v[0] for k, v in kw.items()}
    second = {k: v[1] for k, v in kw.items()}
    if swap:
        first, second = second, first
    return Systems({TAGS[0]: trend(TAGS[0], a, **first), TAGS[1]: trend(TAGS[1], b, **second)})


both_orders = pytest.mark.parametrize("swap", [False, True], ids=["primary", "reference"])

# ----------------------------------------------------------------------------------------- the verdict

LIMIT = EXCURSION_LIMIT_C
VERDICT_CASES = {
    # The review's case: fresh, synchronous, stable, agreeing within 2 °C, one above the limit.
    "one_above_one_below": ((flat(6.0), flat(4.5)), Verdict.POSSIBLE_EXCURSION),
    "one_just_above": ((flat(LIMIT + 0.1), flat(LIMIT - 0.1)), Verdict.POSSIBLE_EXCURSION),
    "both_above": ((flat(6.0), flat(5.5)), Verdict.EXCURSION_CONFIRMED),
    "both_exactly_at_limit": ((flat(LIMIT), flat(LIMIT)), Verdict.PROBES_AGREE),
    # A sustained above-limit excursion that recovered: product was out of limit, not a harmless transient.
    "both_recovered_from_above": ((excursion(2.0, 7.0, start=45, end=25), excursion(2.1, 6.8, start=45, end=25)),
                                  Verdict.EXCURSION_RECOVERED),
    "one_recovered_from_above": ((excursion(2.0, 6.5, start=45, end=25), excursion(2.1, 4.6, start=45, end=25)),
                                 Verdict.POSSIBLE_EXCURSION),
    "both_crossed_one_still_above": ((flat(5.6), excursion(4.0, 6.5, start=45, end=25)),
                                     Verdict.EXCURSION_CONFIRMED),
    "sub_limit_transient": ((excursion(2.0, 4.5, start=45, end=25), excursion(2.1, 4.4, start=45, end=25)),
                            Verdict.TRANSIENT_RECOVERED),
    # No probe rose significantly; the old check mixed one probe's peak with the other's start.
    "no_probe_rose_enough": ((split(excursion(4.0, 4.6, start=45, end=25, ramp=1), flat(3.5), 20), flat(2.2)),
                             Verdict.PROBES_AGREE),
    "one_probe_rose_but_has_not_recovered": ((split(flat(2.2), flat(4.2), 30), flat(3.9)), Verdict.PROBES_AGREE),
}


@both_orders
@pytest.mark.parametrize("case", list(VERDICT_CASES))
def test_each_probe_is_judged_against_the_limit_on_its_own(case: str, swap: bool) -> None:
    profiles, verdict = VERDICT_CASES[case]
    a = assess_chiller(systems(profiles, swap), "CH4")
    assert a.diverging is False, a.summary_line()
    assert a.verdict == verdict, (a.verdict, a.headline)
    hot = [p for p in a.probes if (p.peak_c or 0) > LIMIT]
    if hot:  # anything above the limit is never reported as within limits or as needing no action
        assert "within limits" not in a.headline and "No product action" not in a.recommendation
        assert a.verdict in NEEDS_QA and a.recommendation.startswith(("Keep product on hold", "Treat"))
    if verdict == Verdict.POSSIBLE_EXCURSION:
        assert a.confidence_cap == "unconfirmed" and "unconfirmed" in a.headline
        assert hot[0].tag in a.headline


@both_orders
def test_one_probe_above_the_limit_reaches_the_conclusion_and_the_handover(swap: bool) -> None:
    s = systems((flat(6.0), flat(4.5)), swap)
    a = assess_chiller(s, "CH4")
    hot = TAGS[1] if swap else TAGS[0]
    assert code_conclusion([a]) == a.headline and hot in a.headline and "within limits" not in a.headline
    gate = gate_conclusion([a], "Both probes agree and are within limits.", "confirmed")
    assert gate.confidence == "unconfirmed" and gate.rewritten and gate.recommendations == [a.recommendation]
    s.lot.status = LotStatus.READY
    line = lot_line(s.lot, None, a)
    assert "not on hold, but the CH4 evidence check is 'possible excursion'" in line
    assert "check with QA before loading" in line
    assert required_followups([a]) == [a.recommendation]


@both_orders
def test_a_recovered_excursion_still_requires_qa(swap: bool) -> None:
    profiles = (excursion(2.0, 7.0, start=45, end=25), excursion(2.1, 6.8, start=45, end=25))
    a = assess_chiller(systems(profiles, swap), "CH4")
    assert a.verdict == Verdict.EXCURSION_RECOVERED and a.verdict not in UNCERTAIN
    assert "peak 7.0 °C" in a.headline and "back within" in a.headline
    lot = Systems({}).lot.model_copy(update={"status": LotStatus.READY})
    assert "evidence check is 'excursion recovered'" in lot_line(lot, None, a)
    assert required_followups([a]) == ["Keep product on hold until QA has assessed the time above limit."]


# ----------------------------------------------------------------------------- freshness and agreement


@both_orders
@pytest.mark.parametrize(("age", "quality", "fresh"), [
    (0, Quality.BAD, False),
    (int(FRESHNESS_WINDOW.total_seconds() // 60) + 1, Quality.GOOD, False),
    (int(FRESHNESS_WINDOW.total_seconds() // 60), Quality.GOOD, True),  # exactly at the window: still fresh
], ids=["bad", "older_than_window", "at_window"])
def test_each_probe_is_checked_for_freshness(swap: bool, age: int, quality: Quality, fresh: bool) -> None:
    s = systems((flat(2.0), flat(2.1)), swap, age_min=(age, 0), quality=(quality, Quality.GOOD))
    a = assess_chiller(s, "CH4")
    off = TAGS[1] if swap else TAGS[0]
    if fresh:
        assert a.stale_tags == [] and a.verdict == Verdict.INSUFFICIENT_DATA  # fresh, but not synchronous
        assert "not comparable" in a.headline
    else:
        assert a.stale_tags == [off] and a.verdict == Verdict.INSUFFICIENT_DATA and off in a.headline


@both_orders
@pytest.mark.parametrize(("gap", "diverging"), [
    (DIVERGENCE_LIMIT_C + 0.5, True), (DIVERGENCE_LIMIT_C, False), (DIVERGENCE_LIMIT_C - 0.1, False)])
def test_either_probe_reading_high_or_low_is_a_disagreement(swap: bool, gap: float, diverging: bool) -> None:
    for offset in (gap, -gap):  # the other probe above, then below
        a = assess_chiller(systems((flat(2.0 + offset), flat(2.0)), swap), "CH4")
        assert a.diverging is diverging and abs((a.mean_gap_c or 0) - gap) < 1e-9
        assert (a.verdict == Verdict.PROBES_DISAGREE) is diverging


# --------------------------------------------------------------------------------- release clearance


def held(minutes: float) -> QAHold:
    return QAHold(id="QH-0001", lot_id="L-2231", reason="Check", placed_by="QA Manager",
                  placed_at=NOW - timedelta(minutes=minutes))


HOLD = held(CLEARANCE_WINDOW_MIN + 10)


@both_orders
@pytest.mark.parametrize(("profiles", "reason"), [
    ((excursion(2.0, LIMIT + 0.1, start=20, end=10), excursion(2.0, LIMIT - 0.1, start=20, end=10)),
     "reached 5.1 °C"),
    ((flat(LIMIT + 0.1), flat(LIMIT - 0.5)), "reached 5.1 °C"),
    # Agreeing for the first 20 min and 3 °C apart for the last 10: the 30-min mean (1.0 °C) passes, but
    # the probes disagree now (the verdict says so too).
    ((split(flat(2.0), flat(5.0), 10), flat(2.0)), "differed by 3.0 °C"),
    ((split(flat(2.0), flat(-1.0), 10), flat(2.0)), "differed by 3.0 °C"),
], ids=["peak_just_above", "above_throughout", "recent_gap_above", "recent_gap_below"])
def test_release_clearance_checks_each_probe_at_every_minute(swap: bool, profiles, reason: str) -> None:
    s = systems(profiles, swap)
    c = release_clearance(s, "L-2231", HOLD)
    assert not c.ok and reason in c.reason, c.reason
    if "reached" in reason:
        assert (TAGS[1] if swap else TAGS[0]) in c.reason  # the probe that was out of limit is named


@both_orders
def test_release_clearance_needs_both_probes_fresh(swap: bool) -> None:
    s = systems((flat(2.0), flat(2.1)), swap, age_min=(6, 0))
    c = release_clearance(s, "L-2231", HOLD)
    assert not c.ok and f"{TAGS[1] if swap else TAGS[0]} is stale" in c.reason


@pytest.mark.parametrize(("minutes", "ok"), [(CLEARANCE_WINDOW_MIN - 1, False), (CLEARANCE_WINDOW_MIN, True)])
def test_release_needs_the_full_window_after_the_hold(minutes: int, ok: bool) -> None:
    c = release_clearance(systems((flat(LIMIT), flat(LIMIT - 1.9)), False), "L-2231", held(minutes))
    assert c.ok is ok, c.reason
    if ok:  # at the limit and at the agreement limit is still clear; the lines say what was verified
        assert c.lines[1].startswith("Probes agree: mean gap 1.9 °C, largest 1.9 °C")
        assert c.lines[2] == "Highest reading 5.0 °C (TT-401); limit 5.0 °C"
    else:
        assert f"a release needs {CLEARANCE_WINDOW_MIN} min of readings taken after the hold" in c.reason
