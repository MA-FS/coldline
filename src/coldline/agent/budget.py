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
change. On start it is validated as a whole (:func:`parse_ledger`): the version and field set, a real date
that is not in the future, and finite, non-negative amounts. The version-1 format (``day`` and
``spent_usd`` only) is migrated. Anything else (unreadable, truncated, even valid JSON such as ``{}``) can
never stop the app starting: the file is quarantined, renamed to ``agent_spend.json.quarantined-<UTC time>``
with its contents untouched, and paid mode stays off with a visible reason. While a quarantined file exists
next to the ledger, paid mode stays off after a restart too, because a fresh ledger would under-count the day.
An operator reviews the file, writes a corrected ledger and removes the quarantined copy. When any limit is
hit the run falls back to replay with a visible notice; nothing errors for the visitor.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
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
LEDGER_FIELDS = frozenset({"version", "day", "spent_usd", "unreconciled_usd", "outstanding"})
LEGACY_FIELDS = frozenset({"day", "spent_usd"})  # version 1, before reservations were journalled
QUARANTINE_SUFFIX = ".quarantined-"
DAY_S = 86400.0
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class BudgetExceeded(Exception):
    """The next model call could push today's spend past the cap (or the ledger is unavailable)."""


@dataclass
class Reservation:
    id: int
    usd: float


def _amount(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a finite, non-negative number, not {type(value).__name__}")
    try:
        number = float(value)  # an integer too large for a float raises OverflowError
    except OverflowError:
        raise ValueError(f"{name} is too large to be a spend amount") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite, non-negative number, not {number!r}")
    return number


def parse_ledger(data: object, today: str) -> tuple[str, float, float, dict[str, float]]:
    """Validate a ledger's contents: ``(day, spent, unreconciled, outstanding)``, or ValueError/TypeError.

    Accepts the current format exactly, and the version-1 format (``{"day", "spent_usd"}``), which it
    migrates with nothing unreconciled or outstanding."""
    if not isinstance(data, dict):
        raise TypeError("ledger is not a JSON object")
    keys = set(data)
    if "version" not in data and keys == LEGACY_FIELDS:
        data = {**data, "version": LEDGER_VERSION, "unreconciled_usd": 0.0, "outstanding": {}}
    elif data.get("version") != LEDGER_VERSION or isinstance(data.get("version"), bool) or keys != LEDGER_FIELDS:
        raise ValueError(f"unsupported ledger format (version {data.get('version')!r}, fields {sorted(keys)})")
    day = data["day"]
    if not isinstance(day, str) or not _DAY.fullmatch(day):
        raise ValueError(f"day must be YYYY-MM-DD, not {day!r}")
    datetime.strptime(day, "%Y-%m-%d")  # a real calendar date
    if day > today:
        raise ValueError(f"ledger day {day} is after today ({today}): the clock or the file is wrong")
    spent = _amount(data["spent_usd"], "spent_usd")
    unreconciled = _amount(data["unreconciled_usd"], "unreconciled_usd")
    if unreconciled > spent + 1e-9:
        raise ValueError("unreconciled_usd is more than spent_usd (unreconciled spend is part of spend)")
    raw = data["outstanding"]
    if not isinstance(raw, dict):
        raise TypeError("outstanding must be an object of reservation id -> amount")
    outstanding = {}
    for key, value in raw.items():
        if not key.isdigit():
            raise ValueError(f"outstanding reservation id {key!r} is not a number")
        outstanding[key] = _amount(value, f"outstanding[{key}]")
    return day, spent, unreconciled, outstanding


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
        self.ledger_error = ""  # detail for the operator's log
        self.ledger_notice = ""  # what a visitor is told (no paths or file contents)
        self.overrun = ""  # set when a call cost more than its reservation: the bound did not hold
        self._keep_file = False  # an invalid ledger is evidence: never overwrite it
        self._load()

    # ------------------------------------------------------------------ persistence

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")

    def quarantined(self) -> list[Path]:
        if not self._state_path:
            return []
        return sorted(self._state_path.parent.glob(self._state_path.name + QUARANTINE_SUFFIX + "*"))

    def _quarantine(self, reason: str) -> None:
        """Set an unusable ledger aside, contents untouched, and keep paid mode off. Never raises."""
        assert self._state_path is not None
        self._keep_file = True  # nothing is written at the ledger path by this process
        stamp = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self._state_path.with_name(self._state_path.name + QUARANTINE_SUFFIX + stamp)
        try:
            os.replace(self._state_path, target)
            where = f"moved to {target.name}"
        except OSError as exc:
            where = f"could not be moved aside ({type(exc).__name__}), left in place"
        self.ledger_notice = ("Live runs are disabled: the spend ledger was damaged and has been set aside for an "
                              "operator to check. Recorded runs still work.")
        self._fail(f"invalid spend ledger {where}: {reason}")

    def _load(self) -> None:
        if not self._state_path:
            return
        held = self.quarantined()
        if held:
            self._keep_file = True
            self.ledger_notice = ("Live runs are disabled: a damaged spend ledger is waiting for an operator to "
                                  "check it. Recorded runs still work.")
            self._fail(f"quarantined spend ledger {held[-1].name} needs review before paid mode can resume")
            return
        try:
            if not self._state_path.exists():
                self._save()  # proves the directory is writable before any paid call
                return
            raw = self._state_path.read_bytes()
        except OSError as exc:
            self._keep_file = True
            self._fail(f"could not read the spend ledger ({type(exc).__name__}: {exc})")
            return
        try:
            day, spent, unreconciled, reserved = parse_ledger(json.loads(raw), self._day)
        except Exception as exc:  # noqa: BLE001 - any damage fails closed; it must never stop the app starting
            self._quarantine(f"{type(exc).__name__}: {str(exc)[:200]}")
            return
        if day == self._day:  # an earlier day's spend (and its open reservations) belongs to that day's cap
            outstanding = sum(reserved.values())
            self._spent = spent + outstanding
            self._unreconciled = unreconciled + outstanding
            if outstanding:
                log.warning("spend ledger: US$%.4f reserved by a previous process counted as spent "
                            "(unreconciled)", outstanding)
        self._save()

    def _save(self) -> bool:
        """Atomically replace the ledger. Returns False (and disables paid mode) if it cannot be written."""
        if not self._state_path:
            return True
        if self._keep_file:
            return False
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
            return self.ledger_notice or ("Live runs are disabled: the spend ledger is unavailable. An operator "
                                          "has been alerted.")
        if self.overrun:
            return "Live runs are disabled: a call cost more than its reserved bound. An operator has been alerted."
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
            if self.ledger_error or self.overrun:
                raise BudgetExceeded(self.ledger_error and "the spend ledger is unavailable" or self.overrun)
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
            if actual_usd > reservation.usd + 1e-9:
                # The upper bound did not hold (a model billed above the supported ceiling, or a token count
                # above the byte bound). Fail closed: the cap is only a promise while every bound holds.
                self.overrun = (f"a call cost US${actual_usd:.4f}, more than its reserved bound "
                                f"US${reservation.usd:.4f}")
                log.error("live runs disabled: %s", self.overrun)
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
