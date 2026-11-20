"""Approvals: bound to source versions, executed exactly once, audited, role-checked."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from agent_helpers import SUPERVISOR, call, context, investigate_ch4, plant

from coldline.contracts import ApprovalStatus, Scenario


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


def test_release_proposal_needs_qa_manager() -> None:
    p = plant(Scenario.PROBE_MISMATCH)
    p.place_qa_hold("L-2231", "Check", "QA Manager", [])
    ctx = context(p)
    investigate_ch4(ctx)
    call(ctx, "propose_release_qa_hold", lot_id="L-2231", rationale="Handheld reading confirmed 2.0 °C.",
         evidence_ids=["L-2231", "TT-402"])
    (release,) = ctx.run.approvals
    with pytest.raises(PermissionError):
        ctx.approvals.approve(release.id, SUPERVISOR, p)
    assert ctx.approvals.approve(release.id, "QA Manager", p).executed
    assert p.get_lot("L-2231").status.value == "ready"
