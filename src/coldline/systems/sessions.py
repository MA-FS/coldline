"""Bounded visitor sessions keyed by opaque web-issued session IDs.

Capacity policy, applied when a new visitor arrives:

* Idle sandboxes past the TTL go first.
* One client address may hold at most ``max_per_origin`` sandboxes; a new one from that address replaces
  that address's least recently used sandbox, so one visitor cycling cookies cannot displace others.
  (Visitors behind one shared NAT share this allowance: the trade-off favours everyone else's sandboxes.)
* At full capacity the least recently *used* sandbox is evicted, not the oldest created, so a visitor who
  is actively clicking keeps their plant. A sandbox used within ``protect_recent_s`` is not evicted for
  capacity: when every other sandbox is that recent, the *new* visitor is refused (a "try again" page)
  rather than someone's investigation in progress being cleared.
* A sandbox with an investigation running (``is_busy``) is never evicted. If every candidate is busy the
  new visitor is refused with :class:`SessionCapacityError` rather than breaking a live investigation.

Eviction is an availability trade-off, not a security boundary: an evicted visitor loses their runs and
approvals (never anyone else's). So it is never silent: the manager remembers why a session was cleared
(``idle``, ``capacity`` or ``origin``), and :meth:`SessionManager.take_notice` hands that reason to the web
layer once, when the visitor comes back to a fresh plant.
"""

from collections import OrderedDict
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
    notice: str = ""  # why this visitor's previous sandbox was cleared, until the web layer takes it


EVICTED_MEMORY = 2000  # how many cleared session ids are remembered, to explain the fresh plant


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
        protect_recent_s: float = 0.0,
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
        self._protect = protect_recent_s
        self._seed = seed
        self._clock = clock
        self._on_evict = on_evict
        self._is_busy = is_busy or (lambda _sid: False)
        self._lock = Lock()
        self._entries: dict[str, _Entry] = {}  # least recently used first
        self._cleared: OrderedDict[str, str] = OrderedDict()  # session id -> why its sandbox was cleared

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

    def take_notice(self, session_id: str) -> str:
        """Why this visitor's previous sandbox was cleared (``idle``, ``capacity``, ``origin``), once."""
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None or not entry.notice:
                return ""
            notice, entry.notice = entry.notice, ""
            return notice

    def _drop(self, key: str, why: str, evicted: list[str]) -> None:
        del self._entries[key]
        evicted.append(key)
        self._cleared[key] = why
        self._cleared.move_to_end(key)
        while len(self._cleared) > EVICTED_MEMORY:
            self._cleared.popitem(last=False)

    def _evict_lru(self, candidates: list[str], evicted: list[str], why: str, idle_for: float = 0.0) -> bool:
        now = self._clock()
        for key in candidates:  # already in least-recently-used order
            if not self._is_busy(key) and now - self._entries[key].touched >= idle_for:
                self._drop(key, why, evicted)
                return True
        return False

    def _get(self, session_id: str, origin: str) -> tuple[Sandbox, list[str]]:
        now = self._clock()
        evicted: list[str] = []
        for key in [k for k, e in self._entries.items() if now - e.touched >= self._ttl]:
            if not self._is_busy(key):
                self._drop(key, "idle", evicted)
        if session_id not in self._entries:
            if origin:
                same = [k for k, e in self._entries.items() if e.origin == origin]
                if len(same) >= self._per_origin and not self._evict_lru(same, evicted, "origin"):
                    raise SessionCapacityError("All of this address's sandboxes are busy")
            if len(self._entries) >= self._cap and not self._evict_lru(list(self._entries), evicted, "capacity",
                                                                       idle_for=self._protect):
                raise SessionCapacityError("Every sandbox is in use")
            notice = self._cleared.pop(session_id, "")
            self._entries[session_id] = _Entry(Sandbox(self._seed, self._clock), now, origin, notice)
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
