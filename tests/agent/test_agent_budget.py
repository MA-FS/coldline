"""Cost and abuse control: rate limit, concurrency, reserved daily cap, persistence, pricing."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from coldline.agent import AgentService, AgentSettings
from coldline.agent.budget import BudgetExceeded, BudgetGuard
from coldline.agent.pricing import CONSERVATIVE, Attempt, Usage, cost_usd, price_for, worst_case_cost_usd
from coldline.main import create_app
from coldline.web.security import COOKIE_NAME, read_cookie_value
from coldline.web.settings import WebSettings


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
    r2 = g.reserve(0.97)
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
    g.close()  # the process stops
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
    g = guard(clock, max_concurrent=100, origin_concurrent=100, origin_runs_per_day=4)
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


def test_one_address_cannot_hold_both_live_slots_or_the_whole_cap() -> None:
    """The review's case: with two live slots and a shared cap, one address could occupy both slots and
    spend the day's budget. It now gets one slot at a time and a share of the cap."""
    g = guard(Clock(), daily_cap_usd=3.0, max_concurrent=2, origin_daily_usd_cap=1.0, min_call_reserve_usd=0.05)
    me, other = "203.0.113.7", "198.51.100.2"
    assert g.admit("a", origin=me) == ""
    assert "already in progress" in g.admit("b", origin=me)  # a second cookie, same address
    assert g.admit("c", origin=other) == ""  # someone else still gets the other slot
    g.settle(g.reserve(0.9, origin=me), 0.9)
    with pytest.raises(BudgetExceeded, match="this address has US\\$0.100 of its daily share left"):
        g.reserve(0.2, origin=me)
    g.reserve(0.2, origin=other)  # the cap as a whole still has room for others
    g.finish(me)
    g.settle(g.reserve(0.08, origin=me), 0.08)  # 0.98 of its 1.00 share used: not enough for another call
    assert "share of today's live-run budget (US$1.00)" in g.admit("d", origin=me)


# --------------------------------------------------------------------------- bounds that hold (M1)


def test_reservation_covers_every_attempt_the_request_allows() -> None:
    """With a named fallback one call can bill two attempts, so the bound is the sum of both worst cases (the
    fallback may also read the declined attempt's partial output)."""
    from coldline.agent.loop import input_token_bound
    from coldline.agent.pricing import permitted_models, reservation_usd

    models = permitted_models("claude-sonnet-5-5", ("claude-sonnet-5",))
    assert models == ("claude-sonnet-5-5", "claude-sonnet-5")
    bound = input_token_bound([], [{"role": "user", "content": "Ü" * 1000}])  # 2 bytes a character
    assert bound >= 2000
    reserved = reservation_usd(models, bound, 3000)
    worst = (cost_usd("claude-sonnet-5-5", Usage(cache_creation_input_tokens=bound, output_tokens=3000))
             + cost_usd("claude-sonnet-5", Usage(cache_creation_input_tokens=bound + 3000, output_tokens=3000)))
    assert worst <= reserved + 1e-9
    assert reservation_usd(permitted_models("claude-sonnet-5-5"), bound, 3000) < reserved / 1.9
    with pytest.raises(ValueError):
        permitted_models("claude-sonnet-5-5", ("a", "b"))  # one fallback at most: see the test below


def _two_attempts(model: str = "claude-sonnet-5") -> SimpleNamespace:
    """Usage as the API reports a declined primary and the fallback that answered: the top-level counters
    cover the answering attempt only; ``iterations`` lists both."""
    declined = SimpleNamespace(type="message", model="claude-sonnet-5-5", input_tokens=1000, output_tokens=200,
                               cache_creation_input_tokens=0, cache_read_input_tokens=0)
    answered = SimpleNamespace(type="fallback_message", model=model, input_tokens=1000, output_tokens=200,
                               cache_creation_input_tokens=0, cache_read_input_tokens=0)
    return SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, iterations=[declined, answered])


