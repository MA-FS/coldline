"""Read-only routes: every page, the chiller fragment and the agent's SSE stream.

All of them read the visitor's own sandbox (selected by the session cookie) and the agent service. The
state-changing routes are in :mod:`coldline.web.routes_actions`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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


STREAM_REGISTRATION_WAIT_S = 1.0


def owned_run(rt: Runtime, v: Visitor, run_id: str) -> RunTrace:
    """A run of this visitor's session. Run ids are numbered per visitor, so the lookup is session-scoped
    and other visitors' runs are indistinguishable from missing ones."""
    run = rt.agent.get_run(v.sid, run_id)
    if run is None:
        raise HTTPException(404, f"No run {run_id} in this session.")
    return run


async def wait_for_run(rt: Runtime, v: Visitor, run_id: str) -> RunTrace | None:
    """The run, allowing a moment for it to be registered (a page can connect as a run starts)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STREAM_REGISTRATION_WAIT_S
    while (run := rt.agent.get_run(v.sid, run_id)) is None and loop.time() < deadline:
        await asyncio.sleep(0.05)
    return run


# --------------------------------------------------------------------------- pages


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request, v: V, rt: RT) -> HTMLResponse:
    return page(request, "pages/overview.html", cx.overview(rt, v))


@router.get("/chillers/{asset_id}", response_class=HTMLResponse)
async def chiller(request: Request, asset_id: str, v: V) -> Response:
    """The chiller detail panel for htmx. Opened directly in a browser, it shows the overview focused on
    that chiller instead of a bare fragment without the disclaimer and navigation."""
    ids = cx.chiller_ids(v.plant)
    if asset_id not in ids:
        raise HTTPException(404, f"No chiller {asset_id}.")
    v.prefs.focus = asset_id
    if not request.headers.get("hx-request"):
        return RedirectResponse("/", status_code=303)
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


@router.get("/shell", response_class=HTMLResponse)
async def shell_fragment(request: Request, v: V, rt: RT) -> HTMLResponse:
    """Out-of-band fragments for the page shell; ``app.js`` fetches it after anything that changes state."""
    return page(request, "fragments/shell.html", cx.shell(rt, v, "", ""))


@router.get("/how-it-works", response_class=HTMLResponse)
async def how_it_works(request: Request, v: V, rt: RT) -> HTMLResponse:
    return page(request, "pages/how_it_works.html", cx.shell(rt, v, "how", "How it works"))


# --------------------------------------------------------------------------- agent stream


def resume_cursor(request: Request, after: int | None) -> int:
    """Where to resume: the newest of the page's snapshot cursor (``?after=``) and the browser's
    ``Last-Event-ID`` (set by EventSource on reconnect, and always later than the snapshot)."""
    raw = request.headers.get("last-event-id", "")
    candidates = [max(0, after or 0), int(raw) if raw.isdigit() else 0]
    return max(candidates)


@router.get("/runs/{run_id}/stream")
async def run_stream(request: Request, run_id: str, v: V, rt: RT, after: int | None = None,
                     view: str = "panel") -> EventSourceResponse:
    """Server-sent events for a run: exactly one event per TraceStep, with the step number as its id.

    ``view=panel`` (the overview's agent panel) sends ``step`` events whose HTML also carries, out of band,
    the approval cards that step queued. ``view=trace`` (the run page) sends ``trace-step`` timeline rows.
    Then ``conclusion`` and ``done``. Because each step is one event, a browser that reconnects with
    ``Last-Event-ID: n`` resumes at step n + 1 with nothing skipped or repeated; pages open the stream with
    ``?after=<steps already rendered>`` so attaching mid-run does not repeat the snapshot either. The client
    also drops any fragment whose element id is already on the page (``app.js``).

    A run that is not (or no longer) in this session, for example after a reset, gets a single ``done``
    event with ``data: missing`` rather than a 404: the page closes the stream and refreshes, instead of
    the browser retrying a failed EventSource for ever. Nothing about other sessions is revealed.
    """
    run = await wait_for_run(rt, v, run_id)
    if run is None:
        async def missing() -> AsyncIterator[dict[str, str]]:
            yield {"event": "done", "data": "missing"}

        return EventSourceResponse(missing(), headers={"Cache-Control": "no-store"})
    start = resume_cursor(request, after)
    trace = view == "trace"

    async def events() -> AsyncIterator[dict[str, str]]:
        # An approval travels with the step that queued it (the first step citing its id).
        sent = {rid for s in run.steps[:start] for rid in s.evidence_ids}
        offset = sum(s.latency_ms for s in run.steps[:start])
        async for step in rt.agent.stream(v.sid, run.run_id, after=start):
            eid = str(step.n)
            if trace:
                total = max(offset + step.latency_ms, 1)
                html = _render("fragments/trace_step.html", step=step, offset=offset, total=total)
                yield {"event": "trace-step", "id": eid, "data": html}
            else:
                cards = []
                for approval in run.approvals:
                    if approval.id in step.evidence_ids and approval.id not in sent:
                        sent.add(approval.id)
                        cards.append(_render("fragments/approval_card.html", approval=approval, compact=True))
                html = _render("fragments/agent_step.html", step=step)
                if cards:
                    html += f'<div hx-swap-oob="beforeend:#agent-approvals">{"".join(cards)}</div>'
                yield {"event": "step", "id": eid, "data": html}
            offset += step.latency_ms
        if run.conclusion and not trace:
            yield {"event": "conclusion", "data": _render("fragments/conclusion.html", run=run)}
        yield {"event": "done", "data": run.status.value}

    return EventSourceResponse(events(), ping=15, headers={"Cache-Control": "no-store"})


def _render(template: str, **ctx: object) -> str:
    return templates.get_template(template).render(**ctx)
