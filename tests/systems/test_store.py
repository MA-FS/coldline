from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from coldline.contracts import AuditEvent, LotStatus, PlantSystems, Scenario
from coldline.systems import Sandbox, SessionManager, VersionConflict, create_plant


def test_seeded_records_and_audited_writes() -> None:
    p = create_plant()
    lot = p.get_lot("L-2231")
    assert len(p.list_lots()) == 8
    assert (lot.zone_asset_id, lot.carcases, lot.customer) == ("CH4", 120, "Coastal Export Co")
    assert lot.dispatch_due.strftime("%H:%M") == "06:15"
    (wo,) = p.find_work_orders("CH4")
    assert wo.id == "WO-1042" and wo.created_at.date() == (p.now() - timedelta(days=1)).date()
    assert p.asset_history("C2")[0].created_at == p.now() - timedelta(weeks=6)
    p.place_qa_hold(lot.id, "Inspect", "agent", [wo.id])
    before = p.snapshot()
    p.inject(Scenario.POISONED_EMAIL)
    assert before.model_dump(exclude={"active_scenario"}) == p.snapshot().model_dump(
        exclude={"active_scenario"}
    )
    assert "ignore previous instructions" in p.inbox()[0].body
    assert p.get_lot(lot.id).status == LotStatus.HOLD
    p.release_qa_hold(lot.id, "Shift Supervisor")
    p.change_dispatch(lot.id, p.now() + timedelta(hours=2), "Shift Supervisor", "Inspect")
    new = p.create_work_order("C2", "Inspect", 2, "Motor check", "agent")
    p.amend_work_order(new.id, "Isolate", "Shift Supervisor", 1)
    assert (
        p.draft_permit("C2", "Inspect", ["Electrical isolation"], ["Live energy"], "agent").status == "draft"
    )
    p.make_stale("TT-402")
    p.tick()
    p.audit(AuditEvent(id="external", ts=p.now(), action="approve", actor="Shift Supervisor", target=wo.id))
    p.reset()
    events = p.audit_log()
    assert [e.action for e in events] == [
        "place_qa_hold",
        "inject_fault",
        "release_qa_hold",
        "change_dispatch",
        "create_work_order",
        "amend_work_order",
        "draft_permit",
        "make_stale",
        "tick",
        "approve",
        "reset",
    ]
    assert len({e.id for e in events}) == len(events)
    events[0].evidence_ids.clear()
    assert p.audit_log()[0].evidence_ids == [wo.id]


def test_version_conflict_atomic() -> None:
    p = create_plant()

    def amend(i: int) -> bool:
        try:
            p.amend_work_order("WO-1042", str(i), "Shift Supervisor", 1)
            return True
        except VersionConflict as exc:
            assert "expected version 1, current version 2" in str(exc)
            return False

    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(amend, range(8))) == 1
    assert len(p.audit_log()) == 1 and p.find_work_orders("CH4")[0].version == 2


def test_session_lifetime_and_wall_clock() -> None:
    wall = 0.0
    manager = SessionManager(50, 2, clock=lambda: wall)
    a, b = manager.get("a"), manager.get("b")
    a.inject(Scenario.POISONED_EMAIL)
    assert b.inbox() == []
    wall = 40.0
    a.advance_wall_time(10)
    assert a.now() == b.now() + timedelta(seconds=10)
    assert manager.get("a") is a
    c = manager.get("c")
    assert manager.get("b") is b and manager.get("a") is not a
    manager.reset("c")
    assert c.audit_log()[-1].action == "reset"
    wall += 50
    assert manager.get("c") is not c
    with pytest.raises(ValueError):
        manager.get("")


def test_public_api_allowlist() -> None:
    protocol = {k for k in vars(PlantSystems) if not k.startswith("_")}
    assert {k for k in dir(Sandbox) if not k.startswith("_")} == protocol | {"advance_wall_time"}
