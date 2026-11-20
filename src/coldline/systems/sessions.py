"""Bounded visitor sessions keyed by opaque web-issued session IDs.

Capacity policy, applied when a new visitor arrives:

* Idle sandboxes past the TTL go first.
* One client address may hold at most ``max_per_origin`` sandboxes; a new one from that address replaces
  that address's least recently used sandbox, so one visitor cycling cookies cannot displace others.
  (Visitors behind one shared NAT share this allowance: the trade-off favours everyone else's sandboxes.)
* At full capacity the least recently *used* sandbox is evicted, not the oldest created, so a visitor who
  is actively clicking keeps their plant.
* A sandbox with an investigation running (``is_busy``) is never evicted. If every candidate is busy the
  new visitor is refused with :class:`SessionCapacityError` rather than breaking a live investigation.
"""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from coldline.systems.plant import Sandbox


class SessionCapacityError(RuntimeError):
    """Every sandbox that could make room is running an investigation."""


@dataclass
class _Entry:
    plant: Sandbox
    touched: float
    origin: str = ""


class SessionManager:
    """Synchronize lookup, idle expiry and least-recently-used eviction.

    ``on_evict(session_id)`` is called (outside the lock) for every sandbox dropped by idle expiry or by
    a capacity limit, so other layers can release per-session state such as agent runs.
    """

    def __init__(
        self,
        ttl_seconds: float = 7200,
        max_sessions: int = 200,
        *,
        max_per_origin: int = 20,
        seed: int = 42,
        clock: Callable[[], float] = monotonic,
        on_evict: Callable[[str], None] | None = None,
        is_busy: Callable[[str], bool] | None = None,
    ) -> None:
        if ttl_seconds <= 0 or max_sessions < 1 or max_per_origin < 1:
            raise ValueError("Session TTL and capacity must be positive")
        self._ttl = ttl_seconds
        self._cap = max_sessions
        self._per_origin = max_per_origin
        self._seed = seed
        self._clock = clock
        self._on_evict = on_evict
        self._is_busy = is_busy or (lambda _sid: False)
        self._lock = Lock()
        self._entries: dict[str, _Entry] = {}  # least recently used first

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, session_id: object) -> bool:
        with self._lock:
            return session_id in self._entries

    def get(self, session_id: str, origin: str = "") -> Sandbox:
        """Get or create this visitor's isolated plant, refreshing its idle TTL."""
        if not session_id:
            raise ValueError("A nonempty visitor session ID is required")
        with self._lock:
            plant, evicted = self._get(session_id, origin)
        self._notify(evicted)
        return plant

    def _evict_lru(self, candidates: list[str], evicted: list[str]) -> bool:
        for key in candidates:  # already in least-recently-used order
            if not self._is_busy(key):
                del self._entries[key]
                evicted.append(key)
                return True
        return False

    def _get(self, session_id: str, origin: str) -> tuple[Sandbox, list[str]]:
        now = self._clock()
        evicted: list[str] = []
        for key in [k for k, e in self._entries.items() if now - e.touched >= self._ttl]:
            if not self._is_busy(key):
                del self._entries[key]
                evicted.append(key)
        if session_id not in self._entries:
            if origin:
                same = [k for k, e in self._entries.items() if e.origin == origin]
                if len(same) >= self._per_origin and not self._evict_lru(same, evicted):
                    raise SessionCapacityError("All of this address's sandboxes are busy")
            if len(self._entries) >= self._cap and not self._evict_lru(list(self._entries), evicted):
                raise SessionCapacityError("Every sandbox is running an investigation")
            self._entries[session_id] = _Entry(Sandbox(self._seed, self._clock), now, origin)
        entry = self._entries.pop(session_id)  # move to the most-recently-used end
        entry.touched = now
        self._entries[session_id] = entry
        return entry.plant, evicted

    def _notify(self, evicted: list[str]) -> None:
        if self._on_evict is None:
            return
        for key in evicted:
            self._on_evict(key)

    def reset(self, session_id: str, origin: str = "") -> Sandbox:
        """Reset only this visitor's plant and keep their audit history."""
        if not session_id:
            raise ValueError("A nonempty visitor session ID is required")
        with self._lock:
            plant, evicted = self._get(session_id, origin)
            plant.reset()
        self._notify(evicted)
        return plant
