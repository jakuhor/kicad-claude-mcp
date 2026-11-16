"""Copper connectivity of a parsed `.kicad_pcb` tree — the ratsnest.

Unlike a schematic, a PCB already carries its net assignment: every pad and
track holds a `(net N "name")`. What the file does not say is whether the
copper actually joins the pads of a net. That is what this module derives, so
the server can answer "what still needs routing".

Read-only. Nothing here mutates the tree.

What counts as a connection:
  - A track joins its two endpoints, on its own layer.
  - Two tracks on the same layer that share a point are joined.
  - A via joins every layer it spans at its point.
  - A pad joins the copper that touches it: a same-net track endpoint inside
    the pad, or a track passing over the pad's centre. A through-hole pad
    (`*.Cu`) is present on every copper layer.
  - A filled zone joins every same-net pad inside its filled outline.

Zones are only connective once KiCAD has filled them. An unfilled zone carries
no `filled_polygon`, and this module then reports `zones_filled: False` rather
than guessing — an unfilled ground pour would otherwise make a board look
routed when it is not.
"""

from __future__ import annotations

import math
from typing import Any

from kicad_claude.adapters.sch_io import (
    find_child,
    find_children,
    is_call,
)
from kicad_claude.adapters import pcb_editor
from kicad_claude.adapters.sch_netlist import _Union, _on_segment
from kicad_claude.utils.geometry import round_mm
from kicad_claude.utils.kicad_strings import normalize_name

_PLACES = 4

Point = tuple[float, float]


def _pt(x: Any, y: Any) -> Point:
    return (round(float(x), _PLACES), round(float(y), _PLACES))


# --------------------------------------------------------------------------- #
# Board layers
# --------------------------------------------------------------------------- #


def copper_layers(tree: list) -> list[str]:
    """Every copper layer of the board, outermost first."""
    layers_node = find_child(tree, "layers")
    out: list[str] = []
    if layers_node:
        for entry in layers_node[1:]:
            if isinstance(entry, list) and len(entry) >= 2 and isinstance(entry[1], str):
                if entry[1].endswith(".Cu"):
                    out.append(entry[1])
    return out or ["F.Cu", "B.Cu"]


def _expand_layers(spec: list[str], board_layers: list[str]) -> list[str]:
    """Resolve a pad's `(layers ...)` to concrete copper layers."""
    out: list[str] = []
    for item in spec:
        if item in ("*.Cu", "*"):
            return list(board_layers)
        if item.endswith(".Cu") and item in board_layers:
            out.append(item)
    return out


# --------------------------------------------------------------------------- #
# Pads
# --------------------------------------------------------------------------- #


def _footprint_rotation(fp: list) -> tuple[float, float, float]:
    at = find_child(fp, "at")
    if not at or len(at) < 3:
        return 0.0, 0.0, 0.0
    return float(at[1]), float(at[2]), (float(at[3]) if len(at) > 3 else 0.0)


def pad_to_board_xy(fx: float, fy: float, angle_deg: float, px: float, py: float) -> Point:
    """Place a pad's footprint-local (px, py) on the board.

    KiCAD's footprint angle is CCW while the file's Y axis points down, so the
    sine term changes sign on Y. Verified against KiCAD's own demo boards: on
    `interf_u`, 258 of 259 rotated pads land within 0.5 mm of same-net copper
    under this form, against 111 under the opposite sign.
    """
    r = math.radians(angle_deg)
    ca, sa = math.cos(r), math.sin(r)
    return _pt(fx + px * ca + py * sa, fy - px * sa + py * ca)


def _net_of(node: list, tree: list | None = None) -> tuple[int, str]:
    """Read the net of a pad / track / via / zone as (number, name).

    KiCAD 10 writes `(net "GND")` with no number, so the *name* is what
    identifies a net here — the number is reported for display only and is 0
    whenever the file does not give one. On an older board the tracks carry
    `(net 6)` and only the board's net table names it, so `tree` is passed
    wherever it is available. An item with no name at all belongs to the
    unconnected net.
    """
    index, name = pcb_editor.read_net_node(node)
    if not name:
        name = pcb_editor.net_of(node, tree)
    return (index or 0), name


