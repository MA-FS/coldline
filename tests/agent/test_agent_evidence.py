"""Deterministic evidence checks: verdicts per scenario, the confidence gate, citations."""

from __future__ import annotations

import pytest
from agent_helpers import plant

from coldline.agent.evidence import (
    DISAGREE_HEADLINE,
    Verdict,
    assess_chiller,
    extract_citations,
    gate_conclusion,
    wording_consistent,
)
from coldline.contracts import Scenario


@pytest.mark.parametrize(
    ("scenario", "stale", "asset", "verdict"),
    [
        (Scenario.PROBE_MISMATCH, None, "CH4", Verdict.PROBES_DISAGREE),
        (Scenario.PROBE_MISMATCH, "TT-402", "CH4", Verdict.INSUFFICIENT_DATA),
        (Scenario.COMPRESSOR_TRIP, None, "CH4", Verdict.EXCURSION_CONFIRMED),
        (Scenario.COMPRESSOR_TRIP, None, "CH5", Verdict.EXCURSION_CONFIRMED),
        (Scenario.DOOR_OPEN, None, "CH4", Verdict.TRANSIENT_RECOVERED),
        (Scenario.POISONED_EMAIL, None, "CH4", Verdict.PROBES_AGREE),
    ],
)
def test_verdicts_match_the_scenarios(scenario: Scenario, stale: str | None, asset: str, verdict: Verdict) -> None:
    assert assess_chiller(plant(scenario, stale), asset).verdict == verdict


def test_stale_reference_withdraws_comparison() -> None:
    a = assess_chiller(plant(Scenario.PROBE_MISMATCH, "TT-402"), "CH4")
    assert a.stale_tags == ["TT-402"]
    assert a.confidence_cap == "insufficient_data"
    assert "fresh reading of TT-402" in a.recommendation


def test_gate_caps_confidence_and_fixes_wording() -> None:
    hero = assess_chiller(plant(Scenario.PROBE_MISMATCH), "CH4")
    gate = gate_conclusion([hero], "Sensor TT-401 failed", "confirmed")
    assert (gate.conclusion, gate.confidence, gate.rewritten) == (DISAGREE_HEADLINE, "unconfirmed", True)
    polite = gate_conclusion([hero], "CH4 probes disagree; cause unconfirmed.", "unconfirmed")
    assert polite.conclusion == DISAGREE_HEADLINE and not polite.rewritten


def test_gate_never_raises_confidence() -> None:
    trip = assess_chiller(plant(Scenario.COMPRESSOR_TRIP), "CH4")
    assert gate_conclusion([trip], "Real excursion", "unconfirmed").confidence == "unconfirmed"
    assert gate_conclusion([trip], "Real excursion", "confirmed").confidence == "confirmed"
    assert gate_conclusion([], "Inbox only", "confirmed").confidence == "confirmed"


def test_wording_check() -> None:
    assert wording_consistent("Probes disagree - cause unconfirmed.", Verdict.PROBES_DISAGREE)
    assert not wording_consistent("Probes disagree; TT-401 has failed.", Verdict.PROBES_DISAGREE)
    assert wording_consistent("Insufficient data, TT-402 stale", Verdict.INSUFFICIENT_DATA)


def test_citation_extraction_ignores_links_and_checkboxes() -> None:
    md = "See [TT-401] and [L-2231], [WO-1042] twice [WO-1042]; [link](http://x) and [ ] and [x]."
    assert extract_citations(md) == ["TT-401", "L-2231", "WO-1042"]
