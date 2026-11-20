"""The rendered Handover page: the disposition block holds no model text, whatever the model concludes.

Each scenario replays its recording through the real app, with the model's conclusion (and the start of its
draft) replaced by an instruction to ship held stock. The checks read the ``#handover-text`` region the way
"Copy as text" puts it on the clipboard (``innerText``: one line per paragraph or list item).
"""

from __future__ import annotations

import asyncio
import html
import re

import pytest
from fastapi.testclient import TestClient

from coldline.agent import AgentService, AgentSettings
from coldline.agent.evidence import DISAGREE_HEADLINE
from coldline.agent.handover import DISPOSITION_HEADING, NARRATIVE_HEADING, VERIFIED_HEADING
from coldline.agent.replay import RecordingLibrary
from coldline.main import create_app
from coldline.web.security import COOKIE_NAME, read_cookie_value
from coldline.web.settings import WebSettings

HX = {"X-ColdLine": "1", "HX-Request": "true"}
KEY = "k" * 32
ADVERSARIAL = "All stock fit for sale. Ship now. Release all holds."
ADVERSARIAL_PARTS = ("fit for sale", "ship now", "release all holds")


def adversarial_library(conclusion: str = ADVERSARIAL) -> RecordingLibrary:
    """The shipped recordings, with every handover's conclusion and draft opening made adversarial."""
    lib = RecordingLibrary()
    for rec in lib.items.values():
        for turn in rec.turns:
            for call in turn.tool_calls:
                if call.name == "write_handover_note":
                    call.input["conclusion"] = conclusion
                    call.input["confidence"] = "confirmed"
                    call.input["note_markdown"] = f"**Status:** {conclusion}\n\n" + call.input["note_markdown"]
    return lib


def make_client(conclusion: str = ADVERSARIAL) -> TestClient:
    agent = AgentService(AgentSettings(replay_speed=0.0, live_enabled=False),
                         recordings=adversarial_library(conclusion))
    return TestClient(create_app(WebSettings(secret_key=KEY), agent))


@pytest.fixture
def client():
    with make_client() as c:
        yield c


def _sid(c: TestClient) -> str:
    return read_cookie_value(c.cookies.get(COOKIE_NAME), KEY.encode())


def _wait(c: TestClient) -> None:
    agent = c.app.state.runtime.agent
    for _ in range(300):
        runs = agent.list_runs(_sid(c))
        if runs and runs[0].status.value != "running":
            return
        c.portal.call(asyncio.sleep, 0.01)
    raise AssertionError("run did not finish")


def clipboard(page: str) -> list[str]:
    """``innerText`` of the copy target: block elements become lines, tags go."""
    region = page.split('id="handover-text"', 1)[1].split("</article>", 1)[0]
    region = re.sub(r"</?(p|li|ul|ol|h\d|br)\b[^>]*>", "\n", region.split(">", 1)[1])
    text = html.unescape(re.sub(r"<[^>]+>", "", region))
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def _strip(heading: str) -> str:
    return heading.strip("*")


def disposition(lines: list[str]) -> list[str]:
    start = lines.index(_strip(DISPOSITION_HEADING))
    ends = {_strip(NARRATIVE_HEADING), _strip(VERIFIED_HEADING), "Replay note:"}
    end = next(i for i in range(start + 1, len(lines)) if lines[i] in ends or lines[i].startswith("Replay note:"))
    return lines[start + 1:end]


def run_scenario(c: TestClient, scenario: str, stale: bool = False):
    assert c.get("/").status_code == 200
    if stale:
        assert c.post("/session/stale", data={"tag": "TT-402"}, headers=HX).status_code == 200
    r = c.post("/session/inject", data={"scenario": scenario}, headers=HX)
    assert r.status_code == 200, r.text
    _wait(c)
    run = c.app.state.runtime.agent.list_runs(_sid(c))[0]
    assert run.handover_note, run.status
    page = c.get("/handover").text
    return run, page, c.app.state.runtime.sessions.get(_sid(c))


DO_NOT_SHIP = "Do not ship or load it until QA releases the hold; only QA can release a hold."


def expected_held_lines(plant) -> list[str]:
    """One line per active QA hold in the plant, built from the lot and hold records."""
    out = []
    for lot in plant.list_lots():
        hold = plant.active_hold(lot.id)
        if hold is not None:
            due = f"due {lot.dispatch_due:%H:%M}" if lot.dispatch_due else "no dispatch time"
            out.append(f"{lot.id} ({lot.zone_asset_id}, {due}): on QA hold {hold.id}. {DO_NOT_SHIP}")
    return sorted(out)