def test_every_billed_attempt_is_settled_at_its_own_rate() -> None:
    """The review's case: two equal-priced attempts settled as one. Each attempt now counts."""
    from coldline.agent.model import billed_usage

    usage = billed_usage(_two_attempts())
    one = (1000 * 2.00 + 200 * 10.00) / 1e6  # Sonnet 5.5 and Sonnet 5 share input and output list prices
    assert len(usage.attempts) == 2 and usage.input_tokens == 2000 and usage.output_tokens == 400
    assert cost_usd("claude-sonnet-5", usage, ("claude-sonnet-5-5", "claude-sonnet-5")) == pytest.approx(2 * one)
    # Each attempt at its own model's rate: an Opus attempt is priced as Opus, not as the answering model.
    opus = billed_usage(_two_attempts("claude-opus-5-5"))
    assert cost_usd("claude-sonnet-5-5", opus) == pytest.approx(one + (1000 * 4.00 + 200 * 20.00) / 1e6)
    # The provider does not always name the model of a "message" attempt (a live call showed model=None for
    # the requested model). With one named fallback it can only be the requested model...
    unnamed = Usage(attempts=(Attempt(model=None, input_tokens=1000, output_tokens=200,
                                      cache_read_input_tokens=10_000),))
    assert cost_usd("claude-sonnet-5-5", unnamed, ("claude-sonnet-5-5", "claude-sonnet-5")) == pytest.approx(
        (1000 * 2.00 + 200 * 10.00 + 10_000 * 0.10) / 1e6)  # Sonnet 5.5's cache-read rate, not Sonnet 5's
    # ...and an attempt that cannot be placed (a longer chain than is supported) is priced at the most
    # expensive model allowed.
    chain = ("claude-haiku-5-5", "claude-sonnet-5", "claude-opus-5-5")
    assert cost_usd("claude-haiku-5-5", unnamed, chain) == pytest.approx(
        (1000 * 4.00 + 200 * 20.00 + 10_000 * 0.20) / 1e6)
    # A recorded turn keeps its attempts.
    assert Usage.from_dict(usage.as_dict()) == usage


def test_at_most_one_fallback_so_measured_spend_is_priced_at_the_model_that_served_it() -> None:
    """With two fallbacks an attempt the provider does not name could be any of them, and was priced as
    measured spend at the most expensive. One fallback at most, checked when the settings are read and when
    the service starts; with one, every unnamed attempt is placed by its kind."""
    from agent_helpers import settings

    from coldline.agent.pricing import permitted_models, split_cost

    two = AgentSettings.from_env({"ANTHROPIC_API_KEY": "x",
                                  "COLDLINE_FALLBACK_MODELS": "claude-sonnet-5, claude-haiku-5-5"})
    assert two.fallbacks == () and any("COLDLINE_FALLBACK_MODELS" in e for e in two.config_errors)
    with pytest.raises(ValueError):
        permitted_models("claude-sonnet-5-5", ("claude-sonnet-5", "claude-haiku-5-5"))
    with pytest.raises(ValueError):
        AgentService(settings(fallback_models=("claude-sonnet-5", "claude-haiku-5-5")))
    # One fallback: the declined primary and the answering fallback, neither named by the provider.
    chain = ("claude-sonnet-5", "claude-haiku-5-5")
    declined = Attempt(model=None, kind="message", input_tokens=1000, output_tokens=50, declined=True,
                       category="cyber")
    answered = Attempt(model=None, kind="fallback_message", input_tokens=1000, output_tokens=200,
                       cache_read_input_tokens=10_000)
    cost = split_cost("claude-sonnet-5", Usage.billed(Usage(), [declined, answered]), chain)
    sonnet = (1000 * 2.00 + 50 * 10.00) / 1e6  # the primary that served the declined attempt
    haiku = (1000 * 0.10 + 200 * 0.50 + 10_000 * 0.01) / 1e6  # the fallback that answered, not Sonnet's rate
    assert cost.measured == pytest.approx(sonnet + haiku) and cost.estimated == 0.0


