"""Samples for ``/styleguide``: every component, rendered from what the app really produces.

Each sample comes from replaying a bundled recording against a scratch sandbox, through the same agent
service, tool registry and approval store a visitor's replay uses, so the catalogue cannot drift from the
pages it documents. Proposals are then decided, declined or expired the way a person would, to show every
card state. Two states a finished replay never holds are derived from those runs: a step still in flight,
and a NEVER-tier request as the tool registry refuses it.

The samples belong to the scratch sandboxes, not the visitor's plant, and never depend on the visitor, so
:class:`Samples` builds and renders them once per process. Every component instance goes through
:func:`coldline.web.inert.inert`: ids are scoped per instance, links do not navigate, and no button, form or
stream sends a request.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from markupsafe import Markup

from coldline.agent import AgentService, AgentSettings
from coldline.agent.approvals import ApprovalStore
from coldline.agent.context import RunContext
from coldline.agent.tools import ToolRegistry
from coldline.contracts import AssetKind, RunStatus, RunTrace, Scenario, StepKind, Tier, TraceStep
from coldline.sim.analysis import DIVERGENCE_LIMIT_C
from coldline.systems import Sandbox, create_plant
from coldline.web import context as cx
from coldline.web.charts import PROBE_LIMIT_LINE, ChartEvent, Series, trend_svg
from coldline.web.render import env, hhmm
from coldline.web.views import plant_view, probe_tags

SUPERVISOR = "Shift Supervisor"
TEMPLATE = "pages/styleguide_samples.html"


@dataclass
class Sample:
    sid: str
    plant: Sandbox
    run: RunTrace


async def _replay(agent: AgentService, scenario: Scenario, stale: str | None = None) -> Sample:
    sid = f"styleguide-{scenario.value}{'-stale' if stale else ''}"
    plant = create_plant()
    plant.inject(scenario)
    if stale:
        plant.make_stale(stale)
    run = await agent.run_to_completion(sid, plant)
    return Sample(sid, plant, run)


def _view(plant: Sandbox) -> Any:
    snap = plant.snapshot()
    tags = [t for a in snap.assets if a.kind == AssetKind.CHILLER for t in probe_tags(a)]
    return plant_view(snap, plant.get_trend(tags, 60))


def _first(run: RunTrace, kind: StepKind, tier: Tier | None = None) -> TraceStep:
    return next(s for s in run.steps if s.kind == kind and (tier is None or s.tier == tier))


def _in_flight(run: RunTrace, steps: int = 3) -> RunTrace:
    """The run as it looked a few steps in: the panel's live state, which a finished replay never holds."""
    return run.model_copy(update={"status": RunStatus.RUNNING, "steps": run.steps[:steps], "conclusion": "",
                                  "confidence": "", "handover_note": "", "approvals": []})


def _refusal(sample: Sample) -> TraceStep:
    """What the registry records when a model asks for a NEVER-tier action (here, releasing a QA hold)."""
    run = RunTrace(run_id=sample.run.run_id, session_id=sample.sid, scenario=sample.run.scenario,
                   started_at=sample.plant.now())
    ctx = RunContext(run=run, systems=sample.plant, approvals=ApprovalStore(sample.sid, lambda: "AP-0000"))
    ToolRegistry().execute(ctx, "release_qa_hold", {"lot_id": "L-2231"})
    return run.steps[-1]


async def catalogue() -> dict[str, Any]:
    """The styleguide's samples, built from the recordings (five replays: a few tens of milliseconds)."""
    agent = AgentService(AgentSettings(replay_speed=0.0, live_enabled=False))
    hero = await _replay(agent, Scenario.PROBE_MISMATCH)
    stale = await _replay(agent, Scenario.PROBE_MISMATCH, stale="TT-402")
    trip = await _replay(agent, Scenario.COMPRESSOR_TRIP)
    poisoned = await _replay(agent, Scenario.POISONED_EMAIL)
    door = await _replay(agent, Scenario.DOOR_OPEN)

    # Views of the plant before anyone decides anything.
    _, detail = cx.detail_context(hero.plant, "CH4", hero.run)
    kpis = cx.impact(hero.plant, agent, hero.sid)["kpis"][:4]
    refused = _refusal(poisoned)

    # Every card state: approve one hero proposal, then a stale probe expires the other; decline one trip
    # proposal and leave the other pending.
    hero_ap = {a.action: a for a in agent.list_approvals(hero.sid)}
    agent.approve(hero.sid, hero_ap["change_dispatch"].id, SUPERVISOR, hero.plant)
    hero.plant.make_stale("TT-402")
    agent.revalidate_approvals(hero.sid, hero.plant)
    trip_ap = agent.list_approvals(trip.sid)
    agent.decline(trip.sid, trip_ap[0].id, SUPERVISOR, trip.plant, "Load can wait for the 07:00 truck")
    approvals = agent.list_approvals(trip.sid) + agent.list_approvals(hero.sid)

    primary, reference = stale.plant.get_trend(["TT-401", "TT-402"], 60)
    stale_chart = trend_svg([Series(primary, "amber"), Series(reference)], width=520, height=170, y_min=1, y_max=7,
                            divergence=("TT-401", "TT-402", DIVERGENCE_LIMIT_C),
                            title="TT-401 vs TT-402 with TT-402 stale")
    door_trend = door.plant.get_trend(["TT-401"], 60)[0]
    peak = max(door_trend.points, key=lambda p: p.value_c or 0.0)
    door_chart = trend_svg([Series(door_trend)], width=520, height=170, y_min=1, y_max=7, limit=PROBE_LIMIT_LINE,
                           events=[ChartEvent(peak.ts, f"Door {hhmm(peak.ts)}")])

    steps = [
        (_first(hero.run, StepKind.THINK), True),
        (_first(hero.run, StepKind.TOOL, Tier.READ), True),
        (_first(hero.run, StepKind.TOOL, Tier.APPROVE), False),
        (_first(hero.run, StepKind.GUARDRAIL, Tier.APPROVE), False),
        (refused, False),
    ]
    cited = [i for s in (*hero.run.steps, *poisoned.run.steps) for i in s.evidence_ids]
    alarm_ids = [a.id for a in hero.plant.list_alarms(active_only=False)]
    return {
        "stale_plant": _view(stale.plant),
        "trip_plant": _view(trip.plant),
        "detail": detail,
        "steps": steps,
        "running": _in_flight(hero.run),
        "approvals": approvals,
        "pending": next(a for a in approvals if a.status == "pending"),
        "events": list(reversed(hero.plant.audit_log(8))),
        "stale_chart": stale_chart,
        "door_chart": door_chart,
        "kpis": kpis,
        "alarms": trip.plant.snapshot().alarms + stale.plant.snapshot().alarms,
        "evidence_ids": list(dict.fromkeys([*alarm_ids, *cited])),
        "conclusions": [(s.run.conclusion, s.run.confidence) for s in (hero, poisoned, stale, door)],
    }


class Samples:
    """The rendered sample region: built on the first request, then reused for the life of the process.

    Repeat visits cost a render of the page shell only, not five replays and two charts.
    """

    def __init__(self) -> None:
        self._html: Markup | None = None
        self._lock = asyncio.Lock()

    async def html(self) -> Markup:
        if self._html is None:
            async with self._lock:  # concurrent first requests build it once between them
                if self._html is None:
                    self._html = Markup(env.get_template(TEMPLATE).render(await catalogue()))
        return self._html


SAMPLES = Samples()
