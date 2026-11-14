"""Board-level geometry read from a `.kicad_pcb` tree: outline loops, zone copper.

Everything here is in KiCAD file coordinates (mm, Y down) and read-only.

- `board_outline_loops` chains every Edge.Cuts primitive (`gr_line`, `gr_arc`,
  `gr_rect`, `gr_circle`, `gr_poly`) into closed polygons. A rounded-corner
  outline is lines plus arcs, so a `gr_rect`-only reader misses it.
- `zone_layer_polygons` yields one polygon per (zone, copper layer). KiCAD 10
  zones carry `(net "GND")` and may span several layers with `(layers ...)`;
  the filled copper is in one `(filled_polygon (layer L) (pts ...))` per
  island. KiCAD stores a filled polygon *fractured*: holes are joined to the
  outer edge by zero-width slits, so an even-odd ray cast and the shoelace
  area are both correct on it without hole handling.
"""

from __future__ import annotations

import math
import re
from typing import Iterator

from kicad_claude.adapters.sch_io import find_child, find_children, is_call

# A ground net by name: GND, GND_USB, AGND, PGND, GNDPWR, VSS, VSSA, 0V, ...
GROUND_NET_RE = re.compile(r"(^|[^A-Z0-9])(\w*GND\w*|VSS\w*|0V)($|[^A-Z0-9])", re.I)

_ENDPOINT_TOL_MM = 0.01
_ARC_SEG_MAX_DEG = 5.0


def is_ground_net(name: str) -> bool:
    """True for a net whose name reads as a ground (see `GROUND_NET_RE`)."""
    return bool(name) and bool(GROUND_NET_RE.search(name.lstrip("/")))


# --------------------------------------------------------------------------- #
# Polygon primitives
# --------------------------------------------------------------------------- #


def polygon_area(poly: list[tuple[float, float]]) -> float:
    """Unsigned shoelace area in mm²."""
    n = len(poly)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """Even-odd ray cast. Correct on KiCAD's fractured filled polygons."""
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _xy(node) -> tuple[float, float] | None:
    if node and len(node) >= 3:
        return float(node[1]), float(node[2])
    return None


