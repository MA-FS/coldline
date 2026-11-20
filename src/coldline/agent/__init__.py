"""ColdLine agent: a bounded tool-use loop with code-enforced tiers, approvals, replay and cost caps.

The web layer uses :func:`get_agent_service` (see :mod:`coldline.agent.service` for the full API).
"""

from coldline.agent.approvals import ApprovalResult
from coldline.agent.budget import BudgetStatus
from coldline.agent.config import AgentSettings
from coldline.agent.service import AgentService, get_agent_service, load_eval_results, public_run_json

__all__ = [
    "AgentService",
    "AgentSettings",
    "ApprovalResult",
    "BudgetStatus",
    "get_agent_service",
    "load_eval_results",
    "public_run_json",
]
