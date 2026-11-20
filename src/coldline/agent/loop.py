"""The agent loop: a small, bounded tool-use loop over the Messages API shape.

    trigger -> [model call -> run each requested tool through the registry -> feed results back] * N
            -> accepted handover note (or a limit) -> conclusion

It is an async generator of :class:`TraceStep` so the web layer can stream each step as it happens.
Bounds: ``max_model_calls`` per run, ``max_tokens_per_call``, a per-call timeout and a wall-clock budget
for the whole run. In live mode every call first reserves an upper bound on its cost against the daily cap
(see :mod:`coldline.agent.budget`): the sum over every attempt the request allows (the model, then each
named server-side fallback). It is settled at the sum of every attempt the response reports, each at its
own model's rate. Of that, only usage the provider's billing rules establish is recorded as measured spend
(the step's ``cost_usd``); a decline before any output in a category that is not billed, or is unknown, is
kept apart in ``run.extra["estimated_usd"]`` (see :func:`coldline.agent.pricing.split_cost`). A call whose
outcome is unknown keeps its reservation (``unreconciled_usd``).

The batch of tool calls in one model turn stops at an accepted handover: the note is final, so a call after
it in the same turn is not run (it gets a refused result), and nothing can change the records it states.
Any failure that should not be shown as a broken agent (API error, timeout, refusal, budget) raises
:class:`LiveRunAborted` after emitting a visible step, and the service falls back to replay.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from coldline.agent.budget import BudgetExceeded, BudgetGuard, Reservation
from coldline.agent.config import AgentSettings
from coldline.agent.context import RunContext
from coldline.agent.evidence import assess_chiller, gate_conclusion
from coldline.agent.model import ModelClient, ModelError, ModelResponse, ScriptExhausted, ToolCall
from coldline.agent.pricing import CallCost, Usage, reservation_usd, split_cost
from coldline.agent.prompts import NUDGE, SYSTEM_PROMPT, build_trigger, prompt_version
from coldline.agent.tools import ToolOutcome, ToolRegistry, affected_chillers
from coldline.contracts import ApprovalStatus, RunStatus, StepKind, Tier, TraceStep


@dataclass(frozen=True)
class LoopLimits:
    max_model_calls: int = 8
    max_tokens_per_call: int = 3000
    run_timeout_s: float = 90.0
    call_timeout_s: float = 40.0
    tool_delay_s: float = 0.0  # replay pacing between tool steps

    @classmethod
    def from_settings(cls, s: AgentSettings, *, replay: bool = False) -> LoopLimits:
        return cls(
            max_model_calls=s.max_model_calls,
            max_tokens_per_call=s.max_tokens_per_call,
            run_timeout_s=600.0 if replay else s.run_timeout_s,
            call_timeout_s=60.0 if replay else s.call_timeout_s,
            tool_delay_s=0.3 * s.replay_speed if replay else 0.0,
        )


class LiveRunAborted(Exception):
    """The live run stopped early; ``reason`` is shown to the visitor with the replay fallback."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


FRAMING_TOKENS = 512  # request framing and per-message markers, added to the byte count
PER_MESSAGE_TOKENS = 16


def input_token_bound(tools: list[dict], messages: list[dict]) -> int:
    """An upper bound on input tokens: one per UTF-8 byte (a token never covers less than a byte), with
    SDK content blocks counted by their repr (which includes field names, so it over-counts)."""
    body = SYSTEM_PROMPT.encode() + json.dumps(tools).encode() + repr(messages).encode()
    return len(body) + FRAMING_TOKENS + PER_MESSAGE_TOKENS * len(messages)


# --------------------------------------------------------------------------- spend for one model call


