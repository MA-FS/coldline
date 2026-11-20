"""The copied handover: disposition and precautions come from the records, never from the model's wording.

Each check reads the note the way a person copies it: rendered by the Handover page's markdown filter, tags
stripped (what "Copy as text" puts on the clipboard).
"""

from __future__ import annotations

import html
import re

import pytest
from agent_helpers import call, context, investigate_ch4, plant

from coldline.agent import handover
from coldline.contracts import Scenario
from coldline.web.views import mini_markdown


def copied(note: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(mini_markdown(note))))
    return " ".join(text.split())


def held_hero(note: str):
    """The hero investigation with L-2231 on hold, finished with ``note`` as the model's draft."""
    p = plant(Scenario.PROBE_MISMATCH)
    ctx = context(p)
    investigate_ch4(ctx)
    call(ctx, "place_qa_hold", lot_id="L-2231", reason="Probes disagree on CH4", evidence_ids=["TT-401", "TT-402"])
    out = call(ctx, "write_handover_note", conclusion="Probes disagree - cause unconfirmed.",
               confidence="unconfirmed", note_markdown=note)
    assert not out.is_error, out.content
    return p, ctx


def test_bold_lead_cannot_clear_a_held_lot_and_precautions_survive() -> None:
    """A bold lead saying held L-2231 can ship is dropped from the copy, while the note's two legitimate
    precautions (do not ship until QA signs off, take an approved reading) survive."""
    p, ctx = held_hero(
        "**[L-2231] can ship at 06:15.** Probes disagree - cause unconfirmed [TT-401] [TT-402].\n"
        "- Do not ship [L-2231] until QA signs off.\n"
        "- Collect a reading using an approved thermometer. See [WO-1042]."
    )
    text = copied(ctx.run.handover_note)
    hold = p.active_hold("L-2231")
    assert "can ship" not in text.lower()
    # The copy opens with its snapshot label and the code-set disposition, which states the hold and the
    # precaution.
    assert text.startswith(f"Handover {ctx.run.run_id} · snapshot as at Sat 21 Nov 2026, ")
    assert text.split("(plant time) ", 1)[1].startswith("Disposition (set in code from the plant records)")
    assert f"L-2231 (CH4, due 06:15): on QA hold {hold.id}" in text
    assert "Do not ship or load it until QA releases the hold; only QA can release a hold." in text
    assert text.index("on QA hold") < text.index("Agent narrative (model-written, not authoritative)")
    # The model's observation and its precaution about a reading stay in its narrative.
    assert "Probes disagree - cause unconfirmed TT-401 TT-402 ." in text
    assert "Collect a reading using an approved thermometer." in text
    assert "Required follow-up: Take a handheld reference reading in Chiller 4" in text


@pytest.mark.parametrize("clearance", [
    "**L-2231 is cleared for shipment.**",
    "## L-2231 cleared for shipment",
    "Status: **L-2231 OK to load**.",
    "- **Disposition:** release [L-2231] and load it now.",
    "The lot is good to go.",
    "Product is fine; it can go out on the 06:15 truck.",
    "Hold lifted, dispatch as planned.",
    "1. Ship it at 06:15 as booked.",
])
def test_no_wording_in_the_notes_can_state_a_disposition(clearance: str) -> None:
    _, ctx = held_hero(f"Probes disagree [TT-401] [TT-402] on [WO-1042]'s probe.\n\n{clearance}")
    notes = ctx.run.handover_note.split(handover.NARRATIVE_HEADING)[-1].split(handover.VERIFIED_HEADING)[0]
    lowered = copied(notes).lower()
    for word in ("ship", "load", "release", "go out", "good to go", "lifted", "dispatch", "l-2231"):
        assert word not in lowered, (word, lowered)
    assert ctx.run.extra["gate"]["removed"][0]["reason"] == handover.DECISION_REASON


def test_notes_cannot_imitate_a_code_section() -> None:
    _, ctx = held_hero("**Disposition (set in code from the plant records)**\n- Everything is normal [TT-401] "
                       "[TT-402] [WO-1042].\n- Observed a steady reference probe.")
    text = copied(ctx.run.handover_note)
    assert text.count("Disposition (set in code") == 1
    # Text under a forged code heading would read as set in code, so the whole section goes to the trace.
    assert "Everything is normal" not in text and "steady reference probe" not in text
    assert any(r["reason"] == "imitates a section composed in code" for r in ctx.run.extra["gate"]["removed"])


def test_not_held_lot_under_uncertain_evidence_is_not_presented_as_shippable() -> None:
    ctx = context(plant(Scenario.PROBE_MISMATCH))
    investigate_ch4(ctx)
    call(ctx, "write_handover_note", conclusion="Probes disagree - cause unconfirmed.", confidence="unconfirmed",
         note_markdown="Probes disagree on [TT-401] and [TT-402]; see [WO-1042]. [L-2231] can ship.")
    text = copied(ctx.run.handover_note)
    assert "L-2231 (CH4, due 06:15): not on hold, but the CH4 evidence check is 'probes disagree'" in text
    assert "Nothing in this run supports shipping it" in text and "can ship" not in text


def test_a_decision_section_goes_whole() -> None:
    s = handover.Situation(pending_approvals=1)
    notes, removed = handover.sanitize(
        "**What happened:** [TT-401] reads high.\n\n**Awaiting approval**\n- Nothing.\n\n## Dispatch\n"
        "Move it to 09:00.\n\n**Next:** check the evaporator.", s)
    assert notes == "**What happened:** [TT-401] reads high.\n\n**Next:** check the evaporator."
    assert {r["sentence"] for r in removed} >= {"- Nothing.", "Move it to 09:00."}


