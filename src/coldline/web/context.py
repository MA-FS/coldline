"""Page contexts built from a visitor's live sandbox and the agent service.

Each function returns exactly the context its template documents (see ``docs/DESIGN.md``); the routes stay
thin. Everything here reads through :class:`~coldline.contracts.PlantSystems` and the agent's public API.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime
from typing import Any

from markupsafe import Markup

from coldline.agent import AgentService, handover, load_eval_results
from coldline.agent.context import EVIDENCE_AT
from coldline.contracts import (
    Alarm,
    AlarmPriority,
    Approval,
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
)
from coldline.records import describe_records
from coldline.web.charts import PROBE_LIMIT_LINE, ChartEvent, Series, trend_svg
from coldline.web.render import base_context, hhmm
from coldline.web.runtime import ROLES, Runtime, Visitor
from coldline.web.views import chiller_detail, citations, plant_view, probe_tags

CHILLER_NAMEPLATE = {"capacity_carcases": 160, "status": "In service"}
FINISHED = {RunStatus.DONE, RunStatus.AWAITING_APPROVAL, RunStatus.INCOMPLETE, RunStatus.FAILED}


# --------------------------------------------------------------------------- shell


def shell(rt: Runtime, v: Visitor, active: str, title: str) -> dict[str, Any]:
    """Base context plus what the shell shows about this visitor: mode, live budget, role, pending count."""
    status = rt.agent.live_status(v.sid, v.ip)
    pending = len(rt.agent.list_approvals(v.sid, ApprovalStatus.PENDING))
    ctx = base_context(
        active,
        v.plant.now(),
        mode=v.prefs.mode,
        title=title,
        pending_approvals=pending,
        live_available=status.live_available,
    )
    ctx.update(live_status=status, role=v.prefs.role, roles=ROLES, session_notice=v.prefs.notice)
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


def run_zones(run: RunTrace | None) -> list[str]:
    """Chillers an investigation looked at (its lot lookups), else the asset its panel names."""
    if run is None:
        return []
    zones = [entry.split(":", 1)[1] for entry in run.extra.get("lookups", []) if entry.startswith("lots:")]
    return zones or [run.extra.get("asset_id", "")]


def detail_context(plant: PlantSystems, focus: str | None,
                   run: RunTrace | None = None) -> tuple[Any, dict[str, Any]]:
    """(PlantView, chiller detail dict) for the mimic and the detail panel."""
    snap = plant.snapshot()
    chillers = [a for a in snap.assets if a.kind == AssetKind.CHILLER]
    tags = [t for a in chillers for t in probe_tags(a)]
    trends = plant.get_trend(tags, 60)
    pv = plant_view(snap, trends, run_zones(run))
    ids = [a.id for a in chillers]
    focus = focus if focus in ids else default_focus(snap.alarms, ids)
    asset = next(a for a in chillers if a.id == focus)
    focus_tags = probe_tags(asset)
    focus_trends = [t for t in trends if t.tag in focus_tags]
    detail = chiller_detail(snap, pv, focus, focus_trends, _alarm_events(snap.alarms, focus_tags))
    # Nameplate facts the simulator does not model; display only.
    detail["asset"] = asset.model_copy(update={"detail": {**CHILLER_NAMEPLATE, **asset.detail}})
    return pv, detail


def state_revision(plant: PlantSystems, run: RunTrace | None, approvals: list[Approval]) -> str:
    """A short fingerprint of what the overview's regions show: the incident (faults and resets), the latest run
    and its status, every proposal's status, the active holds and which probes are not reporting fresh data.
    The mimic's 5-second poll carries it; when it differs from the page's, the page re-renders as a whole, so
    a tab left open never shows one investigation's conclusion and decisions beside another's plant."""
    snap = plant.snapshot()
    material = {
        "incident": snap.incident,
        "run": [run.run_id, run.status.value] if run else None,
        "approvals": sorted([a.id, a.status.value] for a in approvals),
        "holds": sorted(h.id for h in plant.active_holds()),
        "not_fresh": sorted(t for t, r in snap.latest.items() if r.quality != Quality.GOOD),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:12]


def overview(rt: Runtime, v: Visitor) -> dict[str, Any]:
    run = latest_run(rt, v.sid)
    pv, detail = detail_context(v.plant, v.prefs.focus, run)
    every = rt.agent.list_approvals(v.sid)
    # The panel shows the proposals this run asked for, including pending ones an earlier run created on the
    # same evidence and this run reused; others stay on the Approvals page (and in the nav count).
    approvals = [a for a in every if run is not None and run.run_id in (a.run_ids or [a.run_id])]
    approvals.sort(key=lambda a: (len(a.id), a.id))  # proposal order (AP-0001 first), as the stream adds them
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
        "rev": state_revision(v.plant, run, every),
    }


# --------------------------------------------------------------------------- evidence


def evidence_refs(plant: PlantSystems, agent: AgentService, sid: str, ids: list[str]) -> dict[str, EvidenceRef]:
    """Describe each cited record from the visitor's current systems, for evidence tables and citations."""
    return describe_records(plant, {a.id: a for a in agent.list_approvals(sid)}, ids)


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


