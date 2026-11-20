"""Deterministic evidence checks. Python decides what the data supports; the model never does.

Freshness and divergence come from :mod:`coldline.sim.analysis` (the same functions the plant mimic uses),
so the agent, the UI and the tests agree on what "stale" and "disagree" mean. This module adds the
verdict for a chiller, the confidence cap that verdict imposes on any conclusion, and citation parsing.

All temperature limits here are illustrative for the demo, not regulatory values.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from coldline.contracts import Asset, PlantSystems, Quality, Trend
from coldline.sim import analysis

EXCURSION_LIMIT_C = 5.0  # illustrative product air-temperature limit
DIVERGENCE_WINDOW_MIN = 15  # matches the plant's "Δ > 2.0 °C (15 min)" alarm
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


CONFIDENCE_CAP = {
    Verdict.PROBES_AGREE: "confirmed",
    Verdict.EXCURSION_CONFIRMED: "confirmed",
    Verdict.TRANSIENT_RECOVERED: "confirmed",
    Verdict.PROBES_DISAGREE: "unconfirmed",
    Verdict.SINGLE_PROBE: "unconfirmed",
    Verdict.INSUFFICIENT_DATA: "insufficient_data",
}
UNCERTAIN = {Verdict.PROBES_DISAGREE, Verdict.SINGLE_PROBE, Verdict.INSUFFICIENT_DATA}
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

    def short(self) -> str:
        value = "n/a" if self.latest_c is None else f"{self.latest_c:.1f} °C"
        state = "fresh" if self.fresh else f"{self.quality.value}, {self.age_min:.0f} min old"
        return f"{self.tag} {value} ({state})"


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

    latest = [p.latest_c or 0.0 for p in probes]
    peaks = [p.peak_c or 0.0 for p in probes]
    starts = [p.start_c or 0.0 for p in probes]
    if min(latest) > EXCURSION_LIMIT_C:
        return Assessment(
            **base,
            verdict=Verdict.EXCURSION_CONFIRMED,
            headline=f"Confirmed excursion - both {asset.name} probes above {EXCURSION_LIMIT_C:.1f} °C.",
            recommendation="Keep product on hold until QA has assessed the time above limit.",
        )
    if max(peaks) - max(latest) >= RECOVERY_MARGIN_C and max(peaks) - min(starts) >= RISE_SIGNIFICANT_C:
        return Assessment(
            **base,
            verdict=Verdict.TRANSIENT_RECOVERED,
            headline=f"Transient rise in {asset.name} - recovered, probes agree.",
            recommendation="No product action needed; keep monitoring.",
        )
    return Assessment(
        **base,
        verdict=Verdict.PROBES_AGREE,
        headline=f"{asset.name} probes agree and are within limits.",
    )


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
}


def wording_consistent(text: str, verdict: Verdict) -> bool:
    """Does the model's own conclusion already preserve the uncertainty the evidence requires?"""
    lowered = text.lower().replace("unconfirmed", "")
    if CAUSE_CLAIM.search(lowered):
        return False
    required = _REQUIRED_WORDS.get(verdict)
    return required is None or any(word in text.lower() for word in required)


def gate_conclusion(
    assessments: list[Assessment], model_conclusion: str, model_confidence: str
) -> GateResult:
    """Cap the model's confidence at what the evidence supports and enforce uncertainty wording."""
    model_confidence = model_confidence if model_confidence in _RANK else "unconfirmed"
    worst = min(assessments, key=lambda a: _RANK[a.confidence_cap], default=None)
    cap = worst.confidence_cap if worst else "confirmed"
    confidence = min(model_confidence, cap, key=_RANK.__getitem__)
    conclusion = model_conclusion.strip()
    rewritten = False
    if worst is not None and worst.verdict in UNCERTAIN:
        # When the evidence is uncertain, the headline is fixed by code: no invented diagnosis survives.
        # "rewritten" records whether the model's own wording would have overstated the evidence.
        rewritten = not wording_consistent(conclusion, worst.verdict)
        conclusion = worst.headline
    elif not conclusion and worst is not None:
        conclusion = worst.headline
    return GateResult(
        conclusion=conclusion,
        confidence=confidence,
        cap=cap,
        rewritten=rewritten,
        model_conclusion=model_conclusion,
        model_confidence=model_confidence,
        recommendations=[a.recommendation for a in assessments if a.verdict in UNCERTAIN and a.recommendation],
        assessments=assessments,
    )


_CITATION = re.compile(r"\[([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\](?!\()")


def extract_citations(markdown: str) -> list[str]:
    """Record ids cited as ``[ID]`` (markdown links ``[text](url)`` are ignored)."""
    seen: list[str] = []
    for match in _CITATION.finditer(markdown):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen
