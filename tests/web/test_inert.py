"""The rewrite that turns a rendered component into an inert styleguide sample."""

from __future__ import annotations

from coldline.web.inert import inert


def test_ids_and_references_to_them_are_scoped_per_instance() -> None:
    html = ('<section id="agent-panel" aria-labelledby="agent-h other"><h2 id="agent-h">Agent</h2>'
            '<svg><symbol id="m-fan"/><use href="#m-fan"/><rect fill="url(#m-hatch)"/>'
            '<path marker-end="url(#m-fan)"/></svg><label for="x">X</label></section>')
    out = inert(html, "s1")
    assert 'id="s1-agent-panel"' in out and 'id="s1-agent-h"' in out
    assert 'aria-labelledby="s1-agent-h other"' in out  # ids defined elsewhere are left alone
    assert 'href="#s1-m-fan"' in out and 'marker-end="url(#s1-m-fan)"' in out
    assert 'fill="url(#m-hatch)"' in out and 'for="x"' in out


def test_links_keep_their_text_but_do_not_navigate() -> None:
    out = inert('<a class="ev-chip" href="/runs/R-7841" title="Run">R-7841</a><a href="#ev-L-1">L-1</a>', "s")
    assert out == ('<a class="ev-chip" title="Run" aria-disabled="true">R-7841</a>'
                   '<a aria-disabled="true">L-1</a>')


def test_nothing_sends_a_request_or_takes_focus() -> None:
    html = ('<section hx-ext="sse" sse-connect="/runs/R-1/stream" sse-close="done">'
            '<form action="/approvals/AP-1/approve" method="post" hx-post="/x"><input name="note">'
            '<button hx-post="/approvals/AP-1/approve" hx-target="#approval-AP-1">Approve</button>'
            '<button type="submit">Go</button></form>'
            '<g class="m-unit" tabindex="0" role="button" aria-pressed="true" onclick="go()"></g>'
            '<button type="button" data-dismiss data-copy="#x">x</button></section>')
    out = inert(html, "s")
    for gone in ("hx-", "sse-", "action=", "method=", "tabindex", 'role="button"', "onclick", "data-"):
        assert gone not in out, gone
    assert out.count('type="button"') == 3 and "<input name=\"note\" disabled>" in out
    assert 'aria-pressed="true"' in out


def test_text_comments_and_escaping_are_preserved() -> None:
    html = '<p title="a &amp; b" id="p">x -&gt; y &copy; <!-- note --></p><svg viewBox="0 0 1 1"></svg>'
    assert inert(html, "s") == '<p title="a &amp; b" id="s-p">x -&gt; y &copy; <!-- note --></p><svg viewBox="0 0 1 1"></svg>'
