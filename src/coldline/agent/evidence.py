"""Deterministic evidence checks. Python decides what the data supports; the model never does.

Freshness and divergence come from :mod:`coldline.sim.analysis` (the same functions the plant mimic uses),
so the agent, the UI and the tests agree on what "stale" and "disagree" mean. This module adds the
verdict for a chiller, the confidence cap that verdict imposes on any conclusion, and citation parsing.

All temperature limits here are illustrative for the demo, not regulatory values.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel

from coldline.contracts import Asset, AssetKind, PlantSystems, QAHold, Quality, Trend
from coldline.sim import analysis

EXCURSION_LIMIT_C = 5.0  # illustrative product air-temperature limit; a reading above it is out of limit
DIVERGENCE_WINDOW_MIN = analysis.DIVERGENCE_WINDOW_MIN  # matches the plant's "Δ > 2.0 °C (15 min)" alarm
DIVERGENCE_LIMIT_C = analysis.DIVERGENCE_LIMIT_C  # mean probe gap above which the probes disagree
CLEARANCE_WINDOW_MIN = 30  # minutes of verified readings, all taken after the hold, before a release
RECOVERY_MARGIN_C = 1.0  # peak minus latest that counts as "recovered"
RISE_SIGNIFICANT_C = 1.5  # rise over the window that counts as an event

CONFIDENCE_LEVELS = ("insufficient_data", "unconfirmed", "confirmed")
_RANK = {c: i for i, c in enumerate(CONFIDENCE_LEVELS)}


class Verdict(StrEnum):
    PROBES_AGREE = "probes_agree"
    EXCURSION_CONFIRMED = "excursion_confirmed"
    TRANSIENT_RECOVERED = "transient_recovered"
    PROBES_DISAGREE = "probes_disagree"
    SINGLE_PROBE = "single_probe"
    INSUFFICIENT_DATA = "insufficient_data"
    # Added after the others so stored approval versions (an index into this enum) keep their meaning.
    EXCURSION_RECOVERED = "excursion_recovered"  # both probes went above the limit and are back within it
    POSSIBLE_EXCURSION = "possible_excursion"  # one probe went above the limit and the other did not


CONFIDENCE_CAP = {
    Verdict.PROBES_AGREE: "confirmed",
    Verdict.EXCURSION_CONFIRMED: "confirmed",
    Verdict.TRANSIENT_RECOVERED: "confirmed",
    Verdict.PROBES_DISAGREE: "unconfirmed",
    Verdict.SINGLE_PROBE: "unconfirmed",
    Verdict.INSUFFICIENT_DATA: "insufficient_data",
    Verdict.EXCURSION_RECOVERED: "confirmed",
    Verdict.POSSIBLE_EXCURSION: "unconfirmed",
}
UNCERTAIN = {Verdict.PROBES_DISAGREE, Verdict.SINGLE_PROBE, Verdict.INSUFFICIENT_DATA, Verdict.POSSIBLE_EXCURSION}
# Verdicts under which nothing supports shipping the chiller's product until QA has looked at it: the
# evidence is uncertain, or product was above the limit at some point in the window.
NEEDS_QA = UNCERTAIN | {Verdict.EXCURSION_CONFIRMED, Verdict.EXCURSION_RECOVERED}
DISAGREE_HEADLINE = "Probes disagree - cause unconfirmed."


class ProbeStatus(BaseModel):
    tag: str
    description: str = ""
    latest_c: float | None = None
    latest_ts: datetime | None = None
    quality: Quality = Quality.BAD
    age_min: float | None = None
    start_c: float | None = None
    peak_c: float | None = None
    peak_ts: datetime | None = None

    @property
    def fresh(self) -> bool:
        return self.quality == Quality.GOOD

    @property
    def state(self) -> str:
        if self.fresh:
            return "fresh"
        if self.age_min is None:  # no reading at all inside the window
            return f"{self.quality.value}, no recent reading"
        return f"{self.quality.value}, {self.age_min:.0f} min old"

    def short(self) -> str:
        value = "n/a" if self.latest_c is None else f"{self.latest_c:.1f} °C"
        return f"{self.tag} {value} ({self.state})"


class Assessment(BaseModel):
    asset_id: str
    asset_name: str
    primary: str | None
    reference: str | None
    probes: list[ProbeStatus]
    mean_gap_c: float | None = None  # mean |primary - reference| over the divergence window
    diverging: bool | None = None
    verdict: Verdict
    headline: str
    recommendation: str = ""

    @property
    def confidence_cap(self) -> str:
        return CONFIDENCE_CAP[self.verdict]

    @property
    def stale_tags(self) -> list[str]:
        return [p.tag for p in self.probes if not p.fresh]

    @property
    def evidence_ids(self) -> list[str]:
        return [self.asset_id, *(p.tag for p in self.probes)]

    def summary_line(self) -> str:
        parts = [p.short() for p in self.probes]
        if self.mean_gap_c is not None:
            parts.append(f"mean gap {self.mean_gap_c:.1f} °C over {DIVERGENCE_WINDOW_MIN} min")
        return f"{' · '.join(parts)} -> {self.verdict.value.replace('_', ' ')}"

    def for_model(self) -> dict:
        """Compact, deterministic form included in tool results."""
        return {
            "asset_id": self.asset_id,
            "verdict": self.verdict.value,
            "confidence_cap": self.confidence_cap,
            "headline": self.headline,
            "recommendation": self.recommendation,
            "probes": {
                p.tag: {
                    "latest_c": p.latest_c,
                    "quality": p.quality.value,
                    "age_min": None if p.age_min is None else round(p.age_min, 1),
                }
                for p in self.probes
            },
            "mean_gap_c": None if self.mean_gap_c is None else round(self.mean_gap_c, 2),
            "computed_by": "code (not the model)",
        }


def probe_tags(asset: Asset) -> list[str]:
    """Air-temperature probes on a chiller: primary first, independent reference second."""
    return [t for t in asset.tags if t.startswith("TT-")][:2]


def probe_status(trend: Trend, now: datetime) -> ProbeStatus:
    points = [p for p in trend.points if p.value_c is not None]
    status = ProbeStatus(tag=trend.tag, description=trend.description)
    if not trend.points:
        return status
    latest = trend.points[-1]
    status.latest_c = latest.value_c
    status.latest_ts = latest.ts
    status.quality = analysis.freshness(latest, now)
    status.age_min = (now - latest.ts).total_seconds() / 60
    if points:
        status.start_c = points[0].value_c
        peak = max(points, key=lambda p: p.value_c or 0.0)
        status.peak_c, status.peak_ts = peak.value_c, peak.ts
    return status


def assess_chiller(systems: PlantSystems, asset_id: str, window_min: int = 60) -> Assessment:
    asset = systems.get_asset(asset_id)
    now = systems.now()
    tags = probe_tags(asset)
    trends = systems.get_trend(tags, window_min) if tags else []
    probes = [probe_status(t, now) for t in trends]
    base = {
        "asset_id": asset.id,
        "asset_name": asset.name,
        "primary": tags[0] if tags else None,
        "reference": tags[1] if len(tags) > 1 else None,
        "probes": probes,
    }

    stale = [p.tag for p in probes if not p.fresh]
    if not probes or stale:
        names = ", ".join(stale) or "probe data"
        return Assessment(
            **base,
            verdict=Verdict.INSUFFICIENT_DATA,
            headline=f"Insufficient data - {names} stale; comparison withdrawn.",
            recommendation=f"Request a fresh reading of {names} before drawing a conclusion.",
        )
    if len(probes) < 2:
        return Assessment(
            **base,
            verdict=Verdict.SINGLE_PROBE,
            headline=f"{asset.name} has a single probe - reading cannot be cross-checked.",
            recommendation=f"Take a handheld reference reading in {asset.name}.",
        )

    a, b = trends[0], trends[1]
    diverging = analysis.is_diverging(a, b, now, DIVERGENCE_WINDOW_MIN)
    mean_gap = analysis.divergence(a, b, DIVERGENCE_WINDOW_MIN)
    base |= {"diverging": diverging, "mean_gap_c": mean_gap}
    if diverging is None:
        return Assessment(
            **base,
            verdict=Verdict.INSUFFICIENT_DATA,
            headline="Insufficient data - probe samples are not comparable; comparison withdrawn.",
            recommendation=f"Request a fresh, synchronous reading of {a.tag} and {b.tag}.",
        )
    if diverging:
        return Assessment(
            **base,
            verdict=Verdict.PROBES_DISAGREE,
            headline=DISAGREE_HEADLINE,
            recommendation=(
                f"Take a handheld reference reading in {asset.name} and loop-check {a.tag} "
                "before releasing product."
            ),
        )

    return Assessment(**base, **_limit_verdict(asset.name, probes))


def _c(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} °C"


LIMIT_TEXT = f"{EXCURSION_LIMIT_C:.1f} °C"
# Headlines with one fixed shape per chiller, as (one chiller, several chillers). When a run checks several
# chillers with the same result, the conclusion names them in one sentence instead of repeating it.
_SHAPES = {
    Verdict.EXCURSION_CONFIRMED: (f"Confirmed excursion - both {{}} probes above {LIMIT_TEXT}.",
                                  f"Confirmed excursion - both probes above {LIMIT_TEXT} in {{}}."),
    Verdict.TRANSIENT_RECOVERED: ("Transient rise in {} - recovered, probes agree.",) * 2,
    Verdict.PROBES_AGREE: ("{} probes agree and are within limits.",) * 2,
}


def _limit_verdict(name: str, probes: list[ProbeStatus]) -> dict:
    """Verdict, headline and recommendation for two fresh probes that agree, judged probe by probe.

    Every probe is compared with the limit on its own: one probe above the limit is never averaged or
    outvoted away. Over the window (``peak_c``) and now (``latest_c``):

    - every probe above the limit now: a confirmed excursion;
    - every probe above the limit at some point: a confirmed excursion, still running if any probe is above
      now, otherwise recovered (QA still has to assess the time above the limit);
    - some probes above the limit and some not: the crossing is unconfirmed, which is treated as uncertain
      evidence, never as "within limits";
    - no probe above the limit: a transient if every probe that rose significantly has come back down,
      otherwise the probes agree and are within limits.
    """
    limit = LIMIT_TEXT
    above_now = [p for p in probes if p.latest_c is not None and p.latest_c > EXCURSION_LIMIT_C]
    crossed = [p for p in probes if p.peak_c is not None and p.peak_c > EXCURSION_LIMIT_C]
    hold = "Keep product on hold until QA has assessed the time above limit."
    if len(above_now) == len(probes):
        return {"verdict": Verdict.EXCURSION_CONFIRMED,
                "headline": _SHAPES[Verdict.EXCURSION_CONFIRMED][0].format(name),
                "recommendation": hold}
    if len(crossed) == len(probes):
        if above_now:
            still = " and ".join(f"{p.tag} {_c(p.latest_c)}" for p in above_now)
            return {"verdict": Verdict.EXCURSION_CONFIRMED,
                    "headline": f"Confirmed excursion - both {name} probes went above {limit}; {still} still "
                                "above it.",
                    "recommendation": hold}
        top = max(crossed, key=lambda p: p.peak_c or 0.0)
        at = f" at {top.peak_ts:%H:%M}" if top.peak_ts else ""
        return {"verdict": Verdict.EXCURSION_RECOVERED,
                "headline": f"Excursion in {name} - both probes went above {limit} (peak {_c(top.peak_c)} on "
                            f"{top.tag}{at}) and are back within it.",
                "recommendation": hold}
    if crossed:
        hot = crossed[0]
        cool = [p for p in probes if p not in crossed]
        hot_text = (f"{hot.tag} reads {_c(hot.latest_c)}" if hot in above_now
                    else f"{hot.tag} reached {_c(hot.peak_c)}")
        cool_text = " and ".join(f"{p.tag} peaked at {_c(p.peak_c)}" for p in cool)
        return {"verdict": Verdict.POSSIBLE_EXCURSION,
                "headline": f"Possible excursion in {name} - {hot_text}, above {limit}, but {cool_text}; "
                            "unconfirmed.",
                "recommendation": (f"Treat {name} product as possibly above the limit: take a handheld reference "
                                   f"reading in {name} and have QA assess it before releasing product.")}
    risen = [p for p in probes if p.peak_c is not None and p.start_c is not None
             and p.peak_c - p.start_c >= RISE_SIGNIFICANT_C]
    if risen and all(p.peak_c - (p.latest_c or 0.0) >= RECOVERY_MARGIN_C for p in risen):  # type: ignore[operator]
        return {"verdict": Verdict.TRANSIENT_RECOVERED,
                "headline": _SHAPES[Verdict.TRANSIENT_RECOVERED][0].format(name),
                "recommendation": "No product action needed; keep monitoring."}
    return {"verdict": Verdict.PROBES_AGREE, "headline": _SHAPES[Verdict.PROBES_AGREE][0].format(name)}


class GateResult(BaseModel):
    conclusion: str
    confidence: str
    cap: str
    rewritten: bool
    model_conclusion: str
    model_confidence: str
    recommendations: list[str]
    assessments: list[Assessment]


CAUSE_CLAIM = re.compile(
    r"\b(confirmed|failed|faulty|fault is|caused by|due to|definitely|root cause|is broken|has failed)\b"
)
_REQUIRED_WORDS = {
    Verdict.PROBES_DISAGREE: ("disagree", "unconfirmed"),
    Verdict.SINGLE_PROBE: ("unconfirmed", "cross"),
    Verdict.INSUFFICIENT_DATA: ("insufficient", "stale"),
    Verdict.POSSIBLE_EXCURSION: ("unconfirmed", "possible"),
}


def wording_consistent(text: str, verdict: Verdict) -> bool:
    """Does the model's own conclusion already preserve the uncertainty the evidence requires?"""
    lowered = text.lower().replace("unconfirmed", "")
    if CAUSE_CLAIM.search(lowered):
        return False
    required = _REQUIRED_WORDS.get(verdict)
    return required is None or any(word in text.lower() for word in required)


