from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from coldline.contracts import AuditEvent, LotStatus, PlantSystems, Scenario
from coldline.systems import Sandbox, SessionManager, VersionConflict, create_plant
from coldline.systems.sessions import SessionCapacityError


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
    after = p.snapshot()
    assert before.model_dump(exclude={"active_scenario", "incident"}) == after.model_dump(
        exclude={"active_scenario", "incident"}
    )
    assert after.incident == before.incident + 1 and after.physical_scenario is None
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
    c = manager.get("c")  # at capacity: evicts the least recently used sandbox (b), not the oldest (a)
    assert "b" not in manager and manager.get("a") is a
    manager.reset("c")
    assert c.audit_log()[-1].action == "reset"
    wall += 50
    assert manager.get("c") is not c
    with pytest.raises(ValueError):
        manager.get("")


def test_capacity_never_evicts_a_running_investigation() -> None:
    busy = {"a"}
    manager = SessionManager(3600, 2, clock=lambda: 0.0, is_busy=lambda sid: sid in busy)
    a = manager.get("a")
    manager.get("b")
    manager.get("c")  # a is least recently used but busy, so b goes
    assert manager.get("a") is a and "b" not in manager
    busy.add("c")
    with pytest.raises(SessionCapacityError):
        manager.get("d")
    assert "a" in manager and "c" in manager


def test_one_address_cannot_displace_other_visitors() -> None:
    evicted: list[str] = []
    manager = SessionManager(3600, 10, max_per_origin=2, clock=lambda: 0.0, on_evict=evicted.append)
    manager.get("other", origin="10.0.0.9")
    for i in range(5):
        manager.get(f"cookie-{i}", origin="10.0.0.1")
    assert "other" in manager and len(manager) == 3
    assert evicted == ["cookie-0", "cookie-1", "cookie-2"]


def test_history_is_bounded_and_ids_are_never_reused() -> None:
    from coldline.systems.plant import AUDIT_LIMIT, HISTORY_LIMIT

    p = create_plant()
    for _ in range(HISTORY_LIMIT + 5):
        p.inject(Scenario.POISONED_EMAIL)
    ids = [m.id for m in p.inbox()]
    assert len(ids) == HISTORY_LIMIT and ids[-1] == f"MSG-{HISTORY_LIMIT + 5:04}"
    for _ in range(AUDIT_LIMIT):
        p.tick(1)
    log = p.audit_log(AUDIT_LIMIT + 10)
    assert len(log) == AUDIT_LIMIT and log[-1].id == f"AU-{AUDIT_LIMIT + HISTORY_LIMIT + 5:05}"
    for _ in range(HISTORY_LIMIT + 5):
        p.inject(Scenario.COMPRESSOR_TRIP)
    alarms = p.list_alarms(active_only=False)
    assert len(alarms) == HISTORY_LIMIT + 1 and sum(a.active for a in alarms) == 1


def test_public_api_allowlist() -> None:
    protocol = {k for k in vars(PlantSystems) if not k.startswith("_")}
    assert {k for k in dir(Sandbox) if not k.startswith("_")} == protocol | {"advance_wall_time"}


def test_a_recently_used_sandbox_is_kept_and_the_newcomer_refused() -> None:
    """At capacity, a second address's arrival must not evict a visitor who is still active: a sandbox used
    within the protection window stays, and the newcomer is refused instead."""
    wall = 0.0
    manager = SessionManager(3600, 1, protect_recent_s=600, clock=lambda: wall)
    first = manager.get("first", origin="203.0.113.7")
    wall = 300.0
    with pytest.raises(SessionCapacityError):
        manager.get("second", origin="198.51.100.2")
    assert manager.get("first", origin="203.0.113.7") is first
    wall = 300.0 + 601  # idle past the window: now it can make room
    manager.get("second", origin="198.51.100.2")
    assert "first" not in manager


def test_a_visitor_whose_sandbox_was_cleared_is_told_why_once() -> None:
    wall = 0.0
    manager = SessionManager(100, 1, clock=lambda: wall)
    manager.get("a")
    manager.get("b")  # capacity: a is cleared
    manager.get("a")  # a comes back to a fresh plant (b is cleared in turn)
    assert manager.take_notice("a") == "capacity" and manager.take_notice("a") == ""
    wall = 200.0  # past the TTL
    manager.get("a")
    assert manager.take_notice("a") == "idle"
    assert manager.take_notice("never-seen") == ""
