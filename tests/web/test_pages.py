"""What each page shows for the states the replayed scenarios produce, through the real routes.

Session handling, decisions and streams are covered in ``test_routes.py``; these tests pin the page content.
"""

from __future__ import annotations

import asyncio
import re
import statistics
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from coldline.agent import AgentService, AgentSettings
from coldline.contracts import DISCLAIMER, PLANT_NAME
from coldline.main import create_app
from coldline.web import styleguide as sg
from coldline.web.context import eval_rows
from coldline.web.security import COOKIE_NAME, read_cookie_value
from coldline.web.settings import WebSettings

HX = {"X-ColdLine": "1", "HX-Request": "true"}
KEY = "k" * 32
MAIN_PAGES = ("/", "/runs", "/approvals", "/audit", "/handover", "/evals", "/impact", "/how-it-works", "/styleguide")


def make_client(**overrides) -> TestClient:
    agent = AgentService(AgentSettings(replay_speed=0.0, live_enabled=False))
    return TestClient(create_app(WebSettings(secret_key=KEY, **overrides), agent))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with make_client() as c:
        c.get("/")
        yield c


def _settle(c: TestClient) -> None:
    """Replays at speed 0 finish within a few event-loop turns."""
    agent, sid = c.app.state.runtime.agent, read_cookie_value(c.cookies.get(COOKIE_NAME), KEY.encode())
    for _ in range(200):
        if agent.active_run(sid) is None:
            return
        c.portal.call(asyncio.sleep, 0.01)
    raise AssertionError("run did not finish")


def post(c: TestClient, path: str, **data: str) -> None:
    assert c.post(path, data=data, headers=HX).status_code == 200
    _settle(c)


def latest_run_id(c: TestClient) -> str:
    return re.search(r'href="/runs/(R-\d+)"', c.get("/runs").text).group(1)


# --------------------------------------------------------------------------- shell


def _shell_links(html: str) -> set[str]:
    """Same-site links in the page shell: brand, main navigation and footer."""
    shell = "".join(re.findall(r'<header class="topbar">.*?</header>|<footer class="footer">.*?</footer>', html,
                               re.DOTALL))
    return {h for h in re.findall(r'href="([^"]+)"', shell) if h.startswith("/")}


def test_shell_links_resolve_in_production() -> None:
    with make_client(production=True) as c:
        for page in ("/", "/styleguide"):
            html = c.get(page).text
            links = _shell_links(html)
            assert {"/", "/runs", "/approvals", "/how-it-works"} <= links, page
            for href in links:
                assert 200 <= c.get(href, follow_redirects=False).status_code < 400, (page, href)
        assert c.get("/preview/").status_code == 404


@pytest.mark.parametrize("after_run", [False, True], ids=["fresh plant", "after a run"])
def test_every_main_page_shows_the_disclaimer_and_plant_name(client: TestClient, after_run: bool) -> None:
    if after_run:
        post(client, "/session/inject", scenario="probe_mismatch")
    for path in MAIN_PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert DISCLAIMER in response.text and PLANT_NAME in response.text, path


# --------------------------------------------------------------------------- overview


def test_overview_has_swap_regions_and_controls(client: TestClient) -> None:
    post(client, "/session/inject", scenario="probe_mismatch")
    html = client.get("/").text
    for region in ('id="mimic"', 'id="agent-panel"', 'id="alarms"', 'id="chiller-detail"', 'id="agent-steps"'):
        assert region in html
    assert 'hx-post="/session/inject"' in html
    assert 'hx-post="/session/stale"' in html
    for scenario in ("probe_mismatch", "compressor_trip", "door_open", "poisoned_email"):
        assert f'"scenario": "{scenario}"' in html
    assert "PROBE" in html and "MISMATCH" in html
    assert "Probes disagree - cause unconfirmed." in html


def test_a_stale_reference_probe_is_marked_and_the_comparison_withdrawn(client: TestClient) -> None:
    post(client, "/session/inject", scenario="probe_mismatch")
    post(client, "/session/stale", tag="TT-402")
    html = client.get("/").text
    assert "TT-402 STALE" in html
    assert "Insufficient data" in html


def test_a_compressor_trip_shows_the_tripped_compressor(client: TestClient) -> None:
    post(client, "/session/inject", scenario="compressor_trip")
    assert 'data-state="tripped"' in client.get("/").text


def test_a_confirmed_conclusion_says_what_confirmed_it(client: TestClient) -> None:
    post(client, "/session/inject", scenario="door_open")
    html = client.get("/").text
    assert "Conclusion · Confirmed by evidence checks" in html
    assert "Transient rise in Chiller 4 - recovered, probes agree." in html


def test_a_flagged_injection_is_not_badged_as_confirmed_by_evidence(client: TestClient) -> None:
    post(client, "/session/inject", scenario="poisoned_email")
    for html in (client.get("/").text, client.get(f"/runs/{latest_run_id(client)}").text):
        assert "Suspected prompt injection in MSG-0001" in html
        assert "Conclusion · Injection flagged" in html
        assert "Confirmed by evidence checks" not in html


# --------------------------------------------------------------------------- runs, approvals, handover


def test_run_detail_shows_guardrails_and_evidence(client: TestClient) -> None:
    post(client, "/session/inject", scenario="probe_mismatch")
    html = client.get(f"/runs/{latest_run_id(client)}").text
    assert "tl-card--guardrail" in html
    assert "Tier check" in html
    assert 'id="ev-L-2231"' in html


