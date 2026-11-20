"""Describe cited records for a person checking them: one title and detail line per record id.

Shared by the web pages (evidence tables, citations) and the handover, which stores the same description
of every record it cites at the moment it is accepted, so a later reader sees the sources as they were then
next to how they are now. Text that came from outside code (a work-order title, an inbox subject) is shown
as a labelled detail, never as the title.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime

from coldline.contracts import Approval, AssetKind, EvidenceRef, PlantSystems, Quality, WorkOrder

_EVIDENCE_KINDS = [
    (re.compile(r"^(TT|PT|TE|ZS|DS|EV\d+|C\d+)-"), "tag", "Historian tag"),
    (re.compile(r"^L-"), "lot", "Lot"),
    (re.compile(r"^WO-"), "wo", "Work order"),
    (re.compile(r"^(ALM|AL)-"), "alarm", "Alarm"),
    (re.compile(r"^(MSG|EM|IN)-"), "msg", "Inbox (untrusted)"),
    (re.compile(r"^(H|QH)-"), "hold", "QA hold"),
    (re.compile(r"^(PTW|PD|PERMIT)-"), "permit", "Permit draft"),
    (re.compile(r"^(EV|DOOR)-"), "event", "Event"),
    (re.compile(r"^R-"), "run", "Agent run"),
    (re.compile(r"^(A|AP|APR)-"), "approval", "Approval"),
    (re.compile(r"^(CH\d+|C\d+|BONING|DOCK)$"), "asset", "Asset"),
]


def evidence_kind(record_id: str) -> tuple[str, str]:
    """('tag', 'Historian tag') for 'TT-401'. Unknown ids -> ('record', 'Record')."""
    for rx, kind, label in _EVIDENCE_KINDS:
        if rx.match(record_id):
            return kind, label
    return "record", "Record"


def fmt_value(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    s = f"{v:.{digits}f}"
    return s.replace("-", "−")  # proper minus sign


def _hm(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt else "—"  # the plant clock is already plant local time


def _work_orders(plant: PlantSystems) -> dict[str, WorkOrder]:
    out: dict[str, WorkOrder] = {}
    for asset in plant.snapshot().assets:
        for wo in plant.find_work_orders(asset.id, open_only=False):
            out[wo.id] = wo
    return out


def describe_records(plant: PlantSystems, approvals: Mapping[str, Approval],
                     ids: list[str]) -> dict[str, EvidenceRef]:
    """Describe each record id from the plant's current state (unknown ids get their kind's label)."""
    out: dict[str, EvidenceRef] = {}
    snap = plant.snapshot()
    assets = {a.id: a for a in snap.assets}
    tag_owner = {t: a for a in snap.assets for t in a.tags}
    orders: dict[str, WorkOrder] | None = None
    alarms = {a.id: a for a in plant.list_alarms(active_only=False)}
    inbox = {m.id: m for m in plant.inbox()}
    holds = {h.id: h for h in plant.active_holds()}
    for rid in ids:
        kind, label = evidence_kind(rid)
        ref: EvidenceRef | None = None
        try:
            if rid in tag_owner:
                reading = plant.latest([rid])[rid]
                desc = plant.get_trend([rid], 0)[0].description
                state = "STALE" if reading.quality == Quality.STALE else reading.quality.value.upper()
                ref = EvidenceRef(id=rid, kind="tag", title=desc,
                                  detail=f"{fmt_value(reading.value_c)} °C · {state} · {_hm(reading.ts)}")
            elif rid in assets:
                a = assets[rid]
                state = ""
                if a.kind == AssetKind.COMPRESSOR:
                    state = a.state or ("run" if a.running else "standby")
                ref = EvidenceRef(id=rid, kind="asset", title=a.name, detail=state or a.zone.replace("_", " "))
            elif kind == "lot":
                lot = plant.get_lot(rid)
                ref = EvidenceRef(id=rid, kind="lot", title=f"Lot, {lot.carcases} carcases",
                                  detail=f"{lot.customer} · {lot.status.value} · due {_hm(lot.dispatch_due)}",
                                  version=lot.version)
            elif kind == "wo":
                orders = orders if orders is not None else _work_orders(plant)
                if rid in orders:
                    wo = orders[rid]
                    source = "written by the agent" if wo.created_by == "agent" else "CMMS ticket text"
                    ref = EvidenceRef(id=rid, kind="wo", title=f"Work order on {wo.asset_id}, P{wo.priority}",
                                      detail=f"{wo.status} · created {wo.created_at:%d %b} by {wo.created_by} · "
                                             f"title ({source}): “{wo.title}”",
                                      version=wo.version)
            elif kind == "hold":
                hold = holds.get(rid)
                ref = EvidenceRef(id=rid, kind="hold", title="QA hold",
                                  detail=(f"active on {hold.lot_id} since {_hm(hold.placed_at)}" if hold
                                          else "not active (released or replaced)"))
            elif rid in alarms:
                a = alarms[rid]
                ref = EvidenceRef(id=rid, kind="alarm", title=a.description,
                                  detail=f"{a.priority.value.upper()} · {_hm(a.ts)} · "
                                         f"{'active' if a.active else 'cleared'}")
            elif rid in inbox:
                m = inbox[rid]
                ref = EvidenceRef(id=rid, kind="msg", title="Inbox message (untrusted)",
                                  detail=f"from {m.sender} · subject (external text): “{m.subject}”")
            elif rid in approvals:
                ap = approvals[rid]
                ref = EvidenceRef(id=rid, kind="approval", title=ap.title, detail=ap.status.value)
        except (KeyError, ValueError, IndexError):
            ref = None
        out[rid] = ref or EvidenceRef(id=rid, kind=kind, title=label)
    return out
