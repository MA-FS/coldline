"""Model clients behind one small interface.

* :class:`AnthropicModelClient` calls the Messages API (live mode).
* :class:`ScriptedModelClient` plays back a fixed list of model turns. It powers replay mode (recorded
  turns, recorded timing) and the deterministic eval suite (scripted good and adversarial models).

The loop only ever sees :class:`ModelResponse`, so live, replay and evals share every line of tool
execution, guardrail and approval code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel, Field

from coldline.agent.config import DEFAULT_MODEL, AgentSettings
from coldline.agent.pricing import Attempt, Usage

SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ModelResponse:
    model: str
    stop_reason: str
    text: str  # operator-facing progress note(s) the model wrote this turn
    tool_calls: list[ToolCall]
    usage: Usage
    raw_content: list[Any] = field(default_factory=list)  # appended to the conversation unchanged
    latency_ms: int | None = None  # set by replay to show the recorded latency


class ModelError(Exception):
    """A model call failed in a way that should end the live run and fall back to replay.

    ``billing_unknown`` is set when the request may have been processed (timeout, lost connection): the
    budget then keeps the call's reservation instead of assuming it cost nothing.
    """

    def __init__(self, message: str, *, billing_unknown: bool = False) -> None:
        super().__init__(message)
        self.billing_unknown = billing_unknown


class ScriptExhausted(Exception):
    """A scripted client has no turns left."""


class ModelClient(Protocol):
    model: str
    can_nudge: bool  # whether the loop may add a reminder turn when the model stops early

    async def create(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
        timeout_s: float,
    ) -> ModelResponse: ...


# --------------------------------------------------------------------------- live


class AnthropicModelClient:
    """Messages API client: prompt caching on tools + system prompt and on the growing conversation."""

    can_nudge = True

    def __init__(self, settings: AgentSettings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.model = settings.model
        self._settings = settings
        # No SDK retries: one budget reservation covers exactly one HTTP attempt. A failed call ends the
        # live run and the visitor continues on the recorded run instead.
        self._client = client or anthropic.AsyncAnthropic(max_retries=0, timeout=settings.call_timeout_s)

    def _thinking(self) -> dict[str, Any]:
        if self._settings.thinking == "between_tools":
            # No extended thinking; short progress notes between tool calls come back as thinking blocks.
            return {"type": "between_tools"}
        return {"type": "adaptive", "display": "summarized"}

    async def create(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
        timeout_s: float,
    ) -> ModelResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            # Tools render before the system prompt, so this breakpoint caches both (stable across runs).
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": tools,
            "messages": messages,
            "thinking": self._thinking(),
            "output_config": {"effort": self._settings.effort},
            # Automatic breakpoint on the last block: each loop iteration re-reads the prior turns from cache.
            "cache_control": {"type": "ephemeral"},
        }
        fallbacks = self._settings.fallbacks
        try:
            if fallbacks:
                # Named fallbacks only, each capped at the same output limit: the models and the number of
                # billed attempts are then known before the call, so the budget can reserve all of them
                # (``pricing.reservation_usd``). A fallback inherits the request's thinking where its model
                # supports it, and otherwise runs with thinking disabled.
                response = await self._client.beta.messages.create(
                    **params, betas=[SERVER_FALLBACK_BETA], timeout=timeout_s,
                    fallbacks=[{"model": m, "max_tokens": max_tokens} for m in fallbacks],
                )
            else:
                response = await self._client.messages.create(**params, timeout=timeout_s)
        except anthropic.APITimeoutError as exc:
            raise ModelError("model call timed out", billing_unknown=True) from exc
        except anthropic.RateLimitError as exc:
            raise ModelError("model API rate limit") from exc
        except anthropic.AuthenticationError as exc:
            raise ModelError("model API key rejected") from exc
        except anthropic.BadRequestError as exc:
            raise ModelError(f"model API rejected the request: {exc.message[:160]}") from exc
        except anthropic.APIStatusError as exc:
            raise ModelError(f"model API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise ModelError("could not reach the model API", billing_unknown=True) from exc
        return _normalise(response)


def _normalise(response: Any) -> ModelResponse:
    notes: list[str] = []
    calls: list[ToolCall] = []
    for block in response.content:
        kind = getattr(block, "type", None)
        if kind == "text" and block.text.strip():
            notes.append(block.text.strip())
        elif kind == "thinking" and (getattr(block, "thinking", "") or "").strip():
            notes.append(block.thinking.strip())
        elif kind == "tool_use":
            calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input or {})))
    return ModelResponse(
        model=response.model,
        stop_reason=response.stop_reason or "end_turn",
        text="\n".join(notes),
        tool_calls=calls,
        usage=billed_usage(response.usage),
        raw_content=list(response.content),
    )


def _tokens(obj: Any, name: str) -> int:
    return int(getattr(obj, name, 0) or 0)


def billed_usage(u: Any) -> Usage:
    """Usage for every billed attempt of one call. With server-side fallback the top-level counters cover
    only the attempt that produced the message; ``usage.iterations`` lists each attempt (a declined primary,
    the fallback that answered) with its own model and counts, and is the source of truth for billing."""
    top = Usage(
        input_tokens=_tokens(u, "input_tokens"),
        output_tokens=_tokens(u, "output_tokens"),
        cache_creation_input_tokens=_tokens(u, "cache_creation_input_tokens"),
        cache_read_input_tokens=_tokens(u, "cache_read_input_tokens"),
    )
    attempts = [
        Attempt(model=getattr(i, "model", None) or None, kind=str(getattr(i, "type", "") or "message"),
                input_tokens=_tokens(i, "input_tokens"), output_tokens=_tokens(i, "output_tokens"),
                cache_creation_input_tokens=_tokens(i, "cache_creation_input_tokens"),
                cache_read_input_tokens=_tokens(i, "cache_read_input_tokens"))
        for i in getattr(u, "iterations", None) or []
    ]
    return Usage.billed(top, attempts)


# --------------------------------------------------------------------------- scripted / recorded


class ScriptedCall(BaseModel):
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ScriptedTurn(BaseModel):
    """One model turn: an operator-facing note plus the tool calls the model made."""

    think: str = ""
    tool_calls: list[ScriptedCall] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)  # Usage.as_dict(), with per-attempt iterations
    latency_ms: int = 0
    model: str = DEFAULT_MODEL
    stop_reason: str = ""  # default: "tool_use" when there are calls, else "end_turn"
    error: str = ""  # simulate a failed call (tests the fallback path)
    error_billing_unknown: bool = False  # simulate a lost response (timeout, dropped connection)


class ScriptedModelClient:
    can_nudge = False

    def __init__(
        self,
        turns: list[ScriptedTurn],
        *,
        model: str = DEFAULT_MODEL,
        speed: float = 0.0,
        max_delay_s: float = 2.5,
        resolve: Callable[[Any], Any] | None = None,
        usage_fn: Callable[[list[dict[str, Any]], ScriptedTurn], Usage] | None = None,
    ) -> None:
        self.turns = turns
        self.model = model
        self.speed = speed
        self.max_delay_s = max_delay_s
        self.resolve = resolve or (lambda x: x)
        self.usage_fn = usage_fn
        self.calls = 0

    async def create(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
        timeout_s: float,
    ) -> ModelResponse:
        if self.calls >= len(self.turns):
            raise ScriptExhausted
        turn = self.turns[self.calls]
        self.calls += 1
        if self.speed > 0 and turn.latency_ms:
            await asyncio.sleep(min(turn.latency_ms / 1000 * self.speed, self.max_delay_s))
        if turn.error:
            raise ModelError(turn.error, billing_unknown=turn.error_billing_unknown)
        think = self.resolve(turn.think)
        calls = [
            ToolCall(id=f"toolu_s{self.calls:02d}_{k}", name=c.name, input=self.resolve(c.input))
            for k, c in enumerate(turn.tool_calls)
        ]
        raw: list[Any] = [{"type": "text", "text": think}] if think else []
        raw += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
        usage = self.usage_fn(messages, turn) if self.usage_fn else Usage.from_dict(turn.usage)
        return ModelResponse(
            model=turn.model or self.model,
            stop_reason=turn.stop_reason or ("tool_use" if calls else "end_turn"),
            text=think,
            tool_calls=calls,
            usage=usage,
            raw_content=raw,
            latency_ms=turn.latency_ms or None,
        )
