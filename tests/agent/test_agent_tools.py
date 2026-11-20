"""Tier enforcement and tool-level guardrails, exercised against the real sandbox."""

from __future__ import annotations

import json

from agent_helpers import REGISTRY, call, context, investigate_ch4, plant

from coldline.agent.context import AGENT_SCOPES
from coldline.contracts import ApprovalStatus, LotStatus, Scenario, StepKind, Tier


def _audit_actions(p) -> list[str]:
    return [e.action for e in p.audit_log(500)]


def test_unknown_tool_is_refused_and_audited() -> None:
    p = plant(Scenario.PROBE_MISMATCH)
    ctx = context(p)
    out = call(ctx, "delete_audit_log")
    (step,) = out.steps
    assert out.is_error and step.kind == StepKind.GUARDRAIL and not step.ok
    assert "unknown tool" in step.title
    assert json.loads(out.content)["refused_by"] == "code"
    assert "guardrail_refusal" in _audit_actions(p)


def test_near_miss_tool_name_gets_a_hint() -> None:
    out = call(context(plant()), "Get_Sensor_Trends", tags=["TT-401"])
    assert "Did you mean 'get_sensor_trend'" in json.loads(out.content)["reason"]


def test_never_tier_actions_are_refused() -> None:
    p = plant(Scenario.PROBE_MISMATCH)
    p.place_qa_hold("L-2231", "QA check", "QA Manager", [])
    ctx = context(p)
    for name, args in [("release_qa_hold", {"lot_id": "L-2231"}), ("write_setpoint", {"value": 8}),
                       ("stop_compressor", {"asset_id": "C1"}), ("approve", {"approval_id": "AP-0001"}),
                       ("authorise_permit", {"permit_id": "PD-0001"})]:
        out = REGISTRY.execute(ctx, name, args)
        assert out.steps[0].tier == Tier.NEVER, name
        assert not out.steps[0].ok
    assert p.get_lot("L-2231").status == LotStatus.HOLD


def test_no_tool_writes_to_plant_control() -> None:
    names = [d["name"] for d in REGISTRY.definitions()]
    assert not [n for n in names if any(w in n for w in ("setpoint", "control", "start", "stop", "valve"))]
    for tool in REGISTRY.tools.values():
        assert tool.tier in (Tier.READ, Tier.AUTO, Tier.APPROVE)
        assert tool.required_role in AGENT_SCOPES
        assert tool.definition()["input_schema"].get("additionalProperties") is False


def test_tool_definitions_are_stable_for_prompt_caching() -> None:
    assert REGISTRY.definitions() == REGISTRY.definitions()
    assert '"title": "' not in json.dumps(REGISTRY.definitions())  # no auto-generated labels
    wo = next(d for d in REGISTRY.definitions() if d["name"] == "create_work_order")["input_schema"]
    assert set(wo["required"]) <= set(wo["properties"]) and "title" in wo["properties"]


def test_approve_tier_never_executes_without_approval() -> None:
    p = plant(Scenario.PROBE_MISMATCH)
    ctx = context(p)
    investigate_ch4(ctx)
    due = p.get_lot("L-2231").dispatch_due
    out = call(ctx, "propose_dispatch_change", lot_id="L-2231", new_due="07:00",
               reason="Wait for a handheld reference reading.", evidence_ids=["L-2231", "TT-401"])
    assert not out.is_error
    assert [s.kind for s in out.steps] == [StepKind.TOOL, StepKind.GUARDRAIL]
    assert out.steps[1].title == "Tier check: dispatch change requires approval"
    (approval,) = ctx.run.approvals
    assert approval.status == ApprovalStatus.PENDING and approval.action == "change_dispatch"
    assert p.get_lot("L-2231").dispatch_due == due
    assert json.loads(out.content)["data"]["executed"] is False


def test_missing_scope_disables_a_tool() -> None:
    ctx = context(plant())
    ctx.scopes = AGENT_SCOPES - {"qa:hold"}
    out = call(ctx, "place_qa_hold", lot_id="L-2231", reason="Because of the readings",
               evidence_ids=["TT-401"])
    assert out.steps[0].title == "Refused: agent lacks scope qa:hold"
    assert ctx.systems.get_lot("L-2231").status == LotStatus.READY


