"""Replay mode: recorded model decisions, executed against the visitor's own sandbox.

A recording is a :class:`RunTrace` whose ``extra["model_turns"]`` holds the model's side of a run: its
progress notes and the tool calls it made, with token counts and latency. Replay feeds those turns to the
normal loop through :class:`ScriptedModelClient`. Everything else is live: each tool call goes through
the same registry, tier checks, evidence gate and approval store, against this visitor's plant. So the QA
hold really appears, the proposals really wait for approval, and a stale probe really changes the outcome.

Records the agent creates during a run (holds, work orders, permits, approvals) get new ids in every
sandbox, so recordings refer to them by placeholder, e.g. ``{{hold:L-2231}}`` or
``{{approval:change_dispatch:L-2231}}``. ``{{flagged}}`` is the first record flagged as a suspected injection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coldline.agent.context import RunContext
from coldline.agent.model import ScriptedTurn
from coldline.contracts import PlantSystems, Quality, RunTrace, Scenario
from coldline.sim import analysis

RECORDINGS_DIR = Path(__file__).parent / "recordings"
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+(?::[A-Za-z0-9_-]+)*)\}\}")


def resolve_placeholders(obj: Any, ctx: RunContext) -> Any:
    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "flagged":
            return ctx.injection_flags[0] if ctx.injection_flags else match.group(0)
        return ctx.created.get(key, match.group(0))  # unresolved stays visible and fails citation checks

    if isinstance(obj, str):
        return _PLACEHOLDER.sub(sub, obj)
    if isinstance(obj, list):
        return [resolve_placeholders(v, ctx) for v in obj]
    if isinstance(obj, dict):
        return {k: resolve_placeholders(v, ctx) for k, v in obj.items()}
    return obj


def to_placeholders(obj: Any, created: dict[str, str], flagged: list[str]) -> Any:
    """Inverse of :func:`resolve_placeholders`, used when saving a live run as a recording."""
    pairs = [(rid, key) for key, rid in created.items()] + [(rid, "flagged") for rid in flagged[:1]]

    def convert(text: str) -> str:
        for rid, key in pairs:
            text = re.sub(rf"(?<![A-Za-z0-9-]){re.escape(rid)}(?![A-Za-z0-9-])", "{{" + key + "}}", text)
        return text

    if isinstance(obj, str):
        return convert(obj)
    if isinstance(obj, list):
        return [to_placeholders(v, created, flagged) for v in obj]
    if isinstance(obj, dict):
        return {k: to_placeholders(v, created, flagged) for k, v in obj.items()}
    return obj


@dataclass
class Recording:
    id: str
    trace: RunTrace
    turns: list[ScriptedTurn]
    scenario: Scenario | None
    requires_stale: list[str]  # choose this recording when any of these tags is not fresh
    recorded: str  # "hand-authored" | "live"
    model: str
    description: str = ""

    @classmethod
    def from_trace(cls, trace: RunTrace) -> Recording:
        extra = trace.extra
        return cls(
            id=extra.get("recording_id", trace.run_id),
            trace=trace,
            turns=[ScriptedTurn.model_validate(t) for t in extra.get("model_turns", [])],
            scenario=trace.scenario,
            requires_stale=list(extra.get("requires_stale", [])),
            recorded=extra.get("recorded", "live"),
            model=trace.model,
            description=extra.get("description", ""),
        )


class RecordingLibrary:
    def __init__(self, directory: Path = RECORDINGS_DIR) -> None:
        self.directory = directory
        self.items: dict[str, Recording] = {}
        for path in sorted(directory.glob("*.json")):
            trace = RunTrace.model_validate_json(path.read_text())
            if trace.extra.get("model_turns"):
                rec = Recording.from_trace(trace)
                self.items[rec.id] = rec

    def get(self, recording_id: str) -> Recording | None:
        return self.items.get(recording_id)

    def select(self, scenario: Scenario | None, systems: PlantSystems) -> Recording | None:
        """Pick the recording that matches the scenario and the current probe state."""
        candidates = sorted((r for r in self.items.values() if r.scenario == scenario),
                            key=lambda r: -len(r.requires_stale))
        now = systems.now()
        for rec in candidates:
            if not rec.requires_stale:
                return rec
            readings = systems.latest(rec.requires_stale)
            if any(analysis.freshness(r, now) != Quality.GOOD for r in readings.values()):
                return rec
        return None


def save_recording(trace: RunTrace, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(trace.model_dump_json(indent=2) + "\n")