def arc_points(
    start: tuple[float, float],
    mid: tuple[float, float],
    end: tuple[float, float],
) -> list[tuple[float, float]]:
    """Points along the circular arc start -> mid -> end, both ends included."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:  # collinear: a straight segment
        return [start, end]
    ux = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1)
          + (x3**2 + y3**2) * (y1 - y2)) / d
    uy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3)
          + (x3**2 + y3**2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    def ccw(a, b):  # sweep from a to b going counter-clockwise, in [0, 2pi)
        return (b - a) % (2 * math.pi)

    sweep = ccw(a1, a3)
    if ccw(a1, a2) > sweep:  # mid not on the CCW path: go clockwise
        sweep -= 2 * math.pi
    n = max(2, int(math.ceil(abs(math.degrees(sweep)) / _ARC_SEG_MAX_DEG)))
    pts = [(ux + r * math.cos(a1 + sweep * k / n), uy + r * math.sin(a1 + sweep * k / n))
           for k in range(n + 1)]
    pts[0], pts[-1] = start, end
    return pts


def _pts_polygon(pts_node) -> list[tuple[float, float]]:
    """A `(pts (xy ..) (arc (start)(mid)(end)) ...)` node as a point list."""
    out: list[tuple[float, float]] = []
    if not pts_node:
        return out
    for c in pts_node[1:]:
        if is_call(c, "xy") and len(c) >= 3:
            out.append((float(c[1]), float(c[2])))
        elif is_call(c, "arc"):
            s, m, e = (_xy(find_child(c, k)) for k in ("start", "mid", "end"))
            if s and m and e:
                pts = arc_points(s, m, e)
                if out and math.dist(out[-1], pts[0]) < _ENDPOINT_TOL_MM:
                    pts = pts[1:]
                out.extend(pts)
    return out


# --------------------------------------------------------------------------- #
# Board outline
# --------------------------------------------------------------------------- #


def _on_edge_cuts(node) -> bool:
    layer = find_child(node, "layer")
    return bool(layer and len(layer) >= 2 and layer[1] == "Edge.Cuts")


def board_outline_loops(tree: list) -> list[list[tuple[float, float]]]:
    """Every closed Edge.Cuts loop on the board, as polygons, largest first.

    Board-level graphics only (footprint `fp_*` Edge.Cuts are not read).
    Open chains that never close are dropped.
    """
    loops: list[list[tuple[float, float]]] = []
    chains: list[list[tuple[float, float]]] = []  # open pieces to stitch

    for node in tree[1:] if tree else []:
        if not isinstance(node, list) or not node or not _on_edge_cuts(node):
            continue
        if is_call(node, "gr_rect"):
            s, e = _xy(find_child(node, "start")), _xy(find_child(node, "end"))
            if s and e:
                loops.append([(s[0], s[1]), (e[0], s[1]), (e[0], e[1]), (s[0], e[1])])
        elif is_call(node, "gr_circle"):
            c, e = _xy(find_child(node, "center")), _xy(find_child(node, "end"))
            if c and e:
                r = math.dist(c, e)
                n = int(360 / _ARC_SEG_MAX_DEG)
                loops.append([(c[0] + r * math.cos(2 * math.pi * k / n),
                               c[1] + r * math.sin(2 * math.pi * k / n)) for k in range(n)])
        elif is_call(node, "gr_poly"):
            poly = _pts_polygon(find_child(node, "pts"))
            if len(poly) >= 3:
                loops.append(poly)
        elif is_call(node, "gr_line"):
            s, e = _xy(find_child(node, "start")), _xy(find_child(node, "end"))
            if s and e:
                chains.append([s, e])
        elif is_call(node, "gr_arc"):
            s, m, e = (_xy(find_child(node, k)) for k in ("start", "mid", "end"))
            if s and m and e:
                chains.append(arc_points(s, m, e))

    # Stitch open pieces end to end. O(n²), fine for outlines (tens of pieces).
    while chains:
        cur = chains.pop()
        progress = True
        while progress and math.dist(cur[0], cur[-1]) >= _ENDPOINT_TOL_MM:
            progress = False
            for i, piece in enumerate(chains):
                if math.dist(cur[-1], piece[0]) < _ENDPOINT_TOL_MM:
                    cur = cur + piece[1:]
                elif math.dist(cur[-1], piece[-1]) < _ENDPOINT_TOL_MM:
                    cur = cur + piece[::-1][1:]
                elif math.dist(cur[0], piece[-1]) < _ENDPOINT_TOL_MM:
                    cur = piece[:-1] + cur
                elif math.dist(cur[0], piece[0]) < _ENDPOINT_TOL_MM:
                    cur = piece[::-1][:-1] + cur
                else:
                    continue
                chains.pop(i)
                progress = True
                break
        if len(cur) >= 4 and math.dist(cur[0], cur[-1]) < _ENDPOINT_TOL_MM:
            loops.append(cur[:-1])

    loops.sort(key=polygon_area, reverse=True)
    return loops


def board_area_mm2(tree: list) -> float:
    """Outline area minus the area of loops inside it (cutouts)."""
    loops = board_outline_loops(tree)
    if not loops:
        return 0.0
    outer = loops[0]
    area = polygon_area(outer)
    for inner in loops[1:]:
        if point_in_polygon(*inner[0], outer):
            area -= polygon_area(inner)
    return area


# --------------------------------------------------------------------------- #
# Zones
# --------------------------------------------------------------------------- #


def zone_layers(zone: list) -> list[str]:
    """Copper layers of a zone: `(layer "F.Cu")` or `(layers "F.Cu" "In1.Cu")`."""
    single = find_child(zone, "layer")
    if single and len(single) >= 2:
        return [str(single[1])]
    multi = find_child(zone, "layers")
    if multi:
        return [str(x) for x in multi[1:]]
    return []


def zone_layer_polygons(tree: list, net_of) -> Iterator[dict]:
    """One dict per zone polygon per layer: `net`, `layer`, `polygon`, `filled`.

    Filled islands are returned when the zone has been filled (the real
    copper, with clearances and thermal reliefs cut out); an unfilled zone
    falls back to its outline on each of its layers with `filled: False`.
    Keepout zones (`(keepout ...)`) are skipped.

    `net_of(zone) -> str` resolves the zone net (pass `pcb_editor.net_of`); it is
    injected to keep this module free of an import cycle.
    """
    for z in find_children(tree, "zone"):
        if find_child(z, "keepout") is not None:
            continue
        net = net_of(z)
        filled = [fp for fp in find_children(z, "filled_polygon")]
        if filled:
            for fp in filled:
                layer = find_child(fp, "layer")
                poly = _pts_polygon(find_child(fp, "pts"))
                if layer and len(layer) >= 2 and len(poly) >= 3:
                    yield {"net": net, "layer": str(layer[1]), "polygon": poly,
                           "filled": True}
            continue
        outline = _pts_polygon(find_child(find_child(z, "polygon") or [], "pts"))
        if len(outline) < 3:
            continue
        for layer in zone_layers(z):
            if layer.endswith(".Cu") or layer == "*.Cu":
                yield {"net": net, "layer": layer, "polygon": outline, "filled": False}