@dataclass(frozen=True)
class CallHold:
    """The daily-cap reservation for one model call, from before the request until its cost is known.

    Every way a call can end closes the hold exactly once: settled at the reported cost, released when no
    call was made or the API returned an error, or retained when the outcome is unknown. Without a budget
    (replay, scripted runs) there is nothing to reserve and closing is a no-op."""

    ctx: RunContext
    budget: BudgetGuard | None = None
    reservation: Reservation | None = None

    @classmethod
    def reserve(cls, ctx: RunContext, budget: BudgetGuard | None, usd: Callable[[], float],
                origin: str) -> CallHold:
        """Reserve ``usd()`` against the daily cap; raises :class:`BudgetExceeded` when it does not fit."""
        if budget is None:
            return cls(ctx)
        hold = cls(ctx, budget, budget.reserve(usd(), origin=origin))
        # Counted per run: paid calls attempted, and spend whose outcome is unknown (see retain).
        ctx.run.extra["live_calls"] = ctx.run.extra.get("live_calls", 0) + 1
        return hold

    def settle(self, response: ModelResponse, attempts: tuple[str, ...]) -> CallCost:
        """Charge what the response reports. The cap counts the total; only ``measured`` is the step's
        spend, and the estimated part is kept apart in ``run.extra["estimated_usd"]``."""
        cost = split_cost(response.model, response.usage, attempts)
        self._settle(cost.total)
        if cost.estimated:
            extra = self.ctx.run.extra
            extra["estimated_usd"] = round(extra.get("estimated_usd", 0.0) + cost.estimated, 6)
        return cost

    def not_made(self) -> None:
        """No request was sent (a scripted stand-in ran out of turns): release it and uncount the call."""
        if self.reservation is not None:
            self._settle(0.0)
            self.ctx.run.extra["live_calls"] -= 1

    def failed(self, exc: Exception) -> None:
        """An API error response means nothing was generated, so the hold is released. A timeout or lost
        connection may still have been processed and billed, so its reservation is kept."""
        if isinstance(exc, TimeoutError) or getattr(exc, "billing_unknown", False):
            self.retain()
        else:
            self._settle(0.0)

    def retain(self) -> None:
        """An unknown outcome: the budget keeps the full reservation, and the run records it as unreconciled
        so its cost is never shown as a confirmed zero."""
        if self.budget is None or self.reservation is None:
            return
        self.budget.retain(self.reservation)
        extra = self.ctx.run.extra
        extra["unreconciled_usd"] = round(extra.get("unreconciled_usd", 0.0) + self.reservation.usd, 6)

    def _settle(self, actual_usd: float) -> None:
        if self.budget is not None and self.reservation is not None:
            self.budget.settle(self.reservation, actual_usd)


# --------------------------------------------------------------------------- trace for one model call


def _turn_log(ctx: RunContext, tools: list[dict]) -> list[dict]:
    """Where a recorded run keeps its model turns, stamped with the prompt version they were made under."""
    turns = ctx.run.extra.setdefault("model_turns", [])
    ctx.run.extra["prompt_version"] = prompt_version(tools)
    return turns


def _turn_record(response: ModelResponse, latency_ms: int) -> dict:
    """One model turn as stored for a recording, so replay can re-run the same decisions."""
    return {
        "think": response.text,
        "tool_calls": [{"name": c.name, "input": c.input} for c in response.tool_calls],
        "usage": response.usage.as_dict(),
        "latency_ms": latency_ms,
        "model": response.model,
        "stop_reason": response.stop_reason,
    }


def _attempts_note(usage: Usage, estimated_usd: float) -> str:
    """How a call that billed more than one attempt was charged, for the THINK step's usage line."""
    unsure = [a for a in usage.attempts if not a.billing_known]
    if unsure:
        cats = ", ".join(sorted({a.category or "unknown category" for a in unsure}))
        return (f" · {len(usage.attempts)} attempts, {len(unsure)} declined before any output ({cats}): "
                f"+US${estimated_usd:.4f} estimated, not counted as measured")
    return f" · {len(usage.attempts)} billed attempts" if len(usage.attempts) > 1 else ""