async def test_a_fallback_near_the_cap_is_reserved_and_settled_in_full() -> None:
    """A live call that falls back bills two attempts. The reservation covers both, the ledger settles both,
    and the next call is refused because the cap could not cover it."""
    from agent_helpers import plant, settings

    from coldline.agent.loop import input_token_bound
    from coldline.agent.model import AnthropicModelClient
    from coldline.agent.pricing import permitted_models, reservation_usd
    from coldline.agent.prompts import build_trigger
    from coldline.agent.service import AgentService
    from coldline.agent.tools import ToolRegistry
    from coldline.contracts import Scenario

    class FakeMessages:
        calls = 0

        async def create(self, **kwargs):
            FakeMessages.calls += 1
            assert kwargs["fallbacks"] == [{"model": "claude-sonnet-5", "max_tokens": kwargs["max_tokens"]}]
            content = [SimpleNamespace(type="tool_use", id=f"toolu_{FakeMessages.calls}", name="list_active_alarms",
                                       input={})]
            return SimpleNamespace(content=content, usage=_two_attempts(), model="claude-sonnet-5",
                                   stop_reason="tool_use")

    fake = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()), messages=None)
    s, p = settings(), plant(Scenario.PROBE_MISMATCH)
    models = permitted_models(s.model, s.fallbacks)
    tools = ToolRegistry().definitions()
    first = reservation_usd(models, input_token_bound(tools, [{"role": "user", "content": build_trigger(p)}]),
                            s.max_tokens_per_call)
    billed = cost_usd("claude-sonnet-5", billed_usage_of_two())
    cap = first + billed / 2  # room for the first call's whole bound, not for a second call after it is billed
    budget = BudgetGuard(daily_cap_usd=cap, session_runs_per_window=5, max_concurrent=2, min_call_reserve_usd=0.0)
    svc = AgentService(s, budget=budget,
                       live_client_factory=lambda st: AnthropicModelClient(st, client=fake))  # type: ignore[arg-type]
    run = await svc.run_to_completion("s", p, mode="live")
    status = budget.status("s", live_enabled=True, api_key_present=True)
    assert FakeMessages.calls == 1 and run.extra["fallback_reason"] == "daily cost cap reached"
    assert status.spent_today_usd == pytest.approx(billed, abs=1e-4) and billed > 0.0039
    assert status.spent_today_usd <= cap and not budget.overrun
    think = next(st for st in run.steps if st.kind.value == "think")
    assert think.cost_usd == pytest.approx(billed) and "2 billed attempts" in think.result_preview


def billed_usage_of_two() -> Usage:
    from coldline.agent.model import billed_usage

    return billed_usage(_two_attempts())


def test_more_attempts_than_reserved_switch_live_runs_off() -> None:
    """A response reporting attempts the request did not allow costs more than its bound: fail closed."""
    from coldline.agent.pricing import permitted_models, reservation_usd

    reserved = reservation_usd(permitted_models("claude-sonnet-5-5", ("claude-sonnet-5",)), 2000, 300)
    attempts = tuple(Attempt(model="claude-opus-5-5", input_tokens=2000, output_tokens=300) for _ in range(4))
    actual = cost_usd("claude-sonnet-5", Usage(attempts=attempts))
    assert actual > reserved
    g = guard(Clock(), daily_cap_usd=3.0, min_call_reserve_usd=0.0)
    g.settle(g.reserve(reserved), actual)
    assert "more than its reserved bound" in g.admit("s")


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
    assert json.loads(path.read_text())["outstanding"] == {"1": {"usd": 0.4, "day": TODAY}}
    g.close()
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


# --------------------------------------------------------------------------- ledger schema (valid JSON, wrong content)

TODAY = "2027-01-15"  # Clock()'s default time, UTC
VALID = {"version": 2, "day": TODAY, "spent_usd": 0.5, "unreconciled_usd": 0.1, "outstanding": {"3": 0.2}}


