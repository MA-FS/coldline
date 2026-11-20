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


# --------------------------------------------------------------------------- per-origin quota, retention


def test_new_sessions_from_one_address_share_a_daily_quota() -> None:
    clock = Clock()
    g = guard(clock, max_concurrent=100, origin_runs_per_day=4)
    started = [g.admit(f"cookie-{i}", origin="203.0.113.7") for i in range(6)]
    assert started[:4] == ["", "", "", ""] and all("this address" in r for r in started[4:])
    assert g.admit("someone-else", origin="198.51.100.2") == ""
    clock.t += 86_401
    assert g.admit("cookie-9", origin="203.0.113.7") == ""


def test_status_reads_allocate_nothing_and_idle_entries_expire() -> None:
    clock = Clock()
    g = guard(clock)
    for i in range(50):
        g.status(f"visitor-{i}", live_enabled=True, api_key_present=True, origin=f"10.0.0.{i}")
    assert g._runs == {} and g._origin_runs == {}
    g.admit("s", origin="10.0.0.1")
    clock.t += 86_401
    g.admit("t")  # any admission sweeps entries whose windows have passed
    assert set(g._runs) == {"t"} and g._origin_runs == {}


# --------------------------------------------------------------------------- bounds that hold (M1)


def test_reservation_bounds_any_model_that_may_answer() -> None:
    from coldline.agent.loop import input_token_bound
    from coldline.agent.pricing import permitted_models, reservation_usd

    models = permitted_models("claude-sonnet-5-5", server_fallback=True)
    messages = [{"role": "user", "content": "Ü" * 1000}]  # 2 bytes per character: bytes bound tokens
    bound = input_token_bound([], messages)
    assert bound >= 2000
    reserved = reservation_usd(models, bound, 3000)
    # The most expensive permitted model answering with the full input and output still fits the reservation.
    for model in models:
        worst = cost_usd(model, Usage(cache_creation_input_tokens=bound, output_tokens=3000))
        assert worst <= reserved + 1e-9
    assert reservation_usd(permitted_models("claude-sonnet-5-5", False), bound, 3000) < reserved


async def test_expensive_fallback_near_the_cap_stays_under_it() -> None:
    from agent_helpers import plant, settings, turn

    from coldline.agent.model import ScriptedModelClient
    from coldline.agent.service import AgentService
    from coldline.contracts import Scenario

    usage = {"output_tokens": 3000, "cache_creation_input_tokens": 20_000}
    # The API answers every call from the most expensive model, as a server-side fallback might.
    turns = [turn("Reading.", ("get_sensor_trend", {"tags": ["TT-401", "TT-402"]}), usage=usage,
                  model="claude-opus-5-5")] * 8
    budget = BudgetGuard(daily_cap_usd=0.6, session_runs_per_window=5, max_concurrent=2, min_call_reserve_usd=0.0)
    svc = AgentService(settings(), budget=budget, live_client_factory=lambda _: ScriptedModelClient(turns))
    run = await svc.run_to_completion("s", plant(Scenario.PROBE_MISMATCH), mode="live")
    spent = budget.status("s", live_enabled=True, api_key_present=True).spent_today_usd
    assert spent <= 0.6 and run.extra["fallback_reason"] == "daily cost cap reached"
    assert any(s.model_dump().get("cost_usd") for s in run.steps)


async def test_lost_response_keeps_its_reservation_until_reconciled(tmp_path) -> None:
    from agent_helpers import plant, settings, turn

    from coldline.agent.model import ScriptedModelClient
    from coldline.agent.service import AgentService
    from coldline.contracts import Scenario

    path = tmp_path / "agent_spend.json"
    budget = BudgetGuard(daily_cap_usd=3.0, session_runs_per_window=5, max_concurrent=2, min_call_reserve_usd=0.0,
                         state_path=path)
    turns = [turn("", error="model call timed out", error_billing_unknown=True)]
    svc = AgentService(settings(), budget=budget, live_client_factory=lambda _: ScriptedModelClient(turns))
    run = await svc.run_to_completion("s", plant(Scenario.DOOR_OPEN), mode="live")
    status = budget.status("s", live_enabled=True, api_key_present=True)
    assert run.extra["fallback_reason"] == "model call timed out"
    assert status.spent_today_usd > 0 and status.unreconciled_usd == status.spent_today_usd
    assert json.loads(path.read_text())["unreconciled_usd"] > 0
    # An API error response, by contrast, generated nothing and costs nothing.
    clean = BudgetGuard(daily_cap_usd=3.0, session_runs_per_window=5, max_concurrent=2, min_call_reserve_usd=0.0)
    turns = [turn("", error="model API error 529")]
    svc = AgentService(settings(), budget=clean, live_client_factory=lambda _: ScriptedModelClient(turns))
    await svc.run_to_completion("s", plant(Scenario.DOOR_OPEN), mode="live")
    assert clean.status("s", live_enabled=True, api_key_present=True).spent_today_usd == 0


# --------------------------------------------------------------------------- a ledger that fails closed (M2)


def test_reservation_is_journalled_before_the_call_and_survives_a_restart(tmp_path) -> None:
    clock = Clock()
    path = tmp_path / "agent_spend.json"
    g = guard(clock, state_path=path)
    g.reserve(0.4)  # the process stops here, mid-call
    assert json.loads(path.read_text())["outstanding"] == {"1": 0.4}
    restarted = guard(clock, state_path=path)
    status = restarted.status("s", live_enabled=True, api_key_present=True)
    assert status.spent_today_usd == pytest.approx(0.4) and status.unreconciled_usd == pytest.approx(0.4)
    assert json.loads(path.read_text())["outstanding"] == {}


def test_unreadable_ledger_disables_paid_mode(tmp_path) -> None:
    path = tmp_path / "agent_spend.json"
    path.write_text('{"day": "2027-01-15", "spent_usd": 2.9')  # truncated by an interrupted write
    g = guard(Clock(), state_path=path)
    status = g.status("s", live_enabled=True, api_key_present=True)
    assert not status.live_available and not status.ledger_ok and "ledger" in status.reason
    assert "ledger" in g.admit("s")
    with pytest.raises(BudgetExceeded):
        g.reserve(0.01)


def test_ledger_write_failure_refuses_the_call(tmp_path, monkeypatch) -> None:
    g = guard(Clock(), state_path=tmp_path / "agent_spend.json")

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("coldline.agent.budget.tempfile.mkstemp", broken)
    with pytest.raises(BudgetExceeded):
        g.reserve(0.01)
    assert "ledger" in g.admit("s")
    assert g.status("s", live_enabled=True, api_key_present=True).reserved_usd == 0