def _pad_radius(pad: list) -> float:
    """Radius of the pad's bounding circle — half its diagonal.

    Half the longer side is not enough: a track may legitimately end at a
    rectangular pad's corner, which sits at `hypot(w, h) / 2` from the centre.
    """
    size = find_child(pad, "size")
    if size and len(size) >= 3:
        return math.hypot(float(size[1]), float(size[2])) / 2.0
    return 0.25


def _pad_size(pad: list) -> list[float]:
    size = find_child(pad, "size")
    if size and len(size) >= 3:
        return [float(size[1]), float(size[2])]
    return []


def _pad_corners(pad: list, center: Point) -> list[Point]:
    """The pad's bounding rectangle on the board, as four corners.

    The pad `at` angle in the file is absolute (footprint rotation included),
    and turns CCW on a Y-down axis like `pad_to_board_xy`. Round and oval
    pads get their bounding box, a slight over-estimate.
    """
    size = find_child(pad, "size")
    w = float(size[1]) if size and len(size) >= 3 else 0.5
    h = float(size[2]) if size and len(size) >= 3 else 0.5
    pat = find_child(pad, "at")
    angle = float(pat[3]) if pat and len(pat) > 3 else 0.0
    cx, cy = center
    return [pad_to_board_xy(cx, cy, angle, dx, dy)
            for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]


def list_pads(tree: list) -> list[dict]:
    """Every pad on the board, with its absolute position, net and layers."""
    board_layers = copper_layers(tree)
    out: list[dict] = []
    for fp in tree[1:]:
        if not is_call(fp, "footprint"):
            continue
        ref = pcb_editor.get_footprint_reference(fp)
        fx, fy, fa = _footprint_rotation(fp)
        fp_layer_node = find_child(fp, "layer")
        fp_layer = fp_layer_node[1] if fp_layer_node and len(fp_layer_node) > 1 else "F.Cu"

        for pad in find_children(fp, "pad"):
            if len(pad) < 2:
                continue
            number = str(pad[1])
            pad_type = str(pad[2]) if len(pad) > 2 else ""
            net, net_name = _net_of(pad, tree)
            pat = find_child(pad, "at")
            px = float(pat[1]) if pat and len(pat) > 1 else 0.0
            py = float(pat[2]) if pat and len(pat) > 2 else 0.0

            layers_node = find_child(pad, "layers")
            spec = [str(x) for x in (layers_node[1:] if layers_node else [])]
            layers = _expand_layers(spec, board_layers)
            if not layers and pad_type != "np_thru_hole":
                layers = [fp_layer]

            point = pad_to_board_xy(fx, fy, fa, px, py)
            out.append(
                {
                    "ref": ref or "?",
                    "pad": number,
                    "type": pad_type,
                    "net": net,
                    "net_name": net_name,
                    "point": point,
                    "layers": layers,
                    "radius_mm": _pad_radius(pad),
                    "corners": _pad_corners(pad, point),
                    "size_mm": _pad_size(pad),
                    "shape": str(pad[3]) if len(pad) > 3 and not isinstance(pad[3], list) else "",
                }
            )
    return out


def find_pad(tree: list, reference: str, pad_number: str) -> dict | None:
    """One pad by reference and pad number."""
    ref = normalize_name(reference)
    for pad in list_pads(tree):
        if normalize_name(pad["ref"]) == ref and pad["pad"] == pad_number:
            return pad
    return None


# --------------------------------------------------------------------------- #
# Copper
# --------------------------------------------------------------------------- #