@pytest.mark.parametrize("content", [
    "{}",
    "[]",
    '{"version": 2, "day": "2027-01-15", "spent_usd": 0.5, "unreconciled_usd": 0, "outstanding": [0.4]}',
    '{"version": 2, "day": "2027-01-15", "spent_usd": 0.5, "unreconciled_usd": 0, "outstanding": "0.4"}',
    json.dumps(VALID | {"spent_usd": -1}),
    json.dumps(VALID | {"spent_usd": "0.5"}),
    json.dumps(VALID | {"spent_usd": True}),
    json.dumps(VALID).replace('"spent_usd": 0.5', '"spent_usd": NaN'),
    json.dumps(VALID).replace('"spent_usd": 0.5', '"spent_usd": Infinity'),
    json.dumps(VALID | {"unreconciled_usd": 0.9}),
    json.dumps(VALID | {"outstanding": {"3": -0.2}}),
    json.dumps(VALID | {"outstanding": {"x": 0.2}}),
    json.dumps(VALID | {"day": "2027-02-30"}),
    json.dumps(VALID | {"day": "15/01/2027"}),
    json.dumps(VALID | {"day": "2027-01-16"}),  # tomorrow: the clock or the file is wrong
    json.dumps(VALID | {"version": 3}),
    json.dumps(VALID | {"extra": 1}),
    json.dumps({k: v for k, v in VALID.items() if k != "unreconciled_usd"}),
])
def test_valid_json_with_invalid_content_keeps_paid_mode_off_and_the_file_intact(tmp_path, content) -> None:
    path = tmp_path / "agent_spend.json"
    path.write_text(content)
    g = guard(Clock(), state_path=path)
    status = g.status("s", live_enabled=True, api_key_present=True)
    assert not status.live_available and not status.ledger_ok, content
    with pytest.raises(BudgetExceeded):
        g.reserve(0.01)
    # Kept as evidence for the operator, never reset to zero: set aside under a quarantine name.
    (moved,) = g.quarantined()
    assert moved.read_text() == content and not path.exists()


HUGE = json.dumps({"day": "2027-01-14", "spent_usd": 10**310})  # legacy format, an integer no float can hold


@pytest.mark.parametrize("content", [HUGE, json.dumps(VALID | {"outstanding": {"3": 10**400}}),
                                     json.dumps(VALID | {"version": 3,
                                                         "outstanding": {"1": {"usd": 0.1, "day": "2099-01-01"}}}),
                                     "[" * 100_000, b"\xff\xfe not text"])
def test_a_damaged_ledger_never_stops_startup_and_is_quarantined(tmp_path, content) -> None:
    path = tmp_path / "agent_spend.json"
    (path.write_bytes if isinstance(content, bytes) else path.write_text)(content)
    g = guard(Clock(), state_path=path)  # must not raise (OverflowError, RecursionError, decode errors)
    status = g.status("s", live_enabled=True, api_key_present=True)
    assert not status.live_available and not status.ledger_ok
    assert status.reason == ("Live runs are disabled: the spend ledger was damaged and has been set aside for an "
                             "operator to check. Recorded runs still work.")
    assert len(g.quarantined()) == 1 and not path.exists()
    g.close()
    # A restart must not start a fresh (under-counting) ledger while the damaged one awaits review.
    again = guard(Clock(), state_path=path)
    assert not again.status("s", live_enabled=True, api_key_present=True).live_available
    assert "waiting for an operator" in again.admit("s") and not path.exists()
    # Once the operator has reviewed and removed it, paid mode can resume.
    again.close()
    g.quarantined()[0].unlink()
    assert guard(Clock(), state_path=path).status("s", live_enabled=True, api_key_present=True).live_available


