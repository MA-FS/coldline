"""Record replay traces by running the real model against a fresh sandbox.

    uv run python -m evals.record                    # all scenarios
    uv run python -m evals.record probe_mismatch     # one
    uv run python -m evals.record --force door_open  # save even if a scenario check fails
    uv run python -m evals.record --authored         # rebuild the hand-authored recordings (no API calls)

Live recording needs ANTHROPIC_API_KEY. Each recording is checked against its scenario eval before it is saved, so a
recording that the deterministic suite would fail never ships. Ids of records the run created (holds,
work orders, permits, approvals) are replaced with placeholders so the trace replays in any sandbox.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from coldline.agent.config import DEFAULT_MODEL
from coldline.agent.model import ScriptedTurn
from coldline.agent.prompts import prompt_version
from coldline.agent.replay import RECORDINGS_DIR, save_recording, to_placeholders
from coldline.agent.service import AgentService
from coldline.agent.tools import ToolRegistry
from coldline.contracts import RunTrace
from coldline.systems import create_plant
from evals.authored import AUTHORED
from evals.cases import CASE_BY_ID, RECORDINGS, EvalContext, RecordingSpec, stayed_live
from evals.runner import ScriptLibrary, live_settings, scripted_settings


def build_recording(run: RunTrace, spec: RecordingSpec, recorded: str,
                    turns: list[dict] | None = None) -> RunTrace:
    trace = run.model_copy(deep=True)
    created = run.extra.get("created", {})
    flags = run.extra.get("injection_flags", [])
    if turns is None:
        turns = to_placeholders(run.extra.get("model_turns", []), created, flags)
    trace.session_id = "recording"
    trace.approvals = []  # approvals are recreated, with fresh ids and versions, on every replay
    trace.extra = {
        "recording_id": spec.id,
        "recorded": recorded,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "description": spec.description,
        "requires_stale": spec.stale,
        "prompt_version": run.extra.get("prompt_version") or prompt_version(ToolRegistry().definitions()),
        "model_turns": turns,
        "evidence": run.extra.get("evidence", []),
        "gate": run.extra.get("gate", {}),
    }
    return trace


async def record(spec: RecordingSpec, force: bool, authored: list[ScriptedTurn] | None = None) -> bool:
    plant = create_plant()
    spec.setup(plant)
    lots_before = {lot.id: lot for lot in plant.list_lots()}
    if authored is None:
        service = AgentService(live_settings())
        run = await service.run_to_completion(f"record-{spec.id}", plant, scenario=spec.scenario, mode="live")
    else:
        library = ScriptLibrary(spec.id, spec.scenario, authored, model=DEFAULT_MODEL, recorded="hand-authored")
        service = AgentService(scripted_settings(), recordings=library)  # type: ignore[arg-type]
        run = await service.run_to_completion(f"record-{spec.id}", plant, scenario=spec.scenario)
    ctx = EvalContext(plant=plant, run=run, service=service, session_id=f"record-{spec.id}",
                      lots_before=lots_before)
    failures = []
    for check in [*([stayed_live()] if authored is None else []), *CASE_BY_ID[spec.id].checks]:
        passed, detail = check.fn(ctx)
        if not passed:
            failures.append(f"{check.name}: {detail}")
    print(f"{spec.id}: {run.conclusion} [{run.confidence}] US${run.total_cost_usd:.4f} {run.duration_ms} ms")
    for f in failures:
        print(f"   x {f}")
    if failures and not force:
        print("   not saved")
        return False
    path = RECORDINGS_DIR / f"{spec.id}.json"
    if authored is None:
        trace = build_recording(run, spec, "live")
    else:
        trace = build_recording(run, spec, "hand-authored", [t.model_dump() for t in authored])
    save_recording(trace, path)
    print(f"   saved {path.relative_to(RECORDINGS_DIR.parents[3])}")
    return True


async def main_async(ids: list[str], force: bool, authored: bool) -> int:
    if authored:
        specs = [s for s in RECORDINGS if s.id in AUTHORED and (not ids or s.id in ids)]
        ok = [await record(s, force, AUTHORED[s.id]) for s in specs]
    else:
        specs = [s for s in RECORDINGS if s.id not in AUTHORED and (not ids or s.id in ids)]
        ok = [await record(s, force) for s in specs]
    return 0 if all(ok) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", help="recording ids (default: all)")
    parser.add_argument("--force", action="store_true", help="save even if scenario checks fail")
    parser.add_argument("--authored", action="store_true", help="rebuild hand-authored recordings only")
    args = parser.parse_args()
    return asyncio.run(main_async(args.ids, args.force, args.authored))


if __name__ == "__main__":
    raise SystemExit(main())
