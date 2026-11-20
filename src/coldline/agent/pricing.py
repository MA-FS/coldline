"""Model pricing: the single place where tokens become dollars.

Prices are USD per million tokens (Anthropic first-party API list prices). Cache writes use the 5-minute
TTL rate. Unknown models are priced at a deliberately high rate (``CONSERVATIVE``) so a misconfiguration
over-estimates spend instead of slipping past the daily cap.

One API call can bill more than one attempt. With server-side fallback a declined request is re-run on a
named fallback model inside the same call, and the response reports every billed attempt in
``usage.iterations`` (its top-level counters cover only the attempt that produced the returned message).
So the bound and the settlement both work per attempt:

* :func:`reservation_usd` reserves the *sum* of the worst cases of every attempt the request allows: the
  primary model, then each named fallback in order, each with the full output limit and an input that may
  carry the earlier attempts' partial output. Fallbacks are named explicitly (never ``"default"``), so the
  set of models and the number of attempts are known before the call is made.
* :func:`cost_usd` prices each reported attempt at its own model's rate and adds them up. The provider names
  the model of a fallback attempt (``fallback_message``) but not always of a ``message`` attempt. With one
  named fallback an unnamed ``message`` attempt can only be the requested model; with more, it may be a
  declined fallback too, so it is priced at the most expensive model the request allowed. An ambiguous
  liability is never settled below what it may have cost.
* :func:`split_cost` separates what is **measured** from what is **estimated**. A declined attempt is reported
  in ``usage.iterations`` whether or not it is billed: under the provider's published rules a decline that
  arrives before any output is billed only for some refusal categories (``BILLED_BEFORE_OUTPUT``), and an
  attempt that produced output is always billed. Usage the provider reports for an attempt those rules bill
  is measured spend. A decline before any output in another category, or one whose category is unknown, is
  priced from its reported usage but kept apart as an estimate: the daily cap still counts it (an ambiguous
  liability is never settled at zero), and it is never shown as measured spend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float


PRICES: dict[str, Price] = {
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_write=2.50, cache_read=0.10),
    "claude-sonnet-5": Price(input=2.00, output=10.00, cache_write=2.50, cache_read=0.20),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01),
}
CONSERVATIVE = Price(input=10.00, output=50.00, cache_write=12.50, cache_read=1.00)
# Refusal categories for which a decline before any output is billed, per the provider's published billing
# rules ("How refusals are billed"). The list can change, which is why other declines are estimates, not zero.
BILLED_BEFORE_OUTPUT = frozenset({"bio", "frontier_llm", "reasoning_extraction"})
MAX_FALLBACKS = 3  # the API accepts one to three named fallback models


@dataclass(frozen=True)
class Attempt:
    """One billed attempt inside a single API call, as ``usage.iterations`` reports it. ``model`` is None
    when the provider did not name the model that ran it."""

    model: str | None
    kind: str = "message"  # "message" | "fallback_message" | another iteration type
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    declined: bool = False  # this attempt's model declined (a refusal), so another attempt or none followed
    category: str | None = None  # the refusal category, when the response names it

    @property
    def billing_known(self) -> bool:
        """Whether the published rules bill this attempt: any attempt that ran without declining or produced
        output, and a decline before any output only in a billed category."""
        return not self.declined or self.output_tokens > 0 or self.category in BILLED_BEFORE_OUTPUT

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"model": self.model, "type": self.kind, "input_tokens": self.input_tokens,
                               "output_tokens": self.output_tokens,
                               "cache_creation_input_tokens": self.cache_creation_input_tokens,
                               "cache_read_input_tokens": self.cache_read_input_tokens}
        if self.declined:
            out |= {"declined": True, "category": self.category}
        return out


@dataclass(frozen=True)
class Usage:
    """Token counts for one call. With ``attempts`` the counters are the sums over every billed attempt."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    attempts: tuple[Attempt, ...] = ()

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }
        if self.attempts:
            out["iterations"] = [a.as_dict() for a in self.attempts]
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Usage:
        data = data or {}
        attempts = tuple(
            Attempt(model=i.get("model"), kind=str(i.get("type") or "message"),
                    input_tokens=_count(i, "input_tokens"), output_tokens=_count(i, "output_tokens"),
                    cache_creation_input_tokens=_count(i, "cache_creation_input_tokens"),
                    cache_read_input_tokens=_count(i, "cache_read_input_tokens"),
                    declined=bool(i.get("declined")), category=i.get("category"))
            for i in data.get("iterations") or [] if isinstance(i, dict))
        return cls(
            input_tokens=_count(data, "input_tokens"),
            output_tokens=_count(data, "output_tokens"),
            cache_creation_input_tokens=_count(data, "cache_creation_input_tokens"),
            cache_read_input_tokens=_count(data, "cache_read_input_tokens"),
            attempts=attempts,
        )

    @classmethod
    def billed(cls, top: Usage, attempts: list[Attempt]) -> Usage:
        """Usage for a call whose response reported per-attempt usage: the counters become the sums over
        every attempt (the top-level counters cover only the attempt that answered)."""
        if not attempts:
            return top
        return cls(
            input_tokens=sum(a.input_tokens for a in attempts),
            output_tokens=sum(a.output_tokens for a in attempts),
            cache_creation_input_tokens=sum(a.cache_creation_input_tokens for a in attempts),
            cache_read_input_tokens=sum(a.cache_read_input_tokens for a in attempts),
            attempts=tuple(attempts),
        )


