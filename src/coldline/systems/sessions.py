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
    """Synchronize lookup, idle expiry and oldest-created eviction.

    ``on_evict(session_id)`` is called (outside the lock) for every sandbox dropped by idle expiry or by
    the capacity limit, so other layers can release per-session state such as agent runs.
    """

    def __init__(
        self,
        ttl_seconds: float = 7200,
        max_sessions: int = 200,
        *,
        seed: int = 42,
        clock: Callable[[], float] = monotonic,
        on_evict: Callable[[str], None] | None = None,
    ) -> None:
        if ttl_seconds <= 0 or max_sessions < 1:
            raise ValueError("Session TTL and capacity must be positive")
        self._ttl = ttl_seconds
        self._cap = max_sessions
        self._seed = seed
        self._clock = clock
        self._on_evict = on_evict
        self._lock = Lock()
        self._entries: dict[str, _Entry] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, session_id: object) -> bool:
        with self._lock:
            return session_id in self._entries

    def get(self, session_id: str) -> Sandbox:
        """Get or create this visitor's isolated plant, refreshing its idle TTL."""
        if not session_id:
            raise ValueError("A nonempty visitor session ID is required")
        with self._lock:
            plant, evicted = self._get(session_id)
        self._notify(evicted)
        return plant

    def _get(self, session_id: str) -> tuple[Sandbox, list[str]]:
        now = self._clock()
        evicted = [k for k, e in self._entries.items() if now - e.touched >= self._ttl]
        for key in evicted:
            del self._entries[key]
        if session_id not in self._entries:
            if len(self._entries) >= self._cap:
                oldest = next(iter(self._entries))
                del self._entries[oldest]
                evicted.append(oldest)
            self._entries[session_id] = _Entry(Sandbox(self._seed, self._clock), now)
        self._entries[session_id].touched = now
        return self._entries[session_id].plant, evicted

    def _notify(self, evicted: list[str]) -> None:
        if self._on_evict is None:
            return
        for key in evicted:
            self._on_evict(key)

    def reset(self, session_id: str) -> Sandbox:
        """Reset only this visitor's plant and keep their audit history."""
        if not session_id:
            raise ValueError("A nonempty visitor session ID is required")
        with self._lock:
            plant, evicted = self._get(session_id)
            plant.reset()
        self._notify(evicted)
        return plant
