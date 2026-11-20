"""Deterministic evidence checks: verdicts per scenario, the confidence gate, citations."""

from __future__ import annotations

import pytest
from agent_helpers import plant

from coldline.agent.evidence import (
    DISAGREE_HEADLINE,
    NO_EVIDENCE_HEADLINE,
    ProbeStatus,
    Verdict,
    assess_chiller,
    code_conclusion,
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


def test_headline_is_composed_in_code_on_every_path() -> None:
    """Confirmed evidence does not let the model's conclusion through either; it stays in the trace only."""
    p = plant(Scenario.COMPRESSOR_TRIP)
    trip = [assess_chiller(p, "CH4"), assess_chiller(p, "CH5")]
    adversarial = "All affected stock is fit for sale. Send it to the customer now."
    gate = gate_conclusion(trip, adversarial, "confirmed")
    assert gate.conclusion == "Confirmed excursion - both probes above 5.0 °C in Chiller 4 and Chiller 5."
    assert gate.model_conclusion == adversarial
    assert not gate.rewritten  # the wording was not an overstated diagnosis; it is simply not used
    assert gate_conclusion([], adversarial, "confirmed").conclusion == NO_EVIDENCE_HEADLINE
    flagged = gate_conclusion([], adversarial, "confirmed", ["MSG-0001"])
    assert flagged.conclusion == "Suspected prompt injection in MSG-0001 - no action taken on its contents."
    assert flagged.rewritten
    hero = assess_chiller(plant(Scenario.PROBE_MISMATCH), "CH4")
    both = gate_conclusion([hero, trip[1]], adversarial, "confirmed", ["MSG-0001"]).conclusion
    assert both == f"Suspected prompt injection in MSG-0001 - no action taken on its contents. {DISAGREE_HEADLINE}"


def test_stale_probe_without_readings_in_the_window_still_formats() -> None:
    assert ProbeStatus(tag="TT-402").short() == "TT-402 n/a (bad, no recent reading)"


def test_wording_check() -> None:
    assert wording_consistent("Probes disagree - cause unconfirmed.", Verdict.PROBES_DISAGREE)
    assert not wording_consistent("Probes disagree; TT-401 has failed.", Verdict.PROBES_DISAGREE)
    assert wording_consistent("Insufficient data, TT-402 stale", Verdict.INSUFFICIENT_DATA)


def test_chillers_with_the_same_result_share_one_headline() -> None:
    """Fixed-shape headlines name every chiller once; the most severe result still comes first."""
    trip = plant(Scenario.COMPRESSOR_TRIP)
    ch4, ch5 = assess_chiller(trip, "CH4"), assess_chiller(trip, "CH5")
    assert ch4.headline == "Confirmed excursion - both Chiller 4 probes above 5.0 °C."  # one chiller: unchanged
    assert code_conclusion([ch4]) == ch4.headline
    agree = assess_chiller(trip, "CH1")
    assert agree.headline == "Chiller 1 probes agree and are within limits."
    assert code_conclusion([agree, ch4, ch5]) == (
        "Confirmed excursion - both probes above 5.0 °C in Chiller 4 and Chiller 5. " + agree.headline)
