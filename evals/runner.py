"""Run eval cases against a fresh sandbox each and collect JSON-ready results."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coldline.agent.config import AgentSettings
from coldline.agent.model import ScriptedTurn
from coldline.agent.replay import Recording, RecordingLibrary
from coldline.agent.service import EVAL_RESULTS_PATH, AgentService
from coldline.contracts import PlantSystems, RunTrace, Scenario, StepKind
from coldline.systems import create_plant
from evals.cases import CASES, EvalCase, EvalContext, stayed_live


class ScriptLibrary:
    """Serves one fixed list of model turns to the service in place of the recordings."""

    def __init__(self, case_id: str, scenario: Scenario, turns: list[ScriptedTurn], model: str = "scripted",
                 recorded: str = "scripted") -> None:
        trace = RunTrace(run_id=case_id, session_id="eval", scenario=scenario, model=model,
                         started_at=datetime(2026, 1, 1, tzinfo=UTC))
        self._recording = Recording(id=case_id, trace=trace, turns=turns, scenario=scenario,
                                    requires_stale=[], recorded=recorded, model=model)

    def select(self, scenario: Scenario | None, systems: PlantSystems) -> Recording:
        return self._recording


def scripted_settings() -> AgentSettings:
    return AgentSettings(replay_speed=0.0, live_enabled=False, api_key_present=False)


def live_settings() -> AgentSettings:
    return replace(AgentSettings.from_env(), replay_speed=0.0, session_runs_per_hour=1000, max_concurrent_live=1)


async def run_case(case: EvalCase, mode: str = "scripted") -> dict[str, Any]:
    plant = create_plant()
    case.setup(plant)
    lots_before = {lot.id: lot for lot in plant.list_lots()}
    session_id = f"eval-{case.id}"
    if mode == "live":
        service = AgentService(live_settings())
        run = await service.run_to_completion(session_id, plant, scenario=case.scenario, mode="live")
    else:
        library = ScriptLibrary(case.id, case.scenario, case.script) if case.script else RecordingLibrary()
        service = AgentService(scripted_settings(), recordings=library)  # type: ignore[arg-type]
        run = await service.run_to_completion(session_id, plant, scenario=case.scenario, mode="replay")
    ctx = EvalContext(plant=plant, run=run, service=service, session_id=session_id, lots_before=lots_before)
    checks = list(case.checks) + ([stayed_live()] if mode == "live" else [])
    assertions = []
    try:
        if case.post:
            case.post(ctx)
    except Exception as exc:  # noqa: BLE001 - a broken post-step is a failed case, not a crashed suite
        assertions.append({"name": "post-run actions", "passed": False, "detail": f"{type(exc).__name__}: {exc}"})
    for check in checks:
        try:
            passed, detail = check.fn(ctx)
        except Exception as exc:  # noqa: BLE001
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        assertions.append({"name": check.name, "passed": bool(passed), "detail": detail})
    recording = run.extra.get("recording", {})
    return {
        "id": case.id,
        "title": case.title,
        "category": case.category,
        "scenario": case.scenario.value,
        "mode": mode,
        "passed": all(a["passed"] for a in assertions),
        "assertions": assertions,
        "conclusion": run.conclusion,
        "confidence": run.confidence,
        "model": run.model,
        "model_calls": sum(1 for s in run.steps if s.kind == StepKind.THINK),
        "steps": len(run.steps),
        "cost_usd": run.total_cost_usd,
        "latency_ms": run.duration_ms,
        "cost_basis": "live" if mode == "live" else ("recorded" if recording.get("recorded") != "scripted"
                                                      else "scripted model, no tokens"),
        "run_id": run.run_id,
    }


async def run_suite(mode: str = "scripted", case_ids: list[str] | None = None) -> dict[str, Any]:
    cases = [c for c in CASES if (case_ids is None or c.id in case_ids) and (mode == "scripted" or c.live)]
    results = [await run_case(c, mode) for c in cases]
    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        cat = by_category.setdefault(r["category"], {"passed": 0, "total": 0})
        cat["total"] += 1
        cat["passed"] += int(r["passed"])
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": mode,
        "summary": {
            "passed": sum(r["passed"] for r in results),
            "total": len(results),
            "by_category": by_category,
            # Money actually spent by this eval run (live mode only). Scripted runs replay recorded
            # model turns, so their per-case cost_usd is the recorded cost of the original run.
            "spent_usd": round(sum(r["cost_usd"] for r in results), 4) if mode == "live" else 0.0,
            "recorded_cost_usd": round(sum(r["cost_usd"] for r in results if r["cost_basis"] == "recorded"), 4),
        },
        "cases": results,
    }


def write_results(results: dict[str, Any], path: Path = EVAL_RESULTS_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2) + "\n")
    return path
