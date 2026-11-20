"""Cost and abuse control for live (paid) runs.

Three limits, checked in this order:

1. **Per-session rate limit**: at most N live runs per visitor session per rolling hour.
2. **Concurrency**: at most M live runs in flight across the process.
3. **Global daily USD cap**: before every model call the loop *reserves* that call's worst-case cost
   (all input at the cache-write rate plus the full ``max_tokens`` of output). The call only goes ahead if
   ``spent + reserved + worst_case <= cap``; afterwards the reservation is settled at the real cost. A
   burst of concurrent visitors therefore cannot overshoot the cap, even before any response comes back.

When any limit is hit the run falls back to replay with a visible notice; nothing errors for the visitor.
The day's spend is persisted to ``$COLDLINE_DATA_DIR/agent_spend.json`` when that directory exists, so a
restart does not reset the cap.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel


class BudgetExceeded(Exception):
    """The next model call could push today's spend past the cap."""


@dataclass
class Reservation:
    id: int
    usd: float


class BudgetStatus(BaseModel):
    live_enabled: bool
    api_key_present: bool
    live_available: bool
    reason: str = ""
    daily_cap_usd: float
    spent_today_usd: float
    reserved_usd: float
    remaining_usd: float
    session_runs_used: int
    session_runs_limit: int
    session_window_min: int
    active_live_runs: int
    max_concurrent_live: int


class BudgetGuard:
    def __init__(
        self,
        *,
        daily_cap_usd: float,
        session_runs_per_window: int,
        max_concurrent: int,
        min_call_reserve_usd: float,
        state_path: Path | None = None,
        window_s: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.daily_cap_usd = daily_cap_usd
        self.session_runs_per_window = session_runs_per_window
        self.max_concurrent = max_concurrent
        self.min_call_reserve_usd = min_call_reserve_usd
        self.window_s = window_s
        self._clock = clock
        self._state_path = state_path
        self._lock = threading.Lock()
        self._runs: dict[str, deque[float]] = defaultdict(deque)
        self._active = 0
        self._reservations: dict[int, float] = {}
        self._next_reservation = 0
        self._day = self._today()
        self._spent = 0.0
        self._load()

    # ------------------------------------------------------------------ persistence

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")

    def _load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        if data.get("day") == self._day:
            self._spent = float(data.get("spent_usd", 0.0))

    def _save(self) -> None:
        if not self._state_path or not self._state_path.parent.is_dir():
            return
        try:
            self._state_path.write_text(json.dumps({"day": self._day, "spent_usd": round(self._spent, 6)}))
        except OSError:
            pass  # the in-memory cap still applies

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._spent = today, 0.0

    # ------------------------------------------------------------------ admission

    def _session_used(self, session_id: str) -> int:
        q = self._runs[session_id]
        cutoff = self._clock() - self.window_s
        while q and q[0] < cutoff:
            q.popleft()
        return len(q)

    def _refusal(self, session_id: str) -> str:
        self._roll_day()
        if self._session_used(session_id) >= self.session_runs_per_window:
            mins = int(self.window_s // 60)
            return f"Live-run limit reached for this session ({self.session_runs_per_window} per {mins} min)."
        if self._active >= self.max_concurrent:
            return "Live runs are busy right now."
        remaining = self.daily_cap_usd - self._spent - sum(self._reservations.values())
        if remaining < self.min_call_reserve_usd:
            return f"Today's live-run budget (US${self.daily_cap_usd:.2f}) is used up."
        return ""

    def admit(self, session_id: str) -> str:
        """Register a live run start. Returns "" when admitted, else the reason it was refused."""
        with self._lock:
            reason = self._refusal(session_id)
            if not reason:
                self._runs[session_id].append(self._clock())
                self._active += 1
            return reason

    def finish(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    # ------------------------------------------------------------------ per-call reservations

    def reserve(self, usd: float) -> Reservation:
        with self._lock:
            self._roll_day()
            committed = self._spent + sum(self._reservations.values())
            if committed + usd > self.daily_cap_usd:
                raise BudgetExceeded(
                    f"next call could cost up to US${usd:.3f}; US${self.daily_cap_usd - committed:.3f} left today"
                )
            self._next_reservation += 1
            self._reservations[self._next_reservation] = usd
            return Reservation(self._next_reservation, usd)

    def settle(self, reservation: Reservation, actual_usd: float) -> None:
        with self._lock:
            self._reservations.pop(reservation.id, None)
            self._roll_day()
            self._spent += max(0.0, actual_usd)
            self._save()

    # ------------------------------------------------------------------ reporting

    def status(self, session_id: str, *, live_enabled: bool, api_key_present: bool) -> BudgetStatus:
        with self._lock:
            reason = self._refusal(session_id)
            if not live_enabled:
                reason = "Live runs are switched off; showing recorded runs."
            elif not api_key_present:
                reason = "No model API key is configured; showing recorded runs."
            reserved = sum(self._reservations.values())
            return BudgetStatus(
                live_enabled=live_enabled,
                api_key_present=api_key_present,
                live_available=not reason,
                reason=reason,
                daily_cap_usd=self.daily_cap_usd,
                spent_today_usd=round(self._spent, 4),
                reserved_usd=round(reserved, 4),
                remaining_usd=round(max(0.0, self.daily_cap_usd - self._spent - reserved), 4),
                session_runs_used=self._session_used(session_id),
                session_runs_limit=self.session_runs_per_window,
                session_window_min=int(self.window_s // 60),
                active_live_runs=self._active,
                max_concurrent_live=self.max_concurrent,
            )
