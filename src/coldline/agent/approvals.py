"""Human approval for APPROVE-tier actions.

The agent never executes these actions. An APPROVE-tier tool produces a :class:`Proposal`; this module
turns it into an :class:`Approval` bound to:

* ``source_versions``: the evidence the proposal relied on, one categorical value per source. Records
  (lots, work orders) contribute their version, which moves on every hold, release, dispatch change or
  amendment. Probes contribute their quality (good, stale, bad). The chiller contributes its code-computed
  evidence verdict, so "probes disagree" becoming "confirmed excursion" is a change even while both probes
  stay fresh. A release names the exact hold it releases. Every proposal also binds to the plant's
  incident generation, which moves on every fault injection and reset. Ordinary sampling noise moves none
  of these, so proposals expire only on a change a person would care about.
* ``idempotency_key``: a stable hash of who proposed what, in which run and session generation.
* ``decision_token``: an opaque random token issued with the proposal. A decision from the web must carry
  it. Display ids (AP-0001) restart for a visitor whose sandbox expired, so the id alone can name a newer
  proposal; the token cannot. Each store has its own generation, which a token starts with, so a token from
  an expired or reset session is recognised as such and refused with "session expired".

``approve()`` re-reads those values and re-checks the action's own preconditions (a new dispatch time must
still be in the future) inside one sandbox transaction, then executes and audits under the same locks. If
anything changed, the approval becomes ``EXPIRED`` with the reason, and nothing runs. A double click or a
retried request cannot apply an action twice. ``decline()`` needs the same authority as ``approve()`` and
only records the decision. ``revalidate()`` applies the same check without a click, so expiry shows as soon
as the evidence changes.

A second run proposing the same action on the same, still-valid evidence reuses the pending approval and
adds its run id to ``run_ids``, so one decision settles every run that asked for it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from coldline.agent.evidence import Verdict, assess_chiller
from coldline.contracts import Approval, ApprovalStatus, AuditEvent, PlantSystems, Quality
from coldline.sim import analysis

INCIDENT = "incident"  # source_versions key for the plant's incident generation
RESOLVED_KEEP = 100  # decided approvals kept per session beyond those the retained runs reference


@dataclass(frozen=True)
class Source:
    """Evidence an approval depends on. ``asset_id`` locates work orders and the lot a hold belongs to."""

    kind: str  # "work_order" | "lot" | "probe" | "verdict" | "hold" | "incident"
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


class StaleDecision(Exception):
    """A decision whose token does not belong to a current proposal in this session. Nothing is decided.

    ``session_expired`` is true when the token was issued by an earlier generation of the session (its
    sandbox expired, was evicted or was reset), so the page that sent it is out of date as a whole."""

    def __init__(self, message: str, *, session_expired: bool) -> None:
        super().__init__(message)
        self.session_expired = session_expired


class ApprovalResult(BaseModel):
    approval: Approval
    executed: bool
    message: str


_PROBE_STATE = {Quality.GOOD: 1, Quality.STALE: 2, Quality.BAD: 3}
_VERDICTS = list(Verdict)


def record_version(systems: PlantSystems, source: Source) -> int:
    """Current value of one source. ``-1`` means the record no longer exists (always a change)."""
    try:
        if source.kind == "work_order":
            orders = systems.find_work_orders(source.asset_id or "", open_only=False)
            wo = next((w for w in orders if w.id == source.record_id), None)
            return -1 if wo is None else wo.version
        if source.kind == "lot":
            return systems.get_lot(source.record_id).version
        if source.kind == "probe":
            # A probe's evidence "version" is its quality: good -> stale invalidates the comparison.
            reading = systems.latest([source.record_id])[source.record_id]
            return _PROBE_STATE[analysis.freshness(reading, systems.now())]
        if source.kind == "verdict":
            return _VERDICTS.index(assess_chiller(systems, source.record_id).verdict) + 1
        if source.kind == "hold":
            hold = systems.active_hold(source.asset_id or "")
            return 1 if hold is not None and hold.id == source.record_id else -1
        if source.kind == INCIDENT:
            return systems.snapshot().incident
    except (KeyError, ValueError):
        return -1
    raise ValueError(f"unknown source kind {source.kind!r}")


def describe_change(source: Source, old: int, new: int) -> str:
    """One readable clause per changed source, for the expiry message and the audit log."""
    rid = source.record_id
    if source.kind == INCIDENT:
        return "a new fault was injected or the plant was reset (incident changed)"
    if source.kind == "probe":
        names = {v: q.value for q, v in _PROBE_STATE.items()}
        return f"{rid} went from {names.get(old, '?')} to {names.get(new, 'missing')}"
    if source.kind == "verdict":
        def name(v: int) -> str:
            return _VERDICTS[v - 1].value.replace("_", " ") if 0 < v <= len(_VERDICTS) else "unknown"
        return f"{rid} evidence check went from '{name(old)}' to '{name(new)}'"
    if source.kind == "hold":
        return f"{rid} is no longer the active hold on {source.asset_id}"
    if new == -1:
        return f"{rid} no longer exists"
    return f"{rid} changed (v{old} -> v{new})"


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


def precondition_failure(approval: Approval, systems: PlantSystems) -> str:
    """Checks that depend on the clock or on the action itself, repeated at decision time."""
    if approval.action == "change_dispatch":
        due = datetime.fromisoformat(approval.payload["new_due"])
        if due <= systems.now():
            return f"the proposed dispatch time {due:%H:%M} has already passed"
    return ""


def role_allowed(actor: str, roles: tuple[str, ...]) -> bool:
    """Exact role match. The agent and system identities never hold an approver role."""
    return actor in roles and actor.casefold() not in {"agent", "system"}


@dataclass
class _Meta:
    sources: list[Source]
    approver_roles: tuple[str, ...]


@dataclass
class ApprovalStore:
    """Approvals for one visitor session. Thread-safe; all state transitions happen under one lock."""

    session_id: str
    next_id: Callable[[], str]
    generation: str = field(default_factory=lambda: secrets.token_hex(6))  # new for every store
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
        """Create an approval, or reuse a pending one for the same action on still-valid evidence.

        Returns ``(approval, created)``.
        """
        with self._lock, systems.transaction():
            for existing in list(self._items.values()):
                if (existing.status != ApprovalStatus.PENDING or existing.action != proposal.action
                        or existing.payload != proposal.payload):
                    continue
                reason = self._stale_reason(existing, systems)
                if reason:
                    self._expire(existing, "system", systems, reason)
                    continue
                if run_id not in existing.run_ids:
                    existing.run_ids.append(run_id)
                    existing.run_count += 1
                return existing, False
            versions = {s.record_id: record_version(systems, s) for s in proposal.sources}
            key_material = json.dumps(
                {"session": self.session_id, "generation": self.generation, "run": run_id,
                 "action": proposal.action, "payload": proposal.payload},
                sort_keys=True,
                default=str,
            )
            approval = Approval(
                id=self.next_id(),
                run_id=run_id,
                run_ids=[run_id],
                action=proposal.action,
                title=proposal.title,
                rationale=proposal.rationale,
                payload=proposal.payload,
                evidence_ids=proposal.evidence_ids,
                source_versions=versions,
                idempotency_key=hashlib.sha256(key_material.encode()).hexdigest()[:24],
                decision_token=f"{self.generation}.{secrets.token_urlsafe(12)}",
            )
            self._items[approval.id] = approval
            self._meta[approval.id] = _Meta(proposal.sources, proposal.approver_roles)
            self._audit(systems, approval, "agent", "approval_proposed", proposal.title)
            return approval, True

    def bound(self, approval_id: str, token: str) -> Approval:
        """The approval ``token`` was issued for, if it is ``approval_id`` in this store; else StaleDecision."""
        with self._lock:
            approval = self._items.get(approval_id)
            if approval is not None and token and secrets.compare_digest(approval.decision_token, token):
                return approval
        if token.split(".", 1)[0] != self.generation:
            raise StaleDecision("This page is from an earlier session: the sandbox expired or the plant was "
                                "reset. Nothing was decided. Reload the page to see the current proposals.",
                                session_expired=True)
        raise StaleDecision(f"This card does not match a current proposal {approval_id}. Nothing was decided. "
                            "Reload the page.", session_expired=False)

    def approve(self, approval_id: str, actor: str, systems: PlantSystems,
                token: str | None = None) -> ApprovalResult:
        """Decide as ``actor``. With ``token`` (every web decision) the token must be the one issued with this
        proposal in this session generation (see :meth:`bound`)."""
        with self._lock, systems.transaction():
            approval = self._items[approval_id] if token is None else self.bound(approval_id, token)
            if approval.status == ApprovalStatus.APPROVED:
                return ApprovalResult(approval=approval, executed=False,
                                      message="Already approved; not executed again.")
            if approval.status != ApprovalStatus.PENDING:
                why = f" ({approval.note})" if approval.note else ""
                return ApprovalResult(approval=approval, executed=False,
                                      message=f"Approval is {approval.status.value}{why}; nothing was executed.")
            self._check_role(approval, actor)
            reason = self._stale_reason(approval, systems)
            if reason:
                self._expire(approval, actor, systems, reason)
                return ApprovalResult(
                    approval=approval,
                    executed=False,
                    message=f"Expired: {reason}. Nothing was executed; re-run the investigation.",
                )
            if approval.idempotency_key not in self._executed:
                try:
                    EXECUTORS[approval.action](systems, approval, actor)
                except (ValueError, LookupError, PermissionError) as exc:  # version conflict, missing record
                    self._decide(approval, ApprovalStatus.EXPIRED, actor, systems)
                    approval.note = str(exc)
                    self._audit(systems, approval, actor, "approval_failed", str(exc))
                    return ApprovalResult(approval=approval, executed=False, message=f"Not executed: {exc}")
                self._executed.add(approval.idempotency_key)
            self._decide(approval, ApprovalStatus.APPROVED, actor, systems)
            self._audit(systems, approval, actor, "approve", f"{approval.action}: {approval.title}")
            return ApprovalResult(approval=approval, executed=True, message="Approved and executed.")

    def revalidate(self, systems: PlantSystems, actor: str = "system") -> list[Approval]:
        """Expire every pending approval whose evidence changed. Returns the ones that expired.

        ``approve()`` performs the same check, so this only makes expiry visible as soon as the evidence
        changes instead of when someone next clicks Approve.
        """
        expired: list[Approval] = []
        with self._lock, systems.transaction():
            for approval in self._items.values():
                if approval.status != ApprovalStatus.PENDING:
                    continue
                reason = self._stale_reason(approval, systems)
                if reason:
                    self._expire(approval, actor, systems, reason)
                    expired.append(approval)
        return expired

    def decline(self, approval_id: str, actor: str, systems: PlantSystems, reason: str = "",
                token: str | None = None) -> ApprovalResult:
        with self._lock, systems.transaction():
            approval = self._items[approval_id] if token is None else self.bound(approval_id, token)
            if approval.status != ApprovalStatus.PENDING:
                return ApprovalResult(approval=approval, executed=False,
                                      message=f"Approval is {approval.status.value}; nothing changed.")
            self._check_role(approval, actor)
            self._decide(approval, ApprovalStatus.DECLINED, actor, systems)
            self._audit(systems, approval, actor, "decline", reason or approval.title)
            return ApprovalResult(approval=approval, executed=False,
                                  message="Declined. No business system was changed.")

    def prune(self, keep_run_ids: set[str], keep_resolved: int = RESOLVED_KEEP) -> None:
        """Drop decided approvals that no retained run references, beyond the newest ``keep_resolved``, and
        trim every approval's ``run_ids`` to retained runs (the originating run always stays; ``run_count``
        keeps the total).

        Pending approvals are always kept: a person may still decide them.
        """
        with self._lock:
            for a in self._items.values():
                a.run_ids = [r for r in a.run_ids if r == a.run_id or r in keep_run_ids]
            resolved = [a for a in self._items.values() if a.status != ApprovalStatus.PENDING]
            orphans = [a for a in resolved if not set(a.run_ids or [a.run_id]) & keep_run_ids]
            for a in orphans[: max(0, len(orphans) - keep_resolved)]:
                self._items.pop(a.id, None)
                self._meta.pop(a.id, None)
                self._executed.discard(a.idempotency_key)

    # ------------------------------------------------------------------ helpers

    def _check_role(self, approval: Approval, actor: str) -> None:
        roles = self._meta[approval.id].approver_roles
        if not role_allowed(actor, roles):
            raise PermissionError(f"{approval.action} needs a decision by {' or '.join(roles)}, not {actor!r}")

    def _stale_reason(self, approval: Approval, systems: PlantSystems) -> str:
        """Why this approval can no longer be executed, or "" if its evidence still holds."""
        changes = []
        for source in self._meta[approval.id].sources:
            old = approval.source_versions.get(source.record_id)
            new = record_version(systems, source)
            if old != new:
                changes.append(describe_change(source, old if old is not None else -1, new))
        precondition = precondition_failure(approval, systems)
        if precondition:
            changes.append(precondition)
        return "; ".join(changes)

    def _expire(self, approval: Approval, actor: str, systems: PlantSystems, reason: str) -> None:
        self._decide(approval, ApprovalStatus.EXPIRED, actor, systems)
        approval.note = reason
        self._audit(systems, approval, actor, "approval_expired", f"evidence changed since proposal: {reason}")

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
