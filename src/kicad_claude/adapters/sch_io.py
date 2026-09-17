"""Read and write KiCAD `.kicad_sch` / `.kicad_pcb` s-expression files.

Parses with `sexpdata`. Writes with a custom pretty-printer that mimics
KiCAD's tab-indented multi-line layout. KiCAD will accept any well-formed
s-expression and reformats on next save, but our pretty output makes diffs
and debugging easier.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import sexpdata

# KiCAD wraps its own output at this column (a tab counts as one column).
# 118 is the longest line KiCAD 10 emits in its own demo schematics.
LINE_WIDTH = 118

# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_file(path: Path) -> list:
    """Parse a KiCAD s-expression file. Returns the top-level list."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return sexpdata.loads(text)


# --------------------------------------------------------------------------- #
# Pretty-printer
# --------------------------------------------------------------------------- #


def dumps(node: Any, indent: int = 0, path: tuple[str, ...] = ()) -> str:
    """Serialize a parsed s-expression tree the way KiCAD 10 writes it.

    Rules (measured against KiCAD 10's own output — see
    `docs/issue8_formatting_probe.md`):
      - Atom-only lists are inline:               (at 39.37 29.21 0)
      - An inline list longer than `LINE_WIDTH` wraps onto continuation lines
        indented one level, with the closing `)` on its own line. That is how
        `(members ...)` and the base64 chunks of `(data ...)` are written.
      - `(pts ...)` keeps its head alone and packs the points, several to a
        line, wrapping at the same width.
      - A sheet's background alpha is written with four decimals:
        `(sheet ... (fill (color 0 0 0 0.0000)))`.
      - Other lists with sublists put head + leading atoms on the first line,
        then each child on its own indented line.
      - Tab indentation, one tab per level (a tab counts as one column).
      - Closing `)` on its own line at parent indent.

    `path` carries the heads of the enclosing nodes; callers pass nothing.
    """
    if not isinstance(node, list):
        return _atom(node)
    if not node:
        return "()"

    inner_pad = "\t" * (indent + 1)
    outer_pad = "\t" * indent
    head = head_of(node)
    child_path = path + (head,) if head else path

    # Find first child (after the head) that is itself a list.
    first_list_idx: int | None = None
    for i in range(1, len(node)):
        if isinstance(node[i], list):
            first_list_idx = i
            break

    if first_list_idx is None:
        # All atoms — one line, wrapped if it gets too wide.
        if head == "color" and path[-2:] == ("sheet", "fill"):
            return _sheet_fill_color(node)
        head_text = _atom(node[0])
        items = [_atom(c) for c in node[1:]]
        if head == "data" and len(items) > 1:
            # Embedded-file base64: KiCAD writes exactly one chunk per line.
            body = "\n".join([f"({head_text} {items[0]}"] + [inner_pad + it for it in items[1:]])
            return body + "\n" + outer_pad + ")"
        single = "(" + " ".join([head_text, *items]) + ")"
        if not items or len(outer_pad) + len(single) <= LINE_WIDTH:
            return single
        lines = _pack(outer_pad + "(" + head_text, items, inner_pad, LINE_WIDTH)
        lines[0] = lines[0][len(outer_pad):]
        return "\n".join(lines) + "\n" + outer_pad + ")"

    if head == "pts" and all(head_of(c) == "xy" for c in node[1:]):
        # KiCAD keeps the head alone and packs the points, wrapping at the width.
        items = [dumps(c, indent + 1, child_path) for c in node[1:]]
        lines = _pack(inner_pad, items, inner_pad, LINE_WIDTH)
        return "(pts\n" + "\n".join(lines) + "\n" + outer_pad + ")"

    leading = " ".join(_atom(c) for c in node[:first_list_idx])
    rest = node[first_list_idx:]

    lines = ["(" + leading]
    for child in rest:
        if isinstance(child, list):
            lines.append(inner_pad + dumps(child, indent + 1, child_path))
        else:
            lines.append(inner_pad + _atom(child))
    lines.append(outer_pad + ")")
    return "\n".join(lines)


def _sheet_fill_color(node: list) -> str:
    """`(color r g b a)` of a sheet's fill — KiCAD prints the alpha as %.4f."""
    rgb = " ".join(_atom(c) for c in node[1:4])
    alpha = float(node[4]) if len(node) >= 5 else 0.0
    return f"(color {rgb} {alpha:.4f})"


def _pack(first: str, items: list[str], pad: str, width: int) -> list[str]:
    """Lay `items` out on as few lines of `width` columns as fit.

    `first` is the opening line's text before the items — fully indented, either
    the head token (`\\t\\t(members`) or just the continuation padding. Every
    later line starts at `pad`. A single item always gets a line, however long.
    """
    lines: list[str] = []
    current = first
    empty = not current.strip()
    for item in items:
        candidate = current + item if empty else current + " " + item
        if not empty and len(candidate) > width:
            lines.append(current)
            current = pad + item
        else:
            current = candidate
        empty = False
    lines.append(current)
    return lines


def write_file(path: Path, tree: list) -> None:
    """Write `tree` to `path` (KiCAD-style formatting + trailing newline)."""
    Path(path).write_text(dumps(tree) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Atom serialization
# --------------------------------------------------------------------------- #


def _atom(x: Any) -> str:
    if isinstance(x, sexpdata.Symbol):
        return str(x)
    if isinstance(x, bool):  # must precede int — bool is an int subclass
        return "yes" if x else "no"
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
    """Format a float the way KiCAD does: trim trailing zeros, keep no `e`-notation."""
    if math.isnan(x) or math.isinf(x):
        return repr(x)
    if x == 0.0:
        return "0"
    if x == int(x) and abs(x) < 1e15:
        # Integer-valued floats: keep as integer-style "5", not "5.0".
        # KiCAD writes pin angles as `0`, `90` (no decimal).
        return str(int(x))
    # Shortest representation that round-trips, like KiCAD's own output
    # (e.g. `59.209102362204725` stays intact instead of being truncated).
    return repr(x)


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
