"""Turn one rendered component into an inert ``/styleguide`` sample.

The styleguide puts real components side by side, often several of the same kind. Rendered unchanged they
would repeat element ids and keep their links and controls: a sample approval card would still link to
``/runs/R-...``, which in the visitor's own session can be a different run. :func:`inert` rewrites the
markup of one rendered instance so that, without relying on any client-side script:

* every id is scoped under a prefix, together with the references to it (``aria-labelledby``, ``for``,
  SVG ``href="#..."`` and ``url(#...)``), so instances never collide with each other or with the page;
* links keep their look but lose their ``href`` and are marked ``aria-disabled``;
* nothing sends a request: htmx and SSE attributes are dropped, forms lose ``action`` and ``method``,
  buttons become ``type="button"`` and form fields are disabled;
* nothing takes keyboard focus or claims to be a button (``tabindex`` and ``role="button"`` are dropped),
  and the page script's hooks (``data-copy``, ``data-dismiss``) are removed.

Only start tags change; text, comments and end tags are copied byte for byte.
"""

from __future__ import annotations

import re
from html import escape, unescape
from html.parser import HTMLParser

from markupsafe import Markup

_ATTR = re.compile(r"""(\s+)([^\s"'>/=]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'=<>`]+))?""")
_TAG = re.compile(r"<([^\s/>]+)(.*?)(\s*/?>)\Z", re.DOTALL)
_URL_REF = re.compile(r"url\(#([^)\s]+)\)")
_ID_LISTS = frozenset({"aria-labelledby", "aria-describedby", "aria-controls", "aria-owns", "for", "list"})
_DROPPED = frozenset({"tabindex", "action", "method", "formaction", "data-copy", "data-dismiss"})
_DROPPED_PREFIXES = ("hx-", "sse-", "on")
_FIELDS = frozenset({"input", "select", "textarea"})


class _StartTags(HTMLParser):
    """Every start tag in ``source``: its offsets, name and attributes."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.found: list[tuple[int, int, str, dict[str, str | None]]] = []
        self._line_starts = [0] + [m.end() for m in re.finditer("\n", source)]
        self.feed(source)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line, col = self.getpos()
        start = self._line_starts[line - 1] + col
        self.found.append((start, start + len(self.get_starttag_text() or ""), tag, dict(attrs)))

    handle_startendtag = handle_starttag


def inert(html: str, prefix: str) -> Markup:
    """``html`` (one rendered component) as a sample: ids scoped under ``prefix``, no navigation, no requests."""
    source = str(html)
    tags = _StartTags(source).found
    local = {attrs["id"] for _, _, _, attrs in tags if attrs.get("id")}
    out, pos = [], 0
    for start, end, tag, _ in tags:
        out += [source[pos:start], _rewrite(source[start:end], tag, prefix, local)]
        pos = end
    out.append(source[pos:])
    return Markup("".join(out))


def _rewrite(raw: str, tag: str, prefix: str, local: set[str]) -> str:
    m = _TAG.match(raw)
    if m is None:
        return raw
    name, body, close = m.groups()

    def scoped(i: str) -> str:
        return f"{prefix}-{i}" if i in local else i

    attrs, seen = [], set()
    for a in _ATTR.finditer(body):
        space, attr, quoted = a.groups()
        n = attr.lower()
        seen.add(n)
        value = unescape(quoted[1:-1] if quoted and quoted[0] in "\"'" else quoted) if quoted else None
        if n in _DROPPED or n.startswith(_DROPPED_PREFIXES) or (tag == "a" and n in ("href", "xlink:href")):
            continue
        if n == "role" and value == "button":  # a clickable mimic unit: here it is only a picture of one
            continue
        if value is not None:
            if n == "id":
                value = f"{prefix}-{value}"
            elif n in _ID_LISTS:
                value = " ".join(scoped(i) for i in value.split())
            elif n in ("href", "xlink:href") and value.startswith("#"):
                value = "#" + scoped(value[1:])
            elif tag == "button" and n == "type":
                value = "button"
            value = _URL_REF.sub(lambda u: f"url(#{scoped(u.group(1))})", value)
        attrs.append(space + attr + ("" if value is None else f'="{escape(value, quote=True)}"'))
    if tag == "a" and "aria-disabled" not in seen:
        attrs.append(' aria-disabled="true"')
    if tag == "button" and "type" not in seen:
        attrs.append(' type="button"')
    if tag in _FIELDS and "disabled" not in seen:
        attrs.append(" disabled")
    return f"<{name}{''.join(attrs)}{close}"
