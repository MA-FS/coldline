"""Run the deterministic eval suite as part of the default test run."""

from __future__ import annotations

import agent_helpers  # noqa: F401  (puts the evals package on the path)

from evals.cases import CASES
from evals.runner import run_suite


async def test_scripted_eval_suite_passes() -> None:
    results = await run_suite("scripted")
    failures = [f"{c['id']}: {a['name']} ({a['detail']})" for c in results["cases"] for a in c["assertions"]
                if not a["passed"]]
    assert not failures, "\n".join(failures)
    assert results["summary"]["total"] == len(CASES)
    assert {c["category"] for c in results["cases"]} == {"scenario", "adversarial", "approval"}
