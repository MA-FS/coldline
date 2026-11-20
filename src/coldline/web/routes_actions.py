"""State-changing routes: demo controls on the overview and approval decisions.

Every route here is a POST behind :func:`~coldline.web.runtime.limit_posts` (CSRF header check and a per-IP
rate limit). Controls that change the plant and start an agent run are admitted *before* they touch the
plant: while an investigation is running they are refused (409), because changing the evidence under a
running investigation would leave it following a plan made for a different plant; then the stricter per-IP
run limit applies (429). A refused control leaves the plant exactly as it was. Responses are HTML for htmx
to swap, with a toast in the ``HX-Trigger`` header.
"""

from __future__ import annotations

import json
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from coldline.agent.approvals import StaleDecision
from coldline.contracts import Scenario
from coldline.web import context as cx
from coldline.web.render import templates
from coldline.web.runtime import ROLES, Runtime, Visitor, limit_posts, limit_runs, runtime, visitor
from coldline.web.views import SCENARIO_LABELS

router = APIRouter(include_in_schema=False, dependencies=[Depends(limit_posts)])
V = Annotated[Visitor, Depends(visitor)]
RT = Annotated[Runtime, Depends(runtime)]


def toast(title: str, text: str = "", tone: str = "neutral", **extra: Any) -> dict[str, str]:
    return {"HX-Trigger": json.dumps({"toast": {"title": title, "text": text, "tone": tone}, **extra})}


def render_overview(request: Request, rt: Runtime, v: Visitor, headers: dict[str, str]) -> HTMLResponse:
    """The full overview page; the controls swap ``#overview`` out of it with ``hx-select`` (and the header's
    approvals count, mode pill and clock with ``hx-select-oob``)."""
    return templates.TemplateResponse(request, "pages/overview.html", cx.overview(rt, v), headers=headers)


def admit_control(rt: Runtime, v: Visitor, starts_run: bool = True) -> None:
    """Refuse a plant-changing control before it changes anything (see the module docstring)."""
    busy = rt.agent.active_run(v.sid)
    if busy is not None:
        raise HTTPException(
            409, f"The agent is still investigating ({busy.run_id}). Wait for it to finish, or reset the plant.")
    if starts_run:
        limit_runs(v, rt)


async def start_investigation(rt: Runtime, v: Visitor, scenario: Scenario | None) -> tuple[str, str]:
    """Start the visitor's agent run (admission already checked). Returns (run id, sentence for the toast)."""
    busy = rt.agent.active_run(v.sid)
    run = await rt.agent.start_run(v.sid, v.plant, scenario=scenario, mode=v.prefs.mode, origin=v.ip)
    if "asset_id" not in run.extra:  # what the agent panel says it is investigating
        rank = {"red": 0, "amber": 1, "low": 2}
        alarms = sorted((a for a in v.plant.list_alarms() if a.active), key=lambda a: rank[a.priority.value])
        fallback = "the inbox" if run.scenario == Scenario.POISONED_EMAIL else cx.default_focus([], ["CH4"])
        run.extra["asset_id"] = alarms[0].asset_id if alarms else fallback
    if busy is not None and busy.run_id == run.run_id:
        return run.run_id, f"The agent is still working on {run.run_id}; it was not restarted."
    mode = "live model" if v.prefs.mode == "live" else "recorded run (replay)"
    return run.run_id, f"Agent investigating as {run.run_id} ({mode})."


# --------------------------------------------------------------------------- demo controls


@router.post("/session/inject", response_class=HTMLResponse)
async def inject(request: Request, v: V, rt: RT, scenario: Annotated[str, Form()]) -> HTMLResponse:
    try:
        s = Scenario(scenario)
    except ValueError:
        raise HTTPException(422, f"Unknown scenario {scenario!r}.") from None
    admit_control(rt, v)
    v.plant.inject(s)
    v.prefs.focus = None
    expired = rt.agent.revalidate_approvals(v.sid, v.plant)  # a new incident retires older proposals
    v.prefs.recent_expired = {a.id for a in expired}
    _, text = await start_investigation(rt, v, s)
    if expired:
        text += f" {len(expired)} earlier proposal(s) expired because a new incident began."
    return render_overview(request, rt, v, toast(f"Fault injected: {SCENARIO_LABELS[s][0]}", text, "amber"))