def test_pending_approvals_have_actions_and_expired_ones_do_not(client: TestClient) -> None:
    post(client, "/session/inject", scenario="probe_mismatch")
    pending = client.get("/approvals").text
    assert 'hx-post="/approvals/AP-0001/approve"' in pending
    post(client, "/session/stale", tag="TT-402")
    expired = client.get("/approvals?status=expired").text
    assert "Expired: TT-402 went from good to stale" in expired and "Nothing was executed." in expired
    assert "/approve" not in expired


def test_approvals_are_listed_in_proposal_order_as_on_the_overview(client: TestClient) -> None:
    post(client, "/session/inject", scenario="probe_mismatch")
    overview = re.findall(r'id="approval-(AP-\d+)"', client.get("/").text)
    listed = re.findall(r'id="approval-(AP-\d+)"', client.get("/approvals").text)
    assert overview == listed == ["AP-0001", "AP-0002"]


def test_handover_renders_citation_chips(client: TestClient) -> None:
    post(client, "/session/inject", scenario="probe_mismatch")
    html = client.get("/handover").text
    assert 'class="cite cite--wo"' in html
    assert "[WO-1042]" not in html


# --------------------------------------------------------------------------- styleguide


def _samples(html: str) -> str:
    return html[html.index('<div class="sg-samples" hx-disable>'):html.index('<footer class="footer">')]


def test_styleguide_samples_come_from_replays(client: TestClient) -> None:
    samples = _samples(client.get("/styleguide").text)
    for state in ("pending", "approved", "declined", "expired"):
        assert f"approval approval--{state}" in samples
    assert "step--running" in samples and "step--blocked" in samples
    assert "agent-status--running" in samples  # the in-flight panel
    assert "Refused: NEVER-tier action" in samples
    for conclusion in ("Probes disagree - cause unconfirmed.", "Insufficient data - TT-402 stale"):
        assert conclusion in samples
    assert client.get("/runs").text.count('class="rowlink"') == 0  # the visitor's own plant is untouched


def test_styleguide_samples_cannot_reach_the_visitors_records(client: TestClient) -> None:
    """Sample run and approval ids (R-7841, AP-0001) also exist in visitor sessions, so the samples must not
    navigate, post or stream anywhere, whether or not htmx is running, and must not repeat page ids."""
    post(client, "/session/inject", scenario="door_open")  # the visitor now has records of their own
    html = client.get("/styleguide").text
    samples = _samples(html)
    ids = re.findall(r'\sid="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    assert 'id="agent-panel"' not in samples and 'id="approval-AP-0001"' not in samples
    hrefs = re.findall(r'\shref="([^"]*)"', samples)
    assert all(h.startswith("#") and h[1:] in ids for h in hrefs)  # SVG references within one sample only
    assert '<a class="ev-chip' in samples and 'href="#ev-' not in samples  # chips are shown, not linked
    assert '<a aria-disabled="true">R-7841</a>' in samples  # shown, not linked
    assert not re.search(r'\s(hx-[a-z-]+|sse-[a-z-]+|action|formaction|tabindex)=', samples)
    forms = re.findall(r"<form\b[^>]*>", samples)
    assert all("action=" not in f and "method=" not in f for f in forms)
    assert all('type="button"' in b for b in re.findall(r"<button\b[^>]*>", samples))
    assert client.get("/runs").text.count('class="rowlink"') == 1  # only the visitor's own run


def test_styleguide_samples_are_built_once_and_shared_by_visitors(client: TestClient,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    builds = []
    build = sg.catalogue

    async def counted_build():
        builds.append(1)
        return await build()

    monkeypatch.setattr(sg, "catalogue", counted_build)
    monkeypatch.setattr(sg, "SAMPLES", sg.Samples())
    first = _samples(client.get("/styleguide").text)
    second = _samples(client.get("/styleguide").text)
    with make_client() as other:
        third = _samples(other.get("/styleguide").text)
    assert len(builds) == 1
    assert first == second == third


# --------------------------------------------------------------------------- evals


def test_eval_medians_come_from_one_labelled_population(client: TestClient) -> None:
    """Cost and latency medians cover the scenario recordings only (approval cases replay the same
    recordings and adversarial cases are scripted at zero cost), and an even count takes the mean of the
    middle two, not the upper one."""
    _, meta = eval_rows({"cases": [
        {"id": f"s{i}", "title": "t", "category": "scenario", "scenario": "door_open", "passed": True,
         "assertions": [], "cost_usd": c, "latency_ms": ms, "mode": "scripted"}
        for i, (c, ms) in enumerate([(0.01, 1000), (0.02, 3000), (0.03, 5000), (0.05, 9000)])
    ] + [{"id": "a", "title": "t", "category": "adversarial", "scenario": "door_open", "passed": True,
          "assertions": [], "cost_usd": 0.0, "latency_ms": 0, "mode": "scripted"}]})
    assert meta["median_n"] == 4 and meta["median_cost"] == pytest.approx(0.025)
    assert meta["median_latency"] == statistics.median([1000, 3000, 5000, 9000]) == 4000
    page = client.get("/evals").text
    assert "median of the 5 scenario recordings, as recorded" in page and "Median cost per scenario" in page