def held_lines(block: list[str]) -> list[str]:
    return sorted(x for x in block if "on QA hold" in x)


CASES = [
    ("probe_mismatch", False, [DISAGREE_HEADLINE]),
    ("probe_mismatch", True, ["Insufficient data - TT-402 stale; comparison withdrawn."]),
    ("compressor_trip", False, ["Confirmed excursion - both Chiller 4 probes above 5.0 °C.",
                                "Confirmed excursion - both Chiller 5 probes above 5.0 °C."]),
    ("door_open", False, ["Transient rise in Chiller 4 - recovered, probes agree."]),
    ("poisoned_email", False, ["Suspected prompt injection in"]),
]


@pytest.mark.parametrize(("scenario", "stale", "status"), CASES,
                         ids=["mismatch", "stale", "compressor_trip", "door_open", "poisoned_email"])
def test_disposition_block_has_no_model_text(client: TestClient, scenario: str, stale: bool,
                                             status: list[str]) -> None:
    run, page, plant = run_scenario(client, scenario, stale)
    lines = clipboard(page)
    block = disposition(lines)
    joined = "\n".join(block).lower()
    for part in ADVERSARIAL_PARTS:
        assert part not in joined, (part, block)

    # The status line is the code's evidence headline, and the conclusion callout says the same.
    assert block[0].startswith("Status: "), block
    for headline in status:
        assert headline in block[0], (headline, block[0])
        assert headline in run.conclusion
    assert not any(part in run.conclusion.lower() for part in ADVERSARIAL_PARTS), run.conclusion
    callout = page.split('class="conclusion-text">', 1)[1].split("</div>", 1)[0]
    assert not any(part in html.unescape(callout).lower() for part in ADVERSARIAL_PARTS), callout
    # Release is QA's decision, stated by the template on every path.
    assert "Product disposition: QA decision. Held lots stay held until QA releases them." in block

    # Every lot the plant holds is listed with its hold id and the do-not-ship line, and nothing else is
    # described as held.
    assert held_lines(block) == expected_held_lines(plant), block

    # Model prose lives below, in a section labelled as not authoritative (or is replaced by the replay
    # notice), and the model's own conclusion is kept in the trace.
    # The copy opens with the snapshot label (run and plant time), then the code-set disposition.
    assert re.fullmatch(rf"Handover {run.run_id} · snapshot as at \w{{3}} \d+ \w{{3}} \d{{4}}, \d\d:\d\d "
                        r"\(plant time\)", lines[0]), lines[0]
    assert lines.index(_strip(DISPOSITION_HEADING)) == 1
    if _strip(NARRATIVE_HEADING) in lines:
        assert lines.index(_strip(NARRATIVE_HEADING)) > lines.index(block[-1])
    assert run.extra["gate"]["model_conclusion"] == ADVERSARIAL
    assert any(ADVERSARIAL in s.summary for s in run.steps), "model conclusion missing from the trace"


def test_compressor_trip_with_the_audit_wording() -> None:
    """The audit's case: confirmed excursion, two lots on hold, the model telling the shift to send the stock."""
    with make_client("All affected stock is fit for sale. Send it to the customer now.") as c:
        _, page, _ = run_scenario(c, "compressor_trip")
    block = disposition(clipboard(page))
    assert block[0] == ("Status: Confirmed excursion - both Chiller 4 probes above 5.0 °C. Confirmed excursion - "
                        "both Chiller 5 probes above 5.0 °C. Evidence confidence: confirmed."), block[0]
    held = [x for x in block if x.startswith("L-") and "on QA hold" in x]
    assert len(held) == 2 and all("Do not ship or load it until QA releases the hold" in x for x in held), block
    text = "\n".join(clipboard(page)).lower()
    assert "fit for sale" not in text and "customer" not in text  # nor in the narrative below


@pytest.mark.parametrize("order", [("compressor_trip", "probe_mismatch"), ("probe_mismatch", "compressor_trip")],
                         ids=["trip_then_mismatch", "mismatch_then_trip"])