def _tracks(tree: list) -> list[dict]:
    """Every `(segment ...)` and `(arc ...)` as a straight-line approximation.

    An arc is treated as the chord between its endpoints. That can only make
    connectivity look better than it is where an arc's middle touches copper
    its ends do not — rare, and it never hides a genuinely missing link,
    because both arc ends stay joined either way.
    """
    out = []
    for node in tree[1:]:
        if not (is_call(node, "segment") or is_call(node, "arc")):
            continue
        start = find_child(node, "start")
        end = find_child(node, "end")
        if not start or not end or len(start) < 3 or len(end) < 3:
            continue
        layer_node = find_child(node, "layer")
        width_node = find_child(node, "width")
        net, net_name = _net_of(node, tree)
        out.append(
            {
                "start": _pt(start[1], start[2]),
                "end": _pt(end[1], end[2]),
                "layer": str(layer_node[1]) if layer_node and len(layer_node) > 1 else "",
                "net": net,
                "net_name": net_name,
                "width_mm": float(width_node[1]) if width_node and len(width_node) > 1 else 0.25,
            }
        )
    return out


def _vias(tree: list, board_layers: list[str]) -> list[dict]:
    out = []
    for node in tree[1:]:
        if not is_call(node, "via"):
            continue
        at = find_child(node, "at")
        if not at or len(at) < 3:
            continue
        layers_node = find_child(node, "layers")
        spec = [str(x) for x in (layers_node[1:] if layers_node else [])]
        # A via spans from its first to its last named layer.
        if len(spec) >= 2 and spec[0] in board_layers and spec[-1] in board_layers:
            lo, hi = board_layers.index(spec[0]), board_layers.index(spec[-1])
            span = board_layers[min(lo, hi): max(lo, hi) + 1]
        else:
            span = list(board_layers)
        net, net_name = _net_of(node, tree)
        size = find_child(node, "size")
        radius = float(size[1]) / 2.0 if size and len(size) > 1 else 0.3
        out.append(
            {
                "point": _pt(at[1], at[2]),
                "layers": span,
                "net": net,
                "net_name": net_name,
                "radius_mm": radius,
            }
        )
    return out


def _filled_zones(tree: list) -> tuple[list[dict], bool, int]:
    """Filled zone polygons, plus whether every zone on the board was filled."""
    zones: list[dict] = []
    total = 0
    unfilled = 0
    for node in tree[1:]:
        if not is_call(node, "zone"):
            continue
        total += 1
        net, net_name = _net_of(node, tree)
        polys = []
        for fp in find_children(node, "filled_polygon"):
            layer_node = find_child(fp, "layer")
            pts = find_child(fp, "pts")
            xy = find_children(pts, "xy") if pts else []
            if len(xy) >= 3:
                polys.append(
                    {
                        "layer": str(layer_node[1]) if layer_node and len(layer_node) > 1 else "",
                        "points": [_pt(p[1], p[2]) for p in xy],
                        "net": net,
                        "net_name": net_name,
                    }
                )
        if not polys:
            unfilled += 1
        zones.extend(polys)
    return zones, unfilled == 0, unfilled


def is_pour(node: list, tree: list | None = None) -> bool:
    """A copper pour: a `(zone ...)` on a net that is not a keepout / rule area."""
    if not is_call(node, "zone") or find_child(node, "keepout") is not None:
        return False
    return bool(_net_of(node, tree)[1])


def pour_outlines(tree: list) -> list[dict]:
    """Outline polygon, net and copper layers of every pour (not the fill)."""
    board_layers = copper_layers(tree)
    out = []
    for node in tree[1:]:
        if not is_pour(node, tree):
            continue
        spec = [str(x) for n in (find_child(node, "layer"), find_child(node, "layers"))
                if n for x in n[1:] if isinstance(x, str)]
        layers = _expand_layers(spec, board_layers)
        for poly in find_children(node, "polygon"):
            pts = find_child(poly, "pts")
            xy = find_children(pts, "xy") if pts else []
            if len(xy) >= 3:
                out.append({"net_name": _net_of(node, tree)[1], "layers": layers,
                            "points": [_pt(q[1], q[2]) for q in xy]})
    return out


