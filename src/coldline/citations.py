"""How agent text cites plant records. The one definition every reader of a citation uses.

A citation is a record id in square brackets. A record id is one or more segments of uppercase letters and
digits, joined by single hyphens, starting with a letter. That covers every id the plant issues:

* bare asset ids: ``[CH4]``, ``[C2]``, ``[BONING]``
* hyphenated ids: ``[TT-401]``, ``[EV4-SUP]``, ``[L-2231]``, ``[WO-1042]``, ``[QH-0001]``, ``[AP-0002]``

Anything else in brackets is plain text: lowercase, dots, spaces or a trailing hyphen (``[tt-401]``,
``[TT-401.PV]``, ``[see TT-401]``). An inline Markdown link ``[TT-401](https://...)`` is not a citation,
not even its label, so a link cannot pass for verified evidence or be shown as a record chip. Other link
forms are not special-cased: the label in a reference-style link ``[TT-401][1]`` reads as a citation, and
like every citation it passes validation only if a tool in the run returned exactly that id.

Validating a handover (every cited id must have been returned by a tool in the run), recording what the
note was composed from, and showing its sources and chips on the page all use :func:`cited_ids` or
:func:`replace_citations`, so what was checked is exactly what is shown.
"""

from __future__ import annotations

import re
from collections.abc import Callable

RECORD_ID = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*"
CITATION = re.compile(rf"\[({RECORD_ID})\](?!\()")


def cited_ids(text: str) -> list[str]:
    """The record ids cited in ``text``, in order of first citation, each once."""
    return list(dict.fromkeys(CITATION.findall(text)))


def replace_citations(text: str, render: Callable[[str], str]) -> str:
    """``text`` with each citation replaced by ``render(record_id)``.

    For HTML, escape ``text`` first: escaping leaves brackets, uppercase letters, digits and hyphens as
    they are, so the same citations are found, and ``render`` escapes what it emits."""
    return CITATION.sub(lambda m: render(m.group(1)), text)
