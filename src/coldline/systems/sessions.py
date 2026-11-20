"""Bounded visitor sessions keyed by opaque web-issued session IDs."""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from coldline.systems.plant import Sandbox


@dataclass
class _Entry:
    plant: Sandbox
    touched: float


class SessionManager:
    """Synchronize lookup, idle expiry and oldest-created eviction."""

    def __init__(
        self,
        ttl_seconds: float = 7200,
        max_sessions: int = 200,
        *,
        seed: int = 42,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_sessions < 1:
            raise ValueError("Session TTL and capacity must be positive")
        self._ttl = ttl_seconds
        self._cap = max_sessions
        self._seed = seed
        self._clock = clock
        self._lock = Lock()
        self._entries: dict[str, _Entry] = {}

    def get(self, session_id: str) -> Sandbox:
        """Get or create this visitor's isolated plant, refreshing its idle TTL."""
        if not session_id:
            raise ValueError("A nonempty visitor session ID is required")
        with self._lock:
            return self._get(session_id)

    def _get(self, session_id: str) -> Sandbox:
        now = self._clock()
        for key in [k for k, e in self._entries.items() if now - e.touched >= self._ttl]:
            del self._entries[key]
        if session_id not in self._entries:
            if len(self._entries) >= self._cap:
                del self._entries[next(iter(self._entries))]
            self._entries[session_id] = _Entry(Sandbox(self._seed, self._clock), now)
        self._entries[session_id].touched = now
        return self._entries[session_id].plant

    def reset(self, session_id: str) -> Sandbox:
        """Reset only this visitor's plant and keep their audit history."""
        if not session_id:
            raise ValueError("A nonempty visitor session ID is required")
        with self._lock:
            plant = self._get(session_id)
            plant.reset()
            return plant
