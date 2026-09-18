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


def _net_of(node: list) -> tuple[int, str]:
    """Read a `(net N "name")` child. Older files omit the number."""
    net_node = find_child(node, "net")
    if not net_node or len(net_node) < 2:
        return 0, ""
    first = net_node[1]
    if isinstance(first, int):
        name = str(net_node[2]) if len(net_node) > 2 else ""
        return first, normalize_name(name)
    # `(net "VCC")` — a name with no number.
    return 0, normalize_name(str(first))


def _pad_radius(pad: list) -> float:
    """Radius of the pad's bounding circle — half its diagonal.

    Half the longer side is not enough: a track may legitimately end at a
    rectangular pad's corner, which sits at `hypot(w, h) / 2` from the centre.
    """
    size = find_child(pad, "size")
    if size and len(size) >= 3:
        return math.hypot(float(size[1]), float(size[2])) / 2.0
    return 0.25


def list_pads(tree: list) -> list[dict]:
    """Every pad on the board, with its absolute position, net and layers."""
    board_layers = copper_layers(tree)
    out: list[dict] = []
    for fp in tree[1:]:
        if not is_call(fp, "footprint"):
            continue
        ref = None
        for prop in find_children(fp, "property"):
            from kicad_claude.adapters.sch_io import property_name_index

            i = property_name_index(prop)
            if len(prop) > i + 1 and prop[i] == "Reference":
                ref = str(prop[i + 1])
                break
        fx, fy, fa = _footprint_rotation(fp)
        fp_layer_node = find_child(fp, "layer")
        fp_layer = fp_layer_node[1] if fp_layer_node and len(fp_layer_node) > 1 else "F.Cu"

        for pad in find_children(fp, "pad"):
            if len(pad) < 2:
                continue
            number = str(pad[1])
            pad_type = str(pad[2]) if len(pad) > 2 else ""
            net, net_name = _net_of(pad)
            pat = find_child(pad, "at")
            px = float(pat[1]) if pat and len(pat) > 1 else 0.0
            py = float(pat[2]) if pat and len(pat) > 2 else 0.0

            layers_node = find_child(pad, "layers")
            spec = [str(x) for x in (layers_node[1:] if layers_node else [])]
            layers = _expand_layers(spec, board_layers)
            if not layers and pad_type != "np_thru_hole":
                layers = [fp_layer]

            out.append(
                {
                    "ref": ref or "?",
                    "pad": number,
                    "type": pad_type,
                    "net": net,
                    "net_name": net_name,
                    "point": pad_to_board_xy(fx, fy, fa, px, py),
                    "layers": layers,
                    "radius_mm": _pad_radius(pad),
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
        net, _name = _net_of(node)
        out.append(
            {
                "start": _pt(start[1], start[2]),
                "end": _pt(end[1], end[2]),
                "layer": str(layer_node[1]) if layer_node and len(layer_node) > 1 else "",
                "net": net,
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
        net, _name = _net_of(node)
        size = find_child(node, "size")
        radius = float(size[1]) / 2.0 if size and len(size) > 1 else 0.3
        out.append(
            {
                "point": _pt(at[1], at[2]),
                "layers": span,
                "net": net,
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
        net, _name = _net_of(node)
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
                    }
                )
        if not polys:
            unfilled += 1
        zones.extend(polys)
    return zones, unfilled == 0, unfilled


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


def _zone_touches_pad(pad: dict, poly: list[Point]) -> bool:
    """True if a filled zone reaches the pad.

    Two cases. A pad swallowed by the pour has its centre inside the polygon.
    A thermally relieved pad sits in a hole of that polygon and is joined by
    narrow spokes, so its centre reads as outside — but the spoke copper runs
    over the pad, which puts the polygon boundary within the pad's own radius.
    """
    return _zone_touches_point(pad["point"], pad["radius_mm"], poly)


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
    by_layer_net: dict[tuple[str, int], list[dict]] = {}
    for track in tracks:
        by_layer_net.setdefault((track["layer"], track["net"]), []).append(track)
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
            if track["layer"] in via["layers"] and track["net"] == via["net"]:
                if _on_segment(via["point"], track["start"], track["end"]):
                    uf.union(("cu", track["layer"], track["start"]),
                             ("cu", track["layer"], via["point"]))

    # Copper that lands in a filled plane. On a 4-layer board an SMD pad
    # reaches the inner GND plane only this way: pad -> via -> zone.
    for zone in zones:
        zone_key = ("zone", zone["layer"], zone["net"])
        for via in vias:
            if via["net"] != zone["net"] or zone["layer"] not in via["layers"]:
                continue
            if _zone_touches_point(via["point"], via["radius_mm"], zone["points"]):
                uf.union(zone_key, ("cu", zone["layer"], via["point"]))
        for track in tracks:
            if track["net"] != zone["net"] or track["layer"] != zone["layer"]:
                continue
            for end in (track["start"], track["end"]):
                if _zone_touches_point(end, 0.0, zone["points"]):
                    uf.union(zone_key, ("cu", track["layer"], track["start"]))
                    break

    # Pads.
    for i, pad in enumerate(pads):
        pad_key = ("pad", i)
        uf.add(pad_key)
        if pad["net"] == 0:
            continue
        for track in tracks:
            if track["net"] != pad["net"]:
                continue
            if _touches(pad, track):
                uf.union(pad_key, ("cu", track["layer"], track["start"]))
        for via in vias:
            if via["net"] != pad["net"]:
                continue
            if math.hypot(via["point"][0] - pad["point"][0],
                          via["point"][1] - pad["point"][1]) <= pad["radius_mm"] + 1e-4:
                uf.union(pad_key, ("cu", via["layers"][0], via["point"]))
        for zone in zones:
            if zone["net"] != pad["net"] or zone["layer"] not in pad["layers"]:
                continue
            if _zone_touches_pad(pad, zone["points"]):
                uf.union(pad_key, ("zone", zone["layer"], zone["net"]))

    # Collect pads per net, grouped by what they are joined to.
    by_net: dict[int, dict] = {}
    for i, pad in enumerate(pads):
        if pad["net"] == 0:
            continue
        entry = by_net.setdefault(
            pad["net"], {"net": pad["net"], "name": pad["net_name"], "groups": {}}
        )
        root = uf.find(("pad", i))
        entry["groups"].setdefault(root, []).append(pad)

    return {
        "pads": pads,
        "nets": by_net,
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
                    "net_number": net["net"],
                    "from": {"ref": pa["ref"], "pad": pa["pad"],
                             "position_mm": list(pa["point"])},
                    "to": {"ref": pb["ref"], "pad": pb["pad"],
                           "position_mm": list(pb["point"])},
                    "distance_mm": round_mm(dist),
                }
            )
            merged = merged + rest.pop(best_idx)

    items.sort(key=lambda i: i["distance_mm"], reverse=True)
    return {
        "count": len(items),
        "unrouted": items,
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
            "net_number": net["net"],
            "pads": sum(len(g) for g in groups),
            "connected_groups": [
                [{"ref": p["ref"], "pad": p["pad"]} for p in group] for group in groups
            ],
            "routed": len(groups) <= 1,
            "zones_filled": conn["zones_filled"],
        }
    raise KeyError(f"no net named {net_name!r} on this board")