def test_the_app_starts_and_replays_with_a_damaged_ledger(tmp_path) -> None:
    """Cold start with the audit's file: live off with a visible reason, the hero replay still completes."""
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    damaged = json.dumps({"day": yesterday, "spent_usd": 10**310})
    (tmp_path / "agent_spend.json").write_text(damaged)
    agent = AgentService(AgentSettings(replay_speed=0.0, live_enabled=True, api_key_present=True,
                                       data_dir=str(tmp_path)))
    key, hx = "k" * 32, {"X-ColdLine": "1", "HX-Request": "true"}
    with TestClient(create_app(WebSettings(secret_key=key), agent)) as c:
        assert c.get("/healthz").status_code == 200
        page = c.get("/").text
        assert "the spend ledger was damaged and has been set aside" in page
        r = c.post("/session/mode", data={"mode": "live"}, headers=hx)
        assert r.status_code == 200 and "set aside" in r.headers.get("hx-trigger", "")
        assert c.post("/session/inject", data={"scenario": "probe_mismatch"}, headers=hx).status_code == 200
        sid = read_cookie_value(c.cookies.get(COOKIE_NAME), key.encode())
        for _ in range(300):
            runs = agent.list_runs(sid)
            if runs and runs[0].status.value != "running":
                break
            c.portal.call(asyncio.sleep, 0.01)
        assert runs[0].status.value != "running" and runs[0].handover_note
    assert [f.read_text() for f in tmp_path.glob("agent_spend.json.quarantined-*")] == [damaged]


def test_a_valid_ledger_loads_and_the_version_1_format_is_migrated(tmp_path) -> None:
    path = tmp_path / "agent_spend.json"
    path.write_text(json.dumps(VALID))
    status = guard(Clock(), state_path=path).status("s", live_enabled=True, api_key_present=True)
    assert status.ledger_ok and status.spent_today_usd == pytest.approx(0.7)  # 0.5 spent + 0.2 outstanding
    assert status.unreconciled_usd == pytest.approx(0.3)
    path.write_text(json.dumps({"day": TODAY, "spent_usd": 0.9}))
    status = guard(Clock(), state_path=path).status("s", live_enabled=True, api_key_present=True)
    assert status.ledger_ok and status.spent_today_usd == pytest.approx(0.9)
    assert json.loads(path.read_text())["version"] == 3
    path.write_text(json.dumps(VALID | {"day": "2027-01-14"}))  # yesterday's figures roll over...
    status = guard(Clock(), state_path=path).status("s", live_enabled=True, api_key_present=True)
    # ...but its reservation that never settled is still owed: it carries into today, unreconciled.
    assert status.ledger_ok and status.spent_today_usd == pytest.approx(0.2)
    assert status.unreconciled_usd == pytest.approx(0.2)


# --------------------------------------------------------------------------- one writer, liabilities kept (M5)


def test_a_second_writer_on_the_same_ledger_is_refused(tmp_path) -> None:
    """The review's case: two guards on one US$1 ledger each admitted a US$0.60 reservation. The second guard
    cannot take the writer lock, so it admits nothing and never touches the file."""
    path = tmp_path / "agent_spend.json"
    first, second = guard(Clock(), state_path=path), guard(Clock(), state_path=path)
    first.reserve(0.6)
    assert "another process is already using the spend ledger" in second.admit("s")
    with pytest.raises(BudgetExceeded):
        second.reserve(0.6)
    assert json.loads(path.read_text())["outstanding"] == {"1": {"usd": 0.6, "day": TODAY}}
    first.close()  # once the first process stops, a new one can take over and counts what it left open
    third = guard(Clock(), state_path=path)
    assert third.status("s", live_enabled=True, api_key_present=True).spent_today_usd == pytest.approx(0.6)


def test_a_restart_after_midnight_keeps_yesterdays_open_reservation(tmp_path) -> None:
    clock = Clock()
    path = tmp_path / "agent_spend.json"
    g = guard(clock, state_path=path)
    g.reserve(0.25)  # the process stops mid-call, just before midnight
    g.close()
    clock.t += 86_400
    status = guard(clock, state_path=path).status("s", live_enabled=True, api_key_present=True)
    assert status.spent_today_usd == pytest.approx(0.25) and status.unreconciled_usd == pytest.approx(0.25)


