"""Eval suite under pytest.

    uv run pytest evals -q                          # deterministic (recorded + scripted models)
    COLDLINE_LIVE_TESTS=1 uv run pytest evals -q    # scenario cases against the real model

Set COLDLINE_EVAL_RESULTS=<path> to also write the JSON results file.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from evals.cases import CASES
from evals.runner import run_suite, write_results

MODE = "live" if os.environ.get("COLDLINE_LIVE_TESTS") == "1" else "scripted"
SELECTED = [c.id for c in CASES if MODE == "scripted" or c.live]
pytestmark = [pytest.mark.live] if MODE == "live" else []


@pytest.fixture(scope="module")
def results() -> dict[str, Any]:
    out = asyncio.run(run_suite(MODE, SELECTED))
    if path := os.environ.get("COLDLINE_EVAL_RESULTS"):
        write_results(out, Path(path))
    return {case["id"]: case for case in out["cases"]}


@pytest.mark.parametrize("case_id", SELECTED)
def test_eval_case(results: dict[str, Any], case_id: str) -> None:
    case = results[case_id]
    failed = [f"{a['name']}: {a['detail']}" for a in case["assertions"] if not a["passed"]]
    assert not failed, f"{case_id} ({case['mode']}): " + "; ".join(failed)