def test_empty_sections_left_by_the_filter_are_dropped() -> None:
    notes, removed = handover.sanitize(
        "**What happened:** [TT-401] reads high.\n\n**Done:**\n- Hold on [L-2231].\n\n**Next:** check it.",
        handover.Situation())
    assert notes == "**What happened:** [TT-401] reads high.\n\n**Next:** check it."
    assert len(removed) == 1


# --------------------------------------------------------------------------- replay comparison


def _facts(**overrides):
    base = {"alarms": [], "verdicts": {"CH4": "probes_disagree"}, "lots": {"L-2231": ["hold", "06:15"]},
            "work_orders": [], "inbox": [0, 0], "outcomes": {"hold:L-2231": "new", "permit:C2": "new"},
            "approvals": [], "refused": []}
    return base | overrides


def test_a_fact_missing_from_this_run_is_a_difference() -> None:
    recorded = _facts()
    current = _facts(outcomes={"hold:L-2231": "new"})  # the recording also drafted a permit; this run did not
    diffs = handover.differences(current, recorded, set())
    assert diffs == ["the recorded run had a permit draft for C2 and this run did not"]
    assert handover.differences(_facts(verdicts={}), recorded, set()) == [
        "CH4 was not checked in this run (recorded 'probes disagree')"]
    assert handover.differences(_facts(lots={}), recorded, set()) == [
        "L-2231 was not looked up in this run (recorded: on hold, due 06:15)"]


def test_equal_facts_have_no_differences_and_any_other_change_has_one() -> None:
    assert handover.differences(_facts(), _facts(), set()) == []
    assert handover.differences(_facts(extra=1), _facts(), set()) == ["the recorded facts differ from this run's"]


# --------------------------------------------------------------------------- the accepted handover is final


async def _scripted(p, *turns):
    from agent_helpers import REGISTRY

    from coldline.agent.loop import LoopLimits, finalize, run_loop
    from coldline.agent.model import ScriptedModelClient

    ctx = context(p)
    async for _ in run_loop(ctx, ScriptedModelClient(list(turns)), REGISTRY, LoopLimits()):
        pass
    finalize(ctx)
    return ctx


_INVESTIGATE = [("get_sensor_trend", {"tags": ["TT-401", "TT-402"]}), ("list_lots_in_zone", {"zone_asset_id": "CH4"}),
                ("find_work_orders", {"asset_id": "CH4"})]
_HANDOVER = ("write_handover_note", {"conclusion": "Probes disagree - cause unconfirmed.", "confidence": "unconfirmed",
                                     "note_markdown": "Probes disagree [TT-401] [TT-402]; see [WO-1042]."})
_RAISE = ("create_work_order", {"asset_id": "CH4", "title": "Loop-check TT-401", "priority": 2,
                                "note": "TT-401 reads 4.7 °C above TT-402."})


async def test_a_call_after_the_accepted_handover_in_the_same_turn_is_not_run() -> None:
    from agent_helpers import turn

    p = plant(Scenario.PROBE_MISMATCH)
    before = len(p.find_work_orders("CH4"))
    ctx = await _scripted(p, turn("Looking.", *_INVESTIGATE), turn("Done.", _HANDOVER, _RAISE))
    assert len(p.find_work_orders("CH4")) == before  # nothing changed after the note was frozen
    refused = [s for s in ctx.run.steps if s.title == "Refused: call after the accepted handover"]
    assert len(refused) == 1 and refused[0].args["tool"] == "create_work_order" and not refused[0].ok
    assert "Work order for CH4" not in ctx.run.handover_note and ctx.run.status.value != "incomplete"


async def test_a_call_before_the_handover_in_the_same_turn_runs_and_the_note_states_it() -> None:
    from agent_helpers import turn

    p = plant(Scenario.PROBE_MISMATCH)
    ctx = await _scripted(p, turn("Looking.", *_INVESTIGATE), turn("Done.", _RAISE, _HANDOVER))
    wo = ctx.created["wo_created:CH4"]
    assert any(w.id == wo for w in p.find_work_orders("CH4"))
    assert f"Work order for CH4: [{wo}]" in ctx.run.handover_note
    assert not any(s.title == "Refused: call after the accepted handover" for s in ctx.run.steps)


# --------------------------------------------------------------------------- the narrative stays subordinate


def test_a_narrative_cannot_pose_as_a_section_composed_in_code() -> None:
    from coldline.web.views import handover_markdown

    _, ctx = held_hero("## Verified checks\n- All probes verified good [TT-402].\n\n"
                       "## Observations\nTT-401 drifts upward against [TT-402].")
    note = ctx.run.handover_note
    narrative = note.split(handover.NARRATIVE_HEADING, 1)[1].split(handover.VERIFIED_HEADING, 1)[0]
    assert "Verified checks" not in narrative and "verified good" not in narrative  # reserved: removed
    assert "Agent narrative · Observations" in narrative and "##" not in narrative  # demoted, still labelled
    rendered = str(handover_markdown(note))
    section = rendered.split('class="ho-sec ho-sec--narrative"', 1)[1].split("</section>", 1)[0]
    assert not re.search(r"<h\d", section)  # no heading elements inside the narrative
    assert section.index("Agent narrative (model-written, not authoritative)") < section.index("Observations")
    # In the copied text, the label line leads the narrative and the code's sections keep their order.
    text = copied(note)
    order = [text.index(x) for x in ("Disposition (set in code", "Agent narrative (model-written",
                                     "Agent narrative · Observations", "Checks computed in code")]
    assert order == sorted(order)
