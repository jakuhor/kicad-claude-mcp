"""KiCAD `{brace}` escape codec for user-visible names.

KiCAD stores characters that would be ambiguous inside a name as brace
sequences: a local net `VBUS/5V` is written `VBUS{slash}5V`. The table is
KiCAD's own, from `common/string_utils.cpp` (`EscapeString` /
`UnescapeString`). It applies to net names, label text, sheet names,
symbol and footprint names, and field values.

These functions are for the *boundary* only — decoding a stored name before
showing it, and comparing a name a caller typed against a stored one. The
parsed tree keeps the escaped form so that a write round-trips byte for byte
(see `adapters/sch_io.py`).
"""

from __future__ import annotations

import re

# common/string_utils.cpp
_BRACE_ESCAPES: dict[str, str] = {
    "{dblquote}": '"',
    "{quote}": "'",
    "{lt}": "<",
    "{gt}": ">",
    "{backslash}": "\\",
    "{slash}": "/",
    "{bar}": "|",
    "{colon}": ":",
    "{space}": " ",
    "{amp}": "&",
    "{tab}": "\t",
    "{newline}": "\n",
    "{return}": "\r",
    "{brace}": "{",
}

# Inverse table. `{brace}` is listed first on encode so that a literal `{`
# is escaped before the sequences that introduce one.
_CHAR_ESCAPES: dict[str, str] = {"{": "{brace}"} | {
    char: seq for seq, char in _BRACE_ESCAPES.items() if char != "{"
}

_BRACE_RE = re.compile(r"\{[a-z]+\}")


def unescape_braces(s: str) -> str:
    """Decode KiCAD `{brace}` sequences: `VBUS{slash}5V` -> `VBUS/5V`.

    An unknown sequence is left alone, so a name that legitimately contains
    `{foo}` survives.
    """
    if "{" not in s:
        return s
    return _BRACE_RE.sub(lambda m: _BRACE_ESCAPES.get(m.group(0), m.group(0)), s)


def escape_braces(s: str) -> str:
    """Encode every escapable character: `VBUS/5V` -> `VBUS{slash}5V`.

    Context-agnostic, unlike KiCAD's own `EscapeString`, which escapes a
    different subset per field type. Use it to build a name, never to rewrite
    one that was read from a file — that would escape characters KiCAD had
    left bare. For matching a caller's name against a stored one, use
    `normalize_name` on both sides instead.
    """
    return "".join(_CHAR_ESCAPES.get(c, c) for c in s)


def normalize_name(s: str) -> str:
    """Reduce a name to its comparable form.

    Decoding both sides matches a caller who typed the decoded name
    (`VBUS/5V`) *and* one who copied the escaped form out of the file
    (`VBUS{slash}5V`), without ever escaping more than KiCAD did.
    """
    return unescape_braces(s)
