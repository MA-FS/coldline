"""The service: replay as the default, live with fallbacks, streaming, approvals through the facade."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from agent_helpers import SUPERVISOR, plant, settings, turn

from coldline.agent.budget import BudgetGuard
from coldline.agent.config import AgentSettings
from coldline.agent.model import AnthropicModelClient, ScriptedModelClient
from coldline.agent.replay import RecordingLibrary
from coldline.agent.service import AgentService, load_eval_results, public_run_json
from coldline.contracts import ApprovalStatus, LotStatus, RunStatus, Scenario, StepKind

LIBRARY = RecordingLibrary()


def replay_service(**kw) -> AgentService:
    return AgentService(settings(live_enabled=False, api_key_present=False, **kw))


def live_service(turns, **kw) -> AgentService:
    return AgentService(settings(**kw), live_client_factory=lambda s: ScriptedModelClient(turns))


# --------------------------------------------------------------------------- replay


async def test_replay_stream_shape_and_side_effects() -> None:
    svc, p = replay_service(), plant(Scenario.PROBE_MISMATCH)
    run = await svc.run_to_completion("s1", p)
    kinds = [s.kind for s in run.steps]
    assert run.mode == "replay" and run.extra["recording"]["id"] == "probe_mismatch"
    assert kinds[0] == StepKind.THINK and kinds[-1] == StepKind.CONCLUSION
    assert [s.n for s in run.steps] == list(range(1, len(run.steps) + 1))
    assert run.total_cost_usd > 0 and run.total_tokens > 0 and run.duration_ms > 0
    assert p.get_lot("L-2231").status == LotStatus.HOLD  # the hold really lands in this sandbox
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert {a.action for a in run.approvals} == {"amend_work_order", "change_dispatch"}
    assert "{{" not in run.handover_note and run.confidence == "unconfirmed"


async def test_replayed_approval_really_executes() -> None:
    svc, p = replay_service(), plant(Scenario.PROBE_MISMATCH)
    run = await svc.run_to_completion("s1", p)
    dispatch = next(a for a in run.approvals if a.action == "change_dispatch")
    result = svc.approve("s1", dispatch.id, SUPERVISOR, p)
    assert result.executed and p.get_lot("L-2231").dispatch_due.isoformat() == dispatch.payload["new_due"]
    amend = next(a for a in run.approvals if a.action == "amend_work_order")
    svc.decline("s1", amend.id, SUPERVISOR, p)
    assert run.status == RunStatus.DONE
    statuses = {a.action: a.status for a in svc.list_approvals("s1")}
    assert statuses == {"change_dispatch": ApprovalStatus.APPROVED, "amend_work_order": ApprovalStatus.DECLINED}


async def test_stale_probe_selects_the_stale_recording_and_expires_old_approvals() -> None:
    svc, p = replay_service(), plant(Scenario.PROBE_MISMATCH)
    hero = await svc.run_to_completion("s1", p)
    p.make_stale("TT-402")
    stale = await svc.run_to_completion("s1", p)
    assert stale.extra["recording"]["id"] == "probe_mismatch_stale"
    assert stale.confidence == "insufficient_data"
    assert not [a for a in stale.approvals if a.action == "change_dispatch"]
    old = next(a for a in hero.approvals if a.action == "change_dispatch")
    assert svc.approve("s1", old.id, SUPERVISOR, p).approval.status == ApprovalStatus.EXPIRED
    assert [r.run_id for r in svc.list_runs("s1")] == [stale.run_id, hero.run_id]


@pytest.mark.parametrize("rec_id", sorted(LIBRARY.items))
async def test_every_recording_replays_cleanly(rec_id: str) -> None:
    from evals.cases import SPECS

    p = plant()
    SPECS[rec_id].setup(p)
    run = await replay_service().run_to_completion("s", p, scenario=SPECS[rec_id].scenario)
    assert run.extra["recording"]["id"] == rec_id
    assert run.handover_note and "{{" not in json.dumps([s.model_dump(mode="json") for s in run.steps])
    assert not [s for s in run.steps if s.kind == StepKind.ERROR]


def test_recordings_carry_provenance_and_no_stale_ids() -> None:
    assert set(LIBRARY.items) == {"probe_mismatch", "probe_mismatch_stale", "compressor_trip", "door_open",
                                  "poisoned_email"}
    for rec in LIBRARY.items.values():
        assert rec.recorded in {"live", "hand-authored"} and rec.trace.extra["prompt_version"]
        turns = json.dumps([t.model_dump() for t in rec.turns])
        assert not re.search(r"\b(QH|AP|PD|MSG)-\d{4}\b", turns), rec.id  # created ids are placeholders


async def test_no_recording_for_unknown_state() -> None:
    run = await replay_service().run_to_completion("s", plant())
    assert run.status == RunStatus.FAILED and run.steps[-1].kind == StepKind.ERROR


async def test_stream_and_sse_events_follow_a_running_replay() -> None:
    svc, p = AgentService(settings(live_enabled=False, replay_speed=0.02)), plant(Scenario.DOOR_OPEN)
    run = await svc.start_run("s1", p)
    assert run.status == RunStatus.RUNNING
    assert (await svc.start_run("s1", p)).run_id == run.run_id  # one run per session at a time
    streamed = [s.n async for s in svc.stream("s1", run.run_id)]
    events = [e async for e in svc.stream_events("s1", run.run_id)]
    assert streamed == [s.n for s in run.steps]
    assert [e["event"] for e in events][-1] == "done" and len(events) == len(run.steps) + 1
    assert "model_turns" not in json.loads(events[-1]["data"])["extra"]
    assert "model_turns" not in json.loads(public_run_json(run))["extra"]


def test_eval_results_are_packaged() -> None:
    results = load_eval_results()
    assert results["summary"]["total"] >= 10 and results["summary"]["passed"] == results["summary"]["total"]


# --------------------------------------------------------------------------- live and fallback


async def test_kill_switch_falls_back_to_replay_with_notice() -> None:
    svc = AgentService(settings(live_enabled=False))
    run = await svc.run_to_completion("s", plant(Scenario.PROBE_MISMATCH), mode="live")
    assert run.mode == "replay" and run.extra["fallback_reason"] == "Live runs are switched off"
    assert run.steps[0].title == "Fallback: continuing with the recorded run"
    assert run.confidence == "unconfirmed"


async def test_model_error_mid_run_falls_back_to_replay() -> None:
    turns = [turn("Reading probes.", ("get_sensor_trend", {"tags": ["TT-401", "TT-402"]})),
             turn("", error="model API error 529")]
    p = plant(Scenario.PROBE_MISMATCH)
    run = await live_service(turns).run_to_completion("s", p, mode="live")
    titles = [s.title for s in run.steps]
    assert "Model call failed" in titles and "Fallback: continuing with the recorded run" in titles
    assert run.mode == "replay" and run.extra["fallback_reason"] == "model API error 529"
    assert run.handover_note and p.get_lot("L-2231").status == LotStatus.HOLD


async def test_refusal_stop_reason_falls_back() -> None:
    run = await live_service([turn("", stop_reason="refusal")]).run_to_completion(
        "s", plant(Scenario.DOOR_OPEN), mode="live")
    assert "Model declined the request" in [s.title for s in run.steps] and run.mode == "replay"


async def test_live_run_records_turns_costs_and_prompt_version() -> None:
    usage = {"input_tokens": 4, "output_tokens": 300, "cache_creation_input_tokens": 200,
             "cache_read_input_tokens": 4800}
    turns = [
        turn("Checking CH4.", ("get_sensor_trend", {"tags": ["TT-401", "TT-402"]}),
             ("list_lots_in_zone", {"zone_asset_id": "CH4"}), ("find_work_orders", {"asset_id": "CH4"}), usage=usage),
        turn("Recovered transient.", ("write_handover_note", {
            "conclusion": "Transient rise in Chiller 4 - recovered.", "confidence": "confirmed",
            "note_markdown": "[TT-401] and [TT-402] recovered; [L-2231] unaffected; [WO-1042] open."}), usage=usage),
    ]
    run = await live_service(turns).run_to_completion("s", plant(Scenario.DOOR_OPEN), mode="live")
    assert run.mode == "live" and run.status == RunStatus.DONE and run.confidence == "confirmed"
    assert len(run.extra["model_turns"]) == 2 and run.extra["prompt_version"]
    per_call = (4 * 2 + 300 * 10 + 200 * 2.5 + 4800 * 0.1) / 1e6
    assert run.total_cost_usd == pytest.approx(2 * per_call)


async def test_step_limit_bounds_the_loop() -> None:
    looping = [turn("Again.", ("get_lot", {"lot_id": "L-2231"})) for _ in range(10)]
    svc = live_service(looping, max_model_calls=3)
    run = await svc.run_to_completion("s", plant(Scenario.DOOR_OPEN), mode="live")
    assert sum(1 for s in run.steps if s.kind == StepKind.THINK) == 3
    assert "Step limit reached" in [s.title for s in run.steps]
    assert run.mode == "live" and run.steps[-1].kind == StepKind.CONCLUSION


async def test_budget_exhausted_mid_run_falls_back() -> None:
    s = settings()
    budget = BudgetGuard(daily_cap_usd=0.05, session_runs_per_window=5, max_concurrent=2, min_call_reserve_usd=0.0)
    turns = [turn("Reading.", ("get_sensor_trend", {"tags": ["TT-401", "TT-402"]}),
                  usage={"output_tokens": 3000, "cache_creation_input_tokens": 5000})] * 3
    svc = AgentService(s, budget=budget, live_client_factory=lambda _: ScriptedModelClient(turns))
    run = await svc.run_to_completion("s", plant(Scenario.PROBE_MISMATCH), mode="live")
    assert "Budget check: daily cost cap reached" in [st.title for st in run.steps]
    assert run.extra["fallback_reason"] == "daily cost cap reached" and run.mode == "replay"
    assert budget.status("s", live_enabled=True, api_key_present=True).spent_today_usd <= 0.05


async def test_session_rate_limit_falls_back() -> None:
    svc = live_service([], session_runs_per_hour=0)
    run = await svc.run_to_completion("s", plant(Scenario.DOOR_OPEN), mode="live")
    assert "Live-run limit reached" in run.extra["fallback_reason"]


# --------------------------------------------------------------------------- Messages API request shape


class _FakeMessages:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def create(self, **kwargs):
        self.kwargs = kwargs
        content = [SimpleNamespace(type="thinking", thinking="Checking CH4.", signature="sig"),
                   SimpleNamespace(type="tool_use", id="toolu_1", name="get_lot", input={"lot_id": "L-2231"})]
        usage = SimpleNamespace(input_tokens=3, output_tokens=50, cache_creation_input_tokens=0,
                                cache_read_input_tokens=4800)
        return SimpleNamespace(content=content, usage=usage, model="claude-sonnet-5-5", stop_reason="tool_use")


async def test_anthropic_client_request_shape() -> None:
    fake = SimpleNamespace(messages=_FakeMessages(), beta=SimpleNamespace(messages=_FakeMessages()))
    client = AnthropicModelClient(AgentSettings(), client=fake)  # type: ignore[arg-type]
    resp = await client.create(system="SYS", tools=[{"name": "t"}], messages=[{"role": "user", "content": "x"}],
                               max_tokens=3000, timeout_s=10)
    kw = fake.beta.messages.kwargs
    assert kw["model"] == "claude-sonnet-5-5" and kw["max_tokens"] == 3000
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["cache_control"] == {"type": "ephemeral"}
    assert kw["thinking"] == {"type": "between_tools"} and kw["output_config"] == {"effort": "medium"}
    # A named fallback with the same output cap (never "default"): the attempts the budget reserves for are
    # exactly the attempts the request allows.
    assert kw["fallbacks"] == [{"model": "claude-sonnet-5", "max_tokens": 3000}]
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert "tool_choice" not in kw
    assert resp.text == "Checking CH4." and resp.tool_calls[0].name == "get_lot"
    assert resp.raw_content[0].signature == "sig"  # thinking blocks go back unchanged

    plain = AnthropicModelClient(AgentSettings(server_fallback=False, model="claude-opus-5-5",
                                               thinking="adaptive"), client=fake)  # type: ignore[arg-type]
    await plain.create(system="S", tools=[], messages=[], max_tokens=10, timeout_s=1)
    assert fake.messages.kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "fallbacks" not in fake.messages.kwargs


def test_settings_from_env() -> None:
    s = AgentSettings.from_env({"COLDLINE_MODEL": "claude-opus-5-5", "COLDLINE_DAILY_USD_CAP": "1.5",
                                "COLDLINE_LIVE_ENABLED": "0", "ANTHROPIC_API_KEY": "x"})
    assert (s.model, s.thinking, s.daily_usd_cap, s.live_enabled, s.api_key_present) == (
        "claude-opus-5-5", "adaptive", 1.5, False, True)
    assert s.fallbacks == () and not s.config_errors  # no default fallback for a model without one
    default = AgentSettings.from_env({})
    assert default.thinking == "between_tools" and default.fallbacks == ("claude-sonnet-5",)
    assert AgentSettings.from_env({"COLDLINE_SERVER_FALLBACK": "0"}).fallbacks == ()
    named = AgentSettings.from_env({"COLDLINE_FALLBACK_MODELS": "claude-haiku-5-5"})
    assert named.fallbacks == ("claude-haiku-5-5",) and not named.config_errors


@pytest.mark.parametrize("env", [
    {"COLDLINE_DAILY_USD_CAP": "nan"}, {"COLDLINE_DAILY_USD_CAP": "inf"}, {"COLDLINE_DAILY_USD_CAP": "-1"},
    {"COLDLINE_CALL_TIMEOUT_S": "0"}, {"COLDLINE_RUN_TIMEOUT_S": "1e999"}, {"COLDLINE_MAX_TOKENS_PER_CALL": "x"},
    {"COLDLINE_FALLBACK_MODELS": "claude-sonnet-5, claude-haiku-5-5"}, {"COLDLINE_FALLBACK_MODELS": "claude-sonnet-5-5"},
], ids=["nan_cap", "inf_cap", "negative_cap", "zero_timeout", "infinite_timeout", "not_a_number",
        "two_fallbacks", "fallback_is_primary"])
async def test_invalid_paid_mode_settings_fall_back_to_replay(env: dict[str, str]) -> None:
    """A non-finite cap would disable every comparison against it: an invalid paid-mode setting keeps its safe
    default and switches live runs off, with a visible reason; replay still works."""
    s = AgentSettings.from_env({"ANTHROPIC_API_KEY": "x", "COLDLINE_REPLAY_SPEED": "0", **env})
    assert s.config_errors and s.daily_usd_cap == 3.0
    svc = AgentService(s, live_client_factory=lambda _: pytest.fail("no live call may be made"))
    assert not svc.live_status("s").live_available and "invalid" in svc.live_status("s").reason
    run = await svc.run_to_completion("s", plant(Scenario.DOOR_OPEN), mode="live")
    assert run.mode == "replay" and "invalid" in run.extra["fallback_reason"] and run.handover_note


async def test_forget_session_drops_state() -> None:
    svc, p = replay_service(), plant(Scenario.DOOR_OPEN)
    run = await svc.run_to_completion("gone", p)
    svc.forget_session("gone")
    assert svc.get_run("gone", run.run_id) is None and svc.list_runs("gone") == []


async def test_concurrent_sessions_are_isolated() -> None:
    svc = replay_service()
    p1, p2 = plant(Scenario.PROBE_MISMATCH), plant(Scenario.DOOR_OPEN)
    r1, r2 = await asyncio.gather(svc.run_to_completion("a", p1), svc.run_to_completion("b", p2))
    assert p1.get_lot("L-2231").status == LotStatus.HOLD and p2.get_lot("L-2231").status == LotStatus.READY
    assert svc.list_approvals("b") == [] and len(svc.list_approvals("a")) == 2
    assert r1.session_id == "a" and r2.session_id == "b"