def run_evidence(plant: PlantSystems, agent: AgentService, sid: str, run: RunTrace) -> list[dict[str, Any]]:
    """The trace's evidence rows: each cited record as it read when the run cited it (``then``, with the plant
    time and step), and how it reads now only where that differs, labelled as now. A record the run did not
    describe when it cited it (a run recorded before descriptions were stored) shows only its current state,
    labelled as such, never as the run's evidence."""
    recorded = run.extra.get(EVIDENCE_AT) or {}
    ids = [i for s in run.steps for i in s.evidence_ids]
    ids = list(dict.fromkeys([*ids, *run_evidence_ids(run)]))
    now = evidence_dict(evidence_refs(plant, agent, sid, ids))
    rows = []
    for rid in ids:
        before, after = recorded.get(rid), now.get(rid, {})
        then = _ref_text(before) if before else ""
        current = _ref_text(after)
        rows.append({
            "id": rid,
            "title": (before or after).get("title") or "",
            "then": then,
            "then_at": datetime.fromisoformat(before["at"]) if before and before.get("at") else None,
            "then_step": before.get("step") if before else None,
            "now": current if not before or current != then else "",
            "steps": [s.n for s in run.steps if rid in s.evidence_ids],
        })
    return rows


def _ref_text(ref: dict[str, Any]) -> str:
    detail = ref.get("detail") or ""
    return f"{detail} · v{ref['version']}" if detail and ref.get("version") else detail


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
        limit=PROBE_LIMIT_LINE,
        x_ticks=4,
        title=f"{primary} vs {reference}, the last 60 minutes now (current plant data, not as at this run)",
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
    # Cost and latency medians over one population only: the scenario cases, one per shipped recording. The
    # approval cases replay those same recordings and the adversarial ones use scripted models with no tokens,
    # so mixing them in would count one recording several times and add zeros.
    scenario = [r for r in rows if r["category"] == "scenario"]
    meta = {
        "generated_at": data.get("generated_at"),
        "mode": data.get("mode"),
        "summary": data.get("summary", {}),
        "median_n": len(scenario),
        "median_cost": statistics.median(r["cost_usd"] for r in scenario) if scenario else None,
        "median_latency": statistics.median(r["latency_ms"] for r in scenario) if scenario else None,
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
    # Observed here: how long the visitor actually waited (replay playback or the live run's wall time) and
    # what live calls really cost. Replays also carry the source recording's own cost, shown separately.
    waits = [r.extra.get("wall_ms", 0) for r in runs if r.extra.get("wall_ms")]
    # A live run is one that called the model. A live request refused before any call (cap, limits, kill
    # switch) ran as a replay and cost nothing. Calls whose outcome is unknown are not a confirmed zero:
    # their reserved bound counts as unreconciled until checked against the provider's usage report.
    live = [r for r in runs if r.extra.get("live_calls")]
    refused_live = sum(1 for r in runs if r.extra.get("live_requested") and not r.extra.get("live_calls"))
    live_calls = sum(r.extra.get("live_calls", 0) for r in live)
    live_spend = sum(r.extra.get("live_cost_usd", 0.0) for r in live)
    unreconciled = sum(r.extra.get("unreconciled_usd", 0.0) for r in live)
    estimated = sum(r.extra.get("estimated_usd", 0.0) for r in live)
    carried = agent.live_status(sid).carried_unreconciled  # earlier days' spend still not reconciled
    recorded_costs = [r.extra.get("recorded_cost_usd", 0.0) for r in runs if r.extra.get("recording")]
    authored = sum(1 for r in runs if r.extra.get("recording", {}).get("recorded") == "hand-authored")
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
            "value": _seconds(statistics.median(waits)) if waits else "—",
            "baseline": f"≈ {MANUAL_TRIAGE_MIN} min",
            "note": (f"median wait over {len(waits)} run(s) this session ({len(runs) - len(live)} replayed at "
                     f"recorded pace, {len(live)} live) vs assumed manual triage") if waits else none,
            "spark": [round(d / 1000, 1) for d in waits[::-1]] if len(waits) > 1 else None,
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
            "label": "Live model spend",
            "value": f"${live_spend:.3f}" if live else "$0",
            "unit": " ".join(x for x in (f"+ ${estimated:.3f} estimated" if estimated else "",
                                         f"+ ${unreconciled:.3f} unreconciled" if unreconciled else "") if x),
            "note": _live_note(len(live), live_calls, unreconciled, refused_live, estimated, carried),
            "kind": "measured",
        },
        {
            "label": "Recorded cost per investigation",
            "value": f"${statistics.median(recorded_costs):.3f}" if recorded_costs else "—",
            "note": ("median cost of the source recordings replayed this session, not spent here"
                     + (f"; {authored} replay(s) use a hand-authored trace priced from live runs" if authored else ""))
            if recorded_costs else none,
            "kind": "recorded",
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
            "label": "Proposals retired by changed evidence",
            "value": str(expired_n),
            "note": "expired before anyone could act on them: a new fault, a stale probe or a changed record; "
            "nothing ran" if approvals else "No proposals yet this session",
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
            "Agent cost uses list API prices and measured tokens. Live spend is what this session's live calls "
            "cost by the provider's reported usage; estimated and unreconciled amounts are shown apart and only "
            "count against the daily cap. Recorded cost is what the live run behind each replay cost when it was recorded; the replay "
            "itself costs nothing, and the poisoned-email trace is hand-authored with token counts taken from "
            "live runs. Hosting is excluded."
        ),
        (
            "“Held for verification” is not “saved”: most holds are released once a reading confirms the "
            "product is fine."
        ),
    ]
    return {"kpis": kpis, "assumptions": assumptions}


