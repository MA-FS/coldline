"""Request-level protections: session cookie, CSRF check, rate limits and security headers.

* **Session cookie.** An opaque random id, HMAC-signed, ``HttpOnly`` and ``SameSite=Lax``. It only selects
  the visitor's in-memory sandbox; there is nothing in it to tamper with, and a forged id is rejected.
* **CSRF.** Every state-changing request is a POST sent by htmx, which adds an ``X-ColdLine`` header to
  each request. A cross-site form cannot set a custom header, and a cross-site script cannot either without
  a CORS preflight this app never grants. ``Origin``, when present, must match the host.
* **Rate limits.** Sliding windows per client IP for POSTs, for POSTs that start an agent run, and for new
  sandboxes. The agent's own budget (per-session live runs, daily US$ cap) sits behind these.
* **Headers.** A strict Content-Security-Policy (self only, plus the font CDN the pages use), no framing,
  no referrer leakage across origins, no MIME sniffing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from http.cookies import SimpleCookie
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

COOKIE_NAME = "coldline_session"
CSRF_HEADER = "x-coldline"
COOKIE_MAX_AGE_S = 8 * 3600
_SID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


# --------------------------------------------------------------------------- session cookie


def _sign(sid: str, key: bytes) -> str:
    mac = hmac.new(key, sid.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:18]).decode().rstrip("=")


def make_cookie_value(sid: str, key: bytes) -> str:
    return f"{sid}.{_sign(sid, key)}"


def read_cookie_value(value: str | None, key: bytes) -> str | None:
    """The session id from a signed cookie value, or None if it is missing, malformed or forged."""
    if not value or "." not in value:
        return None
    sid, sig = value.rsplit(".", 1)
    if not _SID.match(sid) or not hmac.compare_digest(sig, _sign(sid, key)):
        return None
    return sid


def new_session_id() -> str:
    return secrets.token_urlsafe(18)


# --------------------------------------------------------------------------- security headers


def _inline_script_hashes(template: Path) -> list[str]:
    """CSP hashes for the inline scripts in the base template (only the pre-paint theme switch)."""
    text = template.read_text()
    hashes = []
    for body in re.findall(r"<script>(.*?)</script>", text, flags=re.DOTALL):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        hashes.append(f"'sha256-{digest}'")
    return hashes


def content_security_policy(base_template: Path) -> str:
    scripts = " ".join(["'self'", *_inline_script_hashes(base_template)])
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src {scripts}",
            # Templates use a few style attributes and htmx injects its indicator style.
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src 'self' https://fonts.gstatic.com",
            "img-src 'self' data:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


class SecurityMiddleware:
    """Pure ASGI middleware (safe with streaming responses): resolves the session id and adds headers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        signing_key: bytes,
        csp: str,
        secure_cookies: bool,
    ) -> None:
        self.app = app
        self.key = signing_key
        self.secure = secure_cookies
        self.headers = [
            (b"content-security-policy", csp.encode()),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"x-content-type-options", b"nosniff"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
            (b"cross-origin-opener-policy", b"same-origin"),
        ]
        if secure_cookies:
            self.headers.append((b"strict-transport-security", b"max-age=31536000"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cookie_header = ""
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                cookie_header = value.decode("latin-1")
                break
        jar = SimpleCookie()
        try:
            jar.load(cookie_header)
        except Exception:  # noqa: BLE001 - a malformed cookie header just means "no session"
            jar = SimpleCookie()
        morsel = jar.get(COOKIE_NAME)
        sid = read_cookie_value(morsel.value if morsel else None, self.key)
        is_new = sid is None
        sid = sid or new_session_id()
        state = scope.setdefault("state", {})
        state["sid"] = sid
        state["sid_is_new"] = is_new
        static = scope.get("path", "").startswith("/static/")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(self.headers)
                if is_new and not static:
                    headers.append((b"set-cookie", self._cookie(sid).encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _cookie(self, sid: str) -> str:
        parts = [
            f"{COOKIE_NAME}={make_cookie_value(sid, self.key)}",
            "Path=/",
            f"Max-Age={COOKIE_MAX_AGE_S}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)


# --------------------------------------------------------------------------- CSRF


def check_csrf(request: Request) -> None:
    """Reject state-changing requests that did not come from this app's own pages."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(403, "Missing request header; use the page controls.")
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = request.headers.get("host", "")
        if origin.split("://", 1)[-1] != host:
            raise HTTPException(403, "Cross-origin request refused.")


# --------------------------------------------------------------------------- rate limiting


class RateLimiter:
    """Sliding-window counter per key. In memory, one process: matches the rest of the app's state."""

    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._calls = 0

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self.window_s:
                q.popleft()
            self._calls += 1
            if self._calls % 1000 == 0:
                self._sweep(now)
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def _sweep(self, now: float) -> None:
        for key in [k for k, q in self._hits.items() if not q or q[-1] <= now - self.window_s]:
            del self._hits[key]


def client_ip(request: Request, header: str = "") -> str:
    """The visitor's IP: from a trusted proxy header when configured, else the socket peer."""
    if header:
        value = request.headers.get(header, "").strip()
        if value:
            return value
    return request.client.host if request.client else "unknown"
