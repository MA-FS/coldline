"""Model pricing: the single place where tokens become dollars.

Prices are USD per million tokens (Anthropic first-party API list prices). Cache writes use the 5-minute
TTL rate. Unknown models are priced at a deliberately high rate so a misconfiguration over-estimates
spend instead of slipping past the daily cap.
"""

from __future__ import annotations

from dataclasses import dataclass


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


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, int] | None) -> Usage:
        data = data or {}
        return cls(
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
            cache_creation_input_tokens=int(data.get("cache_creation_input_tokens", 0) or 0),
            cache_read_input_tokens=int(data.get("cache_read_input_tokens", 0) or 0),
        )


def price_for(model: str) -> Price:
    if model in PRICES:
        return PRICES[model]
    # Longest matching prefix, so a dated or suffixed id still resolves to its family.
    for known in sorted(PRICES, key=len, reverse=True):
        if model.startswith(known):
            return PRICES[known]
    return CONSERVATIVE


def cost_usd(model: str, usage: Usage) -> float:
    p = price_for(model)
    total = (
        usage.input_tokens * p.input
        + usage.output_tokens * p.output
        + usage.cache_creation_input_tokens * p.cache_write
        + usage.cache_read_input_tokens * p.cache_read
    )
    return round(total / 1_000_000, 6)


def worst_case_cost_usd(model: str, input_tokens: int, max_output_tokens: int) -> float:
    """Upper bound for one call: every input token billed at the cache-write rate, full max_tokens out."""
    p = price_for(model)
    per_input = max(p.input, p.cache_write)
    return round((input_tokens * per_input + max_output_tokens * p.output) / 1_000_000, 6)


def permitted_models(model: str, server_fallback: bool) -> tuple[str, ...]:
    """Models that may serve (and bill) a request. With server-side fallback the API may answer from
    another model, so every priced model is assumed possible."""
    return (model, *PRICES) if server_fallback else (model,)


def reservation_usd(models: tuple[str, ...], input_tokens: int, max_output_tokens: int) -> float:
    """Upper bound for one call that any of ``models`` may serve: the most expensive one's worst case."""
    return max(worst_case_cost_usd(m, input_tokens, max_output_tokens) for m in models)
