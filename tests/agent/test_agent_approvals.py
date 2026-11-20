"""Approvals: bound to source versions, executed exactly once, audited, role-checked."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from agent_helpers import SUPERVISOR, call, context, investigate_ch4, plant

from coldline.agent.evidence import CLEARANCE_WINDOW_MIN
from coldline.contracts import ApprovalStatus, LotStatus, Scenario


def _hero_with_proposals():
    p = plant(Scenario.PROBE_MISMATCH)
    ctx = context(p)
    investigate_ch4(ctx)
    call(ctx, "propose_work_order_amendment", wo_id="WO-1042", note="TT-401 6.8 °C vs TT-402 2.1 °C; loop-check.",
         rationale="New evidence for the open calibration ticket.", evidence_ids=["TT-401", "TT-402"])
    call(ctx, "propose_dispatch_change", lot_id="L-2231", new_due="07:00",
         reason="Wait for a handheld reference reading.", evidence_ids=["L-2231", "TT-401"])
    amend, dispatch = ctx.run.approvals
    return p, ctx, amend, dispatch


def _wo(p):
    return next(w for w in p.find_work_orders("CH4") if w.id == "WO-1042")


def test_proposals_bind_source_versions_and_idempotency_keys() -> None:
    _, _, amend, dispatch = _hero_with_proposals()
    assert amend.source_versions["WO-1042"] == 1
    assert set(dispatch.source_versions) == {"L-2231", "TT-401", "TT-402", "CH4", "incident"}
    assert amend.idempotency_key != dispatch.idempotency_key


def test_approve_executes_exactly_once() -> None:
    p, ctx, amend, _ = _hero_with_proposals()
    first = ctx.approvals.approve(amend.id, SUPERVISOR, p)
    second = ctx.approvals.approve(amend.id, SUPERVISOR, p)
    assert first.executed and not second.executed
    assert amend.status == ApprovalStatus.APPROVED and amend.decided_by == SUPERVISOR
    assert (_wo(p).version, len(_wo(p).notes)) == (2, 1)
    events = [e for e in p.audit_log(500) if e.action == "approve"]
    assert len(events) == 1 and events[0].run_id == ctx.run.run_id


def test_concurrent_approvals_execute_once() -> None:
    p, ctx, amend, _ = _hero_with_proposals()
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: ctx.approvals.approve(amend.id, SUPERVISOR, p), range(8)))
    assert sum(r.executed for r in results) == 1
    assert _wo(p).version == 2


def test_stale_evidence_expires_pending_approvals() -> None:
    p, ctx, amend, dispatch = _hero_with_proposals()
    due = p.get_lot("L-2231").dispatch_due
    p.make_stale("TT-402")
    result = ctx.approvals.approve(dispatch.id, SUPERVISOR, p)
    assert not result.executed and dispatch.status == ApprovalStatus.EXPIRED
    assert "TT-402" in result.message
    assert p.get_lot("L-2231").dispatch_due == due
    assert ctx.approvals.approve(amend.id, SUPERVISOR, p).approval.status == ApprovalStatus.EXPIRED
    assert "approval_expired" in [e.action for e in p.audit_log(500)]


def test_work_order_changed_elsewhere_expires_amendment() -> None:
    p, ctx, amend, _ = _hero_with_proposals()
    p.amend_work_order("WO-1042", "Technician note", "Maintenance Planner", expected_version=1)
    result = ctx.approvals.approve(amend.id, SUPERVISOR, p)
    assert not result.executed and amend.status == ApprovalStatus.EXPIRED
    assert len(_wo(p).notes) == 1


def test_decline_is_audited_and_changes_nothing() -> None:
    p, ctx, _, dispatch = _hero_with_proposals()
    due = p.get_lot("L-2231").dispatch_due
    result = ctx.approvals.decline(dispatch.id, SUPERVISOR, p, "Load can wait for QA anyway")
    assert dispatch.status == ApprovalStatus.DECLINED and not result.executed
    assert p.get_lot("L-2231").dispatch_due == due
    assert next(e for e in p.audit_log(500) if e.action == "decline").detail == "Load can wait for QA anyway"
    assert not ctx.approvals.approve(dispatch.id, SUPERVISOR, p).executed


def test_roles_are_enforced_and_agent_cannot_self_approve() -> None:
    p, ctx, _, dispatch = _hero_with_proposals()
    for actor in ("agent", "system", "Maintenance Planner"):
        with pytest.raises(PermissionError):
            ctx.approvals.approve(dispatch.id, actor, p)
    assert dispatch.status == ApprovalStatus.PENDING


def test_decline_needs_the_same_authority_as_approve() -> None:
    p, ctx, _, dispatch = _hero_with_proposals()
    for actor in ("QA Manager", "shift supervisor", "Shift Supervisor (acting)", "agent"):
        with pytest.raises(PermissionError):
            ctx.approvals.decline(dispatch.id, actor, p)
    assert dispatch.status == ApprovalStatus.PENDING  # a wrong-role veto cannot pre-empt the supervisor
    assert ctx.approvals.approve(dispatch.id, SUPERVISOR, p).executed


def test_identical_pending_proposal_is_not_duplicated() -> None:
    _, ctx, _, dispatch = _hero_with_proposals()
    call(ctx, "propose_dispatch_change", lot_id="L-2231", new_due="07:00",
         reason="Wait for a handheld reference reading.", evidence_ids=["L-2231", "TT-401"])
    assert len(ctx.approvals.list_all()) == 2
    assert [a.id for a in ctx.run.approvals].count(dispatch.id) == 1


def _held_lot(scenario: Scenario | None = None, minutes_after_hold: int = CLEARANCE_WINDOW_MIN + 1):
    """L-2231 on a QA hold, the plant run on for a while after it, then a fresh investigation of CH4."""
    p = plant(scenario)
    hold = p.place_qa_hold("L-2231", "Check", "QA Manager", [])
    p.tick(minutes_after_hold * 60)
    ctx = context(p)
    investigate_ch4(ctx)
    return p, ctx, hold


def _propose_release(ctx):
    return call(ctx, "propose_release_qa_hold", lot_id="L-2231", rationale="Handheld reading confirmed 2.0 °C.",
                evidence_ids=["L-2231", "TT-402"])


def test_release_is_refused_without_verified_clearance() -> None:
    """On the probe mismatch, a release resting on a reading the model only described (not one verified in
    code) is refused before any proposal exists, so no QA click can release L-2231, and the hold stays."""
    p, ctx, _ = _held_lot(Scenario.PROBE_MISMATCH)
    out = _propose_release(ctx)
    assert out.steps[0].title == "Evidence precondition failed: release needs verified clearance"
    assert "TT-401 and TT-402 disagree" in out.steps[0].summary
    assert not ctx.run.approvals and p.get_lot("L-2231").status == LotStatus.HOLD


@pytest.mark.parametrize(("minutes", "stale", "reason"), [
    (5, None, "a release needs 30 min of readings taken after the hold"),
    (CLEARANCE_WINDOW_MIN + 1, "TT-402", "TT-402 is stale"),
], ids=["too_soon_after_the_hold", "stale_reference_probe"])
def test_release_needs_fresh_readings_taken_after_the_hold(minutes: int, stale: str | None, reason: str) -> None:
    p = plant()
    p.place_qa_hold("L-2231", "Check", "QA Manager", [])
    p.tick(minutes * 60)
    if stale:
        p.make_stale(stale)
    ctx = context(p)
    investigate_ch4(ctx)
    out = _propose_release(ctx)
    assert out.steps[0].title == "Evidence precondition failed: release needs verified clearance"
    assert reason in out.steps[0].summary, out.steps[0].summary
    assert not ctx.run.approvals


def test_verified_clearance_lets_qa_release_and_shows_what_was_checked() -> None:
    p, ctx, hold = _held_lot()
    _propose_release(ctx)
    (release,) = ctx.run.approvals
    assert release.change == [f"Release QA hold {hold.id} on L-2231 (CH4); L-2231 becomes ready to ship."]
    assert len(release.checks) == 3 and "all after " + hold.id in release.checks[0]
    assert release.checks[1].startswith("Probes agree: mean gap")
    with pytest.raises(PermissionError):
        ctx.approvals.approve(release.id, SUPERVISOR, p)
    assert ctx.approvals.approve(release.id, "QA Manager", p).executed
    assert p.get_lot("L-2231").status.value == "ready"


def test_clearance_is_checked_again_when_qa_decides() -> None:
    p, ctx, _ = _held_lot()
    _propose_release(ctx)
    (release,) = ctx.run.approvals
    p.make_stale("TT-402")  # the reference probe stops reporting after the proposal
    result = ctx.approvals.approve(release.id, "QA Manager", p)
    assert not result.executed and release.status == ApprovalStatus.EXPIRED
    assert "release clearance no longer holds: TT-402 is stale" in result.message
    assert p.get_lot("L-2231").status == LotStatus.HOLD


def test_clearance_check_runs_inside_the_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing else about the evidence changed: the clearance re-check alone refuses the release."""
    from coldline.agent import approvals as approvals_module
    from coldline.agent.evidence import Clearance

    p, ctx, _ = _held_lot()
    _propose_release(ctx)
    (release,) = ctx.run.approvals
    monkeypatch.setattr(approvals_module, "release_clearance", lambda *_a, **_k: Clearance(
        lot_id="L-2231", asset_id="CH4", ok=False, reason="TT-401 reached 5.4 °C in the last 30 min"))
    result = ctx.approvals.approve(release.id, "QA Manager", p)
    assert not result.executed and "TT-401 reached 5.4 °C" in result.message
    assert p.get_lot("L-2231").status == LotStatus.HOLD


