"""Page contexts built from a visitor's live sandbox and the agent service.

Each function returns exactly the context its template documents (see ``docs/DESIGN.md``); the routes stay
thin. Everything here reads through :class:`~coldline.contracts.PlantSystems` and the agent's public API.
"""

from __future__ import annotations

import statistics
from typing import Any

from markupsafe import Markup

from coldline.agent import AgentService, load_eval_results
from coldline.contracts import (
    Alarm,
    AlarmPriority,
    ApprovalStatus,
    AssetKind,
    EvalResult,
    EvidenceRef,
    PlantSystems,
    Quality,
    RunStatus,
    RunTrace,
    Scenario,
    StepKind,
    Trend,
    WorkOrder,
)
from coldline.web.charts import ChartEvent, Series, trend_svg
from coldline.web.render import base_context, hhmm
from coldline.web.runtime import ROLES, Runtime, Visitor
from coldline.web.views import chiller_detail, citations, evidence_kind, fmt_value, plant_view, probe_tags

CHILLER_NAMEPLATE = {"capacity_carcases": 160, "status": "In service"}
FINISHED = {RunStatus.DONE, RunStatus.AWAITING_APPROVAL, RunStatus.FAILED}


# --------------------------------------------------------------------------- shell


def shell(rt: Runtime, v: Visitor, active: str, title: str) -> dict[str, Any]:
    """Base context plus what the shell shows about this visitor: mode, live budget, role, pending count."""
    status = rt.agent.live_status(v.sid)
    pending = len(rt.agent.list_approvals(v.sid, ApprovalStatus.PENDING))
    ctx = base_context(
        active,
        v.plant.now(),
        mode=v.prefs.mode,
        title=title,
        pending_approvals=pending,
        live_available=status.live_available,
    )
    ctx.update(live_status=status, role=v.prefs.role, roles=ROLES)
    return ctx


def latest_run(rt: Runtime, sid: str) -> RunTrace | None:
    runs = rt.agent.list_runs(sid)
    return runs[0] if runs else None


# --------------------------------------------------------------------------- overview


def chiller_ids(plant: PlantSystems) -> list[str]:
    return [a.id for a in plant.snapshot().assets if a.kind == AssetKind.CHILLER]


def _alarm_events(alarms: list[Alarm], tags: list[str]) -> list[ChartEvent]:
    return [
        ChartEvent(a.ts, f"Alarm {hhmm(a.ts)}", "red" if a.priority == AlarmPriority.RED else "amber")
        for a in alarms
        if a.active and set(a.tags) & set(tags)
    ]


def default_focus(alarms: list[Alarm], chillers: list[str]) -> str:
    """The chiller an operator should look at first: the one named by the highest-priority active alarm."""
    rank = {AlarmPriority.RED: 0, AlarmPriority.AMBER: 1, AlarmPriority.LOW: 2}
    for alarm in sorted((a for a in alarms if a.active), key=lambda a: rank[a.priority]):
        if alarm.asset_id in chillers:
            return alarm.asset_id
        for tag in alarm.tags:
            for cid in chillers:
                if tag.startswith(f"TT-{cid[2:]}0"):
                    return cid
    return "CH4" if "CH4" in chillers else chillers[0]


def detail_context(plant: PlantSystems, focus: str | None) -> tuple[Any, dict[str, Any]]:
    """(PlantView, chiller detail dict) for the mimic and the detail panel."""
    snap = plant.snapshot()
    chillers = [a for a in snap.assets if a.kind == AssetKind.CHILLER]
    tags = [t for a in chillers for t in probe_tags(a)]
    trends = plant.get_trend(tags, 60)
    pv = plant_view(snap, trends)
    ids = [a.id for a in chillers]
    focus = focus if focus in ids else default_focus(snap.alarms, ids)
    asset = next(a for a in chillers if a.id == focus)
    focus_tags = probe_tags(asset)
    focus_trends = [t for t in trends if t.tag in focus_tags]
    detail = chiller_detail(snap, pv, focus, focus_trends, _alarm_events(snap.alarms, focus_tags))
    # Nameplate facts the simulator does not model; display only.
    detail["asset"] = asset.model_copy(update={"detail": {**CHILLER_NAMEPLATE, **asset.detail}})
    return pv, detail


