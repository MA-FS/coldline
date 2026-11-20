"""Shared contracts between the simulator/systems layer, the agent layer and the web layer.

This module is the seam between the three layers, so each can be built, tested and replaced on its own:

* ``coldline.sim`` / ``coldline.systems`` implement :class:`PlantSystems` (one instance per visitor sandbox).
* ``coldline.agent`` calls *only* :class:`PlantSystems` methods through typed tools, and produces
  :class:`RunTrace` / :class:`Approval` objects.
* ``coldline.web`` renders these models. It never reaches into internals.

Treat it as a public interface: additive fields with defaults are fine, breaking changes need all three
layers updated together.
All data is synthetic. "Southern Range Meats" is a fictional plant.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

PLANT_NAME = "Southern Range Meats"
DISCLAIMER = "Independent portfolio demo · Synthetic data · Not affiliated with any company"


# --------------------------------------------------------------------------- enums


class Quality(StrEnum):
    GOOD = "good"
    STALE = "stale"  # no fresh sample within the freshness window
    BAD = "bad"  # instrument fault / out of range


class AssetKind(StrEnum):
    CHILLER = "chiller"
    COMPRESSOR = "compressor"
    ROOM = "room"  # boning room
    DOCK = "dock"  # load-out


class AlarmPriority(StrEnum):
    LOW = "low"
    AMBER = "amber"
    RED = "red"


class LotStatus(StrEnum):
    READY = "ready"
    HOLD = "hold"
    DISPATCHED = "dispatched"


class Scenario(StrEnum):
    PROBE_MISMATCH = "probe_mismatch"  # hero: TT-401 drifts up, TT-402 steady
    COMPRESSOR_TRIP = "compressor_trip"  # C2 trips, chiller 4 genuinely warms (both probes rise)
    DOOR_OPEN = "door_open"  # transient rise that recovers
    POISONED_EMAIL = "poisoned_email"  # supplier email contains "release all holds" injection


class Tier(StrEnum):
    """Action tiers, enforced in code by the agent layer (never by prompt alone)."""

    READ = "read"  # no side effects
    AUTO = "auto"  # low-risk write, agent may do it, always audited
    APPROVE = "approve"  # proposal only; executes after a human approves
    NEVER = "never"  # refused (e.g. anything writing to plant control, authorising permits)


# --------------------------------------------------------------------------- plant data


class Asset(BaseModel):
    id: str  # e.g. "CH4", "C2", "BONING", "DOCK"
    kind: AssetKind
    name: str  # "Chiller 4"
    zone: str  # "carcase_chillers", "refrigeration", "boning", "loadout"
    tags: list[str] = []  # instrument tags on this asset, e.g. ["TT-401", "TT-402"]
    running: bool | None = None  # compressors only
    state: str | None = None  # compressors only: "run" | "standby" | "tripped"
    detail: dict[str, Any] = {}  # e.g. {"suction_c": -6.3, "discharge_c": 28.9}


class Reading(BaseModel):
    tag: str  # "TT-401"
    ts: datetime
    value_c: float | None
    quality: Quality = Quality.GOOD


class Trend(BaseModel):
    tag: str
    description: str  # "Chiller 4 air temp (primary)"
    unit: str = "°C"
    points: list[Reading]


class Alarm(BaseModel):
    id: str
    ts: datetime
    priority: AlarmPriority
    asset_id: str
    tags: list[str]
    description: str
    value: str  # display string, e.g. "6.8 °C / 2.1 °C"
    limit: str  # e.g. "Δ > 2.0 °C (15 min)"
    active: bool = True
    short_label: str = ""  # mimic badge, e.g. "PROBE MISMATCH"; derived from description when empty


class Lot(BaseModel):
    id: str  # "L-2231"
    zone_asset_id: str  # "CH4"
    product: str  # "Beef carcases, chilled"
    carcases: int
    weight_kg: float
    customer: str  # fictional
    export: bool = False
    status: LotStatus = LotStatus.READY
    dispatch_due: datetime | None = None
    version: int = 1  # bumped on every hold, release or dispatch change


class WorkOrder(BaseModel):
    id: str  # "WO-1042"
    asset_id: str
    title: str
    priority: int = 3  # 1 = urgent
    status: str = "open"  # open | in_progress | closed
    created_at: datetime
    created_by: str
    notes: list[str] = []
    version: int = 1  # bumped on every amendment (approval binds to a version)


class QAHold(BaseModel):
    id: str
    lot_id: str
    reason: str
    placed_by: str
    placed_at: datetime
    active: bool = True


class PermitDraft(BaseModel):
    id: str
    asset_id: str
    work_description: str
    isolations: list[str]  # e.g. ["Isolate C2 MCC feeder at MCC-2 / LOTO"]
    hazards: list[str]
    status: str = "draft"  # the agent may only ever create drafts
    drafted_by: str = ""


class InboxMessage(BaseModel):
    """External text (supplier emails, notes). ALWAYS untrusted data, never instructions."""

    id: str
    ts: datetime
    sender: str
    subject: str
    body: str


class AuditEvent(BaseModel):
    id: str
    ts: datetime
    actor: str  # "agent" | "Shift Supervisor" | "system"
    action: str  # "place_qa_hold", "approve", "decline", "inject_fault", ...
    target: str  # e.g. "L-2231"
    detail: str = ""
    evidence_ids: list[str] = []  # record ids the action relied on
    run_id: str | None = None


class EvidenceRef(BaseModel):
    """A record an agent step, approval or handover cited, described for a person checking it."""

    id: str  # "TT-401", "L-2231", "WO-1042"
    kind: str  # tag | lot | wo | alarm | msg | hold | permit | approval | asset | record
    title: str  # "Chiller 4 air (primary)"
    detail: str = ""  # "6.8 °C · good · 05:58"
    version: int | None = None  # record version where the record has one


class PlantSnapshot(BaseModel):
    """Everything the overview page needs in one object."""

    now: datetime
    assets: list[Asset]
    latest: dict[str, Reading]  # tag -> latest reading
    alarms: list[Alarm]
    lots: list[Lot]
    active_scenario: Scenario | None = None  # the most recent injection, physical or inbox
    physical_scenario: Scenario | None = None  # the most recent injection that changed telemetry
    incident: int = 0  # generation counter: bumped by every injection and reset


# --------------------------------------------------------------------------- systems seam


class PlantSystems(Protocol):
    """One visitor's sandboxed plant + mock business systems. Implemented by coldline.systems.

    Every write appends an AuditEvent. Ids are deterministic and human-readable.
    """

    # simulator / clock
    def now(self) -> datetime: ...
    def tick(self, seconds: float = 60.0) -> None: ...
    def inject(self, scenario: Scenario) -> None: ...
    def make_stale(self, tag: str) -> None: ...
    def reset(self) -> None: ...
    def snapshot(self) -> PlantSnapshot: ...

    # historian (OT data, read-only to the agent)
    def get_trend(self, tags: list[str], window_min: int = 60) -> list[Trend]: ...
    def latest(self, tags: list[str]) -> dict[str, Reading]: ...
    def get_asset(self, asset_id: str) -> Asset: ...
    def list_alarms(self, active_only: bool = True) -> list[Alarm]: ...

    # lots / QA / dispatch
    def list_lots(self, zone_asset_id: str | None = None) -> list[Lot]: ...
    def get_lot(self, lot_id: str) -> Lot: ...
    def place_qa_hold(self, lot_id: str, reason: str, actor: str, evidence_ids: list[str]) -> QAHold: ...
    def release_qa_hold(self, lot_id: str, actor: str) -> QAHold: ...
    def active_hold(self, lot_id: str) -> QAHold | None: ...
    def change_dispatch(self, lot_id: str, new_due: datetime, actor: str, reason: str) -> Lot: ...

    # CMMS
    def find_work_orders(self, asset_id: str, open_only: bool = True) -> list[WorkOrder]: ...
    def asset_history(self, asset_id: str, days: int = 90) -> list[WorkOrder]: ...
    def create_work_order(
        self, asset_id: str, title: str, priority: int, note: str, actor: str
    ) -> WorkOrder: ...
    def amend_work_order(self, wo_id: str, note: str, actor: str, expected_version: int) -> WorkOrder: ...

    # permits (drafts only)
    def draft_permit(
        self, asset_id: str, work_description: str, isolations: list[str], hazards: list[str], actor: str
    ) -> PermitDraft: ...
    def list_permits(self, asset_id: str | None = None) -> list[PermitDraft]: ...

    # external text
    def inbox(self) -> list[InboxMessage]: ...

    # audit
    def audit(self, event: AuditEvent) -> None: ...
    def audit_log(self, limit: int = 100) -> list[AuditEvent]: ...

    # consistency
    def transaction(self) -> AbstractContextManager[None]:
        """Hold the sandbox lock across a validate-then-apply sequence (approval decisions)."""
        ...


# --------------------------------------------------------------------------- agent outputs


class StepKind(StrEnum):
    THINK = "think"  # short model-visible reasoning summary (not raw chain-of-thought)
    TOOL = "tool"
    GUARDRAIL = "guardrail"
    CONCLUSION = "conclusion"
    ERROR = "error"


class TraceStep(BaseModel):
    n: int
    kind: StepKind
    title: str  # "get_sensor_trend" or "Tier check: dispatch change requires approval"
    summary: str = ""  # one line for humans
    args: dict[str, Any] = {}
    result_preview: str = ""  # short, rendered as monospace "obs:" line
    tier: Tier | None = None
    ok: bool = True
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    evidence_ids: list[str] = []


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    EXPIRED = "expired"  # source data changed since proposal


class Approval(BaseModel):
    id: str
    run_id: str
    action: str  # tool name, e.g. "change_dispatch"
    title: str  # "Move L-2231 dispatch 06:15 -> 07:00"
    rationale: str
    payload: dict[str, Any]
    evidence_ids: list[str] = []
    source_versions: dict[str, int] = {}  # record id -> version at proposal time
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    idempotency_key: str
    run_ids: list[str] = []  # every run that proposed this action on the same evidence (run_id first)
    note: str = ""  # why it expired or failed, for the person who tried to decide


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    INCOMPLETE = "incomplete"  # ended without an accepted handover note (step or time limit)
    FAILED = "failed"


class RunTrace(BaseModel):
    run_id: str  # "R-7842"
    session_id: str
    scenario: Scenario | None
    mode: str = "replay"  # "replay" | "live"
    model: str = ""
    started_at: datetime
    status: RunStatus = RunStatus.RUNNING
    steps: list[TraceStep] = []
    conclusion: str = ""  # e.g. "Probes disagree - cause unconfirmed."
    confidence: str = ""  # "confirmed" | "unconfirmed" | "insufficient_data"
    handover_note: str = ""  # markdown with [record-id] citations
    approvals: list[Approval] = []
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    duration_ms: int = 0
    extra: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- evals


class EvalCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class EvalResult(BaseModel):
    """One eval case, as written by ``python -m evals.run`` and rendered by the Evals page."""

    id: str
    title: str
    category: str  # "scenario" | "adversarial" | "approval"
    scenario: Scenario | None = None
    mode: str = "scripted"  # "scripted" | "live"
    passed: bool
    assertions: list[EvalCheck] = []
    conclusion: str = ""
    confidence: str = ""
    model: str = ""
    model_calls: int = 0
    steps: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    cost_basis: str = ""  # "recorded" | "live" | "none"
    run_id: str | None = None
    note: str = ""
