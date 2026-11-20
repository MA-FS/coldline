"""Web-layer settings, read once from the environment.

The agent's own knobs (live runs, cost caps) live in :mod:`coldline.agent.config`.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class WebSettings:
    production: bool = False
    # Signs the session cookie. Unset means a random key per process: sessions are in memory anyway, so a
    # restart loses nothing a stable key would keep.
    secret_key: str = ""
    enable_previews: bool = True  # fixture-backed /preview/* pages; off in production
    client_ip_header: str = ""  # e.g. "Fly-Client-IP" behind a proxy that sets it; "" = socket peer
    session_ttl_s: int = 7200
    max_sessions: int = 200
    max_sessions_per_origin: int = 20  # resident sandboxes per client IP; more replace that IP's oldest
    session_protect_s: int = 600  # at capacity, a sandbox used this recently is kept and the newcomer refused
    posts_per_minute: int = 60  # per client IP, every POST
    runs_per_minute: int = 8  # per client IP, POSTs that start an agent run
    new_sessions_per_10_min: int = 30  # per client IP, sandboxes created

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WebSettings:
        env = os.environ if env is None else env
        production = env.get("COLDLINE_ENV", "").strip().lower() == "production"
        previews_raw = env.get("COLDLINE_PREVIEWS", "").strip().lower()
        previews = (not production) if previews_raw == "" else previews_raw in {"1", "true", "yes", "on"}
        return cls(
            production=production,
            secret_key=env.get("COLDLINE_SECRET_KEY", "").strip(),
            enable_previews=previews,
            client_ip_header=env.get("COLDLINE_CLIENT_IP_HEADER", "").strip(),
            session_ttl_s=_int(env, "COLDLINE_SESSION_TTL_S", 7200),
            max_sessions=_int(env, "COLDLINE_MAX_SESSIONS", 200),
            max_sessions_per_origin=_int(env, "COLDLINE_MAX_SESSIONS_PER_ORIGIN", 20),
            session_protect_s=_int(env, "COLDLINE_SESSION_PROTECT_S", 600),
            posts_per_minute=_int(env, "COLDLINE_POSTS_PER_MINUTE", 60),
            runs_per_minute=_int(env, "COLDLINE_RUNS_PER_MINUTE", 8),
            new_sessions_per_10_min=_int(env, "COLDLINE_NEW_SESSIONS_PER_10_MIN", 30),
        )

    def signing_key(self) -> bytes:
        return (self.secret_key or _PROCESS_KEY).encode()


_PROCESS_KEY = secrets.token_urlsafe(32)