async def test_a_shared_proposal_keeps_references_only_to_retained_runs() -> None:
    from agent_helpers import settings

    from coldline.agent.service import MAX_RUNS_PER_SESSION, AgentService

    svc = AgentService(settings())
    p = plant(Scenario.PROBE_MISMATCH)
    total = MAX_RUNS_PER_SESSION + 5
    for _ in range(total):  # the same investigation again and again: every run reuses the same proposals
        await svc.run_to_completion("s", p)
    retained = {r.run_id for r in svc.list_runs("s")}
    assert len(retained) == MAX_RUNS_PER_SESSION
    for a in svc.list_approvals("s", ApprovalStatus.PENDING):
        assert a.run_ids[0] == a.run_id == "R-7841" and a.run_id not in retained  # the origin is kept
        assert set(a.run_ids[1:]) <= retained and len(a.run_ids) <= MAX_RUNS_PER_SESSION + 1
        assert a.run_count == total


def test_a_token_from_another_store_generation_is_a_stale_session() -> None:
    from coldline.agent.approvals import StaleDecision

    p = plant(Scenario.PROBE_MISMATCH)
    first, second = context(p), context(p)
    for ctx in (first, second):
        investigate_ch4(ctx)
        call(ctx, "propose_dispatch_change", lot_id="L-2231", new_due="07:00",
             reason="Wait for a handheld reference reading.", evidence_ids=["L-2231", "TT-401"])
    old, new = first.run.approvals[0], second.run.approvals[0]
    assert old.id == new.id and old.payload == new.payload and old.decision_token != new.decision_token
    with pytest.raises(StaleDecision) as exc:
        second.approvals.approve(new.id, SUPERVISOR, p, token=old.decision_token)
    assert exc.value.session_expired and new.status == ApprovalStatus.PENDING
    with pytest.raises(StaleDecision) as exc:
        second.approvals.decline(new.id, SUPERVISOR, p, token="")
    assert exc.value.session_expired
    assert second.approvals.approve(new.id, SUPERVISOR, p, token=new.decision_token).executed