def _live_note(runs: int, calls: int, unreconciled: float, refused: int, estimated: float = 0.0,
               carried: dict[str, float] | None = None) -> str:
    parts = []
    if runs:
        parts.append(f"measured: provider-reported usage of {calls} model call(s) in {runs} live run(s) this "
                     "session, at list prices")
    else:
        parts.append("Replays cost nothing; switch to Live to measure real spend")
    if estimated:
        parts.append(f"US${estimated:.3f} more is estimated, not measured: attempts that declined before any "
                     "output in a refusal category the provider does not bill (or an unknown one). Their reported "
                     "usage still counts against the daily cap until reconciled")
    if unreconciled:
        parts.append(f"US${unreconciled:.3f} more is unreconciled: calls whose outcome is unknown (a timeout or "
                     "lost connection) count at their full reserved bound until checked against the provider")
    if carried:
        days = ", ".join(f"US${usd:.3f} from {day}" for day, usd in carried.items())
        parts.append(f"{days} is still unreconciled from earlier days (no longer counted against today's cap)")
    if refused:
        parts.append(f"{refused} live request(s) ran as replay before any model call and cost nothing")
    return "; ".join(parts)


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


def handover_context(rt: Runtime, v: Visitor, run: RunTrace | None) -> dict[str, Any]:
    """The Handover page: the run's note as the snapshot it is, how the plant differs now, and the current
    holds and proposals (shown on every visit, including while an investigation is running or after one
    ended without a handover).

    ``sources`` has one row per cited record: its description when the note was accepted (``then``) and,
    where that differs, how it reads now (``now``), so the table can never contradict the note beside it."""
    runs = rt.agent.list_runs(v.sid)
    approvals = rt.agent.list_approvals(v.sid)
    ctx: dict[str, Any] = {
        "run": run,
        "latest": runs[0] if runs else None,
        "current": handover.current_lines(v.plant, approvals),
        "plant_now": v.plant.now(),
        "sup": None,
        "sources": [],
    }
    if run is None or not run.handover_note:
        return ctx
    later = runs[: next((i for i, r in enumerate(runs) if r.run_id == run.run_id), 0)]
    sup = handover.supersession(run, v.plant, approvals, later)
    then = (run.extra.get("handover_snapshot") or {}).get("sources", {})
    ids = citations(run.handover_note)
    now = evidence_dict(evidence_refs(v.plant, rt.agent, v.sid, ids))
    rows = []
    for rid in ids:
        before, after = then.get(rid), now.get(rid, {})
        base = before or after
        detail_now = after.get("detail") or ""
        rows.append({
            "id": rid,
            "title": base.get("title") or "",
            "then": (before or {}).get("detail") or "",
            "now": detail_now if before is None or detail_now != before.get("detail") else "",
            "has_then": before is not None,
        })
    ctx.update(sup=sup, sources=rows)
    return ctx
