"""Run the eval suite and write the results the Evals page renders.

    uv run python -m evals.run            # deterministic: recorded and scripted models (free)
    uv run python -m evals.run --live     # real model on the scenario cases (needs ANTHROPIC_API_KEY)
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from coldline.agent.service import EVAL_RESULTS_PATH
from evals.runner import run_suite, write_results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="run scenario cases against the real model")
    parser.add_argument("--case", action="append", help="run only this case id (repeatable)")
    parser.add_argument("--out", type=Path, default=EVAL_RESULTS_PATH, help="results JSON path")
    args = parser.parse_args()
    results = asyncio.run(run_suite("live" if args.live else "scripted", args.case))
    for case in results["cases"]:
        mark = "PASS" if case["passed"] else "FAIL"
        print(f"{mark}  {case['category']:<11} {case['id']:<24} {case['confidence']:<17} "
              f"US${case['cost_usd']:.4f}  {case['latency_ms']:>6} ms")
        for a in case["assertions"]:
            if not a["passed"]:
                print(f"        x {a['name']}: {a['detail']}")
    s = results["summary"]
    print(f"\n{s['passed']}/{s['total']} passed ({results['mode']}), spent US${s['spent_usd']:.4f} "
          f"(recorded cost of replayed runs US${s['recorded_cost_usd']:.4f})")
    print(f"results written to {write_results(results, args.out)}")
    return 0 if s["passed"] == s["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
