"""Evidence checks shared by replay and live investigations."""

from datetime import datetime, timedelta

from coldline.contracts import Quality, Reading, Trend

FRESHNESS_WINDOW = timedelta(minutes=5)  # a sample older than this is stale
DIVERGENCE_WINDOW_MIN = 15  # the plant's "Δ > 2.0 °C (15 min)" alarm
DIVERGENCE_LIMIT_C = 2.0  # mean probe gap above which the probes disagree


def freshness(reading: Reading, now: datetime) -> Quality:
    """Classify a sample against the simulation clock (never the wall clock)."""
    if reading.quality == Quality.BAD or reading.value_c is None or reading.ts > now:
        return Quality.BAD
    if now - reading.ts > FRESHNESS_WINDOW:
        return Quality.STALE
    return reading.quality


def is_comparison_valid(a: Reading, b: Reading, now: datetime) -> bool:
    """Require distinct, synchronous, fresh, good instruments."""
    return a.tag != b.tag and a.ts == b.ts and freshness(a, now) == freshness(b, now) == Quality.GOOD


def divergence(trend_a: Trend, trend_b: Trend, window_min: int) -> float | None:
    """Mean absolute gap over the last N aligned minute samples, or None for incomplete evidence.

    Call is_comparison_valid on the latest readings with plant.now() first to detect two
    equally old streams. Only observations in the requested interval contribute.
    """
    if window_min < 1:
        raise ValueError("window_min must be positive")
    if not trend_a.points or not trend_b.points:
        return None
    end = max(trend_a.points[-1].ts, trend_b.points[-1].ts)
    a = {p.ts: p for p in trend_a.points}
    b = {p.ts: p for p in trend_b.points}
    gaps = []
    for minute in range(window_min):
        ts = end - timedelta(minutes=minute)
        if ts not in a or ts not in b or not is_comparison_valid(a[ts], b[ts], ts):
            return None
        x, y = a[ts].value_c, b[ts].value_c
        assert x is not None and y is not None
        gaps.append(abs(x - y))
    return sum(gaps) / window_min


def is_diverging(trend_a: Trend, trend_b: Trend, now: datetime,
                 window_min: int = DIVERGENCE_WINDOW_MIN) -> bool | None:
    """Apply the demo's 2 °C mean-gap threshold. None means evidence is insufficient."""
    if not trend_a.points or not trend_b.points:
        return None
    if not is_comparison_valid(trend_a.points[-1], trend_b.points[-1], now):
        return None
    gap = divergence(trend_a, trend_b, window_min)
    return None if gap is None else gap > DIVERGENCE_LIMIT_C
