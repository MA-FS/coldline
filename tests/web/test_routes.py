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
            stream = b.get(f"/runs/{run_id}/stream")  # ends at once, revealing nothing
            assert "event: done" in stream.text and "data: missing" in stream.text and "event: step" not in stream.text
            approval = rt(a).agent.list_approvals(sid(a))[0]
            assert b.post(f"/approvals/{approval.id}/approve", headers=HX).status_code == 404
            assert "No runs yet" in b.get("/runs").text
            # Ids are numbered per visitor: b's first run and proposal carry the same ids as a's, separately.
            inject(b)
            assert rt(a).agent.list_runs(sid(b))[0].run_id == run_id == "R-7841"
            assert rt(a).agent.list_approvals(sid(b))[-1].id == "AP-0001"
            assert rt(a).sessions.get(sid(a)).get_lot("L-2231").dispatch_due == \
                rt(a).sessions.get(sid(b)).get_lot("L-2231").dispatch_due  # neither touched the other


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


def test_first_visit_offers_the_hero_scenario_and_a_finished_run_folds_its_trace(client: TestClient) -> None:
    first = client.get("/").text
    assert "Run the hero scenario" in first and 'hx-vals=\'{"scenario": "probe_mismatch"}\'' in first
    inject(client)
    page = client.get("/").text
    assert '<details class="agent-trace">' in page and 'id="agent-steps"' in page
    assert page.index("agent-trace") < page.index('id="agent-conclusion"') < page.index("Needs your decision")
    assert "What to try next:" in page and "approve the dispatch change as Shift Supervisor" in page
    assert "Run the hero scenario" not in page


def test_shell_fragment_refreshes_header_count_and_mode(client: TestClient) -> None:
    client.get("/")
    shell = client.get("/shell", headers=HX).text
    assert 'id="nav-approvals-count" hx-swap-oob="true" hidden' in shell and 'id="mode-pill" hx-swap-oob="true"' in shell
    inject(client)
    shell = client.get("/shell", headers=HX).text
    assert 'aria-label="2 pending">2</span>' in shell
    approval = rt(client).agent.list_approvals(sid(client))[0]
    r = client.post(f"/approvals/{approval.id}/decline", headers=HX)
    assert "plant-changed" in r.headers["hx-trigger"]  # the page then refetches /shell
    assert 'aria-label="1 pending">1</span>' in client.get("/shell", headers=HX).text
    full = client.get("/approvals").text
    assert "Technical details" in full and "<summary>Technical details</summary>Idempotency key" in full


def test_reset_clears_runs_and_holds(client: TestClient) -> None:
    client.get("/")
    inject(client)
    r = client.post("/session/reset", headers=HX)
    assert r.status_code == 200 and "No investigation running" in r.text
    assert rt(client).sessions.get(sid(client)).get_lot("L-2231").status == LotStatus.READY
    # The audit trail survives a reset, so numbering continues rather than reusing R-7841.
    inject(client)
    assert rt(client).agent.list_runs(sid(client))[0].run_id == "R-7842"


def test_mode_live_is_refused_with_reason_when_unavailable(client: TestClient) -> None:
    r = client.post("/session/mode", data={"mode": "live"}, headers=HX)
    assert r.status_code == 200 and "Live runs unavailable" in r.headers["hx-trigger"]
    assert "Live runs are switched off" in r.text
    assert client.post("/session/mode", data={"mode": "turbo"}, headers=HX).status_code == 422


def test_chiller_fragment(client: TestClient) -> None:
    r = client.get("/chillers/CH2", headers=HX)
    assert r.status_code == 200 and "Chiller 2 · Details" in r.text and "<html" not in r.text
    assert client.get("/chillers/C2", headers=HX).status_code == 404


def test_direct_navigation_always_gets_the_page_shell(client: TestClient) -> None:
    r = client.get("/chillers/CH2", headers={"Accept": "text/html"})
    assert r.status_code == 200 and DISCLAIMER in r.text and "Chiller 2 · Details" in r.text  # overview, focused
    missing = client.get("/no-such-page", headers={"Accept": "text/html"})
    assert missing.status_code == 404 and DISCLAIMER in missing.text and "<html" in missing.text
    assert client.get("/no-such-page").json() == {"detail": "No page here."}


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


def _events(body: str) -> list[tuple[str, str]]:
    """(event, id) pairs from an SSE body."""
    out, event, eid = [], None, ""
    for line in body.splitlines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("id: "):
            eid = line[4:]
        elif not line.strip() and event:
            out.append((event, eid))
            event, eid = None, ""
    return out


