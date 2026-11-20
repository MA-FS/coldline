"""Agent settings, read once from the environment.

Every knob that changes cost, latency or risk lives here so an operator can see them in one place.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace

DEFAULT_MODEL = "claude-sonnet-5-5"
# Named fallback per model, used when COLDLINE_FALLBACK_MODELS is not set. Claude Sonnet 5.5's fallback list
# may name only Claude Sonnet 5; other models get none unless an operator names them.
DEFAULT_FALLBACKS = {"claude-sonnet-5-5": ("claude-sonnet-5",)}
MAX_FALLBACK_MODELS = 3


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _number(env: Mapping[str, str], name: str, default: float, errors: list[str], *, integer: bool = False,
            minimum: float = 0.0, allow_zero: bool = False) -> float:
    """A finite number at or above ``minimum`` (above it unless ``allow_zero``). Anything else keeps the
    default and records an error: an operator's typo must never loosen a paid-mode limit."""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        errors.append(f"{name}={raw!r} is not a number")
        return default
    if not math.isfinite(value) or value < minimum or (value == minimum and not allow_zero):
        errors.append(f"{name}={raw!r} is out of range")
        return default
    return value


def _fallbacks(env: Mapping[str, str], model: str, errors: list[str]) -> tuple[str, ...]:
    raw = env.get("COLDLINE_FALLBACK_MODELS")
    if raw is None:
        return DEFAULT_FALLBACKS.get(model, ())
    models = tuple(dict.fromkeys(m.strip() for m in raw.split(",") if m.strip()))
    if len(models) > MAX_FALLBACK_MODELS or model in models:
        errors.append(f"COLDLINE_FALLBACK_MODELS names {len(models)} model(s); use 0 to "
                      f"{MAX_FALLBACK_MODELS} models other than the primary")
        return ()
    return models


@dataclass(frozen=True)
class AgentSettings:
    """Runtime configuration for live runs, replay pacing and cost control."""

    model: str = DEFAULT_MODEL
    effort: str = "medium"  # output_config.effort; "medium" suits multi-step tool use
    thinking: str = "between_tools"  # "between_tools" (no extended thinking) or "adaptive"
    server_fallback: bool = True  # API-side retry on a named fallback model if a request is declined
    fallback_models: tuple[str, ...] = ("claude-sonnet-5",)  # named fallbacks, tried in order (max 3)
    live_enabled: bool = True  # kill switch: COLDLINE_LIVE_ENABLED=0 forces replay for everyone
    api_key_present: bool = False
    daily_usd_cap: float = 3.00  # global spend ceiling across all visitors, per UTC day
    session_runs_per_hour: int = 3  # live runs per visitor session per rolling hour
    origin_runs_per_day: int = 6  # live runs per client address per rolling day (all its sessions)
    max_concurrent_live: int = 2
    origin_concurrent_live: int = 1  # live runs in flight per client address
    origin_daily_usd_cap: float = 1.00  # one client address's share of the daily cap (settled + reserved)
    max_model_calls: int = 8  # hard ceiling on model calls per run
    max_tokens_per_call: int = 3000
    run_timeout_s: float = 90.0  # wall-clock budget for one live run
    call_timeout_s: float = 40.0  # per model call
    replay_speed: float = 1.0  # 1.0 = recorded pacing, 0 = instant (tests)
    replay_max_call_delay_s: float = 2.5  # cap on any single replayed model-call pause
    data_dir: str | None = None  # where the daily spend ledger is persisted, if set
    config_errors: tuple[str, ...] = ()  # invalid paid-mode settings: live runs stay off while any exist

    @property
    def fallbacks(self) -> tuple[str, ...]:
        """The fallback models a live request names (none when server-side fallback is switched off)."""
        return self.fallback_models if self.server_fallback else ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentSettings:
        env = os.environ if env is None else env
        errors: list[str] = []
        model = env.get("COLDLINE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        # between_tools is only accepted by claude-sonnet-5-5; other models use adaptive thinking.
        default_thinking = "between_tools" if model.startswith("claude-sonnet-5-5") else "adaptive"

        def num(name: str, default: float, **kw: object) -> float:
            return _number(env, name, default, errors, **kw)  # type: ignore[arg-type]

        settings = cls(
            model=model,
            effort=env.get("COLDLINE_EFFORT", "medium").strip() or "medium",
            thinking=env.get("COLDLINE_THINKING", default_thinking).strip() or default_thinking,
            server_fallback=_bool(env, "COLDLINE_SERVER_FALLBACK", True),
            fallback_models=_fallbacks(env, model, errors),
            live_enabled=_bool(env, "COLDLINE_LIVE_ENABLED", True),
            api_key_present=bool(env.get("ANTHROPIC_API_KEY", "").strip()),
            daily_usd_cap=num("COLDLINE_DAILY_USD_CAP", 3.00, allow_zero=True),
            session_runs_per_hour=int(num("COLDLINE_SESSION_RUNS_PER_HOUR", 3, integer=True, allow_zero=True)),
            origin_runs_per_day=int(num("COLDLINE_ORIGIN_RUNS_PER_DAY", 6, integer=True, allow_zero=True)),
            max_concurrent_live=int(num("COLDLINE_MAX_CONCURRENT_LIVE", 2, integer=True, allow_zero=True)),
            origin_concurrent_live=int(num("COLDLINE_ORIGIN_CONCURRENT_LIVE", 1, integer=True, allow_zero=True)),
            origin_daily_usd_cap=num("COLDLINE_ORIGIN_DAILY_USD_CAP", 1.00, allow_zero=True),
            max_model_calls=int(num("COLDLINE_MAX_MODEL_CALLS", 8, integer=True)),
            max_tokens_per_call=int(num("COLDLINE_MAX_TOKENS_PER_CALL", 3000, integer=True)),
            run_timeout_s=num("COLDLINE_RUN_TIMEOUT_S", 90.0),
            call_timeout_s=num("COLDLINE_CALL_TIMEOUT_S", 40.0),
            replay_speed=num("COLDLINE_REPLAY_SPEED", 1.0, allow_zero=True),
            data_dir=env.get("COLDLINE_DATA_DIR") or None,
        )
        return replace(settings, config_errors=tuple(errors))
