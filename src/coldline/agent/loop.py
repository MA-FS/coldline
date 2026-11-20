"""The agent loop: a small, bounded tool-use loop over the Messages API shape.

    trigger -> [model call -> run each requested tool through the registry -> feed results back] * N
            -> accepted handover note (or a limit) -> conclusion

It is an async generator of :class:`TraceStep` so the web layer can stream each step as it happens.
Bounds: ``max_model_calls`` per run, ``max_tokens_per_call``, a per-call timeout and a wall-clock budget
for the whole run. In live mode every call first reserves its worst-case cost against the daily cap.
Any failure that should not be shown as a broken agent (API error, timeout, refusal, budget) raises
:class:`LiveRunAborted` after emitting a visible step, and the service falls back to replay.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from coldline.agent.budget import BudgetExceeded, BudgetGuard, Reservation
from coldline.agent.config import AgentSettings
from coldline.agent.context import RunContext
from coldline.agent.evidence import assess_chiller, gate_conclusion
from coldline.agent.model import ModelClient, ModelError, ScriptExhausted
from coldline.agent.pricing import cost_usd, worst_case_cost_usd
from coldline.agent.prompts import NUDGE, SYSTEM_PROMPT, build_trigger, prompt_version
from coldline.agent.tools import ToolRegistry, affected_chillers
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


def _settle(budget: BudgetGuard | None, reservation: Reservation | None, actual_usd: float) -> None:
    if budget is not None and reservation is not None:
        budget.settle(reservation, actual_usd)


def _estimate_input_tokens(tools: list[dict], messages: list[dict]) -> int:
    # Deliberately generous (~3 chars per token, repr of SDK objects included) for the cost reservation.
    return (len(SYSTEM_PROMPT) + len(json.dumps(tools)) + len(repr(messages))) // 3


async def run_loop(
    ctx: RunContext,
    client: ModelClient,
    registry: ToolRegistry,
    limits: LoopLimits,
    *,
    budget: BudgetGuard | None = None,
    trigger: str | None = None,
    record_turns: bool = False,
) -> AsyncIterator[TraceStep]:
    tools = registry.definitions()
    messages: list[dict] = [{"role": "user", "content": trigger or build_trigger(ctx.systems)}]
    turns = ctx.run.extra.setdefault("model_turns", []) if record_turns else None
    if record_turns:
        ctx.run.extra["prompt_version"] = prompt_version(tools)
    started = time.monotonic()
    nudged = False

    for call_no in range(1, limits.max_model_calls + 1):
        remaining = limits.run_timeout_s - (time.monotonic() - started)
        if remaining <= 1.0:
            yield ctx.emit(StepKind.ERROR, "Run time limit reached", ok=False,
                           summary=f"Stopped after {limits.run_timeout_s:.0f} s of wall-clock time.")
            raise LiveRunAborted("run time limit reached")

        reservation = None
        if budget is not None:
            worst = worst_case_cost_usd(client.model, _estimate_input_tokens(tools, messages),
                                        limits.max_tokens_per_call)
            try:
                reservation = budget.reserve(worst)
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
        except ScriptExhausted:
            _settle(budget, reservation, 0.0)
            break
        except (ModelError, TimeoutError) as exc:
            _settle(budget, reservation, 0.0)  # a failed call is not billed
            reason = str(exc) or "model call timed out"
            yield ctx.emit(StepKind.ERROR, "Model call failed", summary=reason, ok=False)
            raise LiveRunAborted(reason) from exc
        except asyncio.CancelledError:
            _settle(budget, reservation, 0.0)
            raise

        measured_ms = int((time.monotonic() - t0) * 1000)
        cost = cost_usd(response.model, response.usage)
        _settle(budget, reservation, cost)
        if turns is not None:
            turns.append({
                "think": response.text,
                "tool_calls": [{"name": c.name, "input": c.input} for c in response.tool_calls],
                "usage": response.usage.as_dict(),
                "latency_ms": measured_ms,
                "model": response.model,
                "stop_reason": response.stop_reason,
            })
        u = response.usage
        planned = ", ".join(c.name for c in response.tool_calls)
        yield ctx.emit(
            StepKind.THINK,
            f"Model call {call_no} (limit {limits.max_model_calls})",
            summary=response.text or (f"Calling {planned}" if planned else "No further tool calls."),
            result_preview=(f"in {u.total_input} tok ({u.cache_read_input_tokens} cached) · out "
                            f"{u.output_tokens} tok · US${cost:.4f} · {response.model}"),
            latency_ms=response.latency_ms or measured_ms,
            input_tokens=u.total_input,
            output_tokens=u.output_tokens,
            cost_usd=cost,
        )

        if response.stop_reason == "refusal":
            yield ctx.emit(StepKind.ERROR, "Model declined the request", ok=False,
                           summary="The model stopped with a refusal; no tool calls were run.")
            raise LiveRunAborted("model declined the request")
        if response.stop_reason == "max_tokens":
            yield ctx.emit(StepKind.ERROR, "Model output limit reached", ok=False,
                           summary=f"Output hit max_tokens={limits.max_tokens_per_call}; tool calls discarded.")
            raise LiveRunAborted("model output limit reached")

        messages.append({"role": "assistant", "content": response.raw_content})
        if not response.tool_calls:
            if ctx.handover_accepted or nudged or not client.can_nudge:
                break
            nudged = True
            messages.append({"role": "user", "content": NUDGE})
            continue

        results = []
        for call in response.tool_calls:
            if limits.tool_delay_s:
                await asyncio.sleep(limits.tool_delay_s)
            outcome = registry.execute(ctx, call.name, call.input)
            for step in outcome.steps:
                yield step
            block = {"type": "tool_result", "tool_use_id": call.id, "content": outcome.content}
            if outcome.is_error:
                block["is_error"] = True
            results.append(block)
        messages.append({"role": "user", "content": results})  # all results in one user turn
        if ctx.handover_accepted:
            break
    else:
        yield ctx.guardrail("Step limit reached",
                            f"Stopped after {limits.max_model_calls} model calls without finishing.")


def finalize(ctx: RunContext) -> TraceStep:
    """Close the run: Python-only conclusion if no handover was accepted, totals, status."""
    run = ctx.run
    if not ctx.handover_accepted:
        assessments = [assess_chiller(ctx.systems, a) for a in sorted(affected_chillers(ctx))]
        gate = gate_conclusion(assessments, "", "insufficient_data")
        run.conclusion = gate.conclusion or "Run ended without an accepted handover note."
        run.confidence = gate.confidence
        ctx.emit(StepKind.ERROR, "No accepted handover note", ok=False,
                 summary="The conclusion shown is computed in code from the evidence checks only.")
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
    run.status = RunStatus.AWAITING_APPROVAL if pending else RunStatus.DONE
    return step
