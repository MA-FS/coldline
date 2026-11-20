"""The live app: sessions, isolation, the hero flow in replay, error mapping, CSRF, SSE and headers."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from coldline.agent import AgentService, AgentSettings
from coldline.contracts import DISCLAIMER, ApprovalStatus, LotStatus
from coldline.main import create_app
from coldline.web.security import COOKIE_NAME, make_cookie_value, read_cookie_value
from coldline.web.settings import WebSettings

HX = {"X-ColdLine": "1", "HX-Request": "true"}
KEY = "k" * 32


def make_client(**overrides) -> TestClient:
    settings = WebSettings(secret_key=KEY, **overrides)
    agent = AgentService(AgentSettings(replay_speed=0.0, live_enabled=False))
    return TestClient(create_app(settings, agent))


@pytest.fixture
def client():
    with make_client() as c:
        yield c


def sid(c: TestClient) -> str:
    return read_cookie_value(c.cookies.get(COOKIE_NAME), KEY.encode())


def rt(c: TestClient):
    return c.app.state.runtime


def wait_for_run(c: TestClient) -> None:
    """Replay runs at speed 0 finish within a few event-loop turns; give the app's loop a moment."""
    agent = rt(c).agent
    for _ in range(200):
        runs = agent.list_runs(sid(c))
        if runs and runs[0].status.value != "running":
            return
        c.portal.call(asyncio.sleep, 0.01)
    raise AssertionError("run did not finish")


def inject(c: TestClient, scenario: str = "probe_mismatch"):
    r = c.post("/session/inject", data={"scenario": scenario}, headers=HX)
    assert r.status_code == 200, r.text
    wait_for_run(c)
    return r


# --------------------------------------------------------------------------- sessions


