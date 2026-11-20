"""Per-process wiring: the visitor sandboxes, the agent service and per-visitor UI preferences.

Each visitor gets one :class:`~coldline.systems.Sandbox` (their own plant and business systems) keyed by
the signed session cookie. When a sandbox expires or is evicted, the agent forgets that visitor's runs and
approvals and the web layer drops their preferences, so no state outlives the sandbox.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Literal

from fastapi import HTTPException, Request

from coldline.agent import AgentService, get_agent_service
from coldline.contracts import PlantSystems
from coldline.systems import SessionManager
from coldline.systems.sessions import SessionCapacityError
from coldline.web.security import RateLimiter, check_csrf, client_ip
from coldline.web.settings import WebSettings

ROLES = ("Shift Supervisor", "QA Manager")
Mode = Literal["replay", "live"]


@dataclass
class Prefs:
    mode: Mode = "replay"
    role: str = ROLES[0]
    focus: str | None = None  # chiller shown in the detail panel
    recent_expired: set[str] = field(default_factory=set)  # approvals expired by the visitor's last action


@dataclass
class Runtime:
    settings: WebSettings
    agent: AgentService
    sessions: SessionManager = field(init=False)
    prefs: dict[str, Prefs] = field(default_factory=dict)
    post_limiter: RateLimiter = field(init=False)
    run_limiter: RateLimiter = field(init=False)
    session_limiter: RateLimiter = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        s = self.settings
        self.sessions = SessionManager(
            s.session_ttl_s, s.max_sessions, max_per_origin=s.max_sessions_per_origin, on_evict=self.forget,
            is_busy=lambda sid: self.agent.active_run(sid) is not None,
        )
        self.post_limiter = RateLimiter(s.posts_per_minute, 60)
        self.run_limiter = RateLimiter(s.runs_per_minute, 60)
        self.session_limiter = RateLimiter(s.new_sessions_per_10_min, 600)

    @classmethod
    def create(cls, settings: WebSettings | None = None, agent: AgentService | None = None) -> Runtime:
        return cls(settings or WebSettings.from_env(), agent or get_agent_service())

    def forget(self, session_id: str) -> None:
        """Release everything held for a visitor whose sandbox expired or was evicted."""
        self.agent.forget_session(session_id)
        with self._lock:
            self.prefs.pop(session_id, None)

    def prefs_for(self, session_id: str) -> Prefs:
        with self._lock:
            return self.prefs.setdefault(session_id, Prefs())


@dataclass
class Visitor:
    """The current request's visitor: session id, their sandbox and their UI preferences."""

    sid: str
    plant: PlantSystems
    prefs: Prefs
    ip: str


async def runtime(request: Request) -> Runtime:
    return request.app.state.runtime


async def visitor(request: Request) -> Visitor:
    """FastAPI dependency: resolve (or create) this visitor's sandbox and advance its clock."""
    rt: Runtime = request.app.state.runtime
    sid: str = request.state.sid
    ip = client_ip(request, rt.settings.client_ip_header)
    if sid not in rt.sessions and not rt.session_limiter.allow(ip):
        raise HTTPException(429, "Too many new sessions from this address. Try again in a few minutes.")
    try:
        plant = rt.sessions.get(sid, origin=ip)
    except SessionCapacityError:
        raise HTTPException(503, "The demo is at capacity right now. Try again in a minute.") from None
    plant.advance_wall_time()
    return Visitor(sid=sid, plant=plant, prefs=rt.prefs_for(sid), ip=ip)


async def limit_posts(request: Request) -> None:
    """Router dependency for every POST: CSRF header check, then the per-IP POST limit."""
    check_csrf(request)
    rt: Runtime = request.app.state.runtime
    if not rt.post_limiter.allow(client_ip(request, rt.settings.client_ip_header)):
        raise HTTPException(429, "Too many requests. Slow down a little.")


def limit_runs(v: Visitor, rt: Runtime) -> None:
    if not rt.run_limiter.allow(v.ip):
        raise HTTPException(429, "Too many investigations started from this address. Try again in a minute.")
