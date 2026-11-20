"""Cost and abuse control: rate limit, concurrency, reserved daily cap, persistence, pricing."""

from __future__ import annotations

import json

import pytest

from coldline.agent.budget import BudgetExceeded, BudgetGuard
from coldline.agent.pricing import CONSERVATIVE, Usage, cost_usd, price_for, worst_case_cost_usd


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def guard(clock: Clock, **kw) -> BudgetGuard:
    base = {"daily_cap_usd": 1.0, "session_runs_per_window": 3, "max_concurrent": 2, "min_call_reserve_usd": 0.05}
    return BudgetGuard(**(base | kw), clock=clock)


def test_session_rate_limit_rolls_over() -> None:
    clock = Clock()
    g = guard(clock, max_concurrent=10)
    assert [g.admit("s1") for _ in range(3)] == ["", "", ""]
    assert "Live-run limit" in g.admit("s1")
    assert g.admit("s2") == ""
    clock.t += 3601
    assert g.admit("s1") == ""


def test_concurrency_limit() -> None:
    g = guard(Clock(), session_runs_per_window=10)
    assert g.admit("a") == "" and g.admit("b") == ""
    assert "busy" in g.admit("c")
    g.finish()
    assert g.admit("c") == ""


def test_reservations_cannot_overshoot_the_cap() -> None:
    g = guard(Clock())
    r1 = g.reserve(0.6)
    with pytest.raises(BudgetExceeded):
        g.reserve(0.5)  # 0.6 reserved + 0.5 would pass 1.0 before anything is billed
    g.settle(r1, 0.02)  # the real cost is far below the worst case
    r2 = g.reserve(0.5)
    g.settle(r2, 0.97)
    assert "used up" in g.admit("s")
    status = g.status("s", live_enabled=True, api_key_present=True)
    assert not status.live_available and status.spent_today_usd == pytest.approx(0.99)


def test_daily_spend_persists_and_resets_next_day(tmp_path) -> None:
    clock = Clock()
    path = tmp_path / "agent_spend.json"
    g = guard(clock, state_path=path)
    g.settle(g.reserve(0.3), 0.3)
    assert json.loads(path.read_text())["spent_usd"] == 0.3
    assert guard(clock, state_path=path).status("s", live_enabled=True, api_key_present=True).spent_today_usd == 0.3
    clock.t += 86_400
    assert guard(clock, state_path=path).status("s", live_enabled=True, api_key_present=True).spent_today_usd == 0


def test_status_reports_kill_switch_and_missing_key() -> None:
    g = guard(Clock())
    assert "switched off" in g.status("s", live_enabled=False, api_key_present=True).reason
    assert "API key" in g.status("s", live_enabled=True, api_key_present=False).reason


def test_pricing_table() -> None:
    usage = Usage(input_tokens=10, output_tokens=500, cache_creation_input_tokens=1000, cache_read_input_tokens=5000)
    # Sonnet 5.5: $2 in, $10 out, $2.50 cache write, $0.10 cache read per million tokens
    assert cost_usd("claude-sonnet-5-5", usage) == pytest.approx((20 + 5000 + 2500 + 500) / 1e6)
    assert price_for("claude-sonnet-5-5-preview") == price_for("claude-sonnet-5-5")
    assert price_for("some-new-model") == CONSERVATIVE
    assert worst_case_cost_usd("claude-sonnet-5-5", 10_000, 3000) == pytest.approx((25_000 + 30_000) / 1e6)
