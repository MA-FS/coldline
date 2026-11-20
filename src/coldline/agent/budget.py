"""Cost and abuse control for live (paid) runs.

Admission (once per live run), checked in this order:

1. **Ledger health**: if the spend ledger cannot be read or written, paid mode is off until an operator
   fixes it. A ledger that might under-count is never trusted.
2. **Per-session rate limit**: at most N live runs per visitor session per rolling hour.
3. **Per-origin quota**: at most K live runs per client address per rolling day, so a visitor who keeps
   starting new sessions cannot use the whole shared allowance. (Visitors behind one shared NAT share
   this quota: a deliberate trade-off in favour of protecting the daily cap.)
4. **Concurrency**: at most M live runs in flight across the process.

Per call, the **global daily USD cap**. Before every model call the loop reserves an upper bound for that
call: every input byte counted as a token (a token always covers at least one byte) priced at the
cache-write rate of the most expensive model that may serve the request, plus the full ``max_tokens`` of
output at that model's rate. The call goes ahead only if ``spent + reserved + bound <= cap``. The
reservation is written to the ledger *before* the call. Afterwards it is settled:

* at the real cost when a response arrives,
* at zero when the API answered with an error (nothing was generated, nothing is billed),
* at the full reservation when the outcome is unknown (timeout, lost connection, cancellation). Those
  amounts are kept as ``unreconciled_usd`` so an operator can reconcile them against the provider's usage
  report; until then they count against the cap.

A process that stops mid-call leaves its reservation in the ledger. The next process counts it as spent
(unreconciled), so a restart can never reset the cap or forget liability.

The ledger is ``$COLDLINE_DATA_DIR/agent_spend.json``, replaced atomically (write, fsync, rename) on every
change. When any limit is hit the run falls back to replay with a visible notice; nothing errors for the
visitor.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

log = logging.getLogger(__name__)
LEDGER_VERSION = 2
DAY_S = 86400.0


class BudgetExceeded(Exception):
    """The next model call could push today's spend past the cap (or the ledger is unavailable)."""


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
    unreconciled_usd: float = 0.0
    ledger_ok: bool = True
    session_runs_used: int
    session_runs_limit: int
    session_window_min: int
    origin_runs_used: int = 0
    origin_runs_limit: int = 0
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
        origin_runs_per_day: int = 6,
        state_path: Path | None = None,
        window_s: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.daily_cap_usd = daily_cap_usd
        self.session_runs_per_window = session_runs_per_window
        self.origin_runs_per_day = origin_runs_per_day
        self.max_concurrent = max_concurrent
        self.min_call_reserve_usd = min_call_reserve_usd
        self.window_s = window_s
        self._clock = clock
        self._state_path = state_path
        self._lock = threading.Lock()
        self._runs: dict[str, deque[float]] = {}
        self._origin_runs: dict[str, deque[float]] = {}
        self._active = 0
        self._reservations: dict[int, float] = {}
        self._next_reservation = 0
        self._day = self._today()
        self._spent = 0.0
        self._unreconciled = 0.0
        self.ledger_error = ""
        self._load()

    # ------------------------------------------------------------------ persistence

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")

    def _load(self) -> None:
        if not self._state_path:
            return
        try:
            if not self._state_path.exists():
                self._save()  # proves the directory is writable before any paid call
                return
            data = json.loads(self._state_path.read_text())
            if not isinstance(data, dict):
                raise TypeError("ledger is not a JSON object")
            if data.get("day") == self._day:
                outstanding = sum(float(v) for v in (data.get("outstanding") or {}).values())
                self._spent = float(data.get("spent_usd", 0.0)) + outstanding
                self._unreconciled = float(data.get("unreconciled_usd", 0.0)) + outstanding
                if outstanding:
                    log.warning("spend ledger: US$%.4f reserved by a previous process counted as spent "
                                "(unreconciled)", outstanding)
            self._save()
        except (OSError, ValueError, TypeError) as exc:
            self._fail(f"could not read the spend ledger ({type(exc).__name__}: {exc})")

    def _save(self) -> bool:
        """Atomically replace the ledger. Returns False (and disables paid mode) if it cannot be written."""
        if not self._state_path:
            return True
        payload = {
            "version": LEDGER_VERSION,
            "day": self._day,
            "spent_usd": round(self._spent, 6),
            "unreconciled_usd": round(self._unreconciled, 6),
            "outstanding": {str(k): round(v, 6) for k, v in self._reservations.items()},
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._state_path.parent, prefix=".agent_spend.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(payload, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._state_path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as exc:
            self._fail(f"could not write the spend ledger ({type(exc).__name__}: {exc})")
            return False
        return True

    def _fail(self, reason: str) -> None:
        if not self.ledger_error:
            log.error("live runs disabled: %s", reason)
        self.ledger_error = reason

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._spent, self._unreconciled = today, 0.0, 0.0

    # ------------------------------------------------------------------ admission

    @staticmethod
    def _used(table: dict[str, deque[float]], key: str, cutoff: float) -> int:
        """Prune and count, without creating an entry for a key that has none (status reads allocate
        nothing; idle keys disappear once their window passes)."""
        q = table.get(key)
        if q is None:
            return 0
        while q and q[0] < cutoff:
            q.popleft()
        if not q:
            del table[key]
            return 0
        return len(q)

    def _sweep(self) -> None:
        now = self._clock()
        for table, window in ((self._runs, self.window_s), (self._origin_runs, DAY_S)):
            for key in [k for k, q in table.items() if not q or q[-1] < now - window]:
                del table[key]

    def _refusal(self, session_id: str, origin: str) -> str:
        self._roll_day()
        now = self._clock()
        if self.ledger_error:
            return "Live runs are disabled: the spend ledger is unavailable. An operator has been alerted."
        if self._used(self._runs, session_id, now - self.window_s) >= self.session_runs_per_window:
            mins = int(self.window_s // 60)
            return f"Live-run limit reached for this session ({self.session_runs_per_window} per {mins} min)."
        if origin and self._used(self._origin_runs, origin, now - DAY_S) >= self.origin_runs_per_day:
            return f"Live-run limit reached for this address ({self.origin_runs_per_day} per day)."
        if self._active >= self.max_concurrent:
            return "Live runs are busy right now."
        remaining = self.daily_cap_usd - self._spent - sum(self._reservations.values())
        if remaining < self.min_call_reserve_usd:
            return f"Today's live-run budget (US${self.daily_cap_usd:.2f}) is used up."
        return ""

    def admit(self, session_id: str, origin: str = "") -> str:
        """Register a live run start. Returns "" when admitted, else the reason it was refused."""
        with self._lock:
            self._sweep()
            reason = self._refusal(session_id, origin)
            if not reason:
                now = self._clock()
                self._runs.setdefault(session_id, deque()).append(now)
                if origin:
                    self._origin_runs.setdefault(origin, deque()).append(now)
                self._active += 1
            return reason

    def finish(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    # ------------------------------------------------------------------ per-call reservations

    def reserve(self, usd: float) -> Reservation:
        """Reserve an upper bound for one call, journalled before the call is made."""
        with self._lock:
            self._roll_day()
            if self.ledger_error:
                raise BudgetExceeded("the spend ledger is unavailable")
            committed = self._spent + sum(self._reservations.values())
            if committed + usd > self.daily_cap_usd:
                raise BudgetExceeded(
                    f"next call could cost up to US${usd:.3f}; US${self.daily_cap_usd - committed:.3f} left today"
                )
            self._next_reservation += 1
            self._reservations[self._next_reservation] = usd
            if not self._save():
                self._reservations.pop(self._next_reservation, None)
                raise BudgetExceeded("the spend ledger could not be written")
            return Reservation(self._next_reservation, usd)

    def settle(self, reservation: Reservation, actual_usd: float) -> None:
        """Replace a reservation with the call's real (or zero, for an API error) cost."""
        with self._lock:
            self._reservations.pop(reservation.id, None)
            self._roll_day()
            self._spent += max(0.0, actual_usd)
            if actual_usd > reservation.usd:
                log.warning("call cost US$%.4f exceeded its reservation US$%.4f", actual_usd, reservation.usd)
            self._save()

    def retain(self, reservation: Reservation) -> None:
        """The call's outcome is unknown (timeout, lost connection, cancelled): keep the whole reservation
        as spend, marked unreconciled."""
        with self._lock:
            self._reservations.pop(reservation.id, None)
            self._roll_day()
            self._spent += reservation.usd
            self._unreconciled += reservation.usd
            self._save()

    # ------------------------------------------------------------------ reporting

    def status(self, session_id: str, *, live_enabled: bool, api_key_present: bool,
               origin: str = "") -> BudgetStatus:
        with self._lock:
            reason = self._refusal(session_id, origin)
            if not live_enabled:
                reason = "Live runs are switched off; showing recorded runs."
            elif not api_key_present:
                reason = "No model API key is configured; showing recorded runs."
            now = self._clock()
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
                unreconciled_usd=round(self._unreconciled, 4),
                ledger_ok=not self.ledger_error,
                session_runs_used=self._used(self._runs, session_id, now - self.window_s),
                session_runs_limit=self.session_runs_per_window,
                session_window_min=int(self.window_s // 60),
                origin_runs_used=self._used(self._origin_runs, origin, now - DAY_S) if origin else 0,
                origin_runs_limit=self.origin_runs_per_day,
                active_live_runs=self._active,
                max_concurrent_live=self.max_concurrent,
            )
