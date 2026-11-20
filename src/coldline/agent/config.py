"""Agent settings, read once from the environment.

Every knob that changes cost, latency or risk lives here so an operator can see them in one place.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_MODEL = "claude-sonnet-5-5"


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, default))
    except ValueError:
        return default


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class AgentSettings:
    """Runtime configuration for live runs, replay pacing and cost control."""

    model: str = DEFAULT_MODEL
    effort: str = "medium"  # output_config.effort; "medium" suits multi-step tool use
    thinking: str = "between_tools"  # "between_tools" (no extended thinking) or "adaptive"
    server_fallback: bool = True  # API-side retry on another model if a request is declined
    live_enabled: bool = True  # kill switch: COLDLINE_LIVE_ENABLED=0 forces replay for everyone
    api_key_present: bool = False
    daily_usd_cap: float = 3.00  # global spend ceiling across all visitors, per UTC day
    session_runs_per_hour: int = 3  # live runs per visitor session per rolling hour
    max_concurrent_live: int = 2
    max_model_calls: int = 8  # hard ceiling on model calls per run
    max_tokens_per_call: int = 3000
    run_timeout_s: float = 90.0  # wall-clock budget for one live run
    call_timeout_s: float = 40.0  # per model call
    replay_speed: float = 1.0  # 1.0 = recorded pacing, 0 = instant (tests)
    replay_max_call_delay_s: float = 2.5  # cap on any single replayed model-call pause
    data_dir: str | None = None  # where the daily spend ledger is persisted, if set

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentSettings:
        env = os.environ if env is None else env
        model = env.get("COLDLINE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        # between_tools is only accepted by claude-sonnet-5-5; other models use adaptive thinking.
        default_thinking = "between_tools" if model.startswith("claude-sonnet-5-5") else "adaptive"
        return cls(
            model=model,
            effort=env.get("COLDLINE_EFFORT", "medium").strip() or "medium",
            thinking=env.get("COLDLINE_THINKING", default_thinking).strip() or default_thinking,
            server_fallback=_bool(env, "COLDLINE_SERVER_FALLBACK", True),
            live_enabled=_bool(env, "COLDLINE_LIVE_ENABLED", True),
            api_key_present=bool(env.get("ANTHROPIC_API_KEY", "").strip()),
            daily_usd_cap=_float(env, "COLDLINE_DAILY_USD_CAP", 3.00),
            session_runs_per_hour=_int(env, "COLDLINE_SESSION_RUNS_PER_HOUR", 3),
            max_concurrent_live=_int(env, "COLDLINE_MAX_CONCURRENT_LIVE", 2),
            max_model_calls=_int(env, "COLDLINE_MAX_MODEL_CALLS", 8),
            max_tokens_per_call=_int(env, "COLDLINE_MAX_TOKENS_PER_CALL", 3000),
            run_timeout_s=_float(env, "COLDLINE_RUN_TIMEOUT_S", 90.0),
            call_timeout_s=_float(env, "COLDLINE_CALL_TIMEOUT_S", 40.0),
            replay_speed=_float(env, "COLDLINE_REPLAY_SPEED", 1.0),
            data_dir=env.get("COLDLINE_DATA_DIR") or None,
        )
