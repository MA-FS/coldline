"""Handover integrity: the note a person reads, copies and acts on must agree with what code verified.

``write_handover_note`` applies three layers to the model's draft:

1. :func:`sanitize` removes sentences that contradict code-verified facts: a diagnosis while the evidence
   is uncertain, release or dispatch advice while a lot in scope is held or the evidence is uncertain or
   shows an excursion, a claim that the agent released or approved something (it cannot), and statements
   about plant state that the run's own results contradict (an empty inbox, no active alarms, a held lot
   "not held", nothing awaiting approval). Removed text is kept in the trace and the run record, never in
   the note.
2. :func:`facts` summarises, without record ids that change between sandboxes, what the run observed and
   did. A replay compares it with the recording's summary. When they differ (say a lot was already on hold
   from an earlier fault, or an email is waiting in the inbox), the recorded wording described a different
   plant, so it is replaced by a note composed in code.
3. :func:`verified_block` is composed in code from the run's own results: status, alarms, evidence checks,
   holds, actions, proposals waiting, follow-ups and untrusted input. It is always appended, so the copied
   note carries the authoritative facts whatever the narrative says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from coldline.agent.approvals import APPROVER_ROLES
from coldline.agent.evidence import CAUSE_CLAIM, UNCERTAIN, Assessment, GateResult, Verdict
from coldline.contracts import ApprovalStatus, InboxMessage, Lot, LotStatus, StepKind, Tier, WorkOrder

if TYPE_CHECKING:
    from coldline.agent.context import RunContext

VERIFIED_HEADING = "**Checks computed in code (not by the model)**"

# --------------------------------------------------------------------------- claims


@dataclass
class Situation:
    """What code knows at handover time, used to decide which claims a sentence may make."""

    uncertain: bool = False  # the worst evidence verdict is uncertain
    excursion: bool = False
    held_lots: set[str] = field(default_factory=set)  # lots the run looked at that are on hold now
    scope_lots: set[str] = field(default_factory=set)  # every lot the run looked at
    inbox_count: int = 0
    active_alarms: int = 0
    pending_approvals: int = 0

    @property
    def product_at_risk(self) -> bool:
        return self.uncertain or self.excursion or bool(self.held_lots)


_NEG = r"(?<!not )(?<!never )(?<!n't )"
_RELEASE_ADVICE = [
    re.compile(r"\b(can|could|may|should)\s+(now\s+)?(ship|be shipped|go out|leave site|be dispatched|dispatch"
               r"|be released|be loaded|load)\b"),
    re.compile(_NEG + r"\b(safe|ok|okay|fine|clear|cleared|good)\s+(to|for)\s+(ship|dispatch|release|load|go)"),
    re.compile(_NEG + r"\bready\s+(to|for)\s+(ship|dispatch|shipping|release|load)"),
    re.compile(r"\b(recommend\w*|should|please|go ahead and|can now|ok to|safe to)\b[^.;]{0,30}"
               r"\b(release|lift|remove|clear)\s+(the\s+|all\s+|its\s+)?(qa\s+)?holds?\b"),
    re.compile(r"\bno\s+(product\s+)?(action|hold|holds|delay)\s+(is\s+)?(needed|required)\b"),
    re.compile(r"\bno\s+need\s+(for|to)\s+(a\s+|the\s+)?(hold|delay|check)"),
    re.compile(r"\bship\s+\[?l-\d{4}"),
    # an imperative: "Ship L-2231 at 06:15.", "Release the hold."
    re.compile(r"^\W*(ship|dispatch|release|load)\s+(the\s+|all\s+)?(\[?l-\d{4}|lots?|loads?|product|it|holds?|qa)\b"),
]
_CLAIMED_HOLD = re.compile(r"\b(held|hold on|holds on)\b[^.;]{0,30}?\[?(l-\d{4})")
_SELF_ACTION = re.compile(r"\b(released|approved|authori[sz]ed)\b")
_NEGATED = re.compile(r"\b(no|not|never|none|nothing|cannot)\b|n't\b")
_INBOX_EMPTY = re.compile(r"\binbox\s+(is|was)\s+(empty|clear)\b|\bno\s+(new\s+)?(inbox\s+)?messages\b|\bempty inbox\b")
_NO_ALARMS = re.compile(r"\bno\s+(active\s+)?alarms?\b")
_NOT_HELD = re.compile(r"\b(l-\d{4})\b[^.;]{0,40}\b(is\s+not|isn't|not)\s+(held|on\s+hold)\b")
_NOTHING_PENDING = re.compile(
    r"\b(awaiting|waiting for|pending)\s+approval\s*:?\**\s*(nothing|none)\b"
    r"|\b(nothing|none|no proposals?)\b[^.;]{0,20}\b(awaiting|waiting|pending)\b"
)


def unsupported(sentence: str, s: Situation) -> str:
    """Why a sentence must not stand in the handover, or "" if it is consistent with the checks."""
    text = sentence.lower()
    if s.uncertain and CAUSE_CLAIM.search(text.replace("unconfirmed", "")):
        return "a cause the evidence does not confirm"
    if s.product_at_risk and any(rx.search(text) for rx in _RELEASE_ADVICE):
        return "release or dispatch advice while product is held or the evidence is uncertain"
    if _SELF_ACTION.search(text) and not _NEGATED.search(text):
        return "a claim that the agent did what only a person can do"
    if s.inbox_count and _INBOX_EMPTY.search(text):
        return "an empty inbox, but the inbox holds messages"
    if s.active_alarms and _NO_ALARMS.search(text):
        return "no alarms, but alarms are active"
    for match in _NOT_HELD.finditer(text):
        if match.group(1).upper() in s.held_lots:
            return f"{match.group(1).upper()} not held, but it is on QA hold"
    if not _NEGATED.search(text):
        for match in _CLAIMED_HOLD.finditer(text):
            lot = match.group(2).upper()
            if lot in s.scope_lots and lot not in s.held_lots:
                return f"{lot} held, but it is not on hold"
    if s.pending_approvals and _NOTHING_PENDING.search(text):
        return "nothing awaiting approval, but proposals are pending"
    return ""


_LINE = re.compile(r"^(\s*(?:[-*]|\d+[.)])\s+)?(\*\*[^*]+\*\*:?\s*)?(.*)$")
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\[(*\"A-Z0-9])")


def sanitize(note: str, s: Situation) -> tuple[str, list[dict[str, str]]]:
    """Remove unsupported sentences. Returns the cleaned note and what was removed, with reasons."""
    removed: list[dict[str, str]] = []
    out: list[str] = []
    for raw in note.splitlines():
        m = _LINE.match(raw)  # always matches: every group is optional or ".*"
        assert m is not None
        bullet, lead, body = m.group(1) or "", m.group(2) or "", m.group(3) or ""
        kept = []
        for sentence in _SPLIT.split(body) if body.strip() else []:
            reason = unsupported(sentence, s)
            if reason:
                removed.append({"sentence": sentence.strip(), "reason": reason})
            else:
                kept.append(sentence)
        if body.strip() and not kept:
            if lead and not bullet:
                out.append(lead.rstrip())  # keep a bold section label such as "**What happened:**"
            continue
        out.append(bullet + lead + " ".join(kept) if body.strip() else raw)
    return "\n".join(out).strip(), removed


# --------------------------------------------------------------------------- what the run saw and did


def scope_lots(ctx: RunContext) -> list[str]:
    return sorted(i for i, r in ctx.records.items() if isinstance(r, Lot))


def situation(ctx: RunContext, assessments: list[Assessment]) -> Situation:
    held = set()
    for lot_id in scope_lots(ctx):
        try:
            if ctx.systems.get_lot(lot_id).status == LotStatus.HOLD:
                held.add(lot_id)
        except KeyError:
            continue
    return Situation(
        uncertain=any(a.verdict in UNCERTAIN for a in assessments),
        excursion=any(a.verdict == Verdict.EXCURSION_CONFIRMED for a in assessments),
        held_lots=held,
        scope_lots=set(scope_lots(ctx)),
        inbox_count=len(ctx.systems.inbox()),
        active_alarms=len(ctx.systems.list_alarms(active_only=True)),
        pending_approvals=sum(1 for a in ctx.run.approvals if a.status == ApprovalStatus.PENDING),
    )


def facts(ctx: RunContext, assessments: list[Assessment]) -> dict[str, Any]:
    """Id-free summary of what the run observed and did, comparable between sandboxes."""
    lots = {i: [r.status.value, r.dispatch_due.strftime("%H:%M") if r.dispatch_due else None]
            for i, r in sorted(ctx.records.items()) if isinstance(r, Lot)}
    orders = sorted([r.asset_id, r.status, r.version, r.created_by == "agent"]
                    for r in ctx.records.values() if isinstance(r, WorkOrder))
    refused = sorted({s.title for s in ctx.run.steps if s.kind == StepKind.GUARDRAIL and not s.ok})
    return {
        "alarms": sorted([a.asset_id, a.priority.value, a.description]
                         for a in ctx.systems.list_alarms(active_only=True)),
        "verdicts": {a.asset_id: a.verdict.value for a in assessments},
        "lots": lots,
        "work_orders": orders,
        "inbox": [sum(isinstance(r, InboxMessage) for r in ctx.records.values()), len(ctx.injection_flags)],
        "outcomes": dict(sorted(ctx.outcomes.items())),
        "approvals": sorted(a.title for a in ctx.run.approvals),
        "refused": refused,
    }


_OUTCOME_TEXT = {
    ("hold", "existing"): "{t} was already on hold",
    ("hold", "new"): "{t} was not on hold yet",
    ("wo_created", "existing"): "{t} already had an open work order from the agent",
    ("wo_created", "new"): "a new work order was raised on {t}",
    ("permit", "existing"): "a permit draft for {t} already existed",
    ("permit", "new"): "a new permit draft was needed for {t}",
    ("approval", "reused"): "an identical proposal for {t} was already pending",
    ("approval", "new"): "a new proposal was needed for {t}",
}


def differences(current: dict[str, Any], recorded: dict[str, Any], unresolved: set[str]) -> list[str]:
    """Readable clauses for how this plant differs from the recorded one (empty when they match)."""
    out = []
    if current.get("alarms") != recorded.get("alarms"):
        out.append("the active alarms differ")
    for asset, verdict in current.get("verdicts", {}).items():
        before = recorded.get("verdicts", {}).get(asset)
        if before != verdict:
            out.append(f"{asset}'s evidence check is '{verdict.replace('_', ' ')}'"
                       + (f", recorded '{before.replace('_', ' ')}'" if before else ""))
    def lot(state: list) -> str:
        return f"{'on hold' if state[0] == 'hold' else state[0]}, due {state[1]}"

    for lot_id, state in current.get("lots", {}).items():
        before = recorded.get("lots", {}).get(lot_id)
        if before != state:
            out.append(f"{lot_id} was {lot(state)}" + (f" (recorded: {lot(before)})" if before else ""))
    if current.get("work_orders") != recorded.get("work_orders"):
        out.append("the work orders differ")
    if current.get("inbox") != recorded.get("inbox"):
        n = current.get("inbox", [0])[0]
        out.append(f"the inbox held {n} message(s), recorded {recorded.get('inbox', [0])[0]}")
    for key, value in current.get("outcomes", {}).items():
        if recorded.get("outcomes", {}).get(key) != value:
            out.append(_OUTCOME_TEXT.get((key.split(":")[0], value), "{t}: " + value).format(
                t=key.split(":")[-1]))
    if current.get("approvals") != recorded.get("approvals") and not any("approval" in c for c in out):
        out.append("the proposals differ")
    if current.get("refused") != recorded.get("refused"):
        out.append("code refused a different set of steps")
    if unresolved:
        out.append("recorded references could not be bound to records in this plant")
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------- the code-composed block


def _cited_summary(a: Assessment) -> str:
    parts = []
    for p in a.probes:
        value = "n/a" if p.latest_c is None else f"{p.latest_c:.1f} °C"
        state = "fresh" if p.fresh else f"{p.quality.value}, {p.age_min:.0f} min old"
        parts.append(f"[{p.tag}] {value} ({state})")
    if a.mean_gap_c is not None:
        parts.append(f"mean gap {a.mean_gap_c:.1f} °C over 15 min")
    return f"{' · '.join(parts)} -> {a.verdict.value.replace('_', ' ')}"


def untrusted_line(ctx: RunContext) -> str:
    """What the run did with flagged text, derived from its recorded steps (not asserted)."""
    flagged = set(ctx.injection_flags)
    cited = sorted({i for s in ctx.run.steps if s.ok and s.kind == StepKind.TOOL
                    and s.tier in (Tier.AUTO, Tier.APPROVE) and s.title != "flag_suspected_injection"
                    for i in s.evidence_ids if i in flagged})
    ids = ", ".join(f"[{i}]" for i in ctx.injection_flags)
    if cited:
        return f"Untrusted input flagged: {ids}. Actions in this run cited {', '.join(cited)}: review them."
    return (f"Untrusted input flagged: {ids}. Treated as data: no action in this run cited it, and release "
            "proposals were disabled for the rest of the run.")


def verified_block(ctx: RunContext, gate: GateResult, assessments: list[Assessment],
                   removed: list[dict[str, str]]) -> str:
    lines = [f"- Status: {gate.conclusion} Confidence: {gate.confidence.replace('_', ' ')}."]
    alarms = ctx.systems.list_alarms(active_only=True)
    if alarms:
        lines.append("- Active alarms: " + "; ".join(
            f"[{a.id}] {a.description} ({a.asset_id}, {a.priority.value})" for a in alarms) + ".")
    else:
        lines.append("- Active alarms: none.")
    for a in assessments:
        lines.append(f"- {a.asset_id}: {_cited_summary(a)}")
    held, free = [], []
    for lot_id in scope_lots(ctx):
        try:
            hold = ctx.systems.active_hold(lot_id)
        except KeyError:
            continue
        (held if hold else free).append(f"[{lot_id}] ([{hold.id}])" if hold else f"[{lot_id}]")
    if held:
        lines.append(f"- On QA hold: {', '.join(held)}. Only QA can release a hold.")
    if free:
        lines.append(f"- Not on hold: {', '.join(free)}.")
    for key, label in (("wo_created", "Work order"), ("permit", "Permit draft")):
        for k, rid in sorted(ctx.created.items()):
            if k.startswith(key + ":"):
                existing = ctx.outcomes.get(k, "") == "existing"
                if key == "permit":
                    suffix = " (already drafted; not duplicated)" if existing else ""
                    suffix += "; draft only, a person authorises it"
                else:
                    suffix = " (already open; not duplicated)" if existing else ""
                lines.append(f"- {label} for {k.split(':', 1)[1]}: [{rid}]{suffix}.")
    pending = [a for a in ctx.run.approvals if a.status == ApprovalStatus.PENDING]
    if pending:
        lines.append("- Waiting for approval: " + "; ".join(
            f"[{a.id}] {a.title} ({' or '.join(APPROVER_ROLES.get(a.action, ()))})" for a in pending) + ".")
    else:
        lines.append("- Waiting for approval: nothing.")
    for a in ctx.run.approvals:
        if a.status != ApprovalStatus.PENDING:
            lines.append(f"- [{a.id}] {a.title}: {a.status.value}.")
    for rec in gate.recommendations:
        lines.append(f"- Required follow-up: {rec}")
    if ctx.injection_flags:
        lines.append(f"- {untrusted_line(ctx)}")
    if removed:
        reasons = "; ".join(dict.fromkeys(r["reason"] for r in removed))
        lines.append(f"- Removed from the model's draft: {len(removed)} unsupported sentence(s) ({reasons}).")
    return VERIFIED_HEADING + "\n" + "\n".join(lines)


def replay_notice(diffs: list[str]) -> str:
    return ("**Replay note:** this replay ran against a plant that differs from the recorded run ("
            + "; ".join(diffs[:3]) + "), so the recorded wording is not reused. The handover below is "
            "composed in code from this run's own results.")