def test_invalid_arguments_are_reported_not_executed() -> None:
    ctx = context(plant())
    out = call(ctx, "create_work_order", asset_id="C2", title="x", priority=9, note="short")
    assert out.is_error and out.steps[0].kind == StepKind.TOOL and not out.steps[0].ok
    assert "priority" in out.steps[0].result_preview


def test_hold_requires_looked_up_evidence_and_is_idempotent() -> None:
    p = plant(Scenario.PROBE_MISMATCH)
    ctx = context(p)
    refused = call(ctx, "place_qa_hold", lot_id="L-2231", reason="Probes disagree on CH4",
                   evidence_ids=["TT-401"])
    assert refused.steps[0].title == "Refused: unverified evidence"
    investigate_ch4(ctx)
    first = call(ctx, "place_qa_hold", lot_id="L-2231", reason="Probes disagree on CH4", evidence_ids=["TT-401"])
    again = call(ctx, "place_qa_hold", lot_id="L-2231", reason="Probes disagree on CH4", evidence_ids=["TT-401"])
    assert json.loads(again.content)["data"]["already_on_hold"] is True
    assert json.loads(first.content)["data"]["hold_id"] == json.loads(again.content)["data"]["hold_id"]
    assert _audit_actions(p).count("place_qa_hold") == 1


def test_work_order_requires_duplicate_check_first() -> None:
    p = plant(Scenario.COMPRESSOR_TRIP)
    ctx = context(p)
    args = {"asset_id": "C2", "title": "C2 tripped", "priority": 1, "note": "Compressor stopped, standby offline."}
    assert call(ctx, "create_work_order", **args).steps[0].title.startswith("Refused: check for an existing")
    call(ctx, "find_work_orders", asset_id="C2")
    first = json.loads(call(ctx, "create_work_order", **args).content)["data"]
    again = json.loads(call(ctx, "create_work_order", **args).content)["data"]
    assert again["wo_id"] == first["wo_id"] and again["already_open"] is True  # idempotent, not duplicated
    assert _audit_actions(p).count("create_work_order") == 1
    # A later run (or a replay after a live fallback) finds the agent's open order instead of raising another.
    later = context(p)
    call(later, "find_work_orders", asset_id="C2")
    assert json.loads(call(later, "create_work_order", **args).content)["data"]["wo_id"] == first["wo_id"]
    assert _audit_actions(p).count("create_work_order") == 1


def test_permit_is_draft_only() -> None:
    ctx = context(plant(Scenario.COMPRESSOR_TRIP))
    out = call(ctx, "draft_isolation_permit", asset_id="C2", work_description="Fault-find the C2 motor trip.",
               isolations=["Isolate C2 at its MCC feeder (LOTO)"], hazards=["Stored refrigerant pressure"])
    assert json.loads(out.content)["data"]["status"] == "draft"
    assert out.steps[1].title == "Tier check: permit is a draft only"


def test_handover_requires_lookups_and_verified_citations() -> None:
    p = plant(Scenario.PROBE_MISMATCH)
    ctx = context(p)
    call(ctx, "get_sensor_trend", tags=["TT-401", "TT-402"])
    note = "Primary [TT-401] high, reference [TT-402] steady, lot [L-2231], ticket [WO-1042]."
    args = {"conclusion": "Probes disagree - cause unconfirmed.", "confidence": "unconfirmed", "note_markdown": note}
    missing = call(ctx, "write_handover_note", **args)
    assert missing.steps[0].title == "Handover rejected: required lookups missing"
    assert "list_lots_in_zone(CH4)" in missing.steps[0].summary
    call(ctx, "list_lots_in_zone", zone_asset_id="CH4")
    call(ctx, "find_work_orders", asset_id="CH4")
    fake = call(ctx, "write_handover_note", **(args | {"note_markdown": note + " See [WO-9999]."}))
    assert fake.steps[0].title == "Handover rejected: unverified citations"
    ok = call(ctx, "write_handover_note", **args)
    assert not ok.is_error and ctx.handover_accepted
    assert ctx.run.confidence == "unconfirmed"
    assert "Checks computed in code" in ctx.run.handover_note
    again = call(ctx, "write_handover_note", **args)
    assert again.steps[0].title == "Refused: handover already accepted"