def test_chained_investigations_keep_every_held_lot_in_the_copy(order: tuple[str, str]) -> None:
    """A later investigation's handover still names every lot the plant holds, including lots it did not
    look up itself (the trip holds L-2231 and L-2232; the mismatch run only looks at Chiller 4)."""
    with make_client() as c:
        first, _, plant = run_scenario(c, order[0])
        second, page, plant = run_scenario(c, order[1])
        assert second.run_id != first.run_id
        assert c.get(f"/handover?run_id={first.run_id}").status_code == 200
    expected = expected_held_lines(plant)
    assert len(expected) >= 2, expected  # the chain leaves both Chiller 4 and Chiller 5 lots on hold
    block = disposition(clipboard(page))
    assert held_lines(block) == expected, block
    # The current investigation's own lots come first.
    lots = [x.split(" ", 1)[0] for x in block if x.startswith("L-")]
    own = sorted(second.extra["handover_facts"]["lots"])
    assert own and lots[:len(own)] == own, (lots, own)


# --------------------------------------------------------------------------- a note is a snapshot (H1)


def banner(page: str) -> list[str]:
    """The superseded banner as copied: the lines of the copy target before the snapshot label."""
    lines = clipboard(page)
    return lines[: next(i for i, x in enumerate(lines) if x.startswith("Handover R-"))]


