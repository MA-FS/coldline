"""Human approval for APPROVE-tier actions.

The agent never executes these actions. An APPROVE-tier tool produces a :class:`Proposal`; this module
turns it into an :class:`Approval` bound to:

* ``source_versions``: a version (or content fingerprint) of every record the proposal relied on, and
* ``idempotency_key``: a stable hash of who proposed what, in which run.

``approve()`` re-reads those versions. If any changed (a work order was amended, a probe went stale, a lot
was rescheduled) the approval becomes ``EXPIRED`` and nothing runs. Otherwise the action executes exactly
once and the audit event is written under the same lock, so a double click or a retried request cannot
apply it twice. ``decline()`` only records the decision.
"""

from __future__ import annotations

import hashlib
import json
import threading
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from coldline.contracts import Approval, ApprovalStatus, AuditEvent, LotStatus, PlantSystems, Quality
from coldline.sim import analysis


@dataclass(frozen=True)
class Source:
    """A record an approval depends on. ``asset_id`` locates work orders (looked up per asset)."""

    kind: str  # "work_order" | "lot" | "probe"
    record_id: str
    asset_id: str | None = None


@dataclass
class Proposal:
    action: str  # the systems method that runs on approval, e.g. "change_dispatch"
    title: str
    rationale: str
    payload: dict[str, Any]
    evidence_ids: list[str]
    sources: list[Source]
    approver_roles: tuple[str, ...]


class ApprovalResult(BaseModel):
    approval: Approval
    executed: bool
    message: str


_PROBE_STATE = {Quality.GOOD: 1, Quality.STALE: 2, Quality.BAD: 3}


def _fingerprint(*parts: object) -> int:
    """Short content hash for records that carry no version field."""
    return zlib.crc32("|".join(str(p) for p in parts).encode()) % 1_000_000


def record_version(systems: PlantSystems, source: Source) -> int:
    """Current version of a source record. Records without a version field get a content fingerprint."""
    try:
        if source.kind == "work_order":
            orders = systems.find_work_orders(source.asset_id or "", open_only=False)
            wo = next((w for w in orders if w.id == source.record_id), None)
            return -1 if wo is None else wo.version
        if source.kind == "lot":
            lot = systems.get_lot(source.record_id)
            due = lot.dispatch_due.isoformat() if lot.dispatch_due else ""
            return _fingerprint(due, lot.status == LotStatus.DISPATCHED)
        if source.kind == "probe":
            # A probe's evidence "version" is its quality: good -> stale invalidates the comparison.
            reading = systems.latest([source.record_id])[source.record_id]
            return _PROBE_STATE[analysis.freshness(reading, systems.now())]
    except (KeyError, ValueError):
        return -1
    raise ValueError(f"unknown source kind {source.kind!r}")


# --------------------------------------------------------------------------- executors


def _amend_work_order(systems: PlantSystems, approval: Approval, actor: str) -> None:
    p = approval.payload
    note = f"{p['note']} (proposed by agent run {approval.run_id}, approved by {actor})"
    systems.amend_work_order(p["wo_id"], note, actor, expected_version=approval.source_versions[p["wo_id"]])


def _change_dispatch(systems: PlantSystems, approval: Approval, actor: str) -> None:
    p = approval.payload
    systems.change_dispatch(p["lot_id"], datetime.fromisoformat(p["new_due"]), actor, p["reason"])


def _release_qa_hold(systems: PlantSystems, approval: Approval, actor: str) -> None:
    systems.release_qa_hold(approval.payload["lot_id"], actor)


EXECUTORS: dict[str, Callable[[PlantSystems, Approval, str], None]] = {
    "amend_work_order": _amend_work_order,
    "change_dispatch": _change_dispatch,
    "release_qa_hold": _release_qa_hold,
}
APPROVER_ROLES: dict[str, tuple[str, ...]] = {
    "amend_work_order": ("Shift Supervisor", "Maintenance Planner"),
    "change_dispatch": ("Shift Supervisor",),
    "release_qa_hold": ("QA Manager",),
}


def role_allowed(actor: str, roles: tuple[str, ...]) -> bool:
    who = actor.casefold().strip()
    if who in {"", "agent", "system"}:
        return False  # the agent can never approve its own proposals
    return any(role.casefold() in who for role in roles)


@dataclass
class _Meta:
    sources: list[Source]
    approver_roles: tuple[str, ...]


