"""Fixture-backed preview of every page, plus ``/styleguide``.

Mounted at ``/preview/...`` (development only, see ``COLDLINE_PREVIEWS``) so every page can be reviewed
against known data, independent of the simulator.
Each handler shows the exact context its template expects, which is the contract for the real routes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from coldline.agent.handover import Supersession
from coldline.contracts import ApprovalStatus, Scenario
from coldline.web import fixtures as fx
from coldline.web.charts import ChartEvent, Series, trend_svg
from coldline.web.render import base_context, templates
from coldline.web.views import chiller_detail, citations, plant_view

router = APIRouter(include_in_schema=False)
styleguide_router = APIRouter(include_in_schema=False)  # kept in production; previews are not
P = "/preview"

PAGES = [
    "",
    "/overview",
    "/runs",
    "/runs/R-7842",
    "/approvals",
    "/audit",
    "/evals",
    "/impact",
    "/handover",
    "/how-it-works",
]


def _base(active: str, title: str, **kw: Any) -> dict[str, Any]:
    pending = sum(1 for a in fx.approvals() if a.status == ApprovalStatus.PENDING)
    return base_context(active, fx.NOW, title=title, pending_approvals=pending, href_prefix=P, **kw)


def overview_context(state: str = "hero", focus: str = "CH4") -> dict[str, Any]:
    """state: hero | stale | trip | normal | running | idle"""
    snap_state = {"running": "hero", "idle": "normal"}.get(state, state)
    snap = fx.snapshot(snap_state)
    if snap_state == "trip":
        trends = fx.trip_trends()
    elif snap_state == "normal":
        trends = [
            t.model_copy(
                update={
                    "points": [
                        p.model_copy(update={"value_c": round(2.1 + (p.value_c - 2.1) * 0.03, 2)})
                        for p in t.points
                    ]
                }
            )
            for t in fx.hero_trends()
        ]
    else:
        trends = fx.hero_trends(stale=snap_state == "stale")
    plant = plant_view(snap, trends)
    events = fx.hero_events() if snap_state in {"hero", "stale"} else []
    if focus != "CH4":
        n = focus[-1]
        trends = [
            t.model_copy(
                update={
                    "tag": f"TT-{n}0{i + 1}",
                    "points": [
                        p.model_copy(
                            update={
                                "tag": f"TT-{n}0{i + 1}",
                                "value_c": v,
                                "quality": p.quality.__class__.GOOD,
                            }
                        )
                        for p, v in zip(t.points, fx.spark(i + int(n), 2.0, len(t.points)), strict=True)
                    ],
                }
            )
            for i, t in enumerate(fx.hero_trends())
        ]
        events = []
    detail = chiller_detail(snap, plant, focus, trends, events)
    run = None
    approvals = []
    if state == "hero":
        run, approvals = fx.hero_run(), fx.hero_approvals()
    elif state == "running":
        run = fx.running_run()
    elif state == "stale":
        run, approvals = fx.stale_run(), fx.hero_approvals(ApprovalStatus.EXPIRED)[:1]
    elif state == "trip":
        run = fx.trip_run()
    return {
        "plant": plant,
        "snapshot": snap,
        "detail": detail,
        "run": run,
        "approvals": approvals,
        "scenarios": list(Scenario),
        "poll_url": None,
    }


@router.get("/preview", response_class=HTMLResponse)
def preview_root() -> RedirectResponse:
    return RedirectResponse(f"{P}/", status_code=307)


@router.get("/preview/", response_class=HTMLResponse)
@router.get("/preview/overview", response_class=HTMLResponse)
def preview_overview(request: Request, state: str = "hero") -> HTMLResponse:
    ctx = {**_base("overview", "Plant overview"), **overview_context(state)}
    return templates.TemplateResponse(request, "pages/overview.html", ctx)


@router.get("/preview/chillers/{asset_id}", response_class=HTMLResponse)
def preview_chiller(request: Request, asset_id: str, state: str = "hero") -> HTMLResponse:
    if asset_id not in {f"CH{n}" for n in range(1, 7)}:
        raise HTTPException(404)
    ctx = overview_context(state, focus=asset_id)
    return templates.TemplateResponse(request, "fragments/chiller_detail.html", {"detail": ctx["detail"]})


@router.get("/preview/runs", response_class=HTMLResponse)
def preview_runs(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "pages/runs.html", {**_base("runs", "Agent runs"), "runs": fx.runs()}
    )


@router.get("/preview/runs/{run_id}", response_class=HTMLResponse)
def preview_run(request: Request, run_id: str) -> HTMLResponse:
    run = fx.run_by_id(run_id)
    if run is None:
        raise HTTPException(404)
    chart = None
    if run.scenario == Scenario.PROBE_MISMATCH:
        stale = run.run_id == "R-7843"
        a, b = fx.hero_trends(stale=stale)
        chart = trend_svg(
            [Series(b, "normal"), Series(a, "amber")],
            width=400,
            height=190,
            events=fx.hero_events(),
            divergence=("TT-401", "TT-402", 2.0),
            y_min=1,
            y_max=7,
            limit=(4.0, "High 4.0 °C"),
            x_ticks=4,
            title="TT-401 vs TT-402, last 60 minutes",
        )
    ctx = {
        **_base("runs", f"Run {run.run_id}"),
        "run": run,
        "evidence": fx.evidence(),
        "evidence_chart": chart,
    }
    return templates.TemplateResponse(request, "pages/run_detail.html", ctx)


@router.get("/preview/approvals", response_class=HTMLResponse)
def preview_approvals(request: Request, status: str = "all") -> HTMLResponse:
    items = fx.approvals()
    shown = items if status == "all" else [a for a in items if a.status == status]
    ctx = {
        **_base("approvals", "Approvals"),
        "approvals": shown,
        "all_approvals": items,
        "status_filter": status,
    }
    return templates.TemplateResponse(request, "pages/approvals.html", ctx)


@router.get("/preview/audit", response_class=HTMLResponse)
def preview_audit(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "pages/audit.html", {**_base("audit", "Audit log"), "events": fx.audit_events()}
    )


@router.get("/preview/evals", response_class=HTMLResponse)
def preview_evals(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "pages/evals.html", {**_base("evals", "Evals"), "evals": fx.evals(),
                                       "eval_meta": {"median_n": 0, "median_cost": None, "median_latency": None}}
    )


@router.get("/preview/impact", response_class=HTMLResponse)
def preview_impact(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "pages/impact.html", {**_base("impact", "Impact"), **fx.impact()}
    )


@router.get("/preview/handover", response_class=HTMLResponse)
def preview_handover(request: Request, run_id: str = "R-7842") -> HTMLResponse:
    run = fx.run_by_id(run_id) or fx.hero_run()
    ev = fx.evidence()
    sources = [{"id": rid, "title": ev.get(rid, {}).get("title", ""), "then": ev.get(rid, {}).get("detail", ""),
                "now": "", "has_then": True} for rid in citations(run.handover_note)]
    current = [f"[{a.payload['lot_id']}]: on QA hold." for a in run.approvals if "lot_id" in a.payload][:1] or [
        "No lot is on QA hold."]
    ctx = {**_base("handover", "Shift handover"), "run": run, "latest": run, "plant_now": run.started_at,
           "sup": Supersession(at=run.started_at, current=current), "current": current, "sources": sources}
    return templates.TemplateResponse(request, "pages/handover.html", ctx)


@router.get("/preview/how-it-works", response_class=HTMLResponse)
def preview_how(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "pages/how_it_works.html", _base("how", "How it works"))


@styleguide_router.get("/styleguide", response_class=HTMLResponse)
def styleguide(request: Request) -> HTMLResponse:
    ov = overview_context("stale")
    hero = overview_context("hero")
    trip = overview_context("trip")
    a, b = fx.hero_trends(stale=True)
    stale_chart = trend_svg(
        [Series(a, "amber"), Series(b)],
        width=520,
        height=170,
        events=fx.hero_events(),
        divergence=("TT-401", "TT-402", 2.0),
        y_min=1,
        y_max=7,
        title="Stale segment demo",
    )
    door_chart = trend_svg(
        [Series(fx.hero_trends()[1])],
        width=520,
        height=170,
        events=[ChartEvent(fx.DOOR_OPEN, "Door 05:40")],
        y_min=1,
        y_max=7,
        limit=(4.0, "High 4.0 °C"),
    )
    ctx = {
        **_base("", "Styleguide"),
        "stale_plant": ov["plant"],
        "hero": hero,
        "trip_plant": trip["plant"],
        "run": fx.hero_run(),
        "running": fx.running_run(),
        "stale_run": fx.stale_run(),
        "poisoned": fx.poisoned_run(),
        "approvals": fx.approvals(),
        "events": fx.audit_events()[:4],
        "stale_chart": stale_chart,
        "door_chart": door_chart,
        "impact": fx.impact(),
        "alarms": fx.snapshot("trip").alarms + fx.snapshot("stale").alarms,
    }
    return templates.TemplateResponse(request, "pages/styleguide.html", ctx)
