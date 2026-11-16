"""Schematic geometry: where a placed symbol's body, pins and texts land.

Shared by pin-addressed connection (`connect`), field autoplace and the
readability check. Coordinates are sheet millimetres, Y down (KiCAD-native).

Library graphics use Y up: a library point (lx, ly) lands at
`(sx + rx, sy - ry)` with `(rx, ry) = mirror(rotate(lx, ly, rot))` — the
transform `sch_netlist.pins_of_instance` uses for pins.

Text extents use the advance of each ASCII character in KiCAD's stroke font,
measured from `kicad-cli sch export svg` (KiCAD 10.0.8), and one font size
high; other characters count 0.9 × the size. Good enough to find overlaps,
not to typeset.
"""

from __future__ import annotations

import math
import re

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters.sch_io import find_child, find_children, head_of, is_call, is_symbol
from kicad_claude.utils.geometry import rotate_xy

Box = tuple[float, float, float, float]  # x1, y1, x2, y2 with x1 <= x2, y1 <= y2

CHAR_WIDTH = 0.9   # × font size, for characters outside the table below
# Advance × font size per character, KiCAD 10.0.8 stroke font: each width
# measured as (span("I" + c * 10 + "I") - span("II")) / 10 in an SVG export.
_ADVANCE_GROUPS = (
    ('`', 0.38),
    ("!',.:;Iij", 0.48),
    ('l', 0.52),
    ('^ft', 0.57),
    ('r', 0.62),
    ('()[\\]{}', 0.67),
    ('~', 0.71),
    (' "*JT_vy', 0.76),
    ('Lksxz', 0.81),
    ('?AFVYce', 0.86),
    ('Eabdghnopqu', 0.9),
    ('$0123456789SXZ|', 0.95),
    ('#BCDGKPR', 1.0),
    ('/HNOQUw', 1.05),
    ('%MW', 1.14),
    ('&+-<=>', 1.24),
    ('@', 1.29),
    ('m', 1.33),
)
ADVANCE = {c: v for chars, v in _ADVANCE_GROUPS for c in chars}
LINE_PITCH = 1.6   # × font size, multi-line text
GRID = 1.27

# KiCAD's default page frame: 10 mm margins; the title block sits in the
# bottom-right corner inside the frame.
FRAME_MARGIN = 10.0
TITLE_BLOCK_W = 110.0
TITLE_BLOCK_H = 34.0

PAPER_MM = {  # landscape width, height
    "A0": (1189.0, 841.0), "A1": (841.0, 594.0), "A2": (594.0, 420.0),
    "A3": (420.0, 297.0), "A4": (297.0, 210.0), "A5": (210.0, 148.0),
    "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4),
    "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8),
    "D": (863.6, 558.8), "E": (1117.6, 863.6),
}


# --------------------------------------------------------------------------- #
# Boxes
# --------------------------------------------------------------------------- #


def box_of_points(points) -> Box | None:
    pts = list(points)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def union(a: Box | None, b: Box | None) -> Box | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def inset(b: Box, d: float) -> Box:
    return b[0] + d, b[1] + d, b[2] - d, b[3] - d


def overlap_area(a: Box, b: Box) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def segment_hits_box(p: tuple[float, float], q: tuple[float, float], b: Box) -> bool:
    """Axis-aligned or diagonal segment vs. box (Liang–Barsky clip)."""
    x0, y0 = p
    dx, dy = q[0] - x0, q[1] - y0
    t0, t1 = 0.0, 1.0
    for pk, qk in ((-dx, x0 - b[0]), (dx, b[2] - x0), (-dy, y0 - b[1]), (dy, b[3] - y0)):
        if pk == 0:
            if qk < 0:
                return False
            continue
        t = qk / pk
        if pk < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def round_away(value: float, origin: float) -> float:
    """Round `value` to the grid, away from `origin`."""
    steps = (value - origin) / GRID
    steps = math.ceil(steps - 1e-6) if steps >= 0 else math.floor(steps + 1e-6)
    return round(origin + steps * GRID, 4)