@dataclass
class ApprovalStore:
    """Approvals for one visitor session. Thread-safe; all state transitions happen under one lock."""

    session_id: str
    next_id: Callable[[], str]
    _items: dict[str, Approval] = field(default_factory=dict)
    _meta: dict[str, _Meta] = field(default_factory=dict)
    _executed: set[str] = field(default_factory=set)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def get(self, approval_id: str) -> Approval:
        return self._items[approval_id]

    def list_all(self, status: ApprovalStatus | None = None) -> list[Approval]:
        items = list(self._items.values())
        return [a for a in items if status is None or a.status == status]

    def propose(self, *, run_id: str, systems: PlantSystems, proposal: Proposal) -> tuple[Approval, bool]:
        """Create (or return the identical pending) approval. Returns ``(approval, created)``."""
        with self._lock:
            for existing in self._items.values():
                if (
                    existing.status == ApprovalStatus.PENDING
                    and existing.action == proposal.action
                    and existing.payload == proposal.payload
                ):
                    return existing, False
            versions = {s.record_id: record_version(systems, s) for s in proposal.sources}
            key_material = json.dumps(
                {"session": self.session_id, "run": run_id, "action": proposal.action,
                 "payload": proposal.payload},
                sort_keys=True,
                default=str,
            )
            approval = Approval(
                id=self.next_id(),
                run_id=run_id,
                action=proposal.action,
                title=proposal.title,
                rationale=proposal.rationale,
                payload=proposal.payload,
                evidence_ids=proposal.evidence_ids,
                source_versions=versions,
                idempotency_key=hashlib.sha256(key_material.encode()).hexdigest()[:24],
            )
            self._items[approval.id] = approval
            self._meta[approval.id] = _Meta(proposal.sources, proposal.approver_roles)
            self._audit(systems, approval, "agent", "approval_proposed", proposal.title)
            return approval, True

    def approve(self, approval_id: str, actor: str, systems: PlantSystems) -> ApprovalResult:
        with self._lock:
            approval = self._items[approval_id]
            if approval.status == ApprovalStatus.APPROVED:
                return ApprovalResult(approval=approval, executed=False,
                                      message="Already approved; not executed again.")
            if approval.status != ApprovalStatus.PENDING:
                return ApprovalResult(approval=approval, executed=False,
                                      message=f"Approval is {approval.status.value}; nothing was executed.")
            meta = self._meta[approval_id]
            if not role_allowed(actor, meta.approver_roles):
                raise PermissionError(
                    f"{approval.action} needs approval by {' or '.join(meta.approver_roles)}, not {actor!r}"
                )
            current = {s.record_id: record_version(systems, s) for s in meta.sources}
            changed = sorted(r for r, v in approval.source_versions.items() if current.get(r) != v)
            if changed:
                self._decide(approval, ApprovalStatus.EXPIRED, actor, systems)
                detail = f"source records changed since proposal: {', '.join(changed)}"
                self._audit(systems, approval, actor, "approval_expired", detail)
                return ApprovalResult(
                    approval=approval,
                    executed=False,
                    message=f"Expired: {detail}. Nothing was executed; re-run the investigation.",
                )
            if approval.idempotency_key not in self._executed:
                try:
                    EXECUTORS[approval.action](systems, approval, actor)
                except (ValueError, LookupError, PermissionError) as exc:  # version conflict, missing record
                    self._decide(approval, ApprovalStatus.EXPIRED, actor, systems)
                    self._audit(systems, approval, actor, "approval_failed", str(exc))
                    return ApprovalResult(approval=approval, executed=False, message=f"Not executed: {exc}")
                self._executed.add(approval.idempotency_key)
            self._decide(approval, ApprovalStatus.APPROVED, actor, systems)
            self._audit(systems, approval, actor, "approve", f"{approval.action}: {approval.title}")
            return ApprovalResult(approval=approval, executed=True, message="Approved and executed.")

    def revalidate(self, systems: PlantSystems, actor: str = "system") -> list[Approval]:
        """Expire every pending approval whose source records changed. Returns the ones that expired.

        ``approve()`` performs the same check, so this only makes expiry visible as soon as the evidence
        changes instead of when someone next clicks Approve.
        """
        expired: list[Approval] = []
        with self._lock:
            for approval in self._items.values():
                if approval.status != ApprovalStatus.PENDING:
                    continue
                meta = self._meta[approval.id]
                current = {s.record_id: record_version(systems, s) for s in meta.sources}
                changed = sorted(r for r, v in approval.source_versions.items() if current.get(r) != v)
                if changed:
                    self._decide(approval, ApprovalStatus.EXPIRED, actor, systems)
                    detail = f"source records changed since proposal: {', '.join(changed)}"
                    self._audit(systems, approval, actor, "approval_expired", detail)
                    expired.append(approval)
        return expired

    def decline(self, approval_id: str, actor: str, systems: PlantSystems, reason: str = "") -> ApprovalResult:
        with self._lock:
            approval = self._items[approval_id]
            if approval.status != ApprovalStatus.PENDING:
                return ApprovalResult(approval=approval, executed=False,
                                      message=f"Approval is {approval.status.value}; nothing changed.")
            self._decide(approval, ApprovalStatus.DECLINED, actor, systems)
            self._audit(systems, approval, actor, "decline", reason or approval.title)
            return ApprovalResult(approval=approval, executed=False,
                                  message="Declined. No business system was changed.")

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _decide(approval: Approval, status: ApprovalStatus, actor: str, systems: PlantSystems) -> None:
        approval.status = status
        approval.decided_by = actor
        approval.decided_at = systems.now()

    @staticmethod
    def _audit(systems: PlantSystems, approval: Approval, actor: str, action: str, detail: str) -> None:
        systems.audit(
            AuditEvent(
                id="pending",
                ts=systems.now(),
                actor=actor,
                action=action,
                target=approval.id,
                detail=detail[:500],
                evidence_ids=list(approval.evidence_ids),
                run_id=approval.run_id,
            )
        )
