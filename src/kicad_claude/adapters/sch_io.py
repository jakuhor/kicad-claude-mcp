"""Read and write KiCAD `.kicad_sch` / `.kicad_pcb` s-expression files.

Parses with `sexpdata`. Writes with a custom pretty-printer that mimics
KiCAD's tab-indented multi-line layout. KiCAD will accept any well-formed
s-expression and reformats on next save, but our pretty output makes diffs
and debugging easier.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import sexpdata

from kicad_claude.adapters import kicad_prettify

# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_file(path: Path) -> list:
    """Parse a KiCAD s-expression file. Returns the top-level list.

    A truncated or empty file raises `ValueError` naming the file. `sexpdata`
    signals an empty input with a bare `AssertionError` and unbalanced parens
    with its own `ExpectClosingBracket`; neither says which file failed.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise ValueError(f"{path} is empty — not a KiCAD file")
    try:
        parsed = _RawNumberParser(text).parse()
        if len(parsed) != 1:
            raise ValueError(f"expected one top-level s-expression, got {len(parsed)}")
        tree = parsed[0]
    except Exception as exc:  # sexpdata raises several unrelated types
        raise ValueError(f"{path} is not a well-formed s-expression: {exc}") from exc
    if not isinstance(tree, list) or not tree:
        raise ValueError(f"{path} has no top-level s-expression")
    return tree


# --------------------------------------------------------------------------- #
# Number atoms that remember their spelling
# --------------------------------------------------------------------------- #


class RawFloat(float):
    """A float that remembers the text it was read from.

    KiCAD 10 spells a number per field: `FormatDouble2Str` and
    `FormatInternalUnits` print `{:.10g}`, `{:.10f}` below 0.0001, and keep a
    negative zero as `-0`. Re-deriving that per field would need precision
    knowledge from all of `pcbnew/`, so an atom we never touched is written
    back verbatim instead. A mutated value becomes a plain `float` and goes
    through `_format_float`.
    """

    def __new__(cls, value: float, raw: str) -> "RawFloat":
        self = super().__new__(cls, value)
        self.raw = raw
        return self

    def __reduce__(self):  # copy / deepcopy / pickle keep the spelling
        return (RawFloat, (float(self), self.raw))


class RawInt(int):
    """An int that remembers the text it was read from — see `RawFloat`.

    `-0` matters here: `int("-0")` is `0`, and KiCAD writes the sign back.
    """

    def __new__(cls, value: int, raw: str) -> "RawInt":
        self = super().__new__(cls, value)
        self.raw = raw
        return self

    def __reduce__(self):
        return (RawInt, (int(self), self.raw))


class _RawNumberParser(sexpdata.Parser):
    """`sexpdata.Parser` that wraps numeric atoms in `RawFloat` / `RawInt`."""

    def atom(self, token: str) -> Any:
        value = super().atom(token)
        if isinstance(value, bool):  # must precede int — bool is an int subclass
            return value
        if isinstance(value, int):
            return RawInt(value, token)
        if isinstance(value, float):
            return RawFloat(value, token)
        return value


# --------------------------------------------------------------------------- #
# Pretty-printer
# --------------------------------------------------------------------------- #


def dumps(node: Any) -> str:
    """Serialize a parsed s-expression tree the way KiCAD 10 writes it.

    Two steps, matching KiCAD's own: serialize the tree flat, then run it
    through the port of KiCAD's prettifier in `kicad_prettify.py`. That is how
    KiCAD does it — `OUTPUTFORMATTER` writes the tokens and
    `PRETTIFIED_FILE_OUTPUTFORMATTER` lays them out — so our layout matches by
    construction instead of by measured rules.

    `FORMAT_MODE.NORMAL` is what KiCAD uses for schematics and boards unless the
    `CompactSave` advanced setting is on, and it is off by default.
    """
    return kicad_prettify.prettify(dumps_flat(node), kicad_prettify.FormatMode.NORMAL)


def dumps_flat(node: Any) -> str:
    """Serialize the tree to one line, tokens separated by single spaces.

    The prettifier inserts every newline, so this only has to produce
    well-formed text with the atoms spelled the way KiCAD spells them.
    """
    if not isinstance(node, list):
        return _atom(node)
    if not node:
        return "()"
    return "(" + " ".join(dumps_flat(c) for c in node) + ")"