def snap(value: float) -> float:
    return round(round(value / GRID) * GRID, 4)


# --------------------------------------------------------------------------- #
# Symbol frame
# --------------------------------------------------------------------------- #


class Frame:
    """Placement of one symbol instance: origin, rotation, mirror."""

    def __init__(self, s_node: list):
        at = find_child(s_node, "at")
        self.x = float(at[1]) if at and len(at) > 1 else 0.0
        self.y = float(at[2]) if at and len(at) > 2 else 0.0
        self.rot = float(at[3]) if at and len(at) > 3 else 0.0
        m = find_child(s_node, "mirror")
        self.mirror = str(m[1]) if m and len(m) > 1 else ""

    def _lib_vec(self, lx: float, ly: float) -> tuple[float, float]:
        # KiCAD composes the mirror after the rotation (SCH_SYMBOL::
        # SetOrientation: new = temp x old); at 90 / 270 the order matters.
        rx, ry = rotate_xy(lx, ly, self.rot)
        if self.mirror == "y":
            rx = -rx
        elif self.mirror == "x":
            ry = -ry
        return rx, -ry

    def point(self, lx: float, ly: float) -> tuple[float, float]:
        dx, dy = self._lib_vec(lx, ly)
        return round(self.x + dx, 4), round(self.y + dy, 4)

    def vector(self, lx: float, ly: float) -> tuple[float, float]:
        dx, dy = self._lib_vec(lx, ly)
        return round(dx, 6) + 0.0, round(dy, 6) + 0.0