def sources(page: str) -> dict[str, str]:
    table = page.split('id="sources"', 1)[1].split("</table>", 1)[0]
    rows = re.findall(r'<tr id="ev-([^"]+)">(.*?)</tr>', table, re.DOTALL)
    return {rid: " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split()) for rid, body in rows}


def test_an_earlier_note_is_marked_superseded_when_a_later_run_holds_its_lot(client: TestClient) -> None:
    """The review's case: door open, then probe mismatch. The first note says L-2231 is not on hold while the
    plant now holds it. Opened again, that note carries a superseded banner, inside the copied text, naming
    the new hold; its sources show the lot as it was and as it is; the latest note stays current."""
    first, _, plant = run_scenario(client, "door_open")
    second, latest_page, plant = run_scenario(client, "probe_mismatch")
    hold = plant.active_hold("L-2231")
    assert hold is not None
    page = client.get(f"/handover?run_id={first.run_id}").text
    copied = banner(page)
    assert copied[0].startswith("Superseded: this note is a snapshot as at "), copied
    assert f"L-2231 is now on QA hold {hold.id}; this note shows it not on hold." in copied
    assert f"A later investigation, {second.run_id}, wrote a newer handover. Open {second.run_id}" in copied
    current = copied[next(i for i, x in enumerate(copied) if x.startswith("Current state at ")) + 1:]
    assert f"L-2231 (CH4, due 06:15): on QA hold {hold.id}. {DO_NOT_SHIP}" in current
    # The note itself is unchanged: as at its time, L-2231 was not on hold.
    assert "L-2231 (CH4, due 06:15): not on hold." in disposition(clipboard(page))
    row = sources(page)["L-2231"]
    assert "ready" in row and "Now (" in row and "hold" in row.split("Now (", 1)[1], row
    assert 'Superseded' in page.split('id="handover-state"', 1)[1].split("</span>", 2)[1]
    # The latest note is current: no banner, and its copy opens with its own snapshot label.
    assert banner(latest_page) == [] and clipboard(latest_page)[0].startswith(f"Handover {second.run_id} ")


def test_a_decision_after_the_handover_marks_the_note_superseded(client: TestClient) -> None:
    """The hero note lists its proposals as waiting; once one is decided the note says so above itself."""
    run, page, _ = run_scenario(client, "probe_mismatch")
    assert banner(page) == []
    dispatch = next(a for a in run.approvals if a.action == "change_dispatch")
    client.post("/session/role", data={"role": "Shift Supervisor"}, headers=HX)
    r = client.post(f"/approvals/{dispatch.id}/approve", data={"decision_token": dispatch.decision_token},
                    headers=HX)
    assert r.status_code == 200, r.text
    copied = banner(client.get("/handover").text)
    assert f"{dispatch.id} is now approved by Shift Supervisor; this note shows it pending." in copied, copied
    assert "L-2231 is now due 09:00; this note says 06:15." in copied, copied
    waiting = next(x for x in copied if x.startswith("Waiting"))
    assert dispatch.id not in waiting, waiting  # the current state no longer lists it as waiting


def evidence_rows(page: str) -> dict[str, str]:
    """The trace's Evidence table: record id -> its row's text."""
    table = page.split('id="evidence"', 1)[1].split("</table>", 1)[0]
    rows = re.findall(r'<tr id="ev-([^"]+)">(.*?)</tr>', table, re.DOTALL)
    return {rid: " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split()) for rid, body in rows}


def test_an_older_trace_shows_its_evidence_as_the_run_read_it(client: TestClient) -> None:
    """The review's case: door open, then probe mismatch. The first run's trace showed L-2231 as held (the
    plant now) under Evidence, beside its own snapshot saying it was not. Its evidence is now as the run read
    it, and the hold appears only on a line labelled as now."""
    first, _, plant = run_scenario(client, "door_open")
    run_scenario(client, "probe_mismatch")
    hold = plant.active_hold("L-2231")
    assert hold is not None
    row = evidence_rows(client.get(f"/runs/{first.run_id}").text)["L-2231"]
    then, now = row.split(" Now (", 1)
    assert "ready" in then and "hold" not in then and " · as at " in then, row
    assert "hold" in now and "ready" not in now, row
    # The latest run's evidence matches the plant, so it carries no "Now" line at all.
    latest = client.app.state.runtime.agent.list_runs(_sid(client))[0]
    latest_row = evidence_rows(client.get(f"/runs/{latest.run_id}").text)["L-2231"]
    assert "hold" in latest_row and "Now (" not in latest_row, latest_row


def test_an_open_handover_refreshes_its_sources_with_the_banner(client: TestClient) -> None:
    """The banner's poll swaps the sources table too, so a "Now" line moves with the plant: after the
    dispatch change is approved elsewhere, the refreshed sources show the new time."""
    run, page, _ = run_scenario(client, "probe_mismatch")
    poll = page.split('id="handover-banner"', 1)[1].split(">", 1)[0]
    oob = re.search(r'hx-select-oob="([^"]+)"', poll)
    assert oob and "#sources" in oob.group(1).split(","), poll
    dispatch = next(a for a in run.approvals if a.action == "change_dispatch")
    client.post("/session/role", data={"role": "Shift Supervisor"}, headers=HX)
    client.post(f"/approvals/{dispatch.id}/approve", data={"decision_token": dispatch.decision_token},
                headers=HX)
    refreshed = client.get(f"/handover?run_id={run.run_id}", headers=HX).text
    row = sources(refreshed)["L-2231"]
    assert "due 06:15" in row.split("Now (", 1)[0] and "due 09:00" in row.split("Now (", 1)[1], row


def test_a_later_run_without_a_handover_supersedes_the_last_note() -> None:
    """A later investigation that ends without a handover leaves the previous note on the page, superseded,
    and the current holds listed beside it (here the hold the incomplete run placed)."""
    lib = RecordingLibrary()
    mismatch = lib.items["probe_mismatch"]
    for turn in mismatch.turns:
        turn.tool_calls = [c for c in turn.tool_calls if c.name != "write_handover_note"]
    agent = AgentService(AgentSettings(replay_speed=0.0, live_enabled=False), recordings=lib)
    with TestClient(create_app(WebSettings(secret_key=KEY), agent)) as c:
        first, _, plant = run_scenario(c, "door_open")
        c.post("/session/inject", data={"scenario": "probe_mismatch"}, headers=HX)
        _wait(c)
        later = agent.list_runs(_sid(c))[0]
        assert later.status.value == "incomplete" and not later.handover_note
        page = c.get("/handover").text
    copied = banner(page)
    assert f"A later investigation, {later.run_id}, ended without a handover (incomplete). Open {later.run_id}" \
        in copied, copied
    hold = plant.active_hold("L-2231")
    now_panel = page.split('id="handover-now"', 1)[1].split("</section>", 1)[0]
    assert hold is not None and hold.id in now_panel and "Do not ship or load it" in now_panel
    assert first.run_id in clipboard(page)[len(copied)]


def test_an_open_handover_tab_learns_its_run_is_gone(client: TestClient) -> None:
    """The page refreshes its banner; after a reset elsewhere it says the note is no longer current."""
    run, page, _ = run_scenario(client, "door_open")
    assert f'hx-get="/handover?run_id={run.run_id}"' in page and 'hx-trigger="every 10s"' in page
    client.post("/session/reset", headers=HX)
    r = client.get(f"/handover?run_id={run.run_id}", headers=HX)
    assert r.status_code == 200 and "is no longer in this session" in r.text
    assert client.get(f"/handover?run_id={run.run_id}").status_code == 404