def overview(rt: Runtime, v: Visitor) -> dict[str, Any]:
    pv, detail = detail_context(v.plant, v.prefs.focus)
    run = latest_run(rt, v.sid)
    every = rt.agent.list_approvals(v.sid)
    # The panel shows this run's proposals; older ones stay on the Approvals page (and in the nav count).
    approvals = [a for a in every if run is not None and a.run_id == run.run_id]
    expired = [a for a in every if a.id in v.prefs.recent_expired and a.status == ApprovalStatus.EXPIRED]
    return {
        **shell(rt, v, "overview", "Plant overview"),
        "plant": pv,
        "detail": detail,
        "run": run,
        "approvals": approvals,
        "expired": expired,
        "scenarios": list(Scenario),
        "poll_url": "/",
    }


# --------------------------------------------------------------------------- evidence


def _work_orders(plant: PlantSystems) -> dict[str, WorkOrder]:
    out: dict[str, WorkOrder] = {}
    for asset in plant.snapshot().assets:
        for wo in plant.find_work_orders(asset.id, open_only=False):
            out[wo.id] = wo
    return out


def evidence_refs(plant: PlantSystems, agent: AgentService, sid: str, ids: list[str]) -> dict[str, EvidenceRef]:
    """Describe each cited record from the visitor's current systems, for evidence tables and citations."""
    out: dict[str, EvidenceRef] = {}
    snap = plant.snapshot()
    assets = {a.id: a for a in snap.assets}
    tag_owner = {t: a for a in snap.assets for t in a.tags}
    orders: dict[str, WorkOrder] | None = None
    alarms = {a.id: a for a in plant.list_alarms(active_only=False)}
    inbox = {m.id: m for m in plant.inbox()}
    approvals = {a.id: a for a in agent.list_approvals(sid)}
    for rid in ids:
        kind, label = evidence_kind(rid)
        ref: EvidenceRef | None = None
        try:
            if rid in tag_owner:
                reading = plant.latest([rid])[rid]
                desc = plant.get_trend([rid], 0)[0].description
                state = "STALE" if reading.quality == Quality.STALE else reading.quality.value.upper()
                ref = EvidenceRef(
                    id=rid,
                    kind="tag",
                    title=desc,
                    detail=f"{fmt_value(reading.value_c)} °C · {state} · {hhmm(reading.ts)}",
                )
            elif rid in assets:
                a = assets[rid]
                state = ""
                if a.kind == AssetKind.COMPRESSOR:
                    state = a.state or ("run" if a.running else "standby")
                ref = EvidenceRef(id=rid, kind="asset", title=a.name, detail=state or a.zone.replace("_", " "))
            elif kind == "lot":
                lot = plant.get_lot(rid)
                ref = EvidenceRef(
                    id=rid,
                    kind="lot",
                    title=f"Lot, {lot.carcases} carcases",
                    detail=f"{lot.customer} · {lot.status.value} · due {hhmm(lot.dispatch_due)}",
                    version=lot.version,
                )
            elif kind == "wo":
                orders = orders if orders is not None else _work_orders(plant)
                if rid in orders:
                    wo = orders[rid]
                    ref = EvidenceRef(
                        id=rid,
                        kind="wo",
                        title=wo.title,
                        detail=f"{wo.status} · {wo.asset_id} · created {wo.created_at:%d %b}",
                        version=wo.version,
                    )
            elif rid in alarms:
                a = alarms[rid]
                ref = EvidenceRef(
                    id=rid,
                    kind="alarm",
                    title=a.description,
                    detail=f"{a.priority.value.upper()} · {hhmm(a.ts)} · {'active' if a.active else 'cleared'}",
                )
            elif rid in inbox:
                m = inbox[rid]
                ref = EvidenceRef(id=rid, kind="msg", title=f"{m.subject} (untrusted)", detail=m.sender)
            elif rid in approvals:
                ap = approvals[rid]
                ref = EvidenceRef(id=rid, kind="approval", title=ap.title, detail=ap.status.value)
        except (KeyError, ValueError, IndexError):
            ref = None
        out[rid] = ref or EvidenceRef(id=rid, kind=kind, title=label)
    return out


def evidence_dict(refs: dict[str, EvidenceRef]) -> dict[str, dict[str, Any]]:
    """The ``evidence`` mapping the templates expect: id -> {title, detail, version}."""
    return {rid: r.model_dump(include={"title", "detail", "version"}) for rid, r in refs.items()}