def _emit_think(ctx: RunContext, call_no: int, limits: LoopLimits, response: ModelResponse, cost: CallCost,
                measured_ms: int) -> TraceStep:
    """The THINK step for a completed model call: what the model said or plans to call, and what it cost."""
    u = response.usage
    planned = ", ".join(c.name for c in response.tool_calls)
    return ctx.emit(
        StepKind.THINK,
        f"Model call {call_no} (limit {limits.max_model_calls})",
        summary=response.text or (f"Calling {planned}" if planned else "No further tool calls."),
        result_preview=(f"in {u.total_input} tok ({u.cache_read_input_tokens} cached) · out "
                        f"{u.output_tokens} tok · US${cost.measured:.4f} · {response.model}"
                        f"{_attempts_note(u, cost.estimated)}"),
        latency_ms=response.latency_ms or measured_ms,
        input_tokens=u.total_input,
        output_tokens=u.output_tokens,
        cost_usd=cost.measured,
    )


def _complete_call(ctx: RunContext, hold: CallHold, response: ModelResponse, *, call_no: int, limits: LoopLimits,
                   attempts: tuple[str, ...], turns: list[dict] | None, measured_ms: int) -> TraceStep:
    """Account for a model call that returned: settle its cost, keep the turn for a recording when asked,
    and emit the THINK step that shows it."""
    cost = hold.settle(response, attempts)
    if turns is not None:
        turns.append(_turn_record(response, measured_ms))
    return _emit_think(ctx, call_no, limits, response, cost, measured_ms)


def _unusable_turn(response: ModelResponse, limits: LoopLimits) -> tuple[str, str, str] | None:
    """(step title, summary, abort reason) when the model's turn cannot be acted on, so none of its tool
    calls are run."""
    if response.stop_reason == "refusal":
        return ("Model declined the request", "The model stopped with a refusal; no tool calls were run.",
                "model declined the request")
    if response.stop_reason == "max_tokens":
        return ("Model output limit reached",
                f"Output hit max_tokens={limits.max_tokens_per_call}; tool calls discarded.",
                "model output limit reached")
    return None


# --------------------------------------------------------------------------- tool calls


async def _dispatch(ctx: RunContext, registry: ToolRegistry, call: ToolCall, delay_s: float) -> ToolOutcome:
    if ctx.handover_accepted:
        # The accepted handover ends the run: a later call in the same turn is refused, not run, so no write
        # or proposal can disagree with the note that was just frozen.
        return registry.refuse_after_handover(ctx, call.name, call.input)
    if delay_s:
        await asyncio.sleep(delay_s)
    return registry.execute(ctx, call.name, call.input)


def _tool_result(call: ToolCall, outcome: ToolOutcome) -> dict:
    block = {"type": "tool_result", "tool_use_id": call.id, "content": outcome.content}
    if outcome.is_error:
        block["is_error"] = True
    return block


# --------------------------------------------------------------------------- the loop