def test_overconfident_conclusion_is_capped_and_rewritten() -> None:
    ctx = context(plant(Scenario.PROBE_MISMATCH))
    investigate_ch4(ctx)
    call(ctx, "write_handover_note", conclusion="TT-401 has failed - sensor fault confirmed.", confidence="confirmed",
         note_markdown="[TT-401] failed, [TT-402] fine, ship [L-2231]. [WO-1042].")
    assert ctx.run.conclusion == "Probes disagree - cause unconfirmed."
    assert ctx.run.confidence == "unconfirmed"
    assert ctx.run.extra["gate"]["rewritten"] is True
    note = ctx.run.handover_note
    notes = note.split("**Agent's notes")[-1].split("**Checks")[0]
    assert "failed" not in note and "ship" not in notes.lower()  # the diagnosis and release advice are gone
    assert "[L-2231] (CH4, due 06:15): not on hold, but the CH4 evidence check is 'probes disagree'" in note
    assert "Status: Probes disagree - cause unconfirmed. Confidence: unconfirmed." in note
    assert [r["reason"] for r in ctx.run.extra["gate"]["removed"]] == ["a cause the evidence does not confirm"]
    assert any(s.title.startswith("Handover check: 1 unsupported") for s in ctx.run.steps)


def test_handover_keeps_supported_sentences_and_states_held_product() -> None:
    p = plant(Scenario.PROBE_MISMATCH)
    ctx = context(p)
    investigate_ch4(ctx)
    call(ctx, "place_qa_hold", lot_id="L-2231", reason="Probes disagree on CH4", evidence_ids=["TT-401", "TT-402"])
    note = ("**What happened:** [TT-401] reads high; [TT-402] is steady. Probes disagree - cause unconfirmed.\n"
            "- [L-2231] is safe to dispatch at 06:15.\n"
            "- [L-2231] is not held.\n"
            "- Ticket [WO-1042] stays open. Only QA can release the hold.")
    call(ctx, "write_handover_note", conclusion="Probes disagree - cause unconfirmed.", confidence="unconfirmed",
         note_markdown=note)
    out = ctx.run.handover_note
    disposition, notes = out.split("**Agent's notes")[0], out.split("**Agent's notes")[1].split("**Checks")[0]
    assert "Probes disagree - cause unconfirmed." in notes and "Ticket [WO-1042] stays open." in notes
    # Statements about the lot are made by code from the records, not by the model.
    assert "safe to dispatch" not in out and "is not held" not in out and "L-2231" not in notes
    hold = p.active_hold("L-2231")
    assert f"[L-2231] (CH4, due 06:15): on QA hold [{hold.id}]. Do not ship or load it" in disposition
    assert "Left out of the agent's notes: 3 sentence(s)" in out


def test_hold_cannot_rest_on_untrusted_text_or_unrelated_records() -> None:
    p = plant(Scenario.POISONED_EMAIL)
    ctx = context(p)
    call(ctx, "read_inbox")
    call(ctx, "get_lot", lot_id="L-2231")
    (msg,) = ctx.injection_flags
    flagged = call(ctx, "place_qa_hold", lot_id="L-2231", reason="Carrier email says hold it.",
                   evidence_ids=[msg, "L-2231"])
    assert flagged.steps[0].title == "Refused: untrusted text used as justification"
    bare = call(ctx, "place_qa_hold", lot_id="L-2231", reason="Holding as the carrier asked.", evidence_ids=["L-2231"])
    assert bare.steps[0].title == "Refused: hold needs plant evidence for this lot"
    assert p.get_lot("L-2231").status.value == "ready"