@router.post("/session/stale", response_class=HTMLResponse)
async def stale(request: Request, v: V, rt: RT, tag: Annotated[str, Form()] = "TT-402") -> HTMLResponse:
    if tag not in {t for a in v.plant.snapshot().assets for t in a.tags}:
        raise HTTPException(404, f"No tag {tag}.")
    admit_control(rt, v, starts_run=v.plant.snapshot().active_scenario is not None)
    v.plant.make_stale(tag)
    expired = rt.agent.revalidate_approvals(v.sid, v.plant)
    v.prefs.recent_expired = {a.id for a in expired}
    parts = [f"{tag} stopped reporting; its readings are now stale."]
    if expired:
        parts.append(f"{len(expired)} approval(s) expired because their evidence changed.")
    scenario = v.plant.snapshot().active_scenario
    if scenario is not None:
        _, text = await start_investigation(rt, v, scenario)
        parts.append(text)
    return render_overview(request, rt, v, toast("Reference probe stale", " ".join(parts), "amber"))


@router.post("/session/mode", response_class=HTMLResponse)
async def mode(request: Request, v: V, rt: RT, mode: Annotated[str, Form()]) -> HTMLResponse:
    if mode not in {"replay", "live"}:
        raise HTTPException(422, "Mode must be replay or live.")
    if mode == "live":
        status = rt.agent.live_status(v.sid, v.ip)
        if not status.live_available:
            v.prefs.mode = "replay"
            return render_overview(request, rt, v, toast("Live runs unavailable", status.reason, "amber"))
        left = status.session_runs_limit - status.session_runs_used
        text = f"Next investigation calls the model ({left} live runs left this hour). Falls back to replay on any cap."
        v.prefs.mode = "live"
        return render_overview(request, rt, v, toast("Live mode", text, "neutral"))
    v.prefs.mode = "replay"
    return render_overview(request, rt, v, toast("Replay mode", "Recorded agent decisions, run against your plant."))


@router.post("/session/reset", response_class=HTMLResponse)
async def reset(request: Request, v: V, rt: RT) -> HTMLResponse:
    rt.sessions.reset(v.sid, origin=v.ip)
    rt.agent.forget_session(v.sid, keep_numbering=True)  # the audit trail survives a reset; ids must not repeat
    v.prefs.focus = None
    v.prefs.recent_expired.clear()
    return render_overview(request, rt, v, toast("Plant reset", "Faults, holds, runs and approvals cleared.", "ok"))


@router.post("/session/role")
async def role(v: V, role: Annotated[str, Form()]) -> Response:
    if role not in ROLES:
        raise HTTPException(422, f"Role must be one of: {', '.join(ROLES)}.")
    v.prefs.role = role
    return Response(status_code=204, headers={"HX-Refresh": "true"})


# --------------------------------------------------------------------------- approvals


def _decide(request: Request, v: Visitor, rt: Runtime, approval_id: str, approve: bool, token: str) -> Response:
    """Approve or decline from a card. The card's decision token binds the click to the exact proposal in
    this session generation: a card left open across a sandbox expiry or reset is refused with a toast,
    even when a newer proposal now has the same display id."""
    try:
        if approve:
            result = rt.agent.approve(v.sid, approval_id, v.prefs.role, v.plant, token=token)
        else:
            result = rt.agent.decline(v.sid, approval_id, v.prefs.role, v.plant, token=token)
    except StaleDecision as exc:
        title = "Session expired: reload the page" if exc.session_expired else "Proposal changed: reload the page"
        return Response(status_code=409, headers=toast(title, str(exc), "amber"))
    except PermissionError as exc:
        raise HTTPException(403, f"{exc}. Switch role on the Approvals page.") from None
    tone = "ok" if result.executed else ("neutral" if not approve else "amber")
    title = f"{approval_id} {result.approval.status.value}"
    path = urlparse(request.headers.get("hx-current-url", "")).path
    compact = path in {"", "/"} or path.startswith("/runs/")  # the overview and run pages use compact cards
    ctx = {"approval": result.approval, "compact": compact}
    headers = toast(title, result.message, tone, **{"plant-changed": True})
    return templates.TemplateResponse(request, "fragments/approval_card.html", ctx, headers=headers)


@router.post("/approvals/{approval_id}/approve", response_class=HTMLResponse)
async def approve(request: Request, approval_id: str, v: V, rt: RT,
                  decision_token: Annotated[str, Form()] = "") -> Response:
    return _decide(request, v, rt, approval_id, True, decision_token)


@router.post("/approvals/{approval_id}/decline", response_class=HTMLResponse)
async def decline(request: Request, approval_id: str, v: V, rt: RT,
                  decision_token: Annotated[str, Form()] = "") -> Response:
    return _decide(request, v, rt, approval_id, False, decision_token)