# Most to least severe, for ordering headlines when several chillers were checked.
_SEVERITY = (Verdict.INSUFFICIENT_DATA, Verdict.PROBES_DISAGREE, Verdict.POSSIBLE_EXCURSION, Verdict.SINGLE_PROBE,
             Verdict.EXCURSION_CONFIRMED, Verdict.EXCURSION_RECOVERED, Verdict.TRANSIENT_RECOVERED,
             Verdict.PROBES_AGREE)
NO_EVIDENCE_HEADLINE = "No chiller temperature evidence was checked in this run."


def evidence_cap(assessments: list[Assessment]) -> str:
    """The highest confidence the evidence checks allow (the least certain check decides)."""
    return min((a.confidence_cap for a in assessments), key=_RANK.__getitem__, default="confirmed")


def injection_headline(record_id: str) -> str:
    return f"Suspected prompt injection in {record_id} - no action taken on its contents."


def code_conclusion(assessments: list[Assessment], flagged: list[str] | tuple[str, ...] = ()) -> str:
    """The run's headline, composed only from the evidence checks and the injection flags.

    Uncertain evidence: the worst check's headline, so no diagnosis is implied. Otherwise every chiller's
    headline, most severe first. A run that read hostile text says so first. No model text is used: the
    model's own conclusion is kept in the trace (``GateResult.model_conclusion``)."""
    parts = [injection_headline(flagged[0])] if flagged else []
    worst = min(assessments, key=lambda a: _RANK[a.confidence_cap], default=None)
    if worst is not None and worst.verdict in UNCERTAIN:
        parts.append(worst.headline)
    else:
        parts += _headlines(sorted(assessments, key=lambda a: _SEVERITY.index(a.verdict)))
    return " ".join(dict.fromkeys(parts)) or NO_EVIDENCE_HEADLINE


