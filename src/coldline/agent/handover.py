"""Handover integrity: the note a person reads, copies and acts on cannot contradict what code enforces.

The note ``write_handover_note`` accepts has three sections, in this order:

1. **Disposition** (:func:`disposition_block`), composed in code from the sandbox at handover time: the
   evidence headline (:func:`~coldline.agent.evidence.code_conclusion`) and its confidence, the fixed rule
   that product disposition is QA's decision, one line per lot the run looked at and per lot the plant
   holds (:func:`disposition_lots`: on hold with the hold id and "do not ship until QA releases it", or not
   on hold), the proposals waiting for a person, and the follow-ups the evidence requires before any
   release. Every line is a template filled from the evidence checks, lot records, holds, approvals and
   canonical record ids; no model text reaches it on any path, so a copied note can never clear a held lot,
   drop a lot an earlier investigation held, or drop a precaution the evidence calls for.
2. **Agent narrative**, the model's own observations, labelled as model-written and not authoritative.
   :func:`sanitize` keeps out of it every sentence that states a product, hold or approval decision (those
   belong to section 1) and every sentence the checks contradict: a cause while the evidence is uncertain,
   a claim that something was released or approved, an empty inbox, no alarms or nothing pending when that
   is false. What it leaves out is listed in the trace and ``run.extra["gate"]["removed"]``. The filter is
   a courtesy to the reader, not the safety mechanism: section 1 does not depend on it.
3. **Checks computed in code** (:func:`verified_block`): alarms, the evidence check per chiller, work
   orders and permit drafts, decided proposals, untrusted input, and what was left out of the narrative.

:func:`facts` summarises, without record ids that change between sandboxes, what the run observed and did.
A replay compares it with the recording's summary (:func:`differences`); when they differ the recorded
narrative described another plant, so section 2 is replaced by a replay notice composed in code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coldline.agent.approvals import APPROVER_ROLES
from coldline.agent.evidence import CAUSE_CLAIM, UNCERTAIN, Assessment, Verdict, code_conclusion, evidence_cap
from coldline.contracts import ApprovalStatus, InboxMessage, Lot, LotStatus, QAHold, StepKind, Tier, WorkOrder

if TYPE_CHECKING:
    from coldline.agent.context import RunContext

DISPOSITION_HEADING = "**Disposition (set in code from the plant records)**"
NARRATIVE_HEADING = "**Agent narrative (model-written, not authoritative)**"
PRODUCT_DISPOSITION = "Product disposition: QA decision. Held lots stay held until QA releases them."
VERIFIED_HEADING = "**Checks computed in code (not by the model)**"

# --------------------------------------------------------------------------- the agent narrative


@dataclass
class Situation:
    """What code knows at handover time, used to decide which statements the narrative may make."""

    uncertain: bool = False  # the worst evidence verdict is uncertain
    inbox_count: int = 0
    active_alarms: int = 0
    pending_approvals: int = 0


# Product, hold and approval decisions are stated by section 1 only. The match is deliberately broad: a
# false positive only moves a sentence out of the narrative (it stays in the trace); a miss is still read
# below the code-set disposition, under a label saying it is not authoritative.
_DECISION = re.compile(
    r"\bl-\d{4}\b|\blots?\b|\bap-\d{4}\b|\bqh-\d{4}\b|\bapprovals?\b|\bapproves?\b|\bdisposition\b"
    r"|\bship\w*|\bdispatch\w*|\bdespatch\w*|\breleas\w*|\bunhold|\bholds?\b|\bheld\b|\bquarantin\w*"
    r"|\bdeliver\w*|\bclear(?:ed|s)?\s+(?:to|for)\b|\bgo(?:es)?\s+out\b|\bleaves?\s+(?:the\s+)?site\b"
    r"|\bgood\s+to\s+go\b|\b(?:ok|okay|safe|fine)\s+to\s+(?:sell|use|go|send)\b"
    r"|\bfit\s+for\b|\bsal(?:e|able)\b|\bsell\w*|\bcustomers?\b"
    r"|\b(?:to|be|can|may|could|should|ok to|safe to)\s+(?:be\s+)?load(?:ed)?\b"
    r"|\bload(?:ed|ing)?\s+(?:it|them|the\s+(?:lot|truck|trailer|product|carcases)|onto|out|now)\b"
    r"|\bproduct\s+(?:is|are|was|looks|seems)\s+(?:fine|ok|okay|safe|good|unaffected|acceptable|in\s+spec"
    r"|within\s+spec)\b"
)
# "I approved", "the change was approved", "approved by the agent" (not "an approved thermometer").
_SELF_ACTION = re.compile(
    r"\b(?:i|we|the agent|agent|coldline)\s+(?:have\s+|has\s+|had\s+|then\s+|also\s+)?"
    r"(?:released|approved|authori[sz]ed|signed\s+off|executed)\b"
    r"|\b(?:was|were|is|are|been|now|got)\s+(?:released|approved|authori[sz]ed|signed\s+off)\b"
    r"|\b(?:released|approved|authori[sz]ed|signed\s+off)\s+by\s+(?:me|us|the agent|coldline)\b"
    r"|\b(?:change|proposal|amendment|request|permit)\s+(?:approved|authori[sz]ed)\b"
)
_NEGATION = re.compile(r"\b(no|not|never|nothing|none|neither|nor)\b|n't\b")
_INBOX_EMPTY = re.compile(r"\binbox\s+(is|was)\s+(empty|clear)\b|\bno\s+(new\s+)?(inbox\s+)?messages\b|\bempty inbox\b")
_NO_ALARMS = re.compile(r"\bno\s+(active\s+)?alarms?\b")
_NOTHING_PENDING = re.compile(
    r"\b(awaiting|waiting for|pending)\s+approval\s*:?\**\s*(nothing|none)\b"
    r"|\b(nothing|none|no proposals?)\b[^.;]{0,20}\b(awaiting|waiting|pending)\b"
)
_RESERVED = re.compile(r"\b(set|computed|composed)\s+in\s+code\b|\bnot\s+by\s+the\s+model\b")

DECISION_REASON = "a product, hold or approval decision, which the lines set in code state from the records"


def unsupported(sentence: str, s: Situation) -> str:
    """Why a sentence must not stand in the agent narrative, or "" if it may."""
    text = sentence.lower()
    if _RESERVED.search(text):
        return "imitates a section composed in code"
    if s.uncertain and CAUSE_CLAIM.search(text.replace("unconfirmed", "")):
        return "a cause the evidence does not confirm"
    if any(not _NEGATION.search(text[max(0, m.start() - 30):m.start()]) for m in _SELF_ACTION.finditer(text)):
        return "a claim that something was released or approved"
    if s.inbox_count and _INBOX_EMPTY.search(text):
        return "an empty inbox, but the inbox holds messages"
    if s.active_alarms and _NO_ALARMS.search(text):
        return "no alarms, but alarms are active"
    if s.pending_approvals and _NOTHING_PENDING.search(text):
        return "nothing awaiting approval, but proposals are pending"
    if _DECISION.search(text):
        return DECISION_REASON
    return ""


def contradictions(removed: list[dict[str, str]]) -> list[dict[str, str]]:
    """Left-out sentences that conflicted with the checks (the rest were decisions, stated in code)."""
    return [r for r in removed if r["reason"] != DECISION_REASON]


_LINE = re.compile(r"^(\s*(?:[-*]|\d+[.)])\s+)?(\*\*[^*]+\*\*:?\s*)?(.*)$")
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\[(*\"A-Z0-9])")
_HEADING = re.compile(r"^\s*#{1,6}\s")


def _label(text: str) -> str:
    return text.strip().strip("*").strip().rstrip(":").strip()


def sanitize(note: str, s: Situation) -> tuple[str, list[dict[str, str]]]:
    """The model's draft without the statements it may not make. Returns the notes and what was left out.

    Every part of a line is checked, including a bold lead such as ``**L-2231 can ship.**``; a lead that
    is only a section label ("**What happened:**") stays while the section has content."""
    removed: list[dict[str, str]] = []
    out: list[tuple[str, bool, bool]] = []  # (line, only a section label, starts a section)

    def check(sentence: str, context: str = "") -> bool:
        reason = unsupported(f"{context} {sentence}".strip(), s)
        if reason:
            removed.append({"sentence": sentence.strip(), "reason": reason})
        return not reason

    skipping = ""  # reason, while inside a section whose label itself states a decision
    for raw in note.splitlines():
        if not raw.strip():
            out.append(("", False, False))
            continue
        m = _LINE.match(raw)  # always matches: every group is optional or ".*"
        assert m is not None
        bullet, lead, body = m.group(1) or "", m.group(2) or "", m.group(3) or ""
        label_like = bool(lead) and not bullet and (not body.strip() or lead.rstrip().endswith(":")
                                                    or lead.rstrip().endswith(":**"))
        if skipping and not (label_like or _HEADING.match(body)):
            removed.append({"sentence": raw.strip(), "reason": skipping})
            continue
        skipping = ""
        if _HEADING.match(body) and not lead:
            reason = unsupported(body.lstrip("# "), s)
            if reason:  # "## Dispatch": the whole section is the code's to state
                removed.append({"sentence": body.strip(), "reason": reason})
                skipping = reason
                continue
        lead_ok = not lead or check(_label(lead))
        if label_like and not lead_ok:
            skipping = removed[-1]["reason"]  # "**Waiting for approval:**": its whole section goes
            for sentence in _SPLIT.split(body) if body.strip() else []:
                removed.append({"sentence": sentence.strip(), "reason": skipping})
            continue
        sentences = _SPLIT.split(body) if body.strip() else []
        # The first sentence is read together with its lead: "**Awaiting approval:** nothing."
        kept = [x for i, x in enumerate(sentences) if check(x, _label(lead) if i == 0 and lead_ok else "")]
        starts = bool(lead and lead_ok and not bullet) or bool(_HEADING.match(body))
        if kept:
            out.append((bullet + (lead if lead_ok else "") + " ".join(kept), False, starts))
        elif lead and lead_ok:
            # A bold label with nothing (left) after it: a section label, kept while its section has content.
            out.append((bullet + lead.rstrip(), not bullet, not bullet))
    return _drop_empty_sections(out), removed


def _drop_empty_sections(lines: list[tuple[str, bool, bool]]) -> str:
    """Remove section labels and headings left with nothing under them, and repeated blank lines."""
    kept: list[str] = []
    for i, (line, label_only, _) in enumerate(lines):
        if label_only or _HEADING.match(line):
            nxt = next((j for j in range(i + 1, len(lines)) if lines[j][0].strip()), None)
            if nxt is None or lines[nxt][2]:  # nothing follows, or the next section starts straight away
                continue
        if not line.strip() and (not kept or not kept[-1].strip()):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


# --------------------------------------------------------------------------- what the run saw and did


def scope_lots(ctx: RunContext) -> list[str]:
    return sorted(i for i, r in ctx.records.items() if isinstance(r, Lot))


def situation(ctx: RunContext, assessments: list[Assessment]) -> Situation:
    return Situation(
        uncertain=any(a.verdict in UNCERTAIN for a in assessments),
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
_OUTCOME_NOUN = {"hold": "a QA hold on {t}", "wo_created": "a work order on {t}", "permit": "a permit draft for {t}",
                 "approval": "a proposal for {t}"}


def _verdict(v: str) -> str:
    return v.replace("_", " ")


def _lot_state(state: list | None) -> str:
    if not state:
        return "not looked up"
    return f"{'on hold' if state[0] == 'hold' else state[0]}, due {state[1]}"


def differences(current: dict[str, Any], recorded: dict[str, Any], unresolved: set[str]) -> list[str]:
    """Readable clauses for how this plant differs from the recorded one; empty only when the two fact
    maps are equal and every recorded reference was bound. Keys present on either side are compared, so a
    fact the recording had and this run lacks counts as much as a new one."""
    if current == recorded and not unresolved:
        return []
    out = []
    if current.get("alarms") != recorded.get("alarms"):
        out.append("the active alarms differ")
    now_v, rec_v = current.get("verdicts", {}), recorded.get("verdicts", {})
    for asset in sorted(set(now_v) | set(rec_v)):
        now, before = now_v.get(asset), rec_v.get(asset)
        if now == before:
            continue
        if now is None:
            out.append(f"{asset} was not checked in this run (recorded '{_verdict(before)}')")
        else:
            out.append(f"{asset}'s evidence check is '{_verdict(now)}'"
                       + (f", recorded '{_verdict(before)}'" if before else ""))
    now_l, rec_l = current.get("lots", {}), recorded.get("lots", {})
    for lot_id in sorted(set(now_l) | set(rec_l)):
        now, before = now_l.get(lot_id), rec_l.get(lot_id)
        if now == before:
            continue
        if now is None:
            out.append(f"{lot_id} was not looked up in this run (recorded: {_lot_state(before)})")
        else:
            out.append(f"{lot_id} was {_lot_state(now)}" + (f" (recorded: {_lot_state(before)})" if before else ""))
    if current.get("work_orders") != recorded.get("work_orders"):
        out.append("the work orders differ")
    if current.get("inbox") != recorded.get("inbox"):
        n = (current.get("inbox") or [0])[0]
        out.append(f"the inbox held {n} message(s), recorded {(recorded.get('inbox') or [0])[0]}")
    now_o, rec_o = current.get("outcomes", {}), recorded.get("outcomes", {})
    for key in sorted(set(now_o) | set(rec_o)):
        now, before = now_o.get(key), rec_o.get(key)
        if now == before:
            continue
        kind, target = key.split(":")[0], key.split(":")[-1]
        if now is None:
            noun = _OUTCOME_NOUN.get(kind, "{t}").format(t=target)
            out.append(f"the recorded run had {noun} and this run did not")
        else:
            out.append(_OUTCOME_TEXT.get((kind, now), "{t}: " + now).format(t=target))
    if current.get("approvals") != recorded.get("approvals") and not any("proposal" in c for c in out):
        out.append("the proposals differ")
    if current.get("refused") != recorded.get("refused"):
        out.append("code refused a different set of steps")
    if unresolved:
        out.append("recorded references could not be bound to records in this plant")
    if not out:  # a difference in a field without its own wording
        out.append("the recorded facts differ from this run's")
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------- sections composed in code


def _hm(lot: Lot) -> str:
    return f"due {lot.dispatch_due:%H:%M}" if lot.dispatch_due else "no dispatch time"


def lot_line(lot: Lot, hold_id: str | None, check: Assessment | None) -> str:
    """One lot's disposition, from the lot record, its active hold and its chiller's evidence check."""
    head = f"[{lot.id}] ({lot.zone_asset_id}, {_hm(lot)})"
    if lot.status == LotStatus.DISPATCHED:
        return f"{head}: already dispatched."
    if hold_id:
        return (f"{head}: on QA hold [{hold_id}]. Do not ship or load it until QA releases the hold; only QA "
                "can release a hold.")
    if check is not None and (check.verdict in UNCERTAIN or check.verdict == Verdict.EXCURSION_CONFIRMED):
        return (f"{head}: not on hold, but the {check.asset_id} evidence check is '{_verdict(check.verdict)}'. "
                "Nothing in this run supports shipping it; check with QA before loading.")
    return f"{head}: not on hold."


def required_followups(assessments: list[Assessment]) -> list[str]:
    """Precautions the evidence itself requires (uncertain or excursion verdicts), in check order."""
    needs = UNCERTAIN | {Verdict.EXCURSION_CONFIRMED}
    return list(dict.fromkeys(a.recommendation for a in assessments if a.verdict in needs and a.recommendation))


def disposition_lots(ctx: RunContext) -> list[tuple[Lot, QAHold | None]]:
    """The lots the disposition lists, read from the plant now: the lots this run looked at, then every
    other lot under an active QA hold. A hold placed by an earlier investigation (or by a person) is still
    in force, so a later handover must name it even when this run never looked that lot up."""
    holds = {h.lot_id: h for h in ctx.systems.active_holds()}
    out: list[tuple[Lot, QAHold | None]] = []
    for lot_id in [*scope_lots(ctx), *sorted(set(holds) - set(scope_lots(ctx)))]:
        try:
            lot = ctx.systems.get_lot(lot_id)
        except KeyError:
            continue
        out.append((lot, holds.get(lot.id)))
    return out


def disposition_block(ctx: RunContext, assessments: list[Assessment]) -> str:
    """Section 1, filled only from the evidence checks, lot records, holds, approvals and fixed templates."""
    checks = {a.asset_id: a for a in assessments}
    status = f"- Status: {code_conclusion(assessments, ctx.flagged_records())}"
    if assessments:
        status += f" Evidence confidence: {_verdict(evidence_cap(assessments))}."
    lines = [status, f"- {PRODUCT_DISPOSITION}"]
    for lot, hold in disposition_lots(ctx):
        lines.append("- " + lot_line(lot, hold.id if hold else None, checks.get(lot.zone_asset_id)))
    pending = [a for a in ctx.run.approvals if a.status == ApprovalStatus.PENDING]
    if pending:
        lines.append("- Waiting for a person's decision: " + "; ".join(
            f"[{a.id}] {a.title} ({' or '.join(APPROVER_ROLES.get(a.action, ()))})" for a in pending)
            + ". Nothing changes until someone approves.")
    else:
        lines.append("- Waiting for approval: nothing.")
    for rec in required_followups(assessments):
        lines.append(f"- Required follow-up: {rec}")
    return DISPOSITION_HEADING + "\n" + "\n".join(lines)


def _cited_summary(a: Assessment) -> str:
    parts = []
    for p in a.probes:
        value = "n/a" if p.latest_c is None else f"{p.latest_c:.1f} °C"
        parts.append(f"[{p.tag}] {value} ({p.state})")
    if a.mean_gap_c is not None:
        parts.append(f"mean gap {a.mean_gap_c:.1f} °C over 15 min")
    return f"{' · '.join(parts)} -> {_verdict(a.verdict)}"


def untrusted_line(ctx: RunContext) -> str:
    """What the run did with flagged text, derived from its recorded steps (not asserted)."""
    records = ctx.flagged_records()
    flagged = set(records)
    cited = sorted({i for s in ctx.run.steps if s.ok and s.kind == StepKind.TOOL
                    and s.tier in (Tier.AUTO, Tier.APPROVE) and s.title != "flag_suspected_injection"
                    for i in s.evidence_ids if i in flagged})
    ids = ", ".join(f"[{i}]" for i in records)
    if cited:
        return f"Untrusted input flagged: {ids}. Actions in this run cited {', '.join(cited)}: review them."
    return (f"Untrusted input flagged: {ids}. Treated as data: no action in this run cited it, and release "
            "proposals were disabled for the rest of the run.")


def verified_block(ctx: RunContext, assessments: list[Assessment], removed: list[dict[str, str]]) -> str:
    lines = []
    alarms = ctx.systems.list_alarms(active_only=True)
    if alarms:
        lines.append("- Active alarms: " + "; ".join(
            f"[{a.id}] {a.description} ({a.asset_id}, {a.priority.value})" for a in alarms) + ".")
    else:
        lines.append("- Active alarms: none.")
    for a in assessments:
        lines.append(f"- {a.asset_id}: {_cited_summary(a)}")
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
    for a in ctx.run.approvals:
        if a.status != ApprovalStatus.PENDING:
            lines.append(f"- [{a.id}] {a.title}: {a.status.value}.")
    if ctx.flagged_records():
        lines.append(f"- {untrusted_line(ctx)}")
    if removed:
        reasons = "; ".join(dict.fromkeys(r["reason"] for r in removed))
        lines.append(f"- Left out of the agent narrative: {len(removed)} sentence(s) ({reasons}). They are in the "
                     "run trace.")
    return VERIFIED_HEADING + "\n" + "\n".join(lines)


def replay_notice(diffs: list[str]) -> str:
    return ("**Replay note:** this replay ran against a plant that differs from the recorded run ("
            + "; ".join(diffs[:3]) + "), so the recorded narrative is not reused. Everything in this handover is "
            "composed in code from this run's own results.")


def compose(ctx: RunContext, assessments: list[Assessment], draft: str,
            diffs: list[str]) -> tuple[str, list[dict[str, str]]]:
    """The accepted note: disposition, then the agent narrative (or the replay notice), then the checks.
    The model's draft feeds the narrative only."""
    s = situation(ctx, assessments)
    removed: list[dict[str, str]] = []
    parts = [disposition_block(ctx, assessments)]
    if diffs:
        parts.append(replay_notice(diffs))
    else:
        notes, removed = sanitize(draft.strip(), s)
        if notes:
            parts.append(NARRATIVE_HEADING + "\n\n" + notes)
    parts.append(verified_block(ctx, assessments, removed))
    return "\n\n".join(parts), removed