def run_evidence_ids(run: RunTrace) -> list[str]:
    ids: list[str] = []
    for step in run.steps:
        for rid in step.evidence_ids:
            if rid not in ids:
                ids.append(rid)
    for approval in run.approvals:
        for rid in approval.evidence_ids:
            if rid not in ids:
                ids.append(rid)
    for rid in citations(run.handover_note):
        if rid not in ids:
            ids.append(rid)
    return ids


def run_chart(plant: PlantSystems, run: RunTrace) -> Markup | None:
    """Probe comparison for the chiller the run investigated, from the visitor's current historian data."""
    snap = plant.snapshot()
    chillers = {a.id: a for a in snap.assets if a.kind == AssetKind.CHILLER}
    asset_id = next((e.get("asset_id") for e in run.extra.get("evidence", []) if e.get("asset_id") in chillers),
                    None)
    if asset_id is None:
        cited = run_evidence_ids(run)
        asset_id = next((cid for cid, a in chillers.items() if set(probe_tags(a)) & set(cited)), None)
    if asset_id is None:
        return None
    primary, reference = probe_tags(chillers[asset_id])
    trends: list[Trend] = plant.get_trend([primary, reference], 60)
    alarmed = any(a.active and primary in a.tags for a in snap.alarms)
    vals = [p.value_c for t in trends for p in t.points if p.value_c is not None]
    return trend_svg(
        [Series(trends[1], "normal"), Series(trends[0], "amber" if alarmed else "normal")],
        width=400,
        height=190,
        events=_alarm_events(snap.alarms, [primary, reference]),
        divergence=(primary, reference, 2.0),
        y_min=min([1.0, *vals]),
        y_max=max([7.0, *vals]),
        limit=(4.0, "High 4.0 °C"),
        x_ticks=4,
        title=f"{primary} vs {reference}, last 60 minutes (current plant data)",
    )


# --------------------------------------------------------------------------- evals