def test_unreconciled_spend_from_an_earlier_day_is_carried_forward_dated(tmp_path) -> None:
    """A call whose outcome is unknown (retained as unreconciled) keeps its record across the daily reset,
    in a running process and after a restart. The cap resets; the amount is not rewritten as US$0.00."""
    clock = Clock()
    path = tmp_path / "agent_spend.json"
    g = guard(clock, state_path=path)
    g.retain(g.reserve(0.3))
    g.close()  # restart the next day
    clock.t += 86_400
    g = guard(clock, state_path=path)
    status = g.status("s", live_enabled=True, api_key_present=True)
    assert status.spent_today_usd == 0 and status.unreconciled_usd == 0  # today's cap is fresh
    assert status.carried_unreconciled == {TODAY: pytest.approx(0.3)}
    assert json.loads(path.read_text())["carried_unreconciled"] == {TODAY: pytest.approx(0.3)}
    g.close()
    # the record survives further restarts and a further day, and keeps its original date
    clock.t += 86_400
    status = guard(clock, state_path=path).status("s", live_enabled=True, api_key_present=True)
    assert status.carried_unreconciled == {TODAY: pytest.approx(0.3)}
    # an uninterrupted process crossing midnight keeps it too
    clock2, path2 = Clock(), tmp_path / "second.json"
    g2 = guard(clock2, state_path=path2)
    g2.retain(g2.reserve(0.2))
    clock2.t += 86_400
    status = g2.status("s", live_enabled=True, api_key_present=True)
    assert status.spent_today_usd == 0 and status.carried_unreconciled == {TODAY: pytest.approx(0.2)}


def test_a_reservation_open_at_midnight_settles_against_the_day_it_settles(tmp_path) -> None:
    """The uninterrupted process and the restarted one agree: an open liability counts on the day it
    resolves, and the journal keeps the day it was reserved for the operator."""
    clock = Clock()
    path = tmp_path / "agent_spend.json"
    g = guard(clock, state_path=path)
    r = g.reserve(0.3)
    clock.t += 86_400
    assert json.loads(path.read_text())["outstanding"]["1"]["day"] == TODAY
    status = g.status("s", live_enabled=True, api_key_present=True)
    assert status.spent_today_usd == 0 and status.reserved_usd == pytest.approx(0.3)  # still counted
    g.settle(r, 0.1)
    ledger = json.loads(path.read_text())
    assert ledger["day"] > TODAY and ledger["spent_usd"] == pytest.approx(0.1) and ledger["outstanding"] == {}


class _Crash(BaseException):
    """The process stops here: nothing after this point runs, and nothing is written."""


@pytest.mark.parametrize("settle", ["retain", "settle"])
@pytest.mark.parametrize("writes", [0, 1, 2], ids=["before_rollover_write", "after_rollover_write", "after_all"])
def test_a_crash_at_any_step_of_the_midnight_rollover_loses_no_open_reservation(tmp_path, monkeypatch,
                                                                               settle, writes) -> None:
    """US$0.25 retained and US$0.40 in flight at 23:59:59; the call resolves two seconds later. The first
    settlement after midnight writes the ledger twice (the new day, then the settlement). The process stops
    after 0, 1 or 2 of those writes; a restart must still count the in-flight US$0.40 (or its settled cost)
    and keep yesterday's US$0.25 as a dated record."""
    from coldline.agent import budget as budget_module

    day_start = datetime(2027, 1, 14, tzinfo=UTC).timestamp()
    clock = Clock(day_start + 86_399)  # 23:59:59 UTC
    path = tmp_path / "agent_spend.json"
    g = guard(clock, state_path=path)
    g.retain(g.reserve(0.25))
    r = g.reserve(0.40)
    clock.t += 2  # 00:00:01 the next day
    real_replace, done = budget_module.os.replace, []

    def replace(src, dst):
        if len(done) >= writes:
            raise _Crash
        real_replace(src, dst)
        done.append(dst)

    monkeypatch.setattr(budget_module.os, "replace", replace)
    try:
        g.retain(r) if settle == "retain" else g.settle(r, 0.10)
    except _Crash:
        pass
    monkeypatch.undo()
    g.close()  # the process is gone; a new one starts on the same data directory
    status = guard(clock, state_path=path).status("s", live_enabled=True, api_key_present=True)
    assert status.ledger_ok and status.carried_unreconciled == {"2027-01-14": pytest.approx(0.25)}
    if writes < 2 or settle == "retain":  # still open (counted as unreconciled) or retained in full
        assert status.spent_today_usd == pytest.approx(0.40) and status.unreconciled_usd == pytest.approx(0.40)
    else:
        assert status.spent_today_usd == pytest.approx(0.10) and status.unreconciled_usd == 0
    assert status.reserved_usd == 0


