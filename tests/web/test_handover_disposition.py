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
    assert lines.index(_strip(DISPOSITION_HEADING)) == 0
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