def detect_newline(path: Path) -> str:
    """The line ending `path` already uses: `\\r\\n` or `\\n`.

    KiCAD writes CRLF on Windows and LF elsewhere, and a file may travel
    between the two. A file that does not exist yet, or holds no newline at
    all, gets the platform default — what KiCAD itself would write here.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return os.linesep
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf and not lf:
        return "\r\n"
    if lf and not crlf:
        return "\n"
    if crlf or lf:
        # Mixed: keep whichever dominates, so the diff stays small.
        return "\r\n" if crlf >= lf else "\n"
    return os.linesep


def write_file(path: Path, tree: list) -> None:
    """Write `tree` to `path` (KiCAD-style formatting + trailing newline).

    The file's existing line ending is preserved. `Path.write_text` would
    translate every `\\n` to `os.linesep`, which rewrites an LF file to CRLF
    on Windows — a whole-file diff for a one-line edit (issue 8).
    """
    p = Path(path)
    newline = detect_newline(p)
    # `dumps` already ends with the trailing newline KiCAD writes.
    text = dumps(tree)
    if newline != "\n":
        text = text.replace("\n", newline)
    p.write_bytes(text.encode("utf-8"))


# --------------------------------------------------------------------------- #
# Atom serialization
# --------------------------------------------------------------------------- #


def _atom(x: Any) -> str:
    if isinstance(x, sexpdata.Symbol):
        return str(x)
    if isinstance(x, bool):  # must precede int — bool is an int subclass
        return "yes" if x else "no"
    if isinstance(x, (RawFloat, RawInt)):
        return x.raw
    if isinstance(x, str):
        return '"' + _escape(x) + '"'
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        return _format_float(x)
    return str(x)


def _escape(s: str) -> str:
    """Backslash-escape a KiCAD string literal.

    `sexpdata` decodes `\\n`, `\\r` and `\\t` into real control characters on
    read, so newlines must be re-escaped on write. A raw newline inside a quoted
    string makes KiCAD refuse to load the file ("Failed to load schematic").

    A TAB is left raw, because that is what KiCAD itself writes (see the
    "Part Description" properties in its CM5 demo). KiCAD does decode a `\\t`
    escape, so escaping it would also be safe — it would just differ from
    KiCAD's own output on the next diff.
    """
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _format_float(x: float) -> str:
    """Format a float the way KiCAD 10 does.

    Port of `EDA_UNIT_UTILS::FormatInternalUnits` (`common/eda_units.cpp`) and
    `FormatDouble2Str` (`common/string_utils.cpp`), which share one rule:
    `{:.10g}`, except below 0.0001 where `%g` would switch to an exponent and
    KiCAD prints `{:.10f}` with the trailing zeros trimmed. `{:.10g}` keeps an
    integer-valued float integer-style (`90`, not `90.0`) and a negative zero
    as `-0`, both of which KiCAD writes.
    """
    if math.isnan(x) or math.isinf(x):
        return repr(x)
    if x != 0.0 and abs(x) <= 0.0001:
        buf = f"{x:.10f}".rstrip("0")
        return buf[:-1] if buf.endswith(".") else buf
    return f"{x:.10g}"


# --------------------------------------------------------------------------- #
# Tree-walking helpers
# --------------------------------------------------------------------------- #


def is_call(node: Any, head: str) -> bool:
    """True if `node` is `(head ...)` — a list whose first elem is the symbol `head`."""
    return (
        isinstance(node, list)
        and len(node) > 0
        and isinstance(node[0], sexpdata.Symbol)
        and str(node[0]) == head
    )


def head_of(node: Any) -> str | None:
    if isinstance(node, list) and node and isinstance(node[0], sexpdata.Symbol):
        return str(node[0])
    return None


def find_child(node: list, head: str) -> list | None:
    """Return the first direct child of `node` that is `(head ...)`, or None."""
    for c in node[1:] if node else []:
        if is_call(c, head):
            return c
    return None


def find_children(node: list, head: str) -> list:
    """Return all direct children of `node` that are `(head ...)`."""
    return [c for c in (node[1:] if node else []) if is_call(c, head)]


def sym(name: str) -> sexpdata.Symbol:
    """Convenience: wrap a Python string as a sexpdata.Symbol."""
    return sexpdata.Symbol(name)


def find_deep(node: Any, head: str) -> list:
    """Return every `(head ...)` node at any depth, outermost first."""
    acc: list = []
    _find_deep_acc(node, head, acc)
    return acc


def _find_deep_acc(node: Any, head: str, acc: list) -> None:
    if not isinstance(node, list):
        return
    if is_call(node, head):
        acc.append(node)
    for child in node:
        _find_deep_acc(child, head, acc)


def is_symbol(x: Any, name: str) -> bool:
    """True if `x` is the bare token `name` — e.g. the `private` in a property."""
    return isinstance(x, sexpdata.Symbol) and str(x) == name


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #


def property_name_index(prop: list) -> int:
    """Index of the *name* atom in a `(property ...)` node.

    KiCAD 9+ may emit `(property private "Name" "Value" ...)`, which shifts
    name and value one place right. The value sits at the returned index + 1.
    """
    return 2 if len(prop) > 1 and is_symbol(prop[1], "private") else 1


def get_property(node: list, name: str) -> str | None:
    """Value of the `(property ... "name" "value")` child of `node`, exact case."""
    for prop in find_children(node, "property"):
        i = property_name_index(prop)
        if len(prop) > i + 1 and prop[i] == name and isinstance(prop[i + 1], str):
            return prop[i + 1]
    return None


def get_properties(node: list) -> dict[str, str]:
    """Every property of `node`, keyed by lower-cased name for loose lookup."""
    out: dict[str, str] = {}
    for prop in find_children(node, "property"):
        i = property_name_index(prop)
        if len(prop) > i + 1 and isinstance(prop[i], str) and isinstance(prop[i + 1], str):
            out[prop[i].lower()] = prop[i + 1]
    return out


def has_flag(node: list, flag: str) -> bool:
    """True if `node` carries `flag`, in any of the three forms KiCAD uses.

    - bare token, KiCAD 5-7:   `(pin ... hide ...)`
    - boolean, post-20241004:  `(pin ... (hide yes) ...)`
    - explicitly off:          `(pin ... (hide no) ...)` -> False
    """
    for child in node:
        if is_symbol(child, flag):
            return True
        if is_call(child, flag) and len(child) >= 2:
            return str(child[1]).lower() in ("yes", "true")
    return False
