"""State for one agent run: what the agent has looked at, created and been refused.

Tools read and write this object; the loop turns it into the final :class:`RunTrace`. Keeping it explicit
(rather than inferring state from the conversation) is what lets code, not the model, decide whether a
handover is allowed and which records it may cite.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from coldline.contracts import (
    Asset,
    AuditEvent,
    InboxMessage,
    PlantSystems,
    RunTrace,
    StepKind,
    Tier,
    TraceStep,
    WorkOrder,
)

if TYPE_CHECKING:
    from coldline.agent.approvals import ApprovalStore
    from coldline.agent.evidence import Assessment

AGENT_ACTOR = "agent"

# Scopes granted to the agent's service identity. Each tool requires one; removing a scope here
# disables the tool without touching the model or the prompt (least privilege, in one place).
AGENT_SCOPES = frozenset(
    {
        "historian:read",
        "assets:read",
        "alarms:read",
        "lots:read",
        "cmms:read",
        "inbox:read",
        "qa:hold",
        "cmms:create",
        "permits:draft",
        "proposals:create",
        "handover:write",
        "security:flag",
    }
)


@dataclass
class RunContext:
    run: RunTrace
    systems: PlantSystems
    approvals: ApprovalStore
    scopes: frozenset[str] = AGENT_SCOPES
    seen: set[str] = field(default_factory=set)  # ids of plant records returned by tools in this run
    lookups: set[tuple[str, str]] = field(default_factory=set)  # ("trend" | "lots" | ..., asset_id)
    created: dict[str, str] = field(default_factory=dict)  # "hold:L-2231" -> "QH-0001"
    # Logical action -> "new" | "existing" | "reused": whether this run created the record or found it.
    # Together with ``created`` this makes writes idempotent per run, including across a live-to-replay
    # fallback, which keeps the same context.
    outcomes: dict[str, str] = field(default_factory=dict)
    records: dict[str, Any] = field(default_factory=dict)  # record id -> last model fetched
    assessments: dict[str, Assessment] = field(default_factory=dict)  # asset id -> latest check
    injection_flags: list[str] = field(default_factory=list)  # ids of inbox or ticket records flagged
    tainted: bool = False  # set once suspected injection text has been read
    handover_accepted: bool = False
    replay_baseline: dict[str, Any] | None = None  # the recording's handover facts, when replaying one
    unresolved: set[str] = field(default_factory=set)  # recording placeholders this plant could not bind
    on_step: Callable[[TraceStep], None] | None = None
    _assets: dict[str, Asset] | None = None

    # ------------------------------------------------------------------ steps

    def emit(self, kind: StepKind, title: str, **fields: Any) -> TraceStep:
        step = TraceStep(n=len(self.run.steps) + 1, kind=kind, title=title, **fields)
        self.run.steps.append(step)
        if self.on_step:
            self.on_step(step)
        return step

    def guardrail(self, title: str, summary: str, *, tier: Tier | None = None, ok: bool = False,
                  args: dict | None = None, evidence_ids: list[str] | None = None) -> TraceStep:
        return self.emit(
            StepKind.GUARDRAIL,
            title,
            summary=summary,
            tier=tier,
            ok=ok,
            args=args or {},
            evidence_ids=evidence_ids or [],
        )

    # ------------------------------------------------------------------ plant lookups

    def assets(self) -> dict[str, Asset]:
        if self._assets is None:
            self._assets = {a.id: a for a in self.systems.snapshot().assets}
        return self._assets

    def asset_for_tag(self, tag: str) -> str | None:
        for asset in self.assets().values():
            if tag in asset.tags:
                return asset.id
        return None

    def is_chiller(self, asset_id: str | None) -> bool:
        asset = self.assets().get(asset_id or "")
        return asset is not None and asset.kind.value == "chiller"

    def see(self, *ids: str) -> None:
        """Mark ids as returned by a tool. Callers pass only ids read off records the plant returned (never
        an argument the model supplied), so ``seen`` is the set of records the run can cite."""
        self.seen.update(i for i in ids if i)

    def flagged_records(self) -> list[str]:
        """Canonical ids of the flagged records, for anything code composes. Only inbox messages and work
        order tickets carry outside text, and only records a tool returned count: a flag on anything else
        is never displayed."""
        out = []
        for i in self.injection_flags:
            record = self.records.get(i)
            if isinstance(record, (InboxMessage, WorkOrder)) and record.id not in out:
                out.append(record.id)
        return out

    # ------------------------------------------------------------------ audit

    def audit(self, action: str, target: str, detail: str = "", evidence_ids: list[str] | None = None,
              actor: str = AGENT_ACTOR) -> None:
        self.systems.audit(
            AuditEvent(
                id="pending",  # the systems layer assigns the sequential id
                ts=self.systems.now(),
                actor=actor,
                action=action,
                target=target,
                detail=detail[:500],
                evidence_ids=evidence_ids or [],
                run_id=self.run.run_id,
            )
        )
