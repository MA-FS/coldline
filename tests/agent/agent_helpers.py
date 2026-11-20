"""Shared helpers for the agent tests: a real sandbox, a run context, and scripted turns."""

from __future__ import annotations

import itertools
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coldline.agent.approvals import ApprovalStore
from coldline.agent.config import AgentSettings
from coldline.agent.context import RunContext
from coldline.agent.model import ScriptedCall, ScriptedTurn
from coldline.agent.tools import ToolRegistry
from coldline.contracts import RunTrace, Scenario
from coldline.systems import Sandbox, create_plant

SUPERVISOR = "Shift Supervisor"

# The eval cases live in the top-level ``evals`` package next to ``src``; make it importable from tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def plant(scenario: Scenario | None = None, stale: str | None = None) -> Sandbox:
    p = create_plant()
    if scenario:
        p.inject(scenario)
    if stale:
        p.make_stale(stale)
    return p


def context(p: Sandbox, scenario: Scenario | None = None) -> RunContext:
    seq = itertools.count(1)
    run = RunTrace(run_id="R-0001", session_id="test", scenario=scenario, started_at=datetime.now(UTC))
    return RunContext(run=run, systems=p, approvals=ApprovalStore("test", lambda: f"AP-{next(seq):04d}"))


REGISTRY = ToolRegistry()


def call(ctx: RunContext, name: str, **args: Any):
    return REGISTRY.execute(ctx, name, args)


def investigate_ch4(ctx: RunContext) -> None:
    call(ctx, "get_sensor_trend", tags=["TT-401", "TT-402"])
    call(ctx, "list_lots_in_zone", zone_asset_id="CH4")
    call(ctx, "find_work_orders", asset_id="CH4")


def turn(think: str, *calls: tuple[str, dict[str, Any]], **kw: Any) -> ScriptedTurn:
    return ScriptedTurn(think=think, tool_calls=[ScriptedCall(name=n, input=i) for n, i in calls], **kw)


def settings(**overrides: Any) -> AgentSettings:
    base = {"replay_speed": 0.0, "live_enabled": True, "api_key_present": True}
    return AgentSettings(**(base | overrides))
