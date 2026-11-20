"""Record replay traces by running the real model against a fresh sandbox.

    uv run python -m evals.record                    # all scenarios
    uv run python -m evals.record probe_mismatch     # one
    uv run python -m evals.record --force door_open  # save even if a scenario check fails
    uv run python -m evals.record --authored         # rebuild the hand-authored recordings (no API calls)
    uv run python -m evals.record --refresh-facts    # recompute stored handover facts (no API calls)

Live recording needs ANTHROPIC_API_KEY. Each recording is checked against its scenario eval before it is saved, so a
recording that the deterministic suite would fail never ships. Ids of records the run created (holds,
work orders, permits, approvals) and of the alarms it saw are replaced with placeholders so the trace
replays in any sandbox. Each recording also stores ``handover_facts``: what the recorded run observed and
did, which a replay compares against before reusing the recorded wording. ``--refresh-facts`` recomputes
them by replaying each recording against a fresh sandbox (the plant it was recorded on).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from coldline.agent.config import DEFAULT_MODEL
from coldline.agent.model import ScriptedTurn
from coldline.agent.prompts import prompt_version
from coldline.agent.replay import RECORDINGS_DIR, RecordingLibrary, save_recording, to_placeholders
from coldline.agent.service import AgentService
from coldline.agent.tools import ToolRegistry
from coldline.contracts import RunTrace
from coldline.systems import create_plant
from evals.authored import AUTHORED
from evals.cases import CASE_BY_ID, RECORDINGS, EvalContext, RecordingSpec, stayed_live
from evals.runner import ScriptLibrary, live_settings, scripted_settings


def build_recording(run: RunTrace, spec: RecordingSpec, recorded: str,
                    turns: list[dict] | None = None, alarms: dict[str, str] | None = None) -> RunTrace:
    trace = run.model_copy(deep=True)
    created = run.extra.get("created", {})
    flags = run.extra.get("injection_flags", [])
    if turns is None:
        turns = to_placeholders(run.extra.get("model_turns", []), created, flags, alarms)
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
        "handover_facts": run.extra.get("handover_facts"),
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
    alarms = {a.id: a.asset_id for a in plant.list_alarms(active_only=False)}
    if authored is None:
        trace = build_recording(run, spec, "live", alarms=alarms)
    else:
        trace = build_recording(run, spec, "hand-authored", [t.model_dump() for t in authored])
    save_recording(trace, path)
    print(f"   saved {path.relative_to(RECORDINGS_DIR.parents[3])}")
    return True


async def baseline_facts(spec: RecordingSpec) -> dict | None:
    """Replay a stored recording against a fresh sandbox and return its handover facts."""
    plant = create_plant()
    spec.setup(plant)
    service = AgentService(scripted_settings())
    run = await service.run_to_completion(f"facts-{spec.id}", plant, scenario=spec.scenario)
    return run.extra.get("handover_facts")


async def refresh_facts(ids: list[str]) -> int:
    library = RecordingLibrary()
    for spec in RECORDINGS:
        if ids and spec.id not in ids or spec.id not in library.items:
            continue
        facts = await baseline_facts(spec)
        if facts is None:
            print(f"{spec.id}: no accepted handover on a fresh replay; not updated")
            return 1
        trace = library.items[spec.id].trace
        trace.extra["handover_facts"] = facts
        save_recording(trace, RECORDINGS_DIR / f"{spec.id}.json")
        print(f"{spec.id}: handover facts refreshed")
    return 0


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
    parser.add_argument("--refresh-facts", action="store_true",
                        help="recompute stored handover facts by replaying against a fresh sandbox")
    args = parser.parse_args()
    if args.refresh_facts:
        return asyncio.run(refresh_facts(args.ids))
    return asyncio.run(main_async(args.ids, args.force, args.authored))


if __name__ == "__main__":
    raise SystemExit(main())
