"""The agent's public API for the web layer.

    service = get_agent_service()
    run = await service.start_run(session_id, plant, mode="replay" | "live")
    async for event in service.stream_events(run.run_id): ...      # SSE: "step" events, then "done"
    service.approve(session_id, approval_id, "Shift Supervisor", plant)
    service.decline(session_id, approval_id, "Shift Supervisor", plant)
    service.list_runs(session_id); service.get_run(run_id); service.list_approvals(session_id)
    service.live_status(session_id)                                 # budget + whether live is available

Replay is the default. A live request that the kill switch, a missing API key or the budget refuses,
or that fails part-way, continues as replay in the same run with a visible notice step.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from coldline.agent.approvals import ApprovalResult, ApprovalStore
from coldline.agent.budget import BudgetGuard, BudgetStatus
from coldline.agent.config import AgentSettings
from coldline.agent.context import RunContext
from coldline.agent.loop import LiveRunAborted, LoopLimits, finalize, run_loop
from coldline.agent.model import AnthropicModelClient, ModelClient, ScriptedModelClient
from coldline.agent.pricing import worst_case_cost_usd
from coldline.agent.replay import RecordingLibrary, resolve_placeholders
from coldline.agent.tools import ToolRegistry
from coldline.contracts import (
    Approval,
    ApprovalStatus,
    PlantSystems,
    RunStatus,
    RunTrace,
    Scenario,
    StepKind,
    TraceStep,
)

Mode = Literal["replay", "live"]
EVAL_RESULTS_PATH = Path(__file__).parent / "data" / "eval_results.json"
MAX_RUNS_PER_SESSION = 25


@dataclass
class _Session:
    approvals: ApprovalStore
    run_ids: list[str] = field(default_factory=list)


@dataclass
class _RunState:
    run: RunTrace
    waiters: set[asyncio.Event] = field(default_factory=set)
    done: bool = False
    task: asyncio.Task | None = None

    def wake(self) -> None:
        for event in list(self.waiters):
            event.set()


class AgentService:
    def __init__(
        self,
        settings: AgentSettings | None = None,
        *,
        budget: BudgetGuard | None = None,
        recordings: RecordingLibrary | None = None,
        live_client_factory: Callable[[AgentSettings], ModelClient] | None = None,
    ) -> None:
        self.settings = settings or AgentSettings.from_env()
        s = self.settings
        self.registry = ToolRegistry()
        self.recordings = recordings or RecordingLibrary()
        state_path = Path(s.data_dir) / "agent_spend.json" if s.data_dir else None
        self.budget = budget or BudgetGuard(
            daily_cap_usd=s.daily_usd_cap,
            session_runs_per_window=s.session_runs_per_hour,
            max_concurrent=s.max_concurrent_live,
            min_call_reserve_usd=worst_case_cost_usd(s.model, 8000, s.max_tokens_per_call),
            state_path=state_path,
        )
        self._live_client_factory = live_client_factory or AnthropicModelClient
        self._live_client: ModelClient | None = None
        self._sessions: dict[str, _Session] = {}
        self._runs: dict[str, _RunState] = {}
        self._run_seq = itertools.count(7841)
        self._approval_seq = itertools.count(1)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ sessions

    def _session(self, session_id: str) -> _Session:
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = _Session(
                    ApprovalStore(session_id, lambda: f"AP-{next(self._approval_seq):04d}")
                )
            return self._sessions[session_id]

    def forget_session(self, session_id: str) -> None:
        """Drop a visitor's runs and approvals (call when their sandbox expires)."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
            for run_id in session.run_ids if session else []:
                state = self._runs.pop(run_id, None)
                if state and state.task and not state.task.done():
                    state.task.cancel()

    # ------------------------------------------------------------------ runs

    def live_status(self, session_id: str) -> BudgetStatus:
        return self.budget.status(session_id, live_enabled=self.settings.live_enabled,
                                  api_key_present=self.settings.api_key_present)

    async def start_run(
        self,
        session_id: str,
        systems: PlantSystems,
        *,
        scenario: Scenario | None = None,
        mode: Mode = "replay",
    ) -> RunTrace:
        """Start a run in the background and return its (still running) trace.

        Only one run per session runs at a time; a second request returns the run in progress.
        """
        session = self._session(session_id)
        for run_id in reversed(session.run_ids):
            state = self._runs.get(run_id)
            if state and not state.done:
                return state.run
        if scenario is None:
            scenario = systems.snapshot().active_scenario
        run = RunTrace(
            run_id=f"R-{next(self._run_seq)}",
            session_id=session_id,
            scenario=scenario,
            mode=mode,
            model=self.settings.model,
            started_at=systems.now(),
        )
        state = _RunState(run)
        with self._lock:
            self._runs[run.run_id] = state
            session.run_ids.append(run.run_id)
            for old in session.run_ids[:-MAX_RUNS_PER_SESSION]:
                if self._runs.get(old) and self._runs[old].done:
                    self._runs.pop(old, None)
            session.run_ids[:] = [r for r in session.run_ids if r in self._runs]
        state.task = asyncio.create_task(self._drive(state, session, systems, mode))
        return run

    async def run_to_completion(self, session_id: str, systems: PlantSystems, **kwargs: Any) -> RunTrace:
        """Convenience for scripts and tests: start a run and wait for it to finish."""
        run = await self.start_run(session_id, systems, **kwargs)
        task = self._runs[run.run_id].task
        if task is not None:
            await task
        return run

    def get_run(self, run_id: str) -> RunTrace | None:
        state = self._runs.get(run_id)
        return state.run if state else None

    def list_runs(self, session_id: str) -> list[RunTrace]:
        session = self._sessions.get(session_id)
        if session is None:
            return []
        return [self._runs[r].run for r in reversed(session.run_ids) if r in self._runs]

    async def stream(self, run_id: str, after: int = 0) -> AsyncIterator[TraceStep]:
        """Yield steps from index ``after`` as they are produced; returns when the run finishes."""
        state = self._runs[run_id]
        i = after
        while True:
            while i < len(state.run.steps):
                yield state.run.steps[i]
                i += 1
            if state.done:
                return
            event = asyncio.Event()
            state.waiters.add(event)
            try:
                if i >= len(state.run.steps) and not state.done:
                    await event.wait()
            finally:
                state.waiters.discard(event)

    async def stream_events(self, run_id: str, after: int = 0) -> AsyncIterator[dict[str, str]]:
        """SSE-ready dicts: one ``step`` event per TraceStep, then a ``done`` event with the RunTrace."""
        async for step in self.stream(run_id, after):
            yield {"event": "step", "id": str(step.n), "data": step.model_dump_json()}
        run = self._runs[run_id].run
        yield {"event": "done", "data": public_run_json(run)}

    # ------------------------------------------------------------------ approvals

    def list_approvals(self, session_id: str, status: ApprovalStatus | None = None) -> list[Approval]:
        return list(reversed(self._session(session_id).approvals.list_all(status)))

    def approve(self, session_id: str, approval_id: str, actor: str, systems: PlantSystems) -> ApprovalResult:
        result = self._session(session_id).approvals.approve(approval_id, actor, systems)
        self._refresh_status(result.approval.run_id)
        return result

    def decline(self, session_id: str, approval_id: str, actor: str, systems: PlantSystems,
                reason: str = "") -> ApprovalResult:
        result = self._session(session_id).approvals.decline(approval_id, actor, systems, reason)
        self._refresh_status(result.approval.run_id)
        return result

    def revalidate_approvals(self, session_id: str, systems: PlantSystems) -> list[Approval]:
        """Expire pending approvals whose evidence changed (for example a probe went stale)."""
        expired = self._session(session_id).approvals.revalidate(systems)
        for run_id in {a.run_id for a in expired}:
            self._refresh_status(run_id)
        return expired

    def _refresh_status(self, run_id: str) -> None:
        state = self._runs.get(run_id)
        if (
            state
            and state.done
            and state.run.status == RunStatus.AWAITING_APPROVAL
            and all(a.status != ApprovalStatus.PENDING for a in state.run.approvals)
        ):
            state.run.status = RunStatus.DONE

    # ------------------------------------------------------------------ internals

    def _context(self, state: _RunState, session: _Session, systems: PlantSystems) -> RunContext:
        return RunContext(run=state.run, systems=systems, approvals=session.approvals,
                          on_step=lambda _step: state.wake())

    def _live_refusal(self, session_id: str) -> str:
        if not self.settings.live_enabled:
            return "Live runs are switched off"
        if not self.settings.api_key_present:
            return "No model API key is configured"
        return self.budget.admit(session_id)

    async def _drive(self, state: _RunState, session: _Session, systems: PlantSystems, mode: Mode) -> None:
        run = state.run
        wall = time.monotonic()
        fallback = ""
        try:
            if mode == "live":
                fallback = self._live_refusal(run.session_id)
                if not fallback:
                    try:
                        await self._run_live(state, session, systems)
                    except LiveRunAborted as exc:
                        fallback = exc.reason
                    finally:
                        self.budget.finish()
                    if not fallback:
                        return
                run.extra["fallback_reason"] = fallback
                run.extra["live_requested"] = True
                ctx = self._context(state, session, systems)
                ctx.guardrail("Fallback: continuing with the recorded run",
                              f"{fallback}. The recorded model decisions now run against your plant through "
                              "the same guardrails.", ok=True)
            run.mode = "replay"
            await self._run_replay(state, session, systems)
        except Exception as exc:  # noqa: BLE001 - a bug must end the run visibly, never hang the stream
            run.status = RunStatus.FAILED
            run.extra["error"] = f"{type(exc).__name__}: {exc}"
            self._context(state, session, systems).emit(StepKind.ERROR, "Run failed", ok=False,
                                                         summary=f"{type(exc).__name__}: {exc}")
        finally:
            wall_ms = int((time.monotonic() - wall) * 1000)
            run.extra["wall_ms"] = wall_ms
            recorded_ms = sum(s.latency_ms for s in run.steps)
            run.duration_ms = wall_ms if run.extra.get("live_requested") or run.mode == "live" else recorded_ms
            state.done = True
            state.wake()

    async def _run_live(self, state: _RunState, session: _Session, systems: PlantSystems) -> None:
        if self._live_client is None:
            self._live_client = self._live_client_factory(self.settings)
        ctx = self._context(state, session, systems)
        state.run.mode = "live"
        async for _ in run_loop(ctx, self._live_client, self.registry, LoopLimits.from_settings(self.settings),
                                budget=self.budget, record_turns=True):
            pass
        finalize(ctx)

    async def _run_replay(self, state: _RunState, session: _Session, systems: PlantSystems) -> None:
        run = state.run
        recording = self.recordings.select(run.scenario, systems)
        ctx = self._context(state, session, systems)
        if recording is None:
            run.status = RunStatus.FAILED
            run.conclusion = "No recorded run matches this plant state."
            ctx.emit(StepKind.ERROR, "No recorded run for this plant state", ok=False,
                     summary="Inject a fault first, or run live.")
            return
        run.model = recording.model
        run.extra["recording"] = {"id": recording.id, "recorded": recording.recorded,
                                  "description": recording.description}
        client = ScriptedModelClient(
            recording.turns,
            model=recording.model,
            speed=self.settings.replay_speed,
            max_delay_s=self.settings.replay_max_call_delay_s,
            resolve=lambda obj: resolve_placeholders(obj, ctx),
        )
        limits = LoopLimits.from_settings(self.settings, replay=True)
        async for _ in run_loop(ctx, client, self.registry, limits):
            pass
        finalize(ctx)


def public_run_json(run: RunTrace) -> str:
    """RunTrace JSON without the raw model turns (they are for recording, not display)."""
    data = run.model_dump(mode="json")
    data["extra"] = {k: v for k, v in data["extra"].items() if k != "model_turns"}
    return json.dumps(data)


def load_eval_results(path: Path = EVAL_RESULTS_PATH) -> dict[str, Any]:
    """Latest eval suite results for the Evals page (written by ``python -m evals.run``)."""
    if not path.exists():
        return {"generated_at": None, "cases": [], "summary": {"passed": 0, "total": 0}}
    return json.loads(path.read_text())


_default: AgentService | None = None


def get_agent_service() -> AgentService:
    """Process-wide service configured from the environment."""
    global _default
    if _default is None:
        _default = AgentService()
    return _default