def test_a_settlement_above_its_reservation_switches_live_runs_off() -> None:
    g = guard(Clock(), daily_cap_usd=3.0, min_call_reserve_usd=0.0)
    g.settle(g.reserve(0.10), 0.25)
    assert "more than its reserved bound" in g.admit("s")
    with pytest.raises(BudgetExceeded):
        g.reserve(0.01)


async def test_unknown_spend_is_shown_as_unreconciled_not_as_a_confirmed_zero() -> None:
    from agent_helpers import plant, settings, turn

    from coldline.agent.model import ScriptedModelClient
    from coldline.agent.service import AgentService
    from coldline.contracts import Scenario
    from coldline.web.context import impact

    budget = BudgetGuard(daily_cap_usd=3.0, session_runs_per_window=5, max_concurrent=2, min_call_reserve_usd=0.0)
    turns = [turn("", error="model call timed out", error_billing_unknown=True)]
    svc = AgentService(settings(), budget=budget, live_client_factory=lambda _: ScriptedModelClient(turns))
    p = plant(Scenario.DOOR_OPEN)
    lost = await svc.run_to_completion("s", p, mode="live")
    assert lost.extra["live_calls"] == 1 and lost.extra["unreconciled_usd"] > 0
    # A live request refused before any call (kill switch) ran as a replay and is not a live run.
    off = AgentService(settings(live_enabled=False), budget=budget)
    refused = await off.run_to_completion("s", plant(Scenario.DOOR_OPEN), mode="live")
    assert refused.extra["live_requested"] and "live_calls" not in refused.extra
    kpi = next(k for k in impact(p, svc, "s")["kpis"] if k["label"] == "Live model spend")
    assert kpi["value"] == "$0.000" and kpi["unit"] == f"+ ${lost.extra['unreconciled_usd']:.3f} unreconciled"
    assert "1 model call(s) in 1 live run(s)" in kpi["note"] and "unreconciled" in kpi["note"]
    kpi_off = next(k for k in impact(p, off, "s")["kpis"] if k["label"] == "Live model spend")
    assert kpi_off["value"] == "$0" and "1 live request(s) ran as replay before any model call" in kpi_off["note"]


def _declined_then_answered(category: str | None, declined_output: int = 0, with_block: bool = True):
    """The review's case: a primary that declined before any output (1,000 input tokens reported) and a
    fallback that answered for US$0.003. The ``fallback`` block names the decline's category."""
    declined = SimpleNamespace(type="message", model="claude-sonnet-5-5", input_tokens=1000,
                               output_tokens=declined_output, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    answered = SimpleNamespace(type="fallback_message", model="claude-sonnet-5", input_tokens=1000, output_tokens=100,
                               cache_creation_input_tokens=0, cache_read_input_tokens=0)
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, iterations=[declined, answered])
    block = SimpleNamespace(type="fallback", trigger=SimpleNamespace(type="refusal", category=category))
    tool = SimpleNamespace(type="tool_use", id="toolu_1", name="list_active_alarms", input={})
    return SimpleNamespace(content=[block, tool] if with_block else [tool], usage=usage, model="claude-sonnet-5",
                           stop_reason="tool_use", stop_details=None)


PRIMARY = 1000 * 2.00 / 1e6  # the declined primary's reported usage, priced: US$0.002
FALLBACK = (1000 * 2.00 + 100 * 10.00) / 1e6  # the answering fallback: US$0.003
CHAIN = ("claude-sonnet-5-5", "claude-sonnet-5")