def _names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _headlines(assessments: list[Assessment]) -> list[str]:
    """Each check's headline in order, with chillers that share a fixed-shape headline named in one sentence
    (at the place of the first of them)."""
    out: list[str | Verdict] = []
    shared: dict[Verdict, list[str]] = {}
    for a in assessments:
        shape = _SHAPES.get(a.verdict)
        if shape is None or a.headline != shape[0].format(a.asset_name):
            out.append(a.headline)
            continue
        if a.verdict not in shared:
            out.append(a.verdict)
        shared.setdefault(a.verdict, []).append(a.asset_name)
    return [_shared_headline(x, shared[x]) if isinstance(x, Verdict) else x for x in out]


def _shared_headline(verdict: Verdict, names: list[str]) -> str:
    one, several = _SHAPES[verdict]
    return one.format(names[0]) if len(names) == 1 else several.format(_names(names))


def gate_conclusion(
    assessments: list[Assessment], model_conclusion: str, model_confidence: str,
    flagged: list[str] | tuple[str, ...] = (),
) -> GateResult:
    """Cap the model's confidence at what the evidence supports; the headline itself is set by code.

    ``rewritten`` records whether the model's own wording would have overstated the evidence (a cause
    claimed on uncertain evidence, or hostile input it did not mention)."""
    model_confidence = model_confidence if model_confidence in _RANK else "unconfirmed"
    worst = min(assessments, key=lambda a: _RANK[a.confidence_cap], default=None)
    cap = evidence_cap(assessments)
    confidence = min(model_confidence, cap, key=_RANK.__getitem__)
    model_conclusion = model_conclusion.strip()
    rewritten = bool(flagged) and "injection" not in model_conclusion.lower()
    if worst is not None and worst.verdict in UNCERTAIN:
        rewritten = rewritten or not wording_consistent(model_conclusion, worst.verdict)
    return GateResult(
        conclusion=code_conclusion(assessments, flagged),
        confidence=confidence,
        cap=cap,
        rewritten=rewritten,
        model_conclusion=model_conclusion,
        model_confidence=model_confidence,
        recommendations=[a.recommendation for a in assessments if a.verdict in UNCERTAIN and a.recommendation],
        assessments=assessments,
    )