def pads_outside_pours(tree: list) -> dict[str, list[str]]:
    """{pour net: ["REF.PAD", ...]} for pads of a pour's net that lie outside
    every outline of that net. A Specctra export turns a pour into a plane:
    Freerouting reaches the plane from a pad above it (through a via on
    another layer) but routes nothing to a pad beyond it."""
    pours = pour_outlines(tree)
    nets = {z["net_name"] for z in pours}
    out: dict[str, set[str]] = {}
    for pad in list_pads(tree):
        if pad["net_name"] not in nets:
            continue
        if not any(z["net_name"] == pad["net_name"] and _zone_touches_pad(pad, z["points"])
                   for z in pours):
            out.setdefault(pad["net_name"], set()).add(f"{pad['ref']}.{pad['pad']}")
    return {k: sorted(v) for k, v in sorted(out.items())}


def _point_segment_distance(p: Point, a: Point, b: Point) -> float:
    """Shortest distance from `p` to the segment a-b."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _distance_to_polygon(p: Point, poly: list[Point]) -> float:
    """Shortest distance from `p` to the polygon's boundary."""
    px, py = p
    best = float("inf")
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-12:
            best = min(best, math.hypot(px - ax, py - ay))
            continue
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best


def _zone_touches_point(point: Point, radius: float, poly: list[Point]) -> bool:
    """True if a filled zone reaches a round copper feature at `point`."""
    if _point_in_polygon(point, poly):
        return True
    return _distance_to_polygon(point, poly) <= radius + 1e-4


def _segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    """True if segments a-b and c-d intersect (touching counts)."""
    def orient(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    d1, d2 = orient(c, d, a), orient(c, d, b)
    d3, d4 = orient(a, b, c), orient(a, b, d)
    if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0) and d1 and d2 and d3 and d4:
        return True
    eps = 1e-6
    return ((abs(d1) < eps and _point_segment_distance(a, c, d) < eps)
            or (abs(d2) < eps and _point_segment_distance(b, c, d) < eps)
            or (abs(d3) < eps and _point_segment_distance(c, a, b) < eps)
            or (abs(d4) < eps and _point_segment_distance(d, a, b) < eps))


def _zone_touches_pad(pad: dict, poly: list[Point]) -> bool:
    """True if a filled zone polygon overlaps the pad's copper.

    A pad swallowed by the pour has its centre inside the polygon. A
    thermally relieved pad sits in a hole of the polygon and is joined by
    spokes that run onto the pad, so a polygon edge crosses the pad outline.
    A relief without a single spoke leaves a gap all round: no crossing, no
    connection (DRC's `starved_thermal` case). The pad's bounding circle is
    not enough here: the relief hole of a large rectangular pad lies inside
    that circle.
    """
    corners = pad.get("corners")
    if not corners:
        return _zone_touches_point(pad["point"], pad["radius_mm"], poly)
    if _point_in_polygon(pad["point"], poly) or any(_point_in_polygon(c, poly) for c in corners):
        return True
    if any(_point_in_polygon(v, corners) for v in poly):
        return True
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        for j in range(4):
            if _segments_cross(a, b, corners[j], corners[(j + 1) % 4]):
                return True
    return False


