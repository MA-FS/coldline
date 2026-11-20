"""Jinja environment for ColdLine pages: one shared ``templates`` object with filters and globals.

Integration usage::

    from coldline.web.render import templates, base_context
    return templates.TemplateResponse(request, "pages/overview.html", {**base_context("overview", now), ...})
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from coldline.contracts import (
    DISCLAIMER,
    PLANT_NAME,
    AlarmPriority,
    ApprovalStatus,
    RunStatus,
    StepKind,
    Tier,
)
from coldline.web import charts, views

WEB_DIR = Path(__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
PLANT_TZ = ZoneInfo("Australia/Perth")
ASSET_VERSION = "4"  # bump to bust static caches

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
env = templates.env
env.trim_blocks = True
env.lstrip_blocks = True


# --------------------------------------------------------------------------- filters


def _local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(PLANT_TZ) if dt.tzinfo else dt


def hhmm(dt: datetime | None) -> str:
    return _local(dt).strftime("%H:%M") if dt else "—"


def hhmmss(dt: datetime | None) -> str:
    return _local(dt).strftime("%H:%M:%S") if dt else "—"


def date_long(dt: datetime | None) -> str:
    return _local(dt).strftime("%a %-d %b %Y") if dt else "—"


def iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


def usd(v: float | None, digits: int = 4) -> str:
    if v is None:
        return "—"
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:.{digits}f}"


def ms(v: float | None) -> str:
    if not v:
        return "0 ms"
    if v >= 1000:
        return f"{v / 1000:.1f} s"
    return f"{int(v)} ms"


def num(v: float | None) -> str:
    return f"{v:,}" if v is not None else "—"


TIER_TONE = {Tier.READ: "read", Tier.AUTO: "auto", Tier.APPROVE: "approve", Tier.NEVER: "never"}
STATUS_TONE = {
    RunStatus.RUNNING: "running",
    RunStatus.AWAITING_APPROVAL: "attention",
    RunStatus.DONE: "neutral",
    RunStatus.INCOMPLETE: "red",
    RunStatus.FAILED: "red",
    ApprovalStatus.PENDING: "attention",
    ApprovalStatus.APPROVED: "ok",
    ApprovalStatus.DECLINED: "neutral",
    ApprovalStatus.EXPIRED: "muted",
}
PRIORITY_TONE = {AlarmPriority.LOW: "low", AlarmPriority.AMBER: "amber", AlarmPriority.RED: "red"}
CONFIDENCE = {
    "confirmed": ("Confirmed", "neutral"),
    "unconfirmed": ("Cause unconfirmed", "amber"),
    "insufficient_data": ("Insufficient data", "amber"),
    "refused": ("Request refused", "red"),
}


def humanize(s: Any) -> str:
    return str(s).replace("_", " ").strip().capitalize() if s is not None else ""


def call_sig(name: str, args: dict[str, Any] | None) -> str:
    """get_sensor_trend(tags=["TT-401", "TT-402"], window_min=60)"""
    if not args:
        return f"{name}()"
    parts = []
    for k, v in args.items():
        if isinstance(v, str):
            parts.append(f'{k}="{v}"')
        elif isinstance(v, list):
            inner = ", ".join(f'"{x}"' if isinstance(x, str) else str(x) for x in v)
            parts.append(f"{k}=[{inner}]")
        else:
            parts.append(f"{k}={v}")
    return f"{name}({', '.join(parts)})"


env.filters.update(
    hhmm=hhmm,
    hhmmss=hhmmss,
    date_long=date_long,
    iso=iso,
    usd=usd,
    ms=ms,
    num=num,
    humanize=humanize,
    temp=views.fmt_value,
    cite=views.cite_chips,
    markdown=views.mini_markdown,
    handover=views.handover_markdown,
    tier_tone=lambda t: TIER_TONE.get(t, "read") if t else "",
    status_tone=lambda s: STATUS_TONE.get(s, "neutral"),
    priority_tone=lambda p: PRIORITY_TONE.get(p, "low"),
)

env.globals.update(
    DISCLAIMER=DISCLAIMER,
    PLANT_NAME=PLANT_NAME,
    NAV=views.NAV,
    SCENARIO_LABELS=views.SCENARIO_LABELS,
    CONFIDENCE=CONFIDENCE,
    StepKind=StepKind,
    Tier=Tier,
    ApprovalStatus=ApprovalStatus,
    RunStatus=RunStatus,
    trend_svg=charts.trend_svg,
    sparkline_svg=charts.sparkline_svg,
    Series=charts.Series,
    ChartEvent=charts.ChartEvent,
    evidence_kind=views.evidence_kind,
    citations=views.citations,
    call_sig=call_sig,
    ASSET_VERSION=ASSET_VERSION,
)


def base_context(
    active: str,
    now: datetime | None = None,
    *,
    mode: str = "replay",
    title: str = "",
    pending_approvals: int = 0,
    live_available: bool = True,
    href_prefix: str = "",
) -> dict[str, Any]:
    """Context every page template expects (shell: disclaimer, top bar, nav, clock, mode).

    ``href_prefix`` lets the preview router keep nav links inside ``/preview``.
    """
    return {
        "active_nav": active,
        "now": now,
        "mode": mode,
        "page_title": title,
        "pending_approvals": pending_approvals,
        "live_available": live_available,
        "href_prefix": href_prefix,
    }
