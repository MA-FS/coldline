"""Handling of untrusted text (supplier emails, free-text ticket notes).

Two layers, neither of which is the real control (the real control is that tiers are enforced in code,
so no text can unlock an action):

1. **Delimiting.** Untrusted text reaches the model only inside ``<untrusted_data>`` tags, with any copy
   of the delimiter inside the text neutralised so a message cannot "close" the block early.
2. **Screening.** A cheap, deterministic pattern screen flags text that tries to instruct the agent. A
   flag taints the run: from then on, proposals to release QA holds are refused outright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PATTERNS: list[tuple[str, str]] = [
    ("override instructions", r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instruction|rule|prompt|polic)"),
    ("release holds", r"\brelease\b.{0,30}\b(all|every|the)?\s*(qa\s*)?holds?\b"),
    ("approve on behalf", r"\bapprove\b.{0,30}\b(dispatch|all|pending|proposal|change)"),
    ("role change", r"\b(you are now|act as|new instructions|system (notice|override|message))\b"),
    ("prompt extraction", r"\b(reveal|print|show)\b.{0,30}\b(system prompt|instructions)\b"),
    ("secrecy request", r"\b(do not|don't)\s+(tell|mention|report|log)\b"),
    ("bypass controls", r"\b(bypass|skip|disable)\b.{0,30}\b(approval|guardrail|check|hold|interlock)"),
]
_COMPILED = [(name, re.compile(rx, re.IGNORECASE | re.DOTALL)) for name, rx in _PATTERNS]
_DELIMITER = re.compile(r"</?\s*untrusted_data[^>]*>", re.IGNORECASE)


@dataclass
class ScreenResult:
    suspected: bool
    reasons: list[str] = field(default_factory=list)


def screen_text(text: str) -> ScreenResult:
    reasons = [name for name, rx in _COMPILED if rx.search(text or "")]
    return ScreenResult(suspected=bool(reasons), reasons=reasons)


def wrap_untrusted(text: str, *, source: str) -> str:
    """Return text inside a labelled block the model is told never to obey."""
    safe = _DELIMITER.sub("[delimiter removed]", text or "")
    safe_source = re.sub(r"[^A-Za-z0-9_.:-]", "", source)[:40]
    return f'<untrusted_data source="{safe_source}">\n{safe}\n</untrusted_data>'