def test_first_visit_sets_a_signed_httponly_lax_cookie(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and DISCLAIMER in r.text
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=") and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert sid(client)
    assert "set-cookie" not in client.get("/").headers  # a valid cookie is kept, not reissued


def test_forged_cookie_gets_a_new_session(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, "AAAAAAAAAAAAAAAAAAAAAAAA.forged")
    r = client.get("/")
    assert r.headers["set-cookie"].startswith(f"{COOKIE_NAME}=")
    assert read_cookie_value(make_cookie_value("x" * 24, KEY.encode()), KEY.encode()) == "x" * 24


def test_two_cookies_get_isolated_sandboxes() -> None:
    with make_client() as a:
        b = TestClient(a.app)
        with b:
            a.get("/")
            b.get("/")
            assert sid(a) != sid(b)
            inject(a)
            assert rt(a).sessions.get(sid(a)).get_lot("L-2231").status == LotStatus.HOLD
            assert rt(a).sessions.get(sid(b)).get_lot("L-2231").status == LotStatus.READY
            run_id = rt(a).agent.list_runs(sid(a))[0].run_id
            assert b.get(f"/runs/{run_id}").status_code == 404  # another visitor's run is invisible
            assert b.get(f"/runs/{run_id}/stream").status_code == 404
            approval = rt(a).agent.list_approvals(sid(a))[0]
            assert b.post(f"/approvals/{approval.id}/approve", headers=HX).status_code == 404
            assert "No runs yet" in b.get("/runs").text


def test_evicted_sandbox_forgets_agent_state(client: TestClient) -> None:
    client.get("/")
    inject(client)
    s = sid(client)
    assert rt(client).agent.list_runs(s)
    rt(client).forget(s)
    assert rt(client).agent.list_runs(s) == []


# --------------------------------------------------------------------------- hero flow


def test_inject_run_approve_then_stale_expires_and_changes_conclusion(client: TestClient) -> None:
    client.get("/")
    r = inject(client)
    assert "Fault injected" in r.headers["hx-trigger"]
    s = sid(client)
    run = rt(client).agent.list_runs(s)[0]
    assert run.conclusion == "Probes disagree - cause unconfirmed."
    overview = client.get("/").text
    assert "Needs your decision" in overview and "QA HOLD" in overview

    dispatch = next(a for a in run.approvals if a.action == "change_dispatch")
    r = client.post(f"/approvals/{dispatch.id}/approve", data={"idempotency_key": dispatch.idempotency_key},
                    headers=HX)
    assert r.status_code == 200 and "Approved" in r.text and "plant-changed" in r.headers["hx-trigger"]
    lot = rt(client).sessions.get(s).get_lot("L-2231")
    assert lot.dispatch_due.isoformat() == dispatch.payload["new_due"]
    again = client.post(f"/approvals/{dispatch.id}/approve", headers=HX)
    assert "not executed again" in again.headers["hx-trigger"]

    r = client.post("/session/stale", data={"tag": "TT-402"}, headers=HX)
    assert r.status_code == 200 and "expired" in r.headers["hx-trigger"]
    amend = next(a for a in rt(client).agent.list_approvals(s) if a.action == "amend_work_order")
    assert amend.status == ApprovalStatus.EXPIRED
    wait_for_run(client)
    latest = rt(client).agent.list_runs(s)[0]
    assert latest.run_id != run.run_id and latest.confidence == "insufficient_data"
    assert "Expired: evidence changed" in client.get("/").text
    for path in ("/runs", f"/runs/{latest.run_id}", "/approvals", "/approvals?status=expired", "/audit",
                 "/evals", "/impact", "/handover", f"/handover?run_id={run.run_id}", "/how-it-works"):
        page = client.get(path)
        assert page.status_code == 200 and DISCLAIMER in page.text, path
    assert "approval_expired" in client.get("/audit").text


def test_reset_clears_runs_and_holds(client: TestClient) -> None:
    client.get("/")
    inject(client)
    r = client.post("/session/reset", headers=HX)
    assert r.status_code == 200 and "No investigation running" in r.text
    assert rt(client).sessions.get(sid(client)).get_lot("L-2231").status == LotStatus.READY


def test_mode_live_is_refused_with_reason_when_unavailable(client: TestClient) -> None:
    r = client.post("/session/mode", data={"mode": "live"}, headers=HX)
    assert r.status_code == 200 and "Live runs unavailable" in r.headers["hx-trigger"]
    assert "Live runs are switched off" in r.text
    assert client.post("/session/mode", data={"mode": "turbo"}, headers=HX).status_code == 422


def test_chiller_fragment(client: TestClient) -> None:
    r = client.get("/chillers/CH2")
    assert r.status_code == 200 and "Chiller 2 · Details" in r.text and "<html" not in r.text
    assert client.get("/chillers/C2").status_code == 404


# --------------------------------------------------------------------------- errors and roles


def test_unknown_ids_are_404(client: TestClient) -> None:
    client.get("/")
    assert client.post("/approvals/AP-9999/approve", headers=HX).status_code == 404
    assert client.get("/runs/R-0000").status_code == 404
    page = client.get("/runs/R-0000", headers={"Accept": "text/html"})
    assert page.status_code == 404 and DISCLAIMER in page.text
    assert client.post("/session/stale", data={"tag": "TT-999"}, headers=HX).status_code == 404
    assert client.post("/session/inject", data={"scenario": "meteor"}, headers=HX).status_code == 422


def test_wrong_role_is_403_and_role_switch_fixes_it(client: TestClient) -> None:
    client.get("/")
    inject(client)
    approval = rt(client).agent.list_approvals(sid(client))[0]
    assert client.post("/session/role", data={"role": "QA Manager"}, headers=HX).status_code == 204
    r = client.post(f"/approvals/{approval.id}/approve", headers=HX)
    assert r.status_code == 403 and "toast" in r.headers["hx-trigger"]
    assert client.post("/session/role", data={"role": "agent"}, headers=HX).status_code == 422
    client.post("/session/role", data={"role": "Shift Supervisor"}, headers=HX)
    assert client.post(f"/approvals/{approval.id}/decline", headers=HX).status_code == 200


def test_mismatched_idempotency_key_is_409(client: TestClient) -> None:
    client.get("/")
    inject(client)
    approval = rt(client).agent.list_approvals(sid(client))[0]
    r = client.post(f"/approvals/{approval.id}/approve", data={"idempotency_key": "nope"}, headers=HX)
    assert r.status_code == 409


# --------------------------------------------------------------------------- CSRF and rate limits


def test_post_without_htmx_header_is_rejected(client: TestClient) -> None:
    client.get("/")
    assert client.post("/session/inject", data={"scenario": "probe_mismatch"}).status_code == 403
    cross = {**HX, "Origin": "https://evil.example"}
    assert client.post("/session/reset", headers=cross).status_code == 403
    same = {**HX, "Origin": "http://testserver"}
    assert client.post("/session/reset", headers=same).status_code == 200
    assert rt(client).agent.list_runs(sid(client)) == []


def test_run_starts_are_rate_limited_per_ip() -> None:
    with make_client(runs_per_minute=2) as c:
        c.get("/")
        codes = [c.post("/session/inject", data={"scenario": "door_open"}, headers=HX).status_code
                 for _ in range(3)]
        assert codes == [200, 200, 429]


def test_new_sessions_are_rate_limited_per_ip() -> None:
    with make_client(new_sessions_per_10_min=2) as c:
        codes = []
        for _ in range(3):
            c.cookies.clear()
            codes.append(c.get("/").status_code)
        assert codes == [200, 200, 429]


# --------------------------------------------------------------------------- SSE


def test_stream_shape_and_resume(client: TestClient) -> None:
    client.get("/")
    inject(client)
    run = rt(client).agent.list_runs(sid(client))[0]
    with client.stream("GET", f"/runs/{run.run_id}/stream") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    events = [line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("event: ")]
    assert events.count("step") == len(run.steps) == events.count("trace-step")
    assert events.count("approval") == len(run.approvals)
    assert events[-2:] == ["conclusion", "done"]
    assert 'class="step' in body and 'class="tl"' in body
    first_cited = {a.id: min(st.n for st in run.steps if a.id in st.evidence_ids) for a in run.approvals}
    last_proposal = max(first_cited.values())
    headers = {"Last-Event-ID": str(last_proposal)}
    with client.stream("GET", f"/runs/{run.run_id}/stream", headers=headers) as r:
        resumed = "".join(r.iter_text())
    ids = [int(line[4:]) for line in resumed.splitlines() if line.startswith("id: ")]
    assert ids and min(ids) == last_proposal + 1
    assert "event: approval" not in resumed  # already delivered with their steps before the reconnect


def test_running_overview_connects_to_the_stream() -> None:
    settings = WebSettings(secret_key=KEY)
    agent = AgentService(AgentSettings(replay_speed=1.0, live_enabled=False))
    with TestClient(create_app(settings, agent)) as c:
        c.get("/")
        html = c.post("/session/inject", data={"scenario": "probe_mismatch"}, headers=HX).text
        assert 'sse-connect="/runs/' in html and 'sse-close="done"' in html


# --------------------------------------------------------------------------- headers, previews


def test_security_headers(client: TestClient) -> None:
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp
    assert "script-src 'self' 'sha256-" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in client.get("/static/js/app.js").headers


def test_templates_autoescape(client: TestClient) -> None:
    from coldline.web.render import templates

    assert templates.env.autoescape is True or callable(templates.env.autoescape)


def test_previews_hidden_in_production() -> None:
    with make_client(enable_previews=False, production=True) as c:
        assert c.get("/preview/").status_code == 404
        assert c.get("/styleguide").status_code == 200
        assert "Secure" in c.get("/").headers["set-cookie"]
        assert c.get("/healthz").json() == {"status": "ok"}


def test_settings_default_previews_off_in_production() -> None:
    assert WebSettings.from_env({"COLDLINE_ENV": "production"}).enable_previews is False
    assert WebSettings.from_env({}).enable_previews is True
