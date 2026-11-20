"""Minute-resolution plant model. A scenario starts at the next whole minute (or now
on a minute boundary) and renders its full evidence window synchronously. Hero timestamps
are relative to injection so repeat injections never rewind the plant clock.
"""

from collections import deque
from datetime import datetime, timedelta, timezone
from math import isfinite
from random import Random

from coldline.contracts import Alarm, AlarmPriority, Asset, AssetKind, Reading, Scenario, Trend
from coldline.sim.analysis import freshness

DEMO_START = datetime(2026, 11, 21, 5, tzinfo=timezone(timedelta(hours=8)))
MINUTE = timedelta(minutes=1)


def _assets() -> list[Asset]:
    assets = [
        Asset(
            id=f"CH{i}",
            kind=AssetKind.CHILLER,
            name=f"Chiller {i}",
            zone="carcase_chillers",
            tags=[f"TT-{i}01", f"TT-{i}02"],
        )
        for i in range(1, 7)
    ]
    assets.extend(
        Asset(
            id=f"C{i}",
            kind=AssetKind.COMPRESSOR,
            name=f"Compressor {i}",
            zone="refrigeration",
            tags=[f"C{i}-SUC", f"C{i}-DIS"],
            running=i != 3,
        )
        for i in range(1, 4)
    )
    assets[3].tags.extend(["EV4-SUP", "EV4-RET"])
    return assets + [
        Asset(id="BONING", kind=AssetKind.ROOM, name="Boning room", zone="boning", tags=["TT-BON"]),
        Asset(id="DOCK", kind=AssetKind.DOCK, name="Load-out dock", zone="loadout", tags=["TT-DOCK"]),
    ]


class Simulator:
    """Per-sandbox scenario engine and bounded historian."""

    def __init__(self, seed: int) -> None:
        self.now = DEMO_START
        self.assets = {a.id: a for a in _assets()}
        self.temps = dict.fromkeys(self.assets, 2.1)
        self.rng = Random(seed)
        self.scenario: Scenario | None = None
        self.origin = DEMO_START
        self.alarms: list[Alarm] = []
        self.offline: set[str] = set()
        self.history: dict[str, deque[tuple[datetime, float]]] = {
            t: deque(maxlen=121) for a in self.assets.values() for t in a.tags
        }
        self.sample_at = self.now - 121 * MINUTE
        for _ in range(121):
            self._sample()

    def _sample(self) -> None:
        self.sample_at += MINUTE
        age = (self.sample_at - self.origin) / MINUTE
        trip = self.scenario == Scenario.COMPRESSOR_TRIP and age >= 1
        hero = self.scenario == Scenario.PROBE_MISMATCH
        for a in self.assets.values():
            if a.kind == AssetKind.COMPRESSOR:
                a.running = a.id != "C3" and not (a.id == "C2" and trip)
                a.state = "run" if a.running else ("tripped" if a.id == "C2" and trip else "standby")
                values = [-6.3, 29.1] if a.running else [9.5, 12.3]
            else:
                target = {"BONING": 8.0, "DOCK": 5.5}.get(a.id, 2.1)
                if trip and a.id in ("CH4", "CH5"):
                    target = 7.4
                door_age = age - 40 if hero else age - 1
                if a.id == "CH4" and (hero or self.scenario == Scenario.DOOR_OPEN) and 0 <= door_age < 2:
                    target += 1.5 if hero else 4.2
                self.temps[a.id] += 0.35 * (target - self.temps[a.id])
                air = self.temps[a.id]
                bias = min(4.7, max(0.0, (age - 45) * 4.7 / 15)) if hero and a.id == "CH4" else 0.0
                values = [air + bias, air, air - 2.3, air + 0.5]
            for tag, value in zip(a.tags, values):
                measured = round(value + self.rng.uniform(-0.055, 0.055), 2)
                if tag not in self.offline:
                    self.history[tag].append((self.sample_at, measured))
            if a.kind == AssetKind.COMPRESSOR:
                a.detail = dict(zip(("suction_c", "discharge_c"), (self.history[t][-1][1] for t in a.tags)))

    def tick(self, seconds: float) -> None:
        """Advance on a fixed minute grid, independent of request cadence."""
        if not isfinite(seconds) or seconds < 0:
            raise ValueError("seconds must be finite and nonnegative")
        self.now += timedelta(seconds=seconds)
        while self.sample_at + MINUTE <= self.now:
            self._sample()

    def inject(self, scenario: Scenario) -> None:
        """Generate the full physical scenario before returning to the caller."""
        if scenario == Scenario.POISONED_EMAIL:
            return
        self.origin = self.sample_at if self.now == self.sample_at else self.sample_at + MINUTE
        self.scenario = scenario
        for alarm in self.alarms:
            alarm.active = False
        duration = {Scenario.PROBE_MISMATCH: 60, Scenario.COMPRESSOR_TRIP: 20, Scenario.DOOR_OPEN: 12}
        self.tick((self.origin + duration[scenario] * MINUTE - self.now).total_seconds())
        if scenario == Scenario.DOOR_OPEN:
            return
        hero = scenario == Scenario.PROBE_MISMATCH
        tags = ["TT-401", "TT-402"]
        self.alarms.append(
            Alarm(
                id=f"AL-{len(self.alarms) + 1:04}",
                ts=self.origin + (42 if hero else 1) * MINUTE,
                priority=AlarmPriority.AMBER if hero else AlarmPriority.RED,
                asset_id="CH4" if hero else "C2",
                tags=tags if hero else tags + ["TT-501", "TT-502"],
                description="Probe mismatch - readings disagree" if hero else "C2 trip - standby offline",
                short_label="PROBE MISMATCH" if hero else "C2 TRIP",
                value=" / ".join(f"{self.history[t][-1][1]:.1f} °C" for t in tags),
                limit="Δ > 2.0 °C (15 min)" if hero else "Running compressors < 2",
            )
        )

    def latest(self, tag: str) -> Reading:
        """Return a detached sample marked with current freshness."""
        ts, value = self.history[tag][-1]
        reading = Reading(tag=tag, ts=ts, value_c=value)
        reading.quality = freshness(reading, self.now)
        return reading

    def trend(self, tag: str, window_min: int) -> Trend:
        """Return measured samples only, without fabricating stale observations."""
        if window_min < 0:
            raise ValueError("window_min must be nonnegative")
        cutoff = self.now - window_min * MINUTE
        points = [Reading(tag=tag, ts=ts, value_c=v) for ts, v in self.history[tag] if ts >= cutoff]
        if points:
            points[-1] = self.latest(tag)
        asset = next(a for a in self.assets.values() if tag in a.tags)
        labels = {
            AssetKind.CHILLER: [
                "air (primary)",
                "air (independent)",
                "evaporator supply",
                "evaporator return",
            ],
            AssetKind.COMPRESSOR: ["suction", "discharge"],
        }
        label = labels.get(asset.kind, ["air"])[asset.tags.index(tag)]
        return Trend(tag=tag, description=f"{asset.name} {label}", points=points)
