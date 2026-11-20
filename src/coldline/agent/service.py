"""The agent's public API for the web layer.

    service = get_agent_service()
    service.active_run(session_id)                                  # admission: one run per session at a time
    run = await service.start_run(session_id, plant, mode="replay" | "live", origin=client_ip)
    async for event in service.stream_events(session_id, run.run_id): ...  # SSE: "step" events, then "done"
    service.approve(session_id, approval_id, "Shift Supervisor", plant, token=card_token)
    service.decline(session_id, approval_id, "Shift Supervisor", plant, token=card_token)
    service.list_runs(session_id); service.get_run(session_id, run_id); service.list_approvals(session_id)
    service.live_status(session_id)                                 # budget + whether live is available

Replay is the default. A live request that the kill switch, a missing API key or the budget refuses,
or that fails part-way, continues as replay in the same run with a visible notice step. The fallback keeps
the run's context (what it looked at, created and flagged), so a write the live part already made is reused,
not repeated, and suspected injection stays flagged.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from coldline.agent.approvals import ApprovalResult, ApprovalStore
from coldline.agent.budget import BudgetGuard, BudgetStatus
from coldline.agent.config import AgentSettings
from coldline.agent.context import RunContext
from coldline.agent.loop import LiveRunAborted, LoopLimits, finalize, input_token_bound, run_loop
from coldline.agent.model import AnthropicModelClient, ModelClient, ScriptedModelClient
from coldline.agent.pricing import permitted_models, reservation_usd
from coldline.agent.replay import RecordingLibrary, resolve_placeholders, selection_note
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
log = logging.getLogger(__name__)
EVAL_RESULTS_PATH = Path(__file__).parent / "data" / "eval_results.json"
MAX_RUNS_PER_SESSION = 25


@dataclass
class _Numbering:
    """Per-visitor counters, so every visitor's first run is R-7841 and first proposal AP-0001."""

    runs: Iterator[int] = field(default_factory=lambda: itertools.count(7841))
    approvals: Iterator[int] = field(default_factory=lambda: itertools.count(1))


