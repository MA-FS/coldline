"""Cost and abuse control: rate limit, concurrency, reserved daily cap, persistence, pricing."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from coldline.agent import AgentService, AgentSettings
from coldline.agent.budget import BudgetExceeded, BudgetGuard
from coldline.agent.pricing import CONSERVATIVE, Usage, cost_usd, price_for, worst_case_cost_usd
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
    # A restart must not start a fresh (under-counting) ledger while the damaged one awaits review.
    again = guard(Clock(), state_path=path)
    assert not again.status("s", live_enabled=True, api_key_present=True).live_available
    assert "waiting for an operator" in again.admit("s") and not path.exists()
    # Once the operator has reviewed and removed it, paid mode can resume.
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
    assert json.loads(path.read_text())["version"] == 2
    path.write_text(json.dumps(VALID | {"day": "2027-01-14"}))  # yesterday's figures roll over
    status = guard(Clock(), state_path=path).status("s", live_enabled=True, api_key_present=True)
    assert status.ledger_ok and status.spent_today_usd == 0


# --------------------------------------------------------------------------- an open fallback set (provider-chosen model)


def test_a_reply_from_an_unlisted_fallback_model_settles_within_its_reservation() -> None:
    from coldline.agent.loop import input_token_bound
    from coldline.agent.pricing import PRICES, permitted_models, reservation_usd

    bound = input_token_bound([], [{"role": "user", "content": "x" * 20_000}])
    reserved = reservation_usd(permitted_models("claude-sonnet-5-5", server_fallback=True), bound, 3000)
    unlisted = "claude-some-future-model"
    assert unlisted not in PRICES and price_for(unlisted) == CONSERVATIVE
    worst = cost_usd(unlisted, Usage(cache_creation_input_tokens=bound, output_tokens=3000))
    assert worst <= reserved + 1e-9
    # Near the cap: the reservation fits, and settling the unlisted model's real cost keeps spend under it.
    g = guard(Clock(), daily_cap_usd=reserved + 0.01, min_call_reserve_usd=0.0)
    g.settle(g.reserve(reserved), worst)
    status = g.status("s", live_enabled=True, api_key_present=True)
    assert status.spent_today_usd <= reserved + 0.01 and status.live_available


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