async def run_loop(
    ctx: RunContext,
    client: ModelClient,
    registry: ToolRegistry,
    limits: LoopLimits,
    *,
    budget: BudgetGuard | None = None,
    trigger: str | None = None,
    record_turns: bool = False,
    billable_models: tuple[str, ...] = (),
    origin: str = "",
) -> AsyncIterator[TraceStep]:
    tools = registry.definitions()
    messages: list[dict] = [{"role": "user", "content": trigger or build_trigger(ctx.systems)}]
    turns = _turn_log(ctx, tools) if record_turns else None
    attempts = billable_models or (client.model,)  # primary first, then each named fallback
    started = time.monotonic()
    nudged = False

    for call_no in range(1, limits.max_model_calls + 1):
        remaining = limits.run_timeout_s - (time.monotonic() - started)
        if remaining <= 1.0:
            yield ctx.emit(StepKind.ERROR, "Run time limit reached", ok=False,
                           summary=f"Stopped after {limits.run_timeout_s:.0f} s of wall-clock time.")
            raise LiveRunAborted("run time limit reached")

        try:
            hold = CallHold.reserve(ctx, budget, origin=origin, usd=lambda: reservation_usd(
                attempts, input_token_bound(tools, messages), limits.max_tokens_per_call))
        except BudgetExceeded as exc:
            yield ctx.guardrail("Budget check: daily cost cap reached", str(exc))
            raise LiveRunAborted("daily cost cap reached") from exc

        timeout = min(remaining, limits.call_timeout_s)
        t0 = time.monotonic()
        try:
            response = await asyncio.wait_for(
                client.create(system=SYSTEM_PROMPT, tools=tools, messages=messages,
                              max_tokens=limits.max_tokens_per_call, timeout_s=timeout),
                timeout=timeout + 2.0,
            )
        except ScriptExhausted:  # a scripted stand-in ran out of turns: no call was made
            hold.not_made()
            break
        except (ModelError, TimeoutError) as exc:
            hold.failed(exc)
            reason = str(exc) or "model call timed out"
            yield ctx.emit(StepKind.ERROR, "Model call failed", summary=reason, ok=False)
            raise LiveRunAborted(reason) from exc
        except asyncio.CancelledError:
            hold.retain()
            raise

        yield _complete_call(ctx, hold, response, call_no=call_no, limits=limits, attempts=attempts, turns=turns,
                             measured_ms=int((time.monotonic() - t0) * 1000))
        unusable = _unusable_turn(response, limits)
        if unusable is not None:
            title, summary, reason = unusable
            yield ctx.emit(StepKind.ERROR, title, ok=False, summary=summary)
            raise LiveRunAborted(reason)

        messages.append({"role": "assistant", "content": response.raw_content})
        if not response.tool_calls:
            if ctx.handover_accepted or nudged or not client.can_nudge:
                break
            nudged = True
            messages.append({"role": "user", "content": NUDGE})
            continue

        results = []
        for call in response.tool_calls:
            outcome = await _dispatch(ctx, registry, call, limits.tool_delay_s)
            for step in outcome.steps:
                yield step
            results.append(_tool_result(call, outcome))
        messages.append({"role": "user", "content": results})  # all results in one user turn
        if ctx.handover_accepted:
            break
    else:
        yield ctx.guardrail("Step limit reached",
                            f"Stopped after {limits.max_model_calls} model calls without finishing.")


def finalize(ctx: RunContext) -> TraceStep:
    """Close the run: totals and status. A run without an accepted handover is marked incomplete and gets
    a Python-only conclusion; it is never shown as a finished investigation."""
    run = ctx.run
    if not ctx.handover_accepted:
        assessments = [assess_chiller(ctx.systems, a) for a in sorted(affected_chillers(ctx))]
        gate = gate_conclusion(assessments, "", "insufficient_data")
        headline = gate.conclusion or "No evidence check was possible."
        run.conclusion = f"Investigation incomplete: no accepted handover. {headline}"
        run.confidence = gate.confidence
        run.extra["incomplete"] = True
        ctx.emit(StepKind.ERROR, "No accepted handover note", ok=False,
                 summary="The run is marked incomplete. The conclusion shown is computed in code from the "
                         "evidence checks only; re-run the investigation.")
    pending = [a for a in run.approvals if a.status == ApprovalStatus.PENDING]
    actions = [s for s in run.steps if s.kind == StepKind.TOOL and s.tier == Tier.AUTO and s.ok
               and s.title != "write_handover_note"]
    run.extra.update(
        created=dict(ctx.created),
        injection_flags=list(ctx.injection_flags),
        lookups=sorted(f"{k}:{a}" for k, a in ctx.lookups),
    )
    step = ctx.emit(
        StepKind.CONCLUSION,
        run.conclusion,
        summary=(f"Confidence: {run.confidence} · {len(actions)} automatic action(s) · "
                 f"{len(pending)} proposal(s) awaiting approval"),
        evidence_ids=sorted({i for s in run.steps for i in s.evidence_ids})[:40],
    )
    run.total_cost_usd = round(sum(s.cost_usd for s in run.steps), 6)
    run.total_tokens = sum(s.input_tokens + s.output_tokens for s in run.steps)
    if not ctx.handover_accepted:
        run.status = RunStatus.INCOMPLETE
    else:
        run.status = RunStatus.AWAITING_APPROVAL if pending else RunStatus.DONE
    return step