def _unit_style(name: str) -> tuple[int, int] | None:
    m = re.search(r"_(\d+)_(\d+)$", name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _lib_def(tree: list, s_node: list) -> list | None:
    lib = find_child(s_node, "lib_id")
    return ed.find_lib_symbol_def(tree, lib[1]) if lib and len(lib) > 1 else None


def _unit_nodes(sym_def: list, s_node: list):
    unit_node = find_child(s_node, "unit")
    want = int(unit_node[1]) if unit_node and len(unit_node) > 1 else 1
    for child in sym_def[2:]:
        if head_of(child) != "symbol" or len(child) < 2:
            continue
        us = _unit_style(str(child[1]))
        if us is None or us[0] not in (0, want) or us[1] not in (0, 1):
            continue
        yield child


def _hidden(node: list) -> bool:
    h = find_child(node, "hide")
    if h is not None and (len(h) < 2 or str(h[1]) == "yes"):
        return True
    return any(is_symbol(x, "hide") for x in node[1:])  # legacy bare flag


def pins(tree: list, s_node: list) -> list[dict]:
    """Pins of one instance: number, name, type, point, `out` (unit vector
    from the body to the connection point), `end` (where the pin meets the
    body) and `hidden`."""
    sym_def = _lib_def(tree, s_node)
    if sym_def is None:
        return []
    fr = Frame(s_node)
    out = []
    for unit in _unit_nodes(sym_def, s_node):
        for pin in find_children(unit, "pin"):
            at = find_child(pin, "at")
            if not at or len(at) < 3:
                continue
            lx, ly = float(at[1]), float(at[2])
            ang = float(at[3]) if len(at) > 3 else 0.0
            length_node = find_child(pin, "length")
            length = float(length_node[1]) if length_node and len(length_node) > 1 else 2.54
            body_dir = (math.cos(math.radians(ang)), math.sin(math.radians(ang)))
            number, name = ed._pin_id(pin)
            vx, vy = fr.vector(-body_dir[0], -body_dir[1])
            out.append({
                "number": number,
                "name": name,
                "type": str(pin[1]) if len(pin) > 1 else "",
                "point": fr.point(lx, ly),
                "end": fr.point(lx + body_dir[0] * length, ly + body_dir[1] * length),
                "out": (round(vx), round(vy)),
                "hidden": _hidden(pin),
            })
    return out


def side_of(out: tuple[int, int]) -> str:
    return {(1, 0): "right", (-1, 0): "left", (0, -1): "top", (0, 1): "bottom"}.get(
        tuple(out), "right")


def body_box(tree: list, s_node: list) -> Box | None:
    """Sheet box of the symbol's drawn body (graphics, not pins or fields)."""
    sym_def = _lib_def(tree, s_node)
    if sym_def is None:
        return None
    fr = Frame(s_node)
    pts: list[tuple[float, float]] = []
    for unit in _unit_nodes(sym_def, s_node):
        for g in unit[2:]:
            h = head_of(g)
            if h == "rectangle":
                s, e = find_child(g, "start"), find_child(g, "end")
                if s and e:
                    pts += [fr.point(float(s[1]), float(s[2])), fr.point(float(e[1]), float(e[2]))]
            elif h in ("polyline", "bezier"):
                p = find_child(g, "pts")
                for xy in find_children(p, "xy") if p else []:
                    pts.append(fr.point(float(xy[1]), float(xy[2])))
            elif h == "circle":
                c, r = find_child(g, "center"), find_child(g, "radius")
                if c and r:
                    cx, cy, rad = float(c[1]), float(c[2]), float(r[1])
                    pts += [fr.point(cx - rad, cy - rad), fr.point(cx + rad, cy + rad)]
            elif h == "arc":
                for k in ("start", "mid", "end"):
                    n = find_child(g, k)
                    if n:
                        pts.append(fr.point(float(n[1]), float(n[2])))
    return box_of_points(pts)


def body_and_pins_box(tree: list, s_node: list) -> Box | None:
    b = body_box(tree, s_node)
    for p in pins(tree, s_node):
        if not p["hidden"]:
            b = union(b, box_of_points([p["point"], p["end"]]))
    return b


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def text_size(node: list) -> float:
    eff = find_child(node, "effects")
    font = find_child(eff, "font") if eff else None
    size = find_child(font, "size") if font else None
    return float(size[1]) if size and len(size) > 1 else 1.27


def justify_of(node: list) -> tuple[str, str]:
    eff = find_child(node, "effects")
    j = find_child(eff, "justify") if eff else None
    words = [str(x) for x in j[1:]] if j else []
    h = "left" if "left" in words else "right" if "right" in words else "center"
    v = "top" if "top" in words else "bottom" if "bottom" in words else "center"
    return h, v


def text_extent(text: str, size: float) -> tuple[float, float]:
    lines = str(text).split("\n") or [""]
    width = max(sum(ADVANCE.get(c, CHAR_WIDTH) for c in line) for line in lines) * size
    height = size + (len(lines) - 1) * LINE_PITCH * size
    return width, height


def text_box(x: float, y: float, w: float, h: float, angle: float, hj: str, vj: str) -> Box:
    """Box of a text block anchored at (x, y), read at `angle` (0 or 90)."""
    if int(angle) % 180 == 0:
        x1 = x if hj == "left" else x - w if hj == "right" else x - w / 2
        y1 = y if vj == "top" else y - h if vj == "bottom" else y - h / 2
        return x1, y1, x1 + w, y1 + h
    # Vertical text reads bottom to top; the top of the glyphs faces -x.
    y2 = y if hj == "left" else y + w if hj == "right" else y + w / 2
    x1 = x if vj == "top" else x - h if vj == "bottom" else x - h / 2
    return x1, y2 - w, x1 + h, y2


def is_text_flipped(fr: Frame, file_angle: float) -> tuple[int, bool]:
    """Displayed angle (0 / 90) of a field and whether KiCAD flips its justify.

    A field's text angle is stored relative to the symbol. KiCAD keeps text
    readable — horizontal left to right, vertical bottom to top — and swaps
    the justification when the symbol's transform would draw it backwards.
    """
    a = math.radians(file_angle)
    vx, vy = fr.vector(math.cos(a), math.sin(a))  # library frame is Y up
    if abs(vx) >= abs(vy):
        return 0, vx < 0
    return 90, vy > 0


def field_box(s_node: list, prop: list) -> Box | None:
    at = find_child(prop, "at")
    if not at or len(at) < 3:
        return None
    fr = Frame(s_node)
    disp, flipped = is_text_flipped(fr, float(at[3]) if len(at) > 3 else 0.0)
    hj, vj = justify_of(prop)
    if flipped:
        hj = {"left": "right", "right": "left"}.get(hj, hj)
        vj = {"top": "bottom", "bottom": "top"}.get(vj, vj)
    size = text_size(prop)
    w, h = text_extent(prop[2] if len(prop) > 2 else "", size)
    return text_box(float(at[1]), float(at[2]), w, h, disp, hj, vj)


def label_box(node: list) -> Box | None:
    """Box of a local / global / hierarchical label's text (and shape)."""
    at = find_child(node, "at")
    if not at or len(at) < 3 or len(node) < 2:
        return None
    x, y = float(at[1]), float(at[2])
    angle = int(float(at[3])) % 360 if len(at) > 3 else 0
    size = text_size(node)
    w, _ = text_extent(node[1], size)
    kind = head_of(node)
    if kind == "global_label":
        w += 2 * size
    elif kind == "hierarchical_label":
        w += size
    h = 1.3 * size if kind == "label" else 1.5 * size
    # Local labels sit on top of their wire; the others are centred on it.
    if angle in (0, 180):
        x1 = x if angle == 0 else x - w
        y1, y2 = (y - h, y) if kind == "label" else (y - h / 2, y + h / 2)
        return x1, y1, x1 + w, y2
    y1 = y - w if angle == 90 else y
    x1, x2 = (x - h, x) if kind == "label" else (x - h / 2, x + h / 2)
    return x1, y1, x2, y1 + w


def note_box(node: list) -> Box | None:
    at = find_child(node, "at")
    if not at or len(at) < 3 or len(node) < 2:
        return None
    size = text_size(node)
    w, h = text_extent(node[1], size)
    hj, vj = justify_of(node)
    # The anchor holds the block's `bottom` / `top` / centre: a multi-line
    # note justified `bottom` (what `add_text` writes) grows upwards.
    x, y = float(at[1]), float(at[2])
    angle = float(at[3]) if len(at) > 3 else 0.0
    if int(angle) % 180 == 0:
        x1 = x if hj == "left" else x - w if hj == "right" else x - w / 2
        y1 = y - h if vj == "bottom" else y if vj == "top" else y - h / 2
        return x1, y1, x1 + w, y1 + h
    return text_box(x, y, w, h, 90, hj, vj)


def page_size(tree: list) -> tuple[float, float]:
    """(width, height) of the sheet in mm, honouring `portrait` and custom sizes."""
    paper = find_child(tree, "paper")
    if not paper or len(paper) < 2:
        return PAPER_MM["A4"]
    name = str(paper[1])
    if name == "User" and len(paper) >= 4:
        return float(paper[2]), float(paper[3])
    w, h = PAPER_MM.get(name, PAPER_MM["A4"])
    if any(str(x) == "portrait" for x in paper[2:]):
        w, h = h, w
    return w, h


def frame_and_title_block(tree: list) -> tuple[Box, Box]:
    w, h = page_size(tree)
    m = FRAME_MARGIN
    frame = (m, m, w - m, h - m)
    tb = (w - m - TITLE_BLOCK_W, h - m - TITLE_BLOCK_H, w - m, h - m)
    return frame, tb


def is_power(tree: list, s_node: list) -> bool:
    ref = ed.get_symbol_property(s_node, "Reference") or ""
    if ref.startswith("#"):
        return True
    sym_def = _lib_def(tree, s_node)
    return sym_def is not None and find_child(sym_def, "power") is not None


def visible_fields(s_node: list) -> list[list]:
    out = []
    for prop in find_children(s_node, "property"):
        if len(prop) < 3 or ed._field_hidden(prop) or str(prop[2]) in ("", "~"):
            continue
        out.append(prop)
    return out


def is_wire(node) -> bool:
    return is_call(node, "wire")
