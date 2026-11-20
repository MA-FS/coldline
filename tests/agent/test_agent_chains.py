"""Scenarios chained in one sandbox: approvals, replays and handovers bind to the plant as it is now.

Visitors rarely start from a clean plant: holds, proposals, work orders and inbox messages from one fault
are still there when they inject the next. These tests replay the shipped recordings in sequence and check
that decisions, side effects and the handover follow the current records, not the recorded ones.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from agent_helpers import SUPERVISOR, call, context, investigate_ch4, plant, settings, turn

from coldline.agent import handover
from coldline.agent.approvals import Source, record_version
from coldline.agent.model import ScriptedModelClient
from coldline.agent.service import AgentService
from coldline.contracts import ApprovalStatus, LotStatus, RunStatus, Scenario, StepKind

M, T, D, E = Scenario.PROBE_MISMATCH, Scenario.COMPRESSOR_TRIP, Scenario.DOOR_OPEN, Scenario.POISONED_EMAIL


def replay_service() -> AgentService:
    return AgentService(settings(live_enabled=False, api_key_present=False))


async def chain(*steps: Scenario | str):
    """Inject each scenario (or "stale") into one sandbox and run the agent after each, like the web does."""
    svc, p = replay_service(), plant()
    runs = []
    for step in steps:
        if step == "stale":
            p.make_stale("TT-402")
            scenario = p.snapshot().active_scenario
        else:
            p.inject(step)
            scenario = step
        svc.revalidate_approvals("s", p)
        runs.append(await svc.run_to_completion("s", p, scenario=scenario))
        runs[-1].extra["test_inbox_count"] = len(p.inbox())
    return svc, p, runs


def _dispatch(run):
    return next(a for a in run.approvals if a.action == "change_dispatch")


# --------------------------------------------------------------------------- superseded evidence


async def test_dispatch_proposal_expires_when_a_new_fault_supersedes_its_evidence() -> None:
    svc, p, (mismatch, trip) = await chain(M, T)
    original = _dispatch(mismatch)
    due = p.get_lot("L-2231").dispatch_due
    result = svc.approve("s", original.id, SUPERVISOR, p)
    assert not result.executed and original.status == ApprovalStatus.EXPIRED
    assert "incident" in original.note
    assert p.get_lot("L-2231").dispatch_due == due  # the obsolete proposal changed nothing
    assert trip.approvals and all(a.status == ApprovalStatus.PENDING for a in trip.approvals)


def test_verdict_change_expires_a_proposal_even_with_fresh_probes() -> None:
    mismatch, trip = plant(M), plant(T)
    verdict = Source("verdict", "CH4")
    # Both plants have fresh probes on CH4, so probe quality alone cannot tell them apart; the verdict can.
    probe = Source("probe", "TT-402")
    assert record_version(mismatch, probe) == record_version(trip, probe)
    assert record_version(mismatch, verdict) != record_version(trip, verdict)
    ctx = context(mismatch)
    investigate_ch4(ctx)
    call(ctx, "propose_dispatch_change", lot_id="L-2231", new_due="09:00",
         reason="Wait for a handheld reference reading.", evidence_ids=["L-2231", "TT-401", "TT-402"])
    (dispatch,) = ctx.run.approvals
    assert dispatch.source_versions["CH4"] == record_version(mismatch, verdict)
    dispatch.source_versions["CH4"] = record_version(trip, verdict)  # as if bound on the other plant
    assert ctx.approvals.approve(dispatch.id, SUPERVISOR, mismatch).approval.status == ApprovalStatus.EXPIRED
    assert "evidence check went from" in dispatch.note


def test_delayed_decision_rechecks_that_the_new_dispatch_time_is_still_ahead() -> None:
    p = plant(M)
    ctx = context(p)
    investigate_ch4(ctx)
    call(ctx, "propose_dispatch_change", lot_id="L-2231", new_due="07:00",
         reason="Wait for a handheld reference reading.", evidence_ids=["L-2231", "TT-401"])
    (dispatch,) = ctx.run.approvals
    p._sim.tick((p.get_lot("L-2231").dispatch_due + timedelta(hours=1) - p.now()).total_seconds())
    result = ctx.approvals.approve(dispatch.id, SUPERVISOR, p)
    assert not result.executed and "already passed" in result.message


def test_release_proposal_names_the_hold_it_releases() -> None:
    p = plant()
    first = p.place_qa_hold("L-2231", "Check", "QA Manager", [])
    p.tick(31 * 60)  # verified clearance: 30 minutes of fresh, agreeing readings after the hold
    ctx = context(p)
    investigate_ch4(ctx)
    call(ctx, "propose_release_qa_hold", lot_id="L-2231", rationale="Handheld reading confirmed 2.0 °C.",
         evidence_ids=["L-2231", "TT-402"])
    (release,) = ctx.run.approvals
    assert release.payload["hold_id"] == first.id
    p.release_qa_hold("L-2231", "QA Manager")  # released and re-held by someone else: a different hold
    p.place_qa_hold("L-2231", "New concern", "QA Manager", [])
    result = ctx.approvals.approve(release.id, "QA Manager", p)
    assert not result.executed and release.status == ApprovalStatus.EXPIRED
    assert p.get_lot("L-2231").status == LotStatus.HOLD


# --------------------------------------------------------------------------- chained replays


async def test_chained_trip_binds_alarm_ids_to_the_current_plant() -> None:
    _, p, (_, trip) = await chain(M, T)
    active = [a.id for a in p.list_alarms(active_only=True)]
    assert active == ["AL-0002"]
    writes = {s.title: json.dumps(s.args) for s in trip.steps if s.title in {"create_work_order",
                                                                           "draft_isolation_permit"}}
    assert set(writes) == {"create_work_order", "draft_isolation_permit"}
    assert all("AL-0002" in text and "AL-0001" not in text for text in writes.values())
    assert "[AL-0002]" in trip.handover_note and "AL-0001" not in trip.handover_note


async def test_email_during_an_active_fault_still_ends_with_a_handover() -> None:
    _, _, (_, email) = await chain(M, E)
    assert email.handover_note and email.status != RunStatus.INCOMPLETE
    assert email.extra["recording"]["id"] == "probe_mismatch"
    assert "selected_because" in email.extra["recording"]
    assert "injection" in email.conclusion.lower() and "Probes disagree" in email.conclusion
    assert "[MSG-0001]" in email.handover_note


async def test_handover_never_contradicts_the_inbox_it_read() -> None:
    _, _, runs = await chain(E, M)
    mismatch = runs[-1]
    assert "inbox is empty" not in mismatch.handover_note.lower()
    assert "[MSG-0001]" in mismatch.handover_note
    assert mismatch.extra["replay_divergence"]  # the recorded wording described an empty inbox
    assert any(s.title.startswith("Replay check") for s in mismatch.steps)


async def test_fresh_replays_keep_the_recorded_wording() -> None:
    for scenario in (M, T, D, E):
        _, _, (run,) = await chain(scenario)
        assert "replay_divergence" not in run.extra, scenario
        assert "**Replay note:**" not in run.handover_note and handover.NARRATIVE_HEADING in run.handover_note, scenario
        # Only decision statements leave the recorded narrative (the disposition says them from the records).
        assert handover.contradictions(run.extra["gate"]["removed"]) == [], (scenario, run.extra["gate"])


@pytest.mark.parametrize("steps", [(M, T, D, E, M), (E, M, "stale", T), (T, T, M, M), (D, M, E, "stale")])
async def test_every_run_in_a_chain_has_a_consistent_handover(steps) -> None:
    _, p, runs = await chain(*steps)
    for run in runs:
        assert run.handover_note, (run.run_id, run.conclusion)
        assert run.status in {RunStatus.DONE, RunStatus.AWAITING_APPROVAL}, run.status
        dumped = json.dumps([s.model_dump(mode="json") for s in run.steps]) + run.handover_note
        assert "{{" not in dumped
        assert not [s for s in run.steps if s.kind == StepKind.ERROR]
        if run.extra["test_inbox_count"]:
            assert "inbox is empty" not in run.handover_note.lower()
    active = {a.id for a in p.list_alarms(active_only=True)}
    cited_alarms = {i for i in json.dumps(runs[-1].handover_note).split("[") if i.startswith("AL-")}
    assert all(c.split("]")[0] in active for c in cited_alarms)


async def test_repeated_trip_reuses_the_open_work_order_and_permit_draft() -> None:
    _, p, (first, second) = await chain(T, T)
    agent_orders = [w for w in p.find_work_orders("C2") if w.created_by == "agent"]
    assert len(agent_orders) == 1
    assert len([d for d in p.list_permits("C2") if d.drafted_by == "agent"]) == 1
    assert second.extra["created"]["wo_created:C2"] == first.extra["created"]["wo_created:C2"]
    assert f"[{agent_orders[0].id}]" in second.handover_note


# --------------------------------------------------------------------------- shared proposals


async def test_one_decision_settles_every_run_that_proposed_it() -> None:
    svc, p = replay_service(), plant(M)
    first = await svc.run_to_completion("s", p)
    second = await svc.run_to_completion("s", p, scenario=M)  # same incident, same evidence
    assert {a.id for a in second.approvals} == {a.id for a in first.approvals}
    for approval in first.approvals:
        assert approval.run_ids == [first.run_id, second.run_id]
        svc.approve("s", approval.id, SUPERVISOR, p)
    assert first.status == RunStatus.DONE and second.status == RunStatus.DONE
    again = svc.approve("s", first.approvals[0].id, SUPERVISOR, p)
    assert not again.executed and "Already approved" in again.message  # exactly once is preserved


# --------------------------------------------------------------------------- fallback keeps the ledger


async def test_live_failure_right_after_a_write_does_not_repeat_it() -> None:
    lookups = [("get_sensor_trend", {"tags": ["TT-401", "TT-402"]}),
               ("get_sensor_trend", {"tags": ["TT-501", "TT-502"]}),
               ("list_lots_in_zone", {"zone_asset_id": "CH4"}), ("list_lots_in_zone", {"zone_asset_id": "CH5"}),
               ("find_work_orders", {"asset_id": "C2"})]
    writes = [("create_work_order", {"asset_id": "C2", "title": "C2 tripped", "priority": 1,
                                     "note": "Compressor stopped, standby offline."}),
              ("draft_isolation_permit", {"asset_id": "C2", "work_description": "Repair the C2 trip.",
                                          "isolations": ["Isolate C2 at its MCC (LOTO)"],
                                          "hazards": ["Stored refrigerant pressure"]})]
    turns = [turn("Checking.", *lookups), turn("Raising the order.", *writes), turn("", error="model API error 529")]
    svc = AgentService(settings(), live_client_factory=lambda s: ScriptedModelClient(turns))
    p = plant(T)
    run = await svc.run_to_completion("s", p, mode="live")
    assert run.mode == "replay" and run.handover_note
    assert len([w for w in p.find_work_orders("C2") if w.created_by == "agent"]) == 1
    assert len(p.list_permits("C2")) == 1
    assert run.extra["created"]["wo_created:C2"] == "WO-1043"
