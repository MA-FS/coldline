"""Read-only routes: every page, the chiller fragment and the agent's SSE stream.

All of them read the visitor's own sandbox (selected by the session cookie) and the agent service. The
state-changing routes are in :mod:`coldline.web.routes_actions`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from coldline.contracts import RunTrace
from coldline.web import context as cx
from coldline.web.render import templates
from coldline.web.runtime import Runtime, Visitor, runtime, visitor
from coldline.web.views import citations

router = APIRouter(include_in_schema=False)
V = Annotated[Visitor, Depends(visitor)]
RT = Annotated[Runtime, Depends(runtime)]


def page(request: Request, template: str, ctx: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx)


def owned_run(rt: Runtime, v: Visitor, run_id: str) -> RunTrace:
    """A run of this visitor's session. Other visitors' runs are indistinguishable from missing ones."""
    run = rt.agent.get_run(run_id)
    if run is None or run.session_id != v.sid:
        raise HTTPException(404, f"No run {run_id} in this session.")
    return run


# --------------------------------------------------------------------------- pages


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request, v: V, rt: RT) -> HTMLResponse:
    return page(request, "pages/overview.html", cx.overview(rt, v))


@router.get("/chillers/{asset_id}", response_class=HTMLResponse)
async def chiller(request: Request, asset_id: str, v: V) -> HTMLResponse:
    ids = cx.chiller_ids(v.plant)
    if asset_id not in ids:
        raise HTTPException(404, f"No chiller {asset_id}.")
    v.prefs.focus = asset_id
    _, detail = cx.detail_context(v.plant, asset_id)
    return page(request, "fragments/chiller_detail.html", {"detail": detail})


@router.get("/runs", response_class=HTMLResponse)
async def runs(request: Request, v: V, rt: RT) -> HTMLResponse:
    ctx = {**cx.shell(rt, v, "runs", "Agent runs"), "runs": rt.agent.list_runs(v.sid)}
    return page(request, "pages/runs.html", ctx)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: str, v: V, rt: RT) -> HTMLResponse:
    run = owned_run(rt, v, run_id)
    refs = cx.evidence_refs(v.plant, rt.agent, v.sid, cx.run_evidence_ids(run))
    ctx = {
        **cx.shell(rt, v, "runs", f"Run {run.run_id}"),
        "run": run,
        "evidence": cx.evidence_dict(refs),
        "evidence_chart": cx.run_chart(v.plant, run),
    }
    return page(request, "pages/run_detail.html", ctx)


@router.get("/approvals", response_class=HTMLResponse)
async def approvals(request: Request, v: V, rt: RT, status: str = "all") -> HTMLResponse:
    ctx = {**cx.shell(rt, v, "approvals", "Approvals"), **cx.approvals_context(rt, v, status)}
    return page(request, "pages/approvals.html", ctx)


@router.get("/audit", response_class=HTMLResponse)
async def audit(request: Request, v: V, rt: RT) -> HTMLResponse:
    events = list(reversed(v.plant.audit_log(500)))
    return page(request, "pages/audit.html", {**cx.shell(rt, v, "audit", "Audit log"), "events": events})


@router.get("/evals", response_class=HTMLResponse)
async def evals(request: Request, v: V, rt: RT) -> HTMLResponse:
    rows, meta = cx.eval_rows()
    ctx = {**cx.shell(rt, v, "evals", "Evals"), "evals": rows, "eval_meta": meta}
    return page(request, "pages/evals.html", ctx)


@router.get("/impact", response_class=HTMLResponse)
async def impact(request: Request, v: V, rt: RT) -> HTMLResponse:
    ctx = {**cx.shell(rt, v, "impact", "Impact"), **cx.impact(v.plant, rt.agent, v.sid)}
    return page(request, "pages/impact.html", ctx)


@router.get("/handover", response_class=HTMLResponse)
async def handover(request: Request, v: V, rt: RT, run_id: str | None = None) -> HTMLResponse:
    run = cx.handover_run(rt, v.sid, run_id)
    if run_id and run is None:
        raise HTTPException(404, f"No run {run_id} in this session.")
    evidence = {}
    if run is not None:
        evidence = cx.evidence_dict(cx.evidence_refs(v.plant, rt.agent, v.sid, citations(run.handover_note)))
    ctx = {**cx.shell(rt, v, "handover", "Shift handover"), "run": run, "evidence": evidence}
    return page(request, "pages/handover.html", ctx)


@router.get("/how-it-works", response_class=HTMLResponse)
async def how_it_works(request: Request, v: V, rt: RT) -> HTMLResponse:
    return page(request, "pages/how_it_works.html", cx.shell(rt, v, "how", "How it works"))


# --------------------------------------------------------------------------- agent stream


def _last_event_id(request: Request, after: int | None) -> int:
    if after is not None:
        return max(0, after)
    raw = request.headers.get("last-event-id", "")
    return int(raw) if raw.isdigit() else 0


@router.get("/runs/{run_id}/stream")
async def run_stream(request: Request, run_id: str, v: V, rt: RT, after: int | None = None) -> EventSourceResponse:
    """Server-sent events for a run.

    ``step`` (agent-panel card) and ``trace-step`` (run-page timeline row) for every TraceStep, ``approval``
    when a step queues a proposal, then ``conclusion`` and ``done``. Event ids are step numbers, so a
    reconnecting browser sends ``Last-Event-ID`` and resumes after the last step it saw.
    """
    run = owned_run(rt, v, run_id)
    start = _last_event_id(request, after)

    async def events() -> AsyncIterator[dict[str, str]]:
        # An approval is sent with the step that queued it (the first step citing its id), so a resumed
        # stream does not repeat approvals delivered before the reconnect.
        sent = {rid for s in run.steps[:start] for rid in s.evidence_ids}
        offset = sum(s.latency_ms for s in run.steps[:start])
        async for step in rt.agent.stream(run.run_id, after=start):
            eid = str(step.n)
            yield {"event": "step", "id": eid, "data": _render("fragments/agent_step.html", step=step)}
            total = max(offset + step.latency_ms, 1)
            yield {
                "event": "trace-step",
                "id": eid,
                "data": _render("fragments/trace_step.html", step=step, offset=offset, total=total),
            }
            offset += step.latency_ms
            for approval in run.approvals:
                if approval.id in step.evidence_ids and approval.id not in sent:
                    sent.add(approval.id)
                    html = _render("fragments/approval_card.html", approval=approval, compact=True)
                    yield {"event": "approval", "id": eid, "data": html}
        if run.conclusion:
            yield {"event": "conclusion", "data": _render("fragments/conclusion.html", run=run)}
        yield {"event": "done", "data": run.status.value}

    return EventSourceResponse(events(), ping=15, headers={"Cache-Control": "no-store"})


def _render(template: str, **ctx: object) -> str:
    return templates.get_template(template).render(**ctx)
