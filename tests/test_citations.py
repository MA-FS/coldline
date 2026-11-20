"""Citations: one grammar for validating a handover, recording its sources and showing them on the page."""

from __future__ import annotations

import asyncio
import re

from fastapi.testclient import TestClient

from coldline.agent import AgentService, AgentSettings
from coldline.citations import cited_ids
from coldline.main import create_app
from coldline.web.security import COOKIE_NAME, read_cookie_value
from coldline.web.settings import WebSettings
from coldline.web.views import cite_chips, mini_markdown

HX = {"X-ColdLine": "1", "HX-Request": "true"}
KEY = "k" * 32


def test_a_bare_asset_id_is_a_citation() -> None:
    assert cited_ids("Compressor [C2] tripped; [CH4] and [BONING] follow.") == ["C2", "CH4", "BONING"]


def test_a_hyphenated_lot_id_is_a_citation() -> None:
    assert cited_ids("Hold [L-2231] on [QH-0001], probes [TT-401] and [EV4-SUP].") == [
        "L-2231", "QH-0001", "TT-401", "EV4-SUP"]


def test_an_inline_markdown_link_is_not_a_citation() -> None:
    assert cited_ids("See [TT-401](https://example.com) and [CH4](#ev-CH4).") == []
    assert cited_ids("[TT-401](https://example.com) then [TT-402]") == ["TT-402"]


def test_a_reference_style_link_label_reads_as_a_citation() -> None:
    """Only inline links are excluded; a reference-style label is checked like any other citation."""
    assert cited_ids("Probe [TT-401][1] drifted.\n\n[1]: https://example.com") == ["TT-401"]


def test_ids_are_listed_once_in_order_and_other_brackets_are_text() -> None:
    md = ("See [TT-401] and [L-2231], [WO-1042] twice [WO-1042]; [link](http://x) and [ ] and [x], "
          "[tt-401], [TT-401.PV], [TT-401-], [see TT-401], [-L-1].")
    assert cited_ids(md) == ["TT-401", "L-2231", "WO-1042"]


def test_chips_follow_the_same_grammar() -> None:
    html = str(cite_chips("Asset [CH4], lot [L-2231], link [TT-401](https://x), text [TT-401.PV]."))
    assert re.findall(r'href="#ev-([^"]+)"', html) == ["CH4", "L-2231"]
    assert "[TT-401](https://x)" in html and "[TT-401.PV]" in html
    assert 'class="cite cite--asset"' in html and 'class="cite cite--lot"' in html


def test_chips_are_rendered_after_escaping() -> None:
    html = str(mini_markdown('<script>alert("[CH4]")</script> [CH4]'))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert html.count('href="#ev-CH4"') == 2


def _finished_run(c: TestClient, scenario: str):
    assert c.post("/session/inject", data={"scenario": scenario}, headers=HX).status_code == 200
    sid = read_cookie_value(c.cookies.get(COOKIE_NAME), KEY.encode())
    agent = c.app.state.runtime.agent
    for _ in range(300):
        runs = agent.list_runs(sid)
        if runs and runs[0].status.value != "running":
            return runs[0]
        c.portal.call(asyncio.sleep, 0.01)
    raise AssertionError("run did not finish")


def test_the_handover_page_lists_every_cited_record_including_a_bare_asset() -> None:
    agent = AgentService(AgentSettings(replay_speed=0.0, live_enabled=False))
    with TestClient(create_app(WebSettings(secret_key=KEY), agent)) as c:
        assert c.get("/").status_code == 200
        run = _finished_run(c, "compressor_trip")
        cited = cited_ids(run.handover_note)
        assert "C2" in cited and "L-2231" in cited
        assert set(cited) <= set(run.extra["handover_snapshot"]["sources"])

        page = c.get(f"/handover?run_id={run.run_id}").text
        table = page.split('id="sources"', 1)[1].split("</table>", 1)[0]
        assert re.findall(r'<tr id="ev-([^"]+)"', table) == cited
        c2 = table.split('<tr id="ev-C2"', 1)[1].split("</tr>", 1)[0]
        assert "Compressor 2" in c2
