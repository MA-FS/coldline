"""View helpers: turn contract models into small, template-friendly view models.

Templates stay dumb. Any logic about *what state to show* (alarm outline, stale marker, trip state, which value
is abnormal) lives here so it is unit-testable and the same everywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

from markupsafe import Markup

from coldline.contracts import (
    Alarm,
    AlarmPriority,
    Asset,
    AssetKind,
    Lot,
    LotStatus,
    PlantSnapshot,
    Quality,
    Reading,
    Scenario,
    Trend,
)

# Display thresholds for the mimic. Carcase chiller air should sit around 0-4 °C; above this a value is
# highlighted (amber) only when the tag is also part of an active alarm. Colour means abnormal, nothing else.
CHILLER_HIGH_C = 4.0

# Evaporator header tags shown above the chillers. The simulator publishes them on a chiller asset as
# ``EV<n>-SUP`` / ``EV<n>-RET``; these are the fallbacks if none is found.
EVAP_SUPPLY_TAG = "EV4-SUP"
EVAP_RETURN_TAG = "EV4-RET"


def probe_tags(asset: Asset) -> list[str]:
    """The air-temperature probes on a chiller (primary first), excluding evaporator tags."""
    return [t for t in asset.tags if t.startswith("TT-")][:2]


SCENARIO_LABELS: dict[Scenario, tuple[str, str]] = {
    Scenario.PROBE_MISMATCH: ("Probe mismatch", "Chiller 4: TT-401 drifts up, TT-402 steady"),
    Scenario.COMPRESSOR_TRIP: ("Compressor trip", "C2 trips; Chiller 4 genuinely warms"),
    Scenario.DOOR_OPEN: ("Door left open", "Transient rise that recovers on its own"),
    Scenario.POISONED_EMAIL: ("Poisoned email", "Supplier email tries to release all QA holds"),
}

NAV = [
    ("overview", "Overview", "/"),
    ("runs", "Agent runs", "/runs"),
    ("approvals", "Approvals", "/approvals"),
    ("audit", "Audit log", "/audit"),
    ("evals", "Evals", "/evals"),
    ("impact", "Impact", "/impact"),
    ("handover", "Handover", "/handover"),
    ("how", "How it works", "/how-it-works"),
]


# --------------------------------------------------------------------------- mimic view model


@dataclass
class TagView:
    tag: str
    value: str  # display string, e.g. "6.8"
    unit: str = "°C"
    quality: Quality = Quality.GOOD
    tone: str = "normal"  # normal | amber | red | stale
    direction: str = ""  # "up" | "down" | "steady" | ""


@dataclass
class ChillerView:
    id: str
    name: str
    short: str  # "Chiller 4"
    tags: list[TagView]
    alarm: str = ""  # "" | amber | red
    badge: str = ""  # "PROBE MISMATCH"
    stale: bool = False


@dataclass
class CompressorView:
    id: str
    state: str  # run | standby | tripped
    suction: str
    discharge: str


@dataclass
class PlantView:
    now: datetime
    compressors: list[CompressorView]
    chillers: list[ChillerView]
    evap_supply: TagView
    evap_return: TagView
    boning_temp: TagView
    due_lot: Lot | None
    alarms: list[Alarm]
    active_alarm_count: int
    scenario: Scenario | None
    lots_by_asset: dict[str, list[Lot]] = field(default_factory=dict)


def fmt_value(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    s = f"{v:.{digits}f}"
    return s.replace("-", "−")  # proper minus sign


def trend_direction(trend: Trend | None, window: int = 15, eps: float = 0.3) -> str:
    if trend is None:
        return ""
    vals = [p.value_c for p in trend.points if p.value_c is not None and p.quality == Quality.GOOD]
    if len(vals) < 2:
        return ""
    d = vals[-1] - vals[-min(window, len(vals))]
    if d > eps:
        return "up"
    if d < -eps:
        return "down"
    return "steady"


def alarm_badge(alarm: Alarm) -> str:
    """Short mimic badge from an alarm description: 'Probe mismatch - readings disagree' -> 'PROBE MISMATCH'."""
    if alarm.short_label:
        return alarm.short_label.upper()[:20]
    head = re.split(r"\s[-–—:]\s", alarm.description, maxsplit=1)[0]
    return head.upper()[:20]


def _tag_view(
    tag: str,
    latest: dict[str, Reading],
    alarm_tags: dict[str, Alarm],
    trends: dict[str, Trend],
    high: float | None = CHILLER_HIGH_C,
) -> TagView:
    r = latest.get(tag)
    if r is None:
        return TagView(tag=tag, value="—", tone="stale", quality=Quality.STALE)
    tone = "normal"
    if r.quality != Quality.GOOD:
        tone = "stale"
    elif tag in alarm_tags and (high is None or (r.value_c is not None and r.value_c > high)):
        tone = "red" if alarm_tags[tag].priority == AlarmPriority.RED else "amber"
    return TagView(
        tag=tag,
        value=fmt_value(r.value_c),
        quality=r.quality,
        tone=tone,
        direction=trend_direction(trends.get(tag)),
    )


def compressor_state(asset: Asset, alarm: Alarm | None = None) -> str:
    """run | standby | tripped. A stopped compressor with an active RED alarm on it is tripped."""
    state = str(asset.state or asset.detail.get("state", "")).lower()
    if state in {"run", "standby", "tripped"}:
        return state
    if asset.detail.get("tripped") or (not asset.running and alarm and alarm.priority == AlarmPriority.RED):
        return "tripped"
    return "run" if asset.running else "standby"


def plant_view(snap: PlantSnapshot, trends: list[Trend] | None = None) -> PlantView:
    tmap = {t.tag: t for t in (trends or [])}
    active = [a for a in snap.alarms if a.active]
    alarm_tags: dict[str, Alarm] = {}
    alarm_assets: dict[str, Alarm] = {}
    # LOW alarms are listed in the alarm table but never colour the mimic; RED beats AMBER.
    rank = {AlarmPriority.AMBER: 1, AlarmPriority.RED: 2}
    for a in sorted((a for a in active if a.priority in rank), key=lambda a: rank[a.priority]):
        for t in a.tags:
            alarm_tags[t] = a
        alarm_assets[a.asset_id] = a

    compressors, chillers = [], []
    evap_sup, evap_ret = EVAP_SUPPLY_TAG, EVAP_RETURN_TAG
    boning = TagView(tag="", value="—")
    for asset in snap.assets:
        if asset.kind == AssetKind.COMPRESSOR:
            state = compressor_state(asset, alarm_assets.get(asset.id))
            live = state == "run"
            compressors.append(
                CompressorView(
                    id=asset.id,
                    state=state,
                    suction=fmt_value(asset.detail.get("suction_c")) if live else "—",
                    discharge=fmt_value(asset.detail.get("discharge_c")) if live else "—",
                )
            )
        elif asset.kind == AssetKind.CHILLER:
            tags = [_tag_view(t, snap.latest, alarm_tags, tmap) for t in probe_tags(asset)]
            for t in asset.tags:
                if t.endswith("-SUP"):
                    evap_sup = t
                elif t.endswith("-RET"):
                    evap_ret = t
            a = alarm_assets.get(asset.id)
            chillers.append(
                ChillerView(
                    id=asset.id,
                    name=asset.name,
                    short=asset.name,
                    tags=tags,
                    alarm=("red" if a and a.priority == AlarmPriority.RED else "amber") if a else "",
                    badge=alarm_badge(a) if a else "",
                    stale=any(t.quality != Quality.GOOD for t in tags),
                )
            )
        elif asset.kind == AssetKind.ROOM and asset.tags:
            boning = _tag_view(asset.tags[0], snap.latest, alarm_tags, tmap, high=None)

    due = sorted(
        [lot for lot in snap.lots if lot.status != LotStatus.DISPATCHED and lot.dispatch_due],
        key=lambda lot: lot.dispatch_due,
    )
    lots_by_asset: dict[str, list[Lot]] = {}
    for lot in snap.lots:
        lots_by_asset.setdefault(lot.zone_asset_id, []).append(lot)

    no_alarm: dict[str, Alarm] = {}
    return PlantView(
        now=snap.now,
        compressors=compressors,
        chillers=chillers,
        evap_supply=_tag_view(evap_sup, snap.latest, no_alarm, tmap, high=None),
        evap_return=_tag_view(evap_ret, snap.latest, no_alarm, tmap, high=None),
        boning_temp=boning,
        due_lot=due[0] if due else None,
        alarms=sorted(snap.alarms, key=lambda a: a.ts, reverse=True),
        active_alarm_count=len(active),
        scenario=snap.active_scenario,
        lots_by_asset=lots_by_asset,
    )


# --------------------------------------------------------------------------- evidence / citations

_EVIDENCE_KINDS = [
    (re.compile(r"^(TT|PT|TE|ZS|DS|EV\d+|C\d+)-"), "tag", "Historian tag"),
    (re.compile(r"^L-"), "lot", "Lot"),
    (re.compile(r"^WO-"), "wo", "Work order"),
    (re.compile(r"^(ALM|AL)-"), "alarm", "Alarm"),
    (re.compile(r"^(MSG|EM|IN)-"), "msg", "Inbox (untrusted)"),
    (re.compile(r"^(H|QH)-"), "hold", "QA hold"),
    (re.compile(r"^(PTW|PD|PERMIT)-"), "permit", "Permit draft"),
    (re.compile(r"^(EV|DOOR)-"), "event", "Event"),
    (re.compile(r"^R-"), "run", "Agent run"),
    (re.compile(r"^(A|AP|APR)-"), "approval", "Approval"),
    (re.compile(r"^(CH\d+|C\d+|BONING|DOCK)$"), "asset", "Asset"),
]


def evidence_kind(record_id: str) -> tuple[str, str]:
    """('tag', 'Historian tag') for 'TT-401'. Unknown ids -> ('record', 'Record')."""
    for rx, kind, label in _EVIDENCE_KINDS:
        if rx.match(record_id):
            return kind, label
    return "record", "Record"


_CITE = re.compile(r"\[([A-Z][A-Z0-9]*-[A-Za-z0-9.\-]+)\]")


def cite_chips(text: str) -> Markup:
    """Escape text and turn [record-id] citations into chips."""

    def chip(m: re.Match) -> str:
        rid = m.group(1)
        kind, label = evidence_kind(rid)
        return (
            f'<a class="cite cite--{kind}" href="#ev-{escape(rid)}" title="{label}: {escape(rid)}">'
            f"{escape(rid)}</a>"
        )

    return Markup(_CITE.sub(chip, str(escape(text))))


def _inline(text: str) -> str:
    s = str(cite_chips(text))
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def mini_markdown(md: str) -> Markup:
    """Tiny, safe markdown subset for handover notes: #/##/### headings, - bullets, 1. lists, paragraphs,
    **bold**, `code`, [record-id] citation chips. Input is escaped first; no raw HTML passes through."""
    out: list[str] = []
    para: list[str] = []
    list_tag: str | None = None

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            close_list()
            continue
        h = re.match(r"^(#{1,3})\s+(.*)$", line)
        ul = re.match(r"^\s*[-*]\s+(.*)$", line)
        ol = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if h:
            flush_para()
            close_list()
            level = len(h.group(1)) + 1  # page owns h1
            out.append(f"<h{level}>{_inline(h.group(2))}</h{level}>")
        elif ul or ol:
            flush_para()
            want = "ul" if ul else "ol"
            if list_tag != want:
                close_list()
                out.append(f"<{want}>")
                list_tag = want
            out.append(f"<li>{_inline((ul or ol).group(1))}</li>")
        else:
            close_list()
            para.append(line.strip())
    flush_para()
    close_list()
    return Markup("\n".join(out))


def citations(md: str) -> list[str]:
    seen: list[str] = []
    for m in _CITE.finditer(md or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


# --------------------------------------------------------------------------- chiller detail


def chiller_detail(
    snap: PlantSnapshot,
    plant: PlantView,
    asset_id: str,
    trends: list[Trend],
    events: list | None = None,
    divergence_c: float = 2.0,
) -> dict:
    """Context for the ``chiller_detail`` macro / ``#chiller-detail`` fragment.

    ``trends`` should be the asset's two probe trends (primary first). ``events`` are
    :class:`coldline.web.charts.ChartEvent` markers (door open, alarm).
    """
    from coldline.web.charts import Series, trend_svg  # local: avoid import cycle in type checkers

    asset = next(a for a in snap.assets if a.id == asset_id)
    chiller = next(c for c in plant.chillers if c.id == asset_id)
    tv = {t.tag: t for t in chiller.tags}
    by_tag = {t.tag: t for t in trends}
    ordered = [by_tag[tag] for tag in probe_tags(asset) if tag in by_tag]
    vals = [p.value_c for t in ordered for p in t.points if p.value_c is not None]
    y_lo = min([1.0, *vals]) if vals else 1.0
    y_hi = max([7.0, *vals]) if vals else 7.0
    charts = []
    for i, tr in enumerate(ordered):
        other = ordered[1 - i] if len(ordered) == 2 else None
        tone = tv[tr.tag].tone if tr.tag in tv and tv[tr.tag].tone in {"amber", "red"} else "normal"
        series = [Series(tr, tone=tone)]
        if other is not None:
            series.insert(0, Series(other, tone="ghost"))
        charts.append(
            trend_svg(
                series,
                width=340,
                height=150,
                events=events or [],
                y_min=y_lo,
                y_max=y_hi,
                divergence=(ordered[0].tag, ordered[1].tag, divergence_c)
                if other is not None and i == 0
                else None,
                title=f"{tr.tag} {tr.description}, last 60 minutes",
            )
        )
    alarm = next((a for a in snap.alarms if a.active and a.asset_id == asset_id), None)
    lot = next(
        (lot for lot in snap.lots if lot.zone_asset_id == asset_id and lot.status != LotStatus.DISPATCHED),
        None,
    )
    return {
        "asset": asset,
        "chiller": chiller,
        "trends": ordered,
        "charts": charts,
        "lot": lot,
        "alarm": alarm,
    }