# --------------------------------------------------------------------------- release clearance


class Clearance(BaseModel):
    """Whether code can verify that a held lot's temperature evidence supports releasing it.

    ``reason`` says why not; ``lines`` are what was verified, shown with the proposal and re-checked when QA
    decides. It is a precondition for *proposing* a release, never a release: QA still decides, and remains
    accountable for anything code cannot see (a hold placed for a reason other than temperature)."""

    lot_id: str
    asset_id: str
    ok: bool
    reason: str = ""
    lines: list[str] = []


def release_clearance(systems: PlantSystems, lot_id: str, hold: QAHold,
                      window_min: int = CLEARANCE_WINDOW_MIN) -> Clearance:
    """Verified clearance for releasing ``hold`` on ``lot_id``: the lot's chiller has two independent probes,
    both fresh and good, sampled every minute for the last ``window_min`` minutes, every one of those minutes
    after the hold was placed, agreeing (the mean gap and the gap at every minute at or below the divergence
    limit) and each probe at or below the product limit throughout. Anything less fails with the reason. A
    model's account of a reading is never evidence."""
    lot = systems.get_lot(lot_id)
    zone = lot.zone_asset_id

    def fail(reason: str) -> Clearance:
        return Clearance(lot_id=lot.id, asset_id=zone, ok=False, reason=reason)

    try:
        asset = systems.get_asset(zone)
    except KeyError:
        return fail(f"{lot.id} is stored in {zone}, which has no plant record")
    tags = probe_tags(asset)
    if asset.kind != AssetKind.CHILLER or len(tags) < 2:
        return fail(f"{asset.name} has no independent probe pair, so code cannot verify the product temperature")
    now = systems.now()
    held_min = (now - hold.placed_at).total_seconds() / 60
    if held_min < window_min:
        return fail(f"{hold.id} was placed {held_min:.0f} min ago; a release needs {window_min} min of readings "
                    "taken after the hold")
    a, b = systems.get_trend(tags, window_min)
    for trend in (a, b):
        quality = analysis.freshness(trend.points[-1], now) if trend.points else Quality.BAD
        if quality != Quality.GOOD:
            return fail(f"{trend.tag} is {quality.value}; a release needs fresh readings from both probes")
    gap = analysis.divergence(a, b, window_min)
    if gap is None:
        return fail(f"{a.tag} and {b.tag} do not have a complete, synchronous reading for every minute of the "
                    f"last {window_min} min")
    end = max(a.points[-1].ts, b.points[-1].ts)
    start = end - timedelta(minutes=window_min - 1)
    if start < hold.placed_at:
        return fail(f"the last {window_min} min of readings start before {hold.id} was placed")
    values = {t.tag: [p.value_c for p in t.points if p.ts >= start and p.value_c is not None] for t in (a, b)}
    if gap > DIVERGENCE_LIMIT_C:
        return fail(f"{a.tag} and {b.tag} disagree: mean gap {gap:.1f} °C over {window_min} min "
                    f"(limit {DIVERGENCE_LIMIT_C:.1f} °C)")
    # The mean over the whole window can hide a recent disagreement (agreeing early, apart now), so every aligned
    # minute is checked too. That also covers the plant's 15-minute divergence rule at the end of the window.
    pairs = {p.ts: p.value_c for p in a.points if p.ts >= start}
    worst_ts, worst_gap = max(((p.ts, abs((p.value_c or 0.0) - (pairs[p.ts] or 0.0)))
                               for p in b.points if p.ts in pairs), key=lambda x: x[1])
    if worst_gap > DIVERGENCE_LIMIT_C:
        return fail(f"{a.tag} and {b.tag} differed by {worst_gap:.1f} °C at {worst_ts:%H:%M} in the last "
                    f"{window_min} min (limit {DIVERGENCE_LIMIT_C:.1f} °C)")
    tag, peak = max(((t, max(v)) for t, v in values.items()), key=lambda x: x[1])
    if peak > EXCURSION_LIMIT_C:
        return fail(f"{tag} reached {peak:.1f} °C in the last {window_min} min (limit {EXCURSION_LIMIT_C:.1f} °C)")
    return Clearance(lot_id=lot.id, asset_id=zone, ok=True, lines=[
        (f"{a.tag} and {b.tag} fresh and sampled every minute from {start:%H:%M} to {end:%H:%M}, all after "
        f"{hold.id} was placed at {hold.placed_at:%H:%M}"),
        (f"Probes agree: mean gap {gap:.1f} °C, largest {worst_gap:.1f} °C, over {window_min} min "
         f"(limit {DIVERGENCE_LIMIT_C:.1f} °C)"),
        f"Highest reading {peak:.1f} °C ({tag}); limit {EXCURSION_LIMIT_C:.1f} °C",
    ])