def _point_in_polygon(p: Point, poly: list[Point]) -> bool:
    """Ray casting. Points exactly on the edge may fall either way."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            t = (y - y1) / (y2 - y1)
            if x < x1 + t * (x2 - x1):
                inside = not inside
    return inside


# --------------------------------------------------------------------------- #
# Connectivity
# --------------------------------------------------------------------------- #


def _touches(pad: dict, track: dict) -> bool:
    """True if a pad and a same-layer track overlap.

    Both are treated as their bounding shapes: the pad as a circle of its
    half-diagonal, the track as a capsule of its width. A track that merely
    runs past the pad's edge still counts, which is what KiCAD does — copper
    that overlaps is copper that connects.
    """
    if track["layer"] not in pad["layers"]:
        return False
    reach = pad["radius_mm"] + track["width_mm"] / 2.0 + 1e-4
    return _point_segment_distance(pad["point"], track["start"], track["end"]) <= reach


def build_connectivity(tree: list) -> dict:
    """Group the pads of every net by what copper actually joins them."""
    board_layers = copper_layers(tree)
    pads = list_pads(tree)
    tracks = _tracks(tree)
    vias = _vias(tree, board_layers)
    zones, zones_filled, unfilled = _filled_zones(tree)

    uf = _Union()

    # Track endpoints, keyed per layer so copper on different layers stays apart.
    # Sharing a point unions them automatically, because the key is the point.
    for track in tracks:
        uf.union(("cu", track["layer"], track["start"]),
                 ("cu", track["layer"], track["end"]))

    # A track ending on another track's middle is a T, and joins it. KiCAD has
    # no junction marker on a PCB: overlapping copper of one net is one node.
    by_layer_net: dict[tuple[str, str], list[dict]] = {}
    for track in tracks:
        by_layer_net.setdefault((track["layer"], track["net_name"]), []).append(track)
    for group in by_layer_net.values():
        for i, track in enumerate(group):
            for end in (track["start"], track["end"]):
                for other in group[i + 1:]:
                    if end in (other["start"], other["end"]):
                        continue  # already one key
                    reach = (track["width_mm"] + other["width_mm"]) / 2.0 + 1e-4
                    if _point_segment_distance(end, other["start"], other["end"]) <= reach:
                        uf.union(("cu", track["layer"], end),
                                 ("cu", other["layer"], other["start"]))

    # Vias bridge layers at one point.
    for via in vias:
        keys = [("cu", layer, via["point"]) for layer in via["layers"]]
        for k in keys[1:]:
            uf.union(keys[0], k)
        # A via lands mid-track as often as at an end.
        for track in tracks:
            if track["layer"] in via["layers"] and track["net_name"] == via["net_name"]:
                if _on_segment(via["point"], track["start"], track["end"]):
                    uf.union(("cu", track["layer"], track["start"]),
                             ("cu", track["layer"], via["point"]))

    # Copper that lands in a filled plane. On a 4-layer board an SMD pad
    # reaches the inner GND plane only this way: pad -> via -> zone.
    # Each filled polygon is its own copper island: a pour split by tracks or
    # a narrow neck is two pieces, joined only through pads, tracks or vias.
    for zi, zone in enumerate(zones):
        zone_key = ("zone", zi)
        uf.add(zone_key)
        for via in vias:
            if via["net_name"] != zone["net_name"] or zone["layer"] not in via["layers"]:
                continue
            if _zone_touches_point(via["point"], via["radius_mm"], zone["points"]):
                uf.union(zone_key, ("cu", zone["layer"], via["point"]))
        for track in tracks:
            if track["net_name"] != zone["net_name"] or track["layer"] != zone["layer"]:
                continue
            for end in (track["start"], track["end"]):
                if _zone_touches_point(end, track["width_mm"] / 2.0, zone["points"]):
                    uf.union(zone_key, ("cu", track["layer"], track["start"]))
                    break

    # Pads.
    for i, pad in enumerate(pads):
        pad_key = ("pad", i)
        uf.add(pad_key)
        if not pad["net_name"]:
            continue
        for track in tracks:
            if track["net_name"] != pad["net_name"]:
                continue
            if _touches(pad, track):
                uf.union(pad_key, ("cu", track["layer"], track["start"]))
        for via in vias:
            if via["net_name"] != pad["net_name"]:
                continue
            if math.hypot(via["point"][0] - pad["point"][0],
                          via["point"][1] - pad["point"][1]) <= pad["radius_mm"] + 1e-4:
                uf.union(pad_key, ("cu", via["layers"][0], via["point"]))
        for zi, zone in enumerate(zones):
            if zone["net_name"] != pad["net_name"] or zone["layer"] not in pad["layers"]:
                continue
            if _zone_touches_pad(pad, zone["points"]):
                uf.union(pad_key, ("zone", zi))

    # Pads of one net whose copper overlaps on a shared layer are one node,
    # as in KiCAD: a USB-C receptacle stacks A4 / B9 and A1 / B12 on one land.
    by_pad_net: dict[str, list[int]] = {}
    for i, pad in enumerate(pads):
        if pad["net_name"] and len(pad.get("corners") or []) == 4:
            by_pad_net.setdefault(pad["net_name"], []).append(i)
    for idx in by_pad_net.values():
        for k, i in enumerate(idx):
            for j in idx[k + 1:]:
                a, b = pads[i], pads[j]
                if not set(a["layers"]) & set(b["layers"]):
                    continue
                reach = a["radius_mm"] + b["radius_mm"]
                if abs(a["point"][0] - b["point"][0]) > reach or                         abs(a["point"][1] - b["point"][1]) > reach:
                    continue
                if _quad_gap(a["corners"], b["corners"]) <= 1e-6:
                    uf.union(("pad", i), ("pad", j))

    # Collect pads per net, grouped by what they are joined to.
    by_net: dict[str, dict] = {}
    for i, pad in enumerate(pads):
        if not pad["net_name"]:
            continue
        entry = by_net.setdefault(
            pad["net_name"], {"net": pad["net"], "name": pad["net_name"], "groups": {}}
        )
        root = uf.find(("pad", i))
        entry["groups"].setdefault(root, []).append(pad)

    # Copper of a net that reaches none of its pads: a track stub left by the
    # router, a via on nothing, a pour island. KiCAD's DRC lists each one as
    # an unconnected item when the net has other copper.
    pad_roots = {uf.find(("pad", i)) for i, p in enumerate(pads) if p["net_name"]}
    islands: dict[Any, dict] = {}

    def _island(key: Any, net_name: str, kind: str, at: Point, layer: str) -> None:
        if not net_name:
            return
        root = uf.find(key)
        if root in pad_roots:
            return
        isl = islands.setdefault(root, {"net": net_name, "items": {}, "at": list(at),
                                        "layer": layer})
        isl["items"][kind] = isl["items"].get(kind, 0) + 1

    for track in tracks:
        _island(("cu", track["layer"], track["start"]), track["net_name"], "track",
                track["start"], track["layer"])
    for via in vias:
        _island(("cu", via["layers"][0], via["point"]), via["net_name"], "via",
                via["point"], via["layers"][0])
    for zi, zone in enumerate(zones):
        _island(("zone", zi), zone["net_name"], "zone_fill", zone["points"][0], zone["layer"])

    return {
        "pads": pads,
        "nets": by_net,
        "islands": list(islands.values()),
        "zones_filled": zones_filled,
        "unfilled_zones": unfilled,
        "board_layers": board_layers,
    }


def _nearest_pair(a: list[dict], b: list[dict]) -> tuple[dict, dict, float]:
    best = None
    for pa in a:
        for pb in b:
            d = math.hypot(pa["point"][0] - pb["point"][0], pa["point"][1] - pb["point"][1])
            if best is None or d < best[2]:
                best = (pa, pb, d)
    return best  # type: ignore[return-value]


def list_unrouted(tree: list) -> dict:
    """Connections a net still needs, longest first.

    One entry per missing link: a net whose pads fall into `k` groups needs
    `k - 1` of them. Each entry names the closest pad pair between the two
    groups it would join, which is the line KiCAD draws in its ratsnest.
    """
    conn = build_connectivity(tree)
    items: list[dict] = []

    for net in conn["nets"].values():
        groups = list(net["groups"].values())
        if len(groups) < 2:
            continue
        # Greedy: repeatedly absorb the group closest to the one being built.
        merged = groups[0]
        rest = groups[1:]
        while rest:
            best_idx, best_pair = 0, None
            for idx, group in enumerate(rest):
                pair = _nearest_pair(merged, group)
                if best_pair is None or pair[2] < best_pair[2]:
                    best_idx, best_pair = idx, pair
            pa, pb, dist = best_pair  # type: ignore[misc]
            items.append(
                {
                    "net": net["name"] or str(net["net"]),
                    # KiCAD 10 boards name nets; a legacy index only when set.
                    **({"net_number": net["net"]} if net["net"] else {}),
                    "from": {"ref": pa["ref"], "pad": pa["pad"],
                             "position_mm": list(pa["point"])},
                    "to": {"ref": pb["ref"], "pad": pb["pad"],
                           "position_mm": list(pb["point"])},
                    "distance_mm": round_mm(dist),
                }
            )
            merged = merged + rest.pop(best_idx)

    items.sort(key=lambda i: i["distance_mm"], reverse=True)
    islands = [{"net": i["net"], "items": i["items"], "layer": i["layer"],
                "at_mm": [round_mm(v) for v in i["at"]]} for i in conn["islands"]]
    return {
        "count": len(items),
        "unrouted": items,
        "islands": islands,
        "zones_filled": conn["zones_filled"],
        "unfilled_zones": conn["unfilled_zones"],
    }


def net_route_status(tree: list, net_name: str) -> dict:
    """Whether one net is fully routed, and how its pads are grouped."""
    conn = build_connectivity(tree)
    wanted = normalize_name(net_name)
    for net in conn["nets"].values():
        if normalize_name(net["name"]) != wanted:
            continue
        groups = list(net["groups"].values())
        return {
            "net": net["name"],
            **({"net_number": net["net"]} if net["net"] else {}),
            "pads": sum(len(g) for g in groups),
            "connected_groups": [
                [{"ref": p["ref"], "pad": p["pad"]} for p in group] for group in groups
            ],
            "routed": len(groups) <= 1,
            "zones_filled": conn["zones_filled"],
        }
    raise KeyError(f"no net named {net_name!r} on this board")


# --------------------------------------------------------------------------- #
# Pad gaps inside a footprint (net-class clearance check)
# --------------------------------------------------------------------------- #


def _quad_gap(a: list[Point], b: list[Point]) -> float:
    """Edge-to-edge distance of two convex quads; 0 when they overlap."""
    if any(_point_in_polygon(p, b) for p in a) or any(_point_in_polygon(p, a) for p in b):
        return 0.0
    best = float("inf")
    for i in range(4):
        a1, a2 = a[i], a[(i + 1) % 4]
        for j in range(4):
            b1, b2 = b[j], b[(j + 1) % 4]
            if _segments_cross(a1, a2, b1, b2):
                return 0.0
            best = min(best, _point_segment_distance(a1, b1, b2),
                       _point_segment_distance(b1, a1, a2))
    return best


def tight_pad_gaps(tree: list, nets: set[str], clearance_mm: float) -> list[dict]:
    """Footprints where a pad on one of `nets` sits closer than `clearance_mm`
    to a pad of another net of the same footprint.

    A net-class clearance applies pad-to-pad inside one footprint too, so a
    class wider than a part's own land spacing makes DRC flag the part on
    every board. One entry per footprint: the tightest pair and its gap.
    """
    wanted = {normalize_name(n) for n in nets}
    by_ref: dict[str, list[dict]] = {}
    for pad in list_pads(tree):
        if pad["net_name"] and pad.get("corners"):
            by_ref.setdefault(pad["ref"], []).append(pad)
    out = []
    for ref, pads in by_ref.items():
        worst = None
        for i, pa in enumerate(pads):
            for pb in pads[i + 1:]:
                if pa["net_name"] == pb["net_name"]:
                    continue
                if normalize_name(pa["net_name"]) not in wanted and                         normalize_name(pb["net_name"]) not in wanted:
                    continue
                if not set(pa["layers"]) & set(pb["layers"]):
                    continue
                gap = _quad_gap(pa["corners"], pb["corners"])
                if gap < clearance_mm - 1e-6 and (worst is None or gap < worst["gap_mm"]):
                    worst = {"ref": ref, "pads": [pa["pad"], pb["pad"]],
                             "nets": [pa["net_name"], pb["net_name"]], "gap_mm": round_mm(gap)}
        if worst:
            out.append(worst)
    return sorted(out, key=lambda w: w["gap_mm"])