def _count(data: dict[str, Any], key: str) -> int:
    return int(data.get(key, 0) or 0)


def price_for(model: str | None) -> Price:
    if not model:
        return CONSERVATIVE
    if model in PRICES:
        return PRICES[model]
    # Longest matching prefix, so a dated or suffixed id still resolves to its family.
    for known in sorted(PRICES, key=len, reverse=True):
        if model.startswith(known):
            return PRICES[known]
    return CONSERVATIVE


def _priced(p: Price, u: Usage | Attempt) -> float:
    return (u.input_tokens * p.input + u.output_tokens * p.output
            + u.cache_creation_input_tokens * p.cache_write + u.cache_read_input_tokens * p.cache_read)


@dataclass(frozen=True)
class CallCost:
    """One call's cost: ``measured`` from provider-reported usage the published rules bill, ``estimated``
    from reported usage whose billing is not established (a decline before any output in a category that
    is not billed, or unknown). The daily cap charges ``total``; only ``measured`` is reported as spend."""

    measured: float = 0.0
    estimated: float = 0.0

    @property
    def total(self) -> float:
        return round(self.measured + self.estimated, 6)


def _attempt_usd(a: Attempt, model: str, candidates: tuple[str, ...], pool: tuple[str, ...]) -> float:
    if a.model:
        return _priced(price_for(a.model), a)
    if a.kind == "message" and candidates and len(candidates) <= 2:
        return _priced(price_for(candidates[0]), a)
    return max(_priced(price_for(m), a) for m in pool)


def split_cost(model: str, usage: Usage, candidates: tuple[str, ...] = ()) -> CallCost:
    """What one call cost, measured and estimated apart. ``candidates`` are the attempts the request allowed,
    the requested model first.

    Each reported attempt is priced at its own model. An attempt that names no model is the requested model's
    when it is a ``message`` attempt and the request named at most one fallback; otherwise it is priced at
    the most expensive of ``candidates`` and ``model``."""
    if not usage.attempts:
        return CallCost(measured=round(_priced(price_for(model), usage) / 1_000_000, 6))
    pool = tuple(dict.fromkeys((model, *candidates)))
    measured = estimated = 0.0
    for a in usage.attempts:
        usd = _attempt_usd(a, model, candidates, pool)
        if a.billing_known:
            measured += usd
        else:
            estimated += usd
    return CallCost(measured=round(measured / 1_000_000, 6), estimated=round(estimated / 1_000_000, 6))


def cost_usd(model: str, usage: Usage, candidates: tuple[str, ...] = ()) -> float:
    """The most one call may have cost (measured plus estimated): what the daily cap charges."""
    return split_cost(model, usage, candidates).total


def worst_case_cost_usd(model: str, input_tokens: int, max_output_tokens: int) -> float:
    """Upper bound for one attempt: every input token billed at the cache-write rate, full max_tokens out."""
    p = price_for(model)
    per_input = max(p.input, p.cache_write)
    return round((input_tokens * per_input + max_output_tokens * p.output) / 1_000_000, 6)


def permitted_models(model: str, fallbacks: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The attempts one request allows, in order: the requested model, then each named fallback model."""
    if len(fallbacks) > MAX_FALLBACKS:
        raise ValueError(f"at most {MAX_FALLBACKS} fallback models, not {len(fallbacks)}")
    return (model, *fallbacks)


def reservation_usd(models: tuple[str, ...], input_tokens: int, max_output_tokens: int) -> float:
    """Upper bound for one call whose attempts are ``models`` (primary first, then each fallback): the sum
    of every attempt's worst case. Attempt *k* may also read the earlier attempts' partial output as
    continuation context, so its input bound grows by ``max_output_tokens`` per earlier attempt."""
    return round(sum(worst_case_cost_usd(m, input_tokens + k * max_output_tokens, max_output_tokens)
                     for k, m in enumerate(models)), 6)