def test_stream_sends_one_event_per_step_with_its_approvals(client: TestClient) -> None:
    client.get("/")
    inject(client)
    run = rt(client).agent.list_runs(sid(client))[0]
    with client.stream("GET", f"/runs/{run.run_id}/stream") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    events = _events(body)
    steps = [eid for ev, eid in events if ev == "step"]
    assert steps == [str(s.n) for s in run.steps]  # exactly one event per step, ids are step numbers
    assert [ev for ev, _ in events][-2:] == ["conclusion", "done"]
    assert body.count('hx-swap-oob="beforeend:#agent-approvals"') >= 1
    assert all(body.count(f'id="approval-{a.id}"') == 1 for a in run.approvals)  # carried once, with its step
    with client.stream("GET", f"/runs/{run.run_id}/stream?view=trace") as r:
        trace = _events("".join(r.iter_text()))
    assert [eid for ev, eid in trace if ev == "trace-step"] == [str(s.n) for s in run.steps]
    assert "approval" not in {ev for ev, _ in trace}


def test_stream_resumes_after_the_snapshot_or_the_newest_reconnect_cursor(client: TestClient) -> None:
    client.get("/")
    inject(client)
    run = rt(client).agent.list_runs(sid(client))[0]
    first_cited = {a.id: min(st.n for st in run.steps if a.id in st.evidence_ids) for a in run.approvals}
    k = max(first_cited.values())
    # A page that rendered k steps attaches with ?after=k: nothing it already shows is sent again.
    with client.stream("GET", f"/runs/{run.run_id}/stream?after={k}") as r:
        attached = "".join(r.iter_text())
    ids = [int(eid) for ev, eid in _events(attached) if ev == "step"]
    assert ids[0] == k + 1 and "approval-AP" not in attached
    # On reconnect the browser repeats the same URL plus Last-Event-ID; the newer cursor wins.
    with client.stream("GET", f"/runs/{run.run_id}/stream?after=2", headers={"Last-Event-ID": str(k)}) as r:
        resumed = "".join(r.iter_text())
    assert next(int(eid) for ev, eid in _events(resumed) if ev == "step") == k + 1
    # Interrupted right after the step that queued an approval: that approval was delivered with it.
    with client.stream("GET", f"/runs/{run.run_id}/stream", headers={"Last-Event-ID": str(k - 1)}) as r:
        partial = "".join(r.iter_text())
    late = [a for a, n in first_cited.items() if n == k]
    assert late and all(f'id="approval-{a}"' in partial for a in late)


def test_pages_attach_to_a_running_stream_after_their_snapshot() -> None:
    settings = WebSettings(secret_key=KEY)
    agent = AgentService(AgentSettings(replay_speed=1.0, live_enabled=False))
    with TestClient(create_app(settings, agent)) as c:
        c.get("/")
        html = c.post("/session/inject", data={"scenario": "probe_mismatch"}, headers=HX).text
        assert 'sse-connect="/runs/' in html and 'sse-close="done"' in html
        run = rt(c).agent.list_runs(sid(c))[0]
        overview = c.get("/").text
        shown = overview.count('class="step ')
        assert f"stream?view=panel&amp;after={shown}" in overview
        page = c.get(f"/runs/{run.run_id}").text
        assert f"/runs/{run.run_id}/stream?view=trace&amp;after=" in page


# --------------------------------------------------------------------------- run admission (H4)


def test_controls_are_refused_while_an_investigation_runs_and_leave_the_plant_untouched() -> None:
    settings = WebSettings(secret_key=KEY)
    agent = AgentService(AgentSettings(replay_speed=1.0, live_enabled=False))
    with TestClient(create_app(settings, agent)) as c:
        c.get("/")
        assert c.post("/session/inject", data={"scenario": "probe_mismatch"}, headers=HX).status_code == 200
        plant = rt(c).sessions.get(sid(c))
        assert rt(c).agent.active_run(sid(c)) is not None
        def controls_state():  # what an inject or stale would change; the running agent writes lots and audit
            snap = plant.snapshot()
            return snap.incident, snap.active_scenario, snap.physical_scenario, snap.alarms, plant._sim.offline

        before = controls_state()
        events = len(plant.audit_log(5000))
        for path, data in (("/session/inject", {"scenario": "compressor_trip"}), ("/session/stale", {"tag": "TT-402"})):
            r = c.post(path, data=data, headers=HX)
            assert r.status_code == 409 and "still investigating" in r.headers["hx-trigger"], path
        assert controls_state() == before
        assert plant.latest(["TT-402"])["TT-402"].quality.value == "good"
        assert [e.action for e in plant.audit_log(5000)[events:] if e.actor != "agent"] == []
        assert len(rt(c).agent.list_runs(sid(c))) == 1


def test_a_rate_limited_start_leaves_the_plant_untouched() -> None:
    with make_client(runs_per_minute=1) as c:
        c.get("/")
        inject(c, "door_open")
        plant = rt(c).sessions.get(sid(c))
        incident, alarms = plant.snapshot().incident, plant.list_alarms(active_only=False)
        r = c.post("/session/inject", data={"scenario": "compressor_trip"}, headers=HX)
        assert r.status_code == 429
        assert plant.snapshot().incident == incident and plant.list_alarms(active_only=False) == alarms
        assert plant.snapshot().physical_scenario.value == "door_open"


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