@dataclass
class _Session:
    approvals: ApprovalStore
    numbering: _Numbering
    run_ids: list[str] = field(default_factory=list)
    runs: dict[str, _RunState] = field(default_factory=dict)

    def state(self, run_id: str) -> _RunState | None:
        return self.runs.get(run_id)


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
        self.billable_models = permitted_models(s.model, s.server_fallback)
        # Admission needs room for at least the first call of a run: prompt, tools and a typical trigger.
        first_call = input_token_bound(self.registry.definitions(), [{"role": "user", "content": "x" * 1500}])
        self.budget = budget or BudgetGuard(
            daily_cap_usd=s.daily_usd_cap,
            session_runs_per_window=s.session_runs_per_hour,
            origin_runs_per_day=s.origin_runs_per_day,
            max_concurrent=s.max_concurrent_live,
            min_call_reserve_usd=reservation_usd(self.billable_models, first_call, s.max_tokens_per_call),
            state_path=state_path,
        )
        self._live_client_factory = live_client_factory or AnthropicModelClient
        self._live_client: ModelClient | None = None
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ sessions

    def _session(self, session_id: str, numbering: _Numbering | None = None) -> _Session:
        with self._lock:
            if session_id not in self._sessions:
                numbers = numbering or _Numbering()
                self._sessions[session_id] = _Session(
                    ApprovalStore(session_id, lambda: f"AP-{next(numbers.approvals):04d}"), numbers
                )
            return self._sessions[session_id]

    def forget_session(self, session_id: str, *, keep_numbering: bool = False) -> None:
        """Drop a visitor's runs and approvals (call when their sandbox expires or is reset).

        A reset keeps the sandbox's audit trail, so it also keeps the numbering: a new run never reuses an id
        the audit log already shows."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        for state in session.runs.values():
            if state.task and not state.task.done():
                state.task.cancel()
        if keep_numbering:
            self._session(session_id, session.numbering)

    def active_run(self, session_id: str) -> RunTrace | None:
        """The session's run in progress, if any. Callers check this before changing the plant."""
        session = self._sessions.get(session_id)
        for run_id in reversed(session.run_ids if session else []):
            state = session.state(run_id) if session else None
            if state and not state.done:
                return state.run
        return None

    # ------------------------------------------------------------------ runs

    def live_status(self, session_id: str, origin: str = "") -> BudgetStatus:
        return self.budget.status(session_id, live_enabled=self.settings.live_enabled,
                                  api_key_present=self.settings.api_key_present, origin=origin)

    async def start_run(
        self,
        session_id: str,
        systems: PlantSystems,
        *,
        scenario: Scenario | None = None,
        mode: Mode = "replay",
        origin: str = "",
    ) -> RunTrace:
        """Start a run in the background and return its (still running) trace.

        Only one run per session runs at a time; a second request returns the run in progress. Web
        controls call :meth:`active_run` first and refuse before touching the plant. The run is registered
        before this returns, so its stream can be opened immediately.
        """
        session = self._session(session_id)
        busy = self.active_run(session_id)
        if busy is not None:
            return busy
        if scenario is None:
            scenario = systems.snapshot().active_scenario
        run = RunTrace(
            run_id=f"R-{next(session.numbering.runs)}",
            session_id=session_id,
            scenario=scenario,
            mode=mode,
            model=self.settings.model,
            started_at=systems.now(),
        )
        state = _RunState(run)
        with self._lock:
            session.runs[run.run_id] = state
            session.run_ids.append(run.run_id)
            for old in session.run_ids[:-MAX_RUNS_PER_SESSION]:
                if session.runs.get(old) and session.runs[old].done:
                    session.runs.pop(old, None)
            session.run_ids[:] = [r for r in session.run_ids if r in session.runs]
        session.approvals.prune(set(session.run_ids))
        state.task = asyncio.create_task(self._drive(state, session, systems, mode, origin))
        return run

    async def run_to_completion(self, session_id: str, systems: PlantSystems, **kwargs: Any) -> RunTrace:
        """Convenience for scripts and tests: start a run and wait for it to finish."""
        run = await self.start_run(session_id, systems, **kwargs)
        state = self._sessions[session_id].state(run.run_id)
        if state is not None and state.task is not None:
            await state.task
        return run

    def _state(self, session_id: str, run_id: str) -> _RunState | None:
        session = self._sessions.get(session_id)
        return session.state(run_id) if session else None

    def get_run(self, session_id: str, run_id: str) -> RunTrace | None:
        """A run of this session. Run ids are numbered per visitor, so lookups are always session-scoped:
        another visitor's run is indistinguishable from a missing one."""
        state = self._state(session_id, run_id)
        return state.run if state else None

    def list_runs(self, session_id: str) -> list[RunTrace]:
        session = self._sessions.get(session_id)
        if session is None:
            return []
        return [session.runs[r].run for r in reversed(session.run_ids) if r in session.runs]

    async def stream(self, session_id: str, run_id: str, after: int = 0) -> AsyncIterator[TraceStep]:
        """Yield steps from index ``after`` as they are produced; returns when the run finishes."""
        state = self._state(session_id, run_id)
        if state is None:
            raise KeyError(run_id)
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

    async def stream_events(self, session_id: str, run_id: str, after: int = 0) -> AsyncIterator[dict[str, str]]:
        """SSE-ready dicts: one ``step`` event per TraceStep, then a ``done`` event with the RunTrace."""
        async for step in self.stream(session_id, run_id, after):
            yield {"event": "step", "id": str(step.n), "data": step.model_dump_json()}
        state = self._state(session_id, run_id)
        if state is not None:
            yield {"event": "done", "data": public_run_json(state.run)}

    # ------------------------------------------------------------------ approvals

    def list_approvals(self, session_id: str, status: ApprovalStatus | None = None) -> list[Approval]:
        return list(reversed(self._session(session_id).approvals.list_all(status)))

    def approve(self, session_id: str, approval_id: str, actor: str, systems: PlantSystems,
                token: str | None = None) -> ApprovalResult:
        """Decide as ``actor``. Web decisions pass the card's ``token``; one from an earlier session generation
        raises :class:`~coldline.agent.approvals.StaleDecision`. An executed action can change other
        proposals' evidence, so the rest of the session's pending approvals are re-checked straight away."""
        store = self._session(session_id).approvals
        result = store.approve(approval_id, actor, systems, token=token)
        if result.executed:
            store.revalidate(systems)
        self._refresh_statuses(session_id)
        return result

    def decline(self, session_id: str, approval_id: str, actor: str, systems: PlantSystems,
                reason: str = "", token: str | None = None) -> ApprovalResult:
        result = self._session(session_id).approvals.decline(approval_id, actor, systems, reason, token=token)
        self._refresh_statuses(session_id)
        return result

    def revalidate_approvals(self, session_id: str, systems: PlantSystems) -> list[Approval]:
        """Expire pending approvals whose evidence changed (a probe went stale, a new fault was injected)."""
        expired = self._session(session_id).approvals.revalidate(systems)
        self._refresh_statuses(session_id)
        return expired

    def _refresh_statuses(self, session_id: str) -> None:
        """A finished run waits for approval only while one of its approvals (possibly shared with a later
        run on the same evidence) is still pending."""
        session = self._sessions.get(session_id)
        for run_id in session.run_ids if session else []:
            state = session.state(run_id) if session else None
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

    def _live_refusal(self, session_id: str, origin: str) -> str:
        if not self.settings.live_enabled:
            return "Live runs are switched off"
        if not self.settings.api_key_present:
            return "No model API key is configured"
        return self.budget.admit(session_id, origin=origin)

    async def _drive(self, state: _RunState, session: _Session, systems: PlantSystems, mode: Mode,
                     origin: str = "") -> None:
        run = state.run
        wall = time.monotonic()
        fallback = ""
        ctx = self._context(state, session, systems)  # shared by the live part and any fallback
        try:
            if mode == "live":
                fallback = self._live_refusal(run.session_id, origin)
                if not fallback:
                    try:
                        await self._run_live(ctx)
                    except LiveRunAborted as exc:
                        fallback = exc.reason
                    finally:
                        self.budget.finish()
                    if not fallback:
                        return
                run.extra["fallback_reason"] = fallback
                run.extra["live_requested"] = True
                run.extra["live_steps"] = len(run.steps)  # steps before this are live; after, replayed
                ctx.guardrail("Fallback: continuing with the recorded run",
                              f"{fallback}. The recorded model decisions now run against your plant through "
                              "the same guardrails; anything already done in this run is reused, not "
                              "repeated.", ok=True)
            run.mode = "replay"
            await self._run_replay(ctx)
        except Exception as exc:  # noqa: BLE001 - a bug must end the run visibly, never hang the stream
            run.status = RunStatus.FAILED
            run.extra["error"] = f"{type(exc).__name__}: {exc}"
            ctx.emit(StepKind.ERROR, "Run failed", ok=False, summary=f"{type(exc).__name__}: {exc}")
        finally:
            wall_ms = int((time.monotonic() - wall) * 1000)
            run.extra["wall_ms"] = wall_ms
            # Keep what was measured here apart from what the source recording measured: live steps cost
            # real money, replayed steps show the cost and latency of the run they replay.
            boundary = run.extra.get("live_steps", len(run.steps) if run.mode == "live" else 0)
            run.extra["live_cost_usd"] = round(sum(s.cost_usd for s in run.steps[:boundary]), 6)
            run.extra["recorded_cost_usd"] = round(sum(s.cost_usd for s in run.steps[boundary:]), 6)
            recorded_ms = sum(s.latency_ms for s in run.steps[boundary:])
            run.extra["recorded_ms"] = recorded_ms
            run.duration_ms = wall_ms if run.extra.get("live_requested") or run.mode == "live" else recorded_ms
            # The run's own writes (holds, amendments) may have changed evidence older proposals relied on.
            try:
                session.approvals.revalidate(systems)
                self._refresh_statuses(run.session_id)
            except Exception:
                log.exception("approval revalidation failed after run %s", run.run_id)
            state.done = True
            state.wake()

    async def _run_live(self, ctx: RunContext) -> None:
        if self._live_client is None:
            self._live_client = self._live_client_factory(self.settings)
        ctx.run.mode = "live"
        async for _ in run_loop(ctx, self._live_client, self.registry, LoopLimits.from_settings(self.settings),
                                budget=self.budget, record_turns=True, billable_models=self.billable_models):
            pass
        finalize(ctx)

    async def _run_replay(self, ctx: RunContext) -> None:
        run, systems = ctx.run, ctx.systems
        recording = self.recordings.select(run.scenario, systems)
        if recording is None:
            run.status = RunStatus.FAILED
            run.conclusion = "No recorded run matches this plant state."
            ctx.emit(StepKind.ERROR, "No recorded run for this plant state", ok=False,
                     summary="Inject a fault first, or run live.")
            return
        run.model = recording.model
        run.extra["recording"] = {"id": recording.id, "recorded": recording.recorded,
                                  "description": recording.description}
        ctx.replay_baseline = recording.facts
        note = selection_note(run.scenario, recording)
        if note:
            run.extra["recording"]["selected_because"] = note
            ctx.guardrail("Replay: covering the active alarm as well", note, ok=True)
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