@pytest.mark.parametrize(("category", "output", "block", "measured", "estimated"), [
    ("cyber", 0, True, FALLBACK, PRIMARY),  # declined before output, category not billed: an estimate
    (None, 0, True, FALLBACK, PRIMARY),  # null category: not billed either
    ("cyber", 0, False, FALLBACK, PRIMARY),  # category unknown: never assumed billed or free
    ("bio", 0, True, PRIMARY + FALLBACK, 0.0),  # a billed category: measured
    ("cyber", 50, True, PRIMARY + 50 * 10.00 / 1e6 + FALLBACK, 0.0),  # declined mid-output: billed, measured
], ids=["unbilled_category", "null_category", "unknown_category", "billed_category", "mid_output"])
def test_only_usage_the_billing_rules_establish_is_measured(category, output, block, measured, estimated) -> None:
    from coldline.agent.model import billed_usage
    from coldline.agent.pricing import split_cost

    response = _declined_then_answered(category, output, block)
    usage = billed_usage(response.usage, response)
    cost = split_cost("claude-sonnet-5", usage, CHAIN)
    assert cost.measured == pytest.approx(measured) and cost.estimated == pytest.approx(estimated)
    assert cost_usd("claude-sonnet-5", usage, CHAIN) == pytest.approx(measured + estimated)  # what the cap counts
    assert Usage.from_dict(usage.as_dict()) == usage  # a recorded turn keeps the decline and its category


def test_a_single_declined_attempt_without_iterations_is_an_estimate() -> None:
    from coldline.agent.model import billed_usage
    from coldline.agent.pricing import split_cost

    usage = SimpleNamespace(input_tokens=1000, output_tokens=0, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, iterations=None)
    response = SimpleNamespace(content=[], usage=usage, model="claude-sonnet-5-5", stop_reason="refusal",
                               stop_details=SimpleNamespace(type="refusal", category="cyber"))
    cost = split_cost("claude-sonnet-5-5", billed_usage(usage, response), CHAIN)
    assert (cost.measured, cost.estimated) == (0.0, pytest.approx(PRIMARY))


async def test_an_unbilled_decline_is_reported_apart_from_measured_spend() -> None:
    """End to end: the trace step and the Impact page show US$0.003 measured and US$0.002 estimated (it was
    US$0.005 measured); the daily cap still counts both."""
    from agent_helpers import plant, settings

    from coldline.agent.model import AnthropicModelClient
    from coldline.agent.service import AgentService
    from coldline.contracts import Scenario
    from coldline.web.context import impact

    class FakeMessages:
        calls = 0

        async def create(self, **kwargs):
            FakeMessages.calls += 1
            if FakeMessages.calls == 1:
                return _declined_then_answered("cyber")
            usage = SimpleNamespace(input_tokens=0, output_tokens=0, cache_creation_input_tokens=0,
                                    cache_read_input_tokens=0, iterations=None)
            return SimpleNamespace(content=[], usage=usage, model="claude-sonnet-5", stop_reason="end_turn",
                                   stop_details=None)

    fake = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()), messages=None)
    budget = BudgetGuard(daily_cap_usd=5.0, session_runs_per_window=5, max_concurrent=2, min_call_reserve_usd=0.0)
    p = plant(Scenario.PROBE_MISMATCH)
    svc = AgentService(settings(), budget=budget,
                       live_client_factory=lambda st: AnthropicModelClient(st, client=fake))  # type: ignore[arg-type]
    run = await svc.run_to_completion("s", p, mode="live")
    think = next(st for st in run.steps if st.kind.value == "think")
    assert think.cost_usd == pytest.approx(FALLBACK)
    assert "declined before any output (cyber)" in think.result_preview and "estimated" in think.result_preview
    assert run.extra["estimated_usd"] == pytest.approx(PRIMARY)
    assert run.extra["live_cost_usd"] == pytest.approx(FALLBACK)
    assert budget.status("s", live_enabled=True, api_key_present=True).spent_today_usd == pytest.approx(
        PRIMARY + FALLBACK, abs=1e-6)
    assert run.status.value != "failed", run.extra
    spend = next(k for k in impact(p, svc, "s")["kpis"] if k["label"] == "Live model spend")
    assert spend["value"] == "$0.003" and spend["unit"] == "+ $0.002 estimated", spend
    assert "estimated, not measured" in spend["note"]