def eval_rows(data: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Eval results in the shape the Evals page renders, plus the run summary."""
    data = data if data is not None else load_eval_results()
    rows = []
    for raw in data.get("cases", []):
        r = EvalResult.model_validate(raw)
        note = f"{r.category.capitalize()} · {r.cost_basis or r.mode}"
        if r.conclusion:
            note += f" · “{r.conclusion}”"
        rows.append(
            {
                "scenario": r.scenario,
                "case": r.title,
                "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in r.assertions],
                "passed": r.passed,
                "cost_usd": r.cost_usd,
                "latency_ms": r.latency_ms,
                "run_id": None,  # eval runs happen in their own sandbox, not this visitor's
                "note": note,
                "category": r.category,
            }
        )
    meta = {
        "generated_at": data.get("generated_at"),
        "mode": data.get("mode"),
        "summary": data.get("summary", {}),
    }
    return rows, meta


# --------------------------------------------------------------------------- impact

MANUAL_TRIAGE_MIN = 35  # assumed: find trends, check lots, check CMMS, phone QA
REVIEW_MIN = 10  # assumed: supervisor reads the trace and decides
EVENTS_PER_MONTH = 12  # assumed for a mid-sized plant with six chillers


def impact(plant: PlantSystems, agent: AgentService, sid: str) -> dict[str, Any]:
    runs = [r for r in agent.list_runs(sid) if r.status in FINISHED and r.status != RunStatus.FAILED]
    approvals = agent.list_approvals(sid)
    events = plant.audit_log(1000)
    durations = [r.duration_ms for r in runs if r.duration_ms]
    costs = [r.total_cost_usd for r in runs]
    held = sorted({e.target for e in events if e.action == "place_qa_hold" and e.actor == "agent"})
    carcases = 0
    for lot_id in held:
        try:
            carcases += plant.get_lot(lot_id).carcases
        except KeyError:
            pass
    blocked = sum(1 for r in runs for s in r.steps if s.kind == StepKind.GUARDRAIL and not s.ok)
    flagged = sum(len(r.extra.get("injection_flags", [])) for r in runs)
    pending = sum(1 for a in approvals if a.status == ApprovalStatus.PENDING)
    expired_n = sum(1 for a in approvals if a.status == ApprovalStatus.EXPIRED)
    decided = [a for a in approvals if a.status in {ApprovalStatus.APPROVED, ApprovalStatus.DECLINED}]
    executed_without_person = sum(
        1 for a in approvals if a.status == ApprovalStatus.APPROVED and (a.decided_by or "").lower() in {"agent", ""}
    )
    saved_h = EVENTS_PER_MONTH * (MANUAL_TRIAGE_MIN - REVIEW_MIN) / 60
    none = "Run a scenario on the overview to measure this."
    kpis = [
        {
            "label": "Alarm to triaged summary",
            "value": _seconds(statistics.median(durations)) if durations else "—",
            "baseline": f"≈ {MANUAL_TRIAGE_MIN} min",
            "note": f"median of {len(durations)} run(s) this session vs assumed manual triage" if durations else none,
            "spark": [round(d / 1000, 1) for d in durations[::-1]] if len(durations) > 1 else None,
            "kind": "measured",
        },
        {
            "label": "Supervisor time saved",
            "value": f"≈ {saved_h:g} h",
            "unit": "/ month",
            "note": f"{EVENTS_PER_MONTH} events × ({MANUAL_TRIAGE_MIN} − {REVIEW_MIN}) min review",
            "kind": "estimate",
        },
        {
            "label": "Product held for verification",
            "value": str(carcases) if held else "0",
            "unit": "carcases",
            "note": f"QA holds by the agent on {', '.join(held)}" if held else "No QA holds placed this session",
            "kind": "measured",
        },
        {
            "label": "Agent cost per investigation",
            "value": f"${statistics.median(costs):.3f}" if costs else "—",
            "note": "median; replays show the recorded cost of the live run they replay" if costs else none,
            "kind": "measured",
        },
        {
            "label": "Consequential actions decided by a person",
            "value": f"{len(decided)} of {len(approvals)}" if approvals else "—",
            "note": f"{pending} pending · {expired_n} expired · {executed_without_person} executed without a "
            "person (APPROVE tier, enforced in code)"
            if approvals
            else "No proposals yet this session",
            "kind": "measured",
        },
        {
            "label": "Unsafe requests blocked or flagged",
            "value": str(blocked + flagged),
            "note": f"{blocked} guardrail refusal(s) · {flagged} injected message(s) flagged, this session",
            "kind": "measured",
        },
    ]
    assumptions = [
        "All numbers come from a synthetic plant in your own sandbox. None come from a real business.",
        (
            f"Manual triage time ({MANUAL_TRIAGE_MIN} min) is an assumption: find the trends, check lots, check "
            "CMMS, phone QA. A real rollout would measure it for two weeks before switching anything on."
        ),
        f"Supervisor review of an agent investigation is assumed to take {REVIEW_MIN} min.",
        (
            f"Event frequency ({EVENTS_PER_MONTH} per month) is an assumption for a mid-sized plant with six "
            "chillers."
        ),
        (
            "Time saved counts supervisor minutes only. It does not count product value, which depends on "
            "customer, grade and whether the product was actually out of spec."
        ),
        (
            "Agent cost uses list API prices and measured tokens. In replay mode the cost shown is that of the "
            "live run being replayed; the replay itself costs nothing. Hosting is excluded."
        ),
        (
            "“Held for verification” is not “saved”: most holds are released once a reading confirms the "
            "product is fine."
        ),
    ]
    return {"kpis": kpis, "assumptions": assumptions}


def _seconds(ms: float) -> str:
    return f"{ms / 1000:.0f} s" if ms >= 1000 else f"{ms:.0f} ms"


# --------------------------------------------------------------------------- approvals


def approvals_context(rt: Runtime, v: Visitor, status: str) -> dict[str, Any]:
    items = rt.agent.list_approvals(v.sid)
    valid = {s.value for s in ApprovalStatus}
    status = status if status in valid else "all"
    shown = items if status == "all" else [a for a in items if a.status == status]
    return {"approvals": shown, "all_approvals": items, "status_filter": status}


def handover_run(rt: Runtime, sid: str, run_id: str | None) -> RunTrace | None:
    runs = rt.agent.list_runs(sid)
    if run_id:
        return next((r for r in runs if r.run_id == run_id), None)
    return next((r for r in runs if r.handover_note), None)
