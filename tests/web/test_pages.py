"""Every preview page renders, carries the disclaimer, and shows the expected states."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coldline.contracts import DISCLAIMER
from coldline.main import app

client = TestClient(app)

PAGES = [
    "/preview/",
    "/preview/overview",
    "/preview/overview?state=stale",
    "/preview/overview?state=trip",
    "/preview/overview?state=running",
    "/preview/overview?state=idle",
    "/preview/runs",
    "/preview/runs/R-7842",
    "/preview/runs/R-7843",
    "/preview/runs/R-7839",
    "/preview/approvals",
    "/preview/approvals?status=expired",
    "/preview/audit",
    "/preview/evals",
    "/preview/impact",
    "/preview/handover",
    "/preview/how-it-works",
    "/styleguide",
]


@pytest.mark.parametrize("path", PAGES)
def test_page_renders_with_disclaimer(path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert DISCLAIMER in r.text
    assert "Southern Range Meats" in r.text


def test_overview_has_swap_regions_and_controls() -> None:
    html = client.get("/preview/").text
    for region in (
        'id="mimic"',
        'id="agent-panel"',
        'id="alarms"',
        'id="chiller-detail"',
        'id="agent-steps"',
    ):
        assert region in html
    assert 'hx-post="/session/inject"' in html
    assert 'hx-post="/session/stale"' in html
    for scenario in ("probe_mismatch", "compressor_trip", "door_open", "poisoned_email"):
        assert f'"scenario": "{scenario}"' in html
    assert "PROBE" in html and "MISMATCH" in html
    assert "Probes disagree - cause unconfirmed." in html


def test_stale_state_marks_reference_probe() -> None:
    html = client.get("/preview/overview?state=stale").text
    assert "TT-402 STALE" in html
    assert "Insufficient data" in html


def test_trip_state_shows_tripped_compressor() -> None:
    html = client.get("/preview/overview?state=trip").text
    assert 'data-state="tripped"' in html


def test_running_run_connects_to_stream() -> None:
    html = client.get("/preview/overview?state=running").text
    assert 'sse-connect="/runs/R-7842/stream?view=panel&amp;after=' in html


def test_chiller_fragment_has_no_shell() -> None:
    r = client.get("/preview/chillers/CH2")
    assert r.status_code == 200
    assert 'id="chiller-detail"' in r.text
    assert "<html" not in r.text
    assert client.get("/preview/chillers/C9").status_code == 404


def test_run_detail_shows_guardrail_and_costs() -> None:
    html = client.get("/preview/runs/R-7842").text
    assert "tl-card--guardrail" in html
    assert "Tier check" in html
    assert 'id="ev-L-2231"' in html


def test_approvals_pending_have_actions_and_expired_do_not() -> None:
    html = client.get("/preview/approvals").text
    assert 'hx-post="/approvals/A-0091/approve"' in html
    expired = client.get("/preview/approvals?status=expired").text
    assert "Expired: source data changed" in expired
    assert "/approve" not in expired


def test_handover_renders_citation_chips() -> None:
    html = client.get("/preview/handover").text
    assert 'class="cite cite--wo"' in html
    assert "[WO-1042]" not in html


def test_unknown_run_is_404() -> None:
    assert client.get("/preview/runs/R-0000").status_code == 404


def test_eval_medians_come_from_one_labelled_population() -> None:
    """The review's case: the "measured" medians mixed recorded, authored and scripted cases and took the
    upper middle value of an even list. They are now the scenario recordings' true medians, labelled."""
    import statistics

    from coldline.web.context import eval_rows

    _, meta = eval_rows({"cases": [
        {"id": f"s{i}", "title": "t", "category": "scenario", "scenario": "door_open", "passed": True,
         "assertions": [], "cost_usd": c, "latency_ms": ms, "mode": "scripted"}
        for i, (c, ms) in enumerate([(0.01, 1000), (0.02, 3000), (0.03, 5000), (0.05, 9000)])
    ] + [{"id": "a", "title": "t", "category": "adversarial", "scenario": "door_open", "passed": True,
          "assertions": [], "cost_usd": 0.0, "latency_ms": 0, "mode": "scripted"}]})
    assert meta["median_n"] == 4 and meta["median_cost"] == pytest.approx(0.025)  # even count: the mean of two
    assert meta["median_latency"] == statistics.median([1000, 3000, 5000, 9000]) == 4000
    page = client.get("/evals").text
    assert "median of the 5 scenario recordings, as recorded" in page and "Median cost per scenario" in page
