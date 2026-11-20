"""Schematic layout: field autoplace and the readability check.

Autoplace follows KiCAD's `AutoplaceFields` for the common case: the fields go
on the side of the symbol with the fewest pins (ties: right, top, left,
bottom), read horizontally, stacked one font pitch apart, a padding away from
the body-and-pins box and rounded to the 1.27 mm grid away from the symbol.
On KiCAD 10's demo schematics this lands exactly where KiCAD put the fields
of R / R_Small / C at rotation 0 and 180 (R at 0: x + 2.54, y ∓ 1.27), and
within one grid step above the part at 90 / 270, where the demos themselves
differ by KiCAD version. A symbol whose best side still carries pins (a
large IC) keeps its library placement unless `force=True`.

The readability check reports what makes a sheet hard to read — it never
moves anything. Rule IDs are `SR-*`.
"""

from __future__ import annotations

import math
from collections import defaultdict

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_geometry as g
from kicad_claude.adapters.sch_io import find_child, find_children, head_of, is_call, sym
from kicad_claude.adapters.sch_netlist import label_joined_nets

SIDES = ("right", "top", "left", "bottom")
PAD = 0.635          # KiCAD's field padding, 25 mil
PITCH = 2.0          # × font size between stacked fields (2.54 at 1.27)


def _field_boxes(fields: list, places: list, horizontal: str, size: float) -> list:
    return [g.text_box(x, y, *g.text_extent(str(f[2]) if len(f) > 2 else "", size), 0,
                       horizontal, "center") for f, (x, y) in zip(fields, places)]


def _others(tree: list, ref: str) -> dict:
    """What a symbol's fields must not land on: everything not its own."""
    items = _items(tree)
    return {"texts": [t["box"] for t in items["texts"] if t["owner"] != ref],
            "bodies": [b["box"] for b in items["bodies"] if b["ref"] != ref],
            "wires": items["wires"]}


def _collisions(boxes: list, others: dict) -> int:
    """Other texts, bodies (ports too) and wires that the field boxes touch."""
    hits = 0
    for bx in boxes:
        inner = g.shrink(bx, 0.1)
        hits += sum(g.overlap_area(bx, t) > 0.15 for t in others["texts"])
        hits += sum(g.overlap_area(inner, g.shrink(b, 0.1)) > 0 for b in others["bodies"])
        hits += sum(g.segment_hits_box(a, c, inner) for a, c in others["wires"])
    return hits


# --------------------------------------------------------------------------- #
# Field autoplace
# --------------------------------------------------------------------------- #


def _set_justify(prop: list, horizontal: str) -> None:
    eff = find_child(prop, "effects")
    if eff is None:
        eff = [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]]
        prop.append(eff)
    eff[:] = [c for c in eff if not is_call(c, "justify")]
    if horizontal != "center":
        hide_at = next((i for i, c in enumerate(eff) if is_call(c, "hide")), len(eff))
        eff.insert(hide_at, [sym("justify"), sym(horizontal)])


def _mark_autoplaced(s_node: list) -> None:
    if find_child(s_node, "fields_autoplaced") is not None:
        return
    at_uuid = next((i for i, c in enumerate(s_node) if is_call(c, "uuid")), len(s_node))
    s_node.insert(at_uuid, [sym("fields_autoplaced"), sym("yes")])


def autoplace_fields(tree: list, s_node: list, force: bool = False,
                     side: str | None = None) -> dict:
    """Place a symbol's visible fields beside its body. Returns what was done.

    `side` forces the side (used for a power port turned away from its pin:
    its name goes beyond the tip). Power ports are left alone otherwise.
    """
    ref = ed.get_symbol_property(s_node, "Reference") or "?"
    if g.is_power(tree, s_node) and side is None:
        return {"reference": ref, "placed": False, "reason": "power port"}
    fields = g.visible_fields(s_node)
    box = g.body_and_pins_box(tree, s_node)
    if not fields or box is None:
        return {"reference": ref, "placed": False, "reason": "no visible fields or body"}

    counts = dict.fromkeys(SIDES, 0)
    for p in g.pins(tree, s_node):
        if not p["hidden"]:
            counts[g.side_of(p["out"])] += 1
    side_given = side is not None
    if side is None:
        side = min(SIDES, key=lambda s: (counts[s], SIDES.index(s)))
    else:
        force = True
    if counts[side] and not force:
        return {"reference": ref, "placed": False, "side": side,
                "reason": f"every side has pins ({counts}); library placement kept"}
    # KiCAD's choice first (fewest pins, then right / top / left / bottom),
    # then the other sides; pin-free sides only unless forced.
    order = ([side] if side_given else
             sorted((sd for sd in SIDES if force or not counts[sd]),
                    key=lambda sd: (counts[sd], SIDES.index(sd))))

    fr = g.Frame(s_node)
    size = max(g.text_size(f) for f in fields)
    pitch = PITCH * size
    n = len(fields)
    # Grid steps from the symbol origin, as KiCAD rounds them.
    cx = round(fr.x + g.snap((box[0] + box[2]) / 2 - fr.x), 4)
    cy = round(fr.y + g.snap((box[1] + box[3]) / 2 - fr.y), 4)

    def places_for(sd: str) -> tuple[list[tuple[float, float]], str]:
        if sd in ("right", "left"):
            x = (g.round_away(box[2] + PAD, fr.x) if sd == "right"
                 else g.round_away(box[0] - PAD, fr.x))
            return ([(x, round(cy + (i - (n - 1) / 2) * pitch, 4)) for i in range(n)],
                    "left" if sd == "right" else "right")
        if sd == "top":
            y_last = g.round_away(box[1] - PAD - size / 2, fr.y)
            return [(cx, round(y_last - (n - 1 - i) * pitch, 4)) for i in range(n)], "center"
        y_first = g.round_away(box[3] + PAD + size / 2, fr.y)
        return [(cx, round(y_first + i * pitch, 4)) for i in range(n)], "center"

    def shifted(sd: str, k: int) -> tuple[list[tuple[float, float]], str]:
        pts, hz = places_for(sd)
        d = round(k * 1.27, 4)
        if sd in ("right", "left"):
            return [(x, round(y + d, 4)) for x, y in pts], hz
        return [(round(x + d, 4), y) for x, y in pts], hz

    # Keep KiCAD's spot when it is clear; otherwise the side and grid offset
    # along it whose texts land on the fewest other texts, bodies and wires.
    others = _others(tree, ref)
    best = None
    for rank, sd in enumerate(order):
        for k in (0, 1, -1, 2, -2, 3, -3):
            pts, hz = shifted(sd, k)
            hits = _collisions(_field_boxes(fields, pts, hz, size), others)
            if best is None or (hits, rank, abs(k)) < best[0]:
                best = ((hits, rank, abs(k)), sd, pts, hz)
            if hits == 0:
                break
        if best[0][0] == 0:
            break
    (collisions, _, _), side, places, horizontal = best

    # The stored angle is relative to the symbol: pick the one that reads
    # horizontally, then pre-flip the justification KiCAD will flip back.
    file_angle = 0 if g.is_text_flipped(fr, 0)[0] == 0 else 90
    flipped = g.is_text_flipped(fr, file_angle)[1]
    stored = horizontal if not flipped else {"left": "right", "right": "left"}.get(
        horizontal, horizontal)

    for prop, (x, y) in zip(fields, places):
        at = find_child(prop, "at")
        if at is None:
            at = [sym("at"), 0, 0, 0]
            prop.insert(3, at)
        while len(at) < 4:
            at.append(0)
        at[1], at[2], at[3] = x, y, file_angle
        _set_justify(prop, stored)
    _mark_autoplaced(s_node)
    out = {"reference": ref, "placed": True, "side": side,
           "fields": [str(f[1]) for f in fields]}
    if collisions:
        out["collisions"] = collisions
    return out


# --------------------------------------------------------------------------- #
# Readability check
# --------------------------------------------------------------------------- #

SEVERITY = {
    "SR-OVERLAP": "warning", "SR-TEXT-ON-BODY": "warning", "SR-LABEL-INTO-BODY": "warning",
    "SR-FRAME": "warning", "SR-TITLE-BLOCK": "warning", "SR-WIRE-THROUGH-BODY": "warning",
    "SR-TEXT-OVER-WIRE": "info", "SR-SHORT-LABEL-NET": "info", "SR-CROSSINGS": "info",
    "SR-OFF-GRID": "warning", "SR-PORT-INTO-BODY": "warning",
}


FRAME_BAND_MM = 2.0  # KiCAD's default sheet border: two frames 2 mm apart


def _items(tree: list) -> dict:
    """Every drawn thing with a box: bodies, fields, labels, notes, wires."""
    bodies, texts, wires = [], [], []
    for s in ed.iter_instance_symbols(tree):
        ref = ed.get_symbol_property(s, "Reference") or "?"
        power = g.is_power(tree, s)
        b = g.body_box(tree, s)
        if b is not None:
            bodies.append({"ref": ref, "box": b, "power": power, "node": s,
                           "parts": g.body_part_boxes(tree, s),
                           "net": ed.get_symbol_property(s, "Value") if power else None})
        for prop in g.visible_fields(s):
            fb = g.field_box(s, prop)
            if fb is not None:
                texts.append({"kind": "field", "id": f"{ref}.{prop[1]}", "owner": ref,
                              "text": str(prop[2]), "box": fb, "power": power})
    for node in tree[1:]:
        h = head_of(node)
        if h in ("label", "global_label", "hierarchical_label"):
            lb = g.label_box(node)
            if lb is not None:
                at = find_child(node, "at")
                texts.append({"kind": h, "id": f"{h}:{node[1]}@{at[1]},{at[2]}",
                              "owner": None, "text": str(node[1]), "box": lb,
                              "anchor": (float(at[1]), float(at[2]))})
        elif h == "text":
            nb = g.note_box(node)
            if nb is not None:
                texts.append({"kind": "note", "id": f"note:{ed.get_uuid(node)}", "owner": None,
                              "text": str(node[1])[:30], "box": nb})
        elif h == "wire":
            pts = find_child(node, "pts")
            xys = find_children(pts, "xy") if pts else []
            if len(xys) >= 2:
                wires.append(((float(xys[0][1]), float(xys[0][2])),
                              (float(xys[-1][1]), float(xys[-1][2]))))
    return {"bodies": bodies, "texts": texts, "wires": wires}


def check_readability(tree: list, max_findings: int = 60,
                      short_label_mm: float = 15.0) -> dict:
    items = _items(tree)
    bodies, texts, wires = items["bodies"], items["texts"], items["wires"]
    findings: list[dict] = []

    def add(rule: str, message: str, where, refs=None, fix: str | None = None):
        f = {"rule": rule, "severity": SEVERITY[rule], "message": message,
             "at_mm": [round(where[0], 2), round(where[1], 2)]}
        if refs:
            f["items"] = refs
        if fix:
            f["fix"] = fix
        findings.append(f)

    def centre(b):
        return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2

    # Text vs text. Raw boxes: two texts whose corners touch already read
    # as one (a field over a port's net name).
    for i, a in enumerate(texts):
        for b in texts[i + 1:]:
            if a["owner"] is not None and a["owner"] == b["owner"]:
                continue  # one symbol's own fields: autoplace stacks them
            if g.overlap_area(a["box"], b["box"]) > 0.15:
                add("SR-OVERLAP", f"{a['id']} overlaps {b['id']}", centre(a["box"]),
                    [a["id"], b["id"]], "move one item or `autoplace_fields`")

    # Text vs bodies (power ports and flags too: a field over a port).
    for t in texts:
        tb = g.inset(t["box"], 0.15)
        for body in bodies:
            if body["power"] and t["owner"] == body["ref"]:
                continue
            if g.overlap_area(tb, body["box"]) <= 0.3:
                continue
            if t["kind"] == "field" and t["owner"] == body["ref"]:
                if _inside(t["box"], body["box"]):
                    continue  # text inside its own body is deliberate (R value, IC name)
                add("SR-TEXT-ON-BODY", f"{t['id']} is drawn over its own symbol",
                    centre(t["box"]), [t["id"]], "`autoplace_fields`")
            elif t["kind"] in ("label", "global_label", "hierarchical_label"):
                add("SR-LABEL-INTO-BODY", f"{t['id']} runs into {body['ref']}",
                    t.get("anchor") or centre(t["box"]), [t["id"], body["ref"]],
                    "point the label away from the body, or add a wire stub")
            else:
                add("SR-OVERLAP", f"{t['id']} overlaps symbol {body['ref']}",
                    centre(t["box"]), [t["id"], body["ref"]], "move the text or the symbol")

    # Bodies vs bodies (power ports included: a GND drawn into a part, two
    # ports stacked on one spot).
    for i, a in enumerate(bodies):
        for b in bodies[i + 1:]:
            if g.overlap_area(g.inset(a["box"], 0.1), g.inset(b["box"], 0.1)) > 0.2:
                if a["power"] and b["power"]:
                    add("SR-OVERLAP", f"ports {a['ref']} and {b['ref']} are stacked",
                        centre(a["box"]), [a["ref"], b["ref"]],
                        "move one port, or `join_power` the pins (one wire, one port)")
                    continue
                rule = "SR-PORT-INTO-BODY" if (a["power"] or b["power"]) else "SR-OVERLAP"
                if rule == "SR-PORT-INTO-BODY":
                    port, part = (a, b) if a["power"] else (b, a)
                    pb_ = g.shrink(port["box"], 0.1)
                    if not any(min(pb_[2], pb[2]) >= max(pb_[0], pb[0])
                               and min(pb_[3], pb[3]) >= max(pb_[1], pb[1])
                               for pb in part["parts"]):
                        continue  # only the part's decoration box is touched
                add(rule, f"{a['ref']} and {b['ref']} overlap", centre(a["box"]),
                    [a["ref"], b["ref"]],
                    "rotate the power port away from the body" if rule == "SR-PORT-INTO-BODY"
                    else "move one symbol")

    # Frame and title block. The frame's outer 2 mm band carries the zone
    # letters: an item in it runs into the border.
    frame, tblock = g.frame_and_title_block(tree)
    frame = g.inset(frame, FRAME_BAND_MM)
    everything = [(t["id"], t["box"]) for t in texts] + [(b["ref"], b["box"]) for b in bodies]
    for ident, box in everything:
        if box[0] < frame[0] or box[1] < frame[1] or box[2] > frame[2] or box[3] > frame[3]:
            add("SR-FRAME", f"{ident} runs onto or past the sheet border", centre(box), [ident],
                "move it inside, or `set_page` to a larger paper")
        elif g.overlap_area(box, tblock) > 0:
            add("SR-TITLE-BLOCK", f"{ident} overlaps the title block", centre(box), [ident],
                "move it out of the bottom-right title block")

    # Wires through bodies and texts over wires.
    for p, q in wires:
        for body in bodies:
            if body["power"]:
                continue
            # The body's box spans decoration (an LED's emission arrows);
            # a hit counts only on one of its graphics.
            if g.segment_hits_box(p, q, g.inset(body["box"], 0.3)) and any(
                    g.segment_hits_box(p, q, g.shrink(part, 0.3)) for part in body["parts"]):
                add("SR-WIRE-THROUGH-BODY", f"wire {p}->{q} crosses {body['ref']}",
                    ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2), [body["ref"]],
                    "route around the symbol")
        for t in texts:
            if t["kind"] in ("label", "global_label", "hierarchical_label"):
                continue  # a label sits on its wire
            if g.segment_hits_box(p, q, g.inset(t["box"], 0.3)):
                add("SR-TEXT-OVER-WIRE", f"wire {p}->{q} runs through {t['id']}",
                    centre(t["box"]), [t["id"]], "move the text off the wire")

    # Wire crossings without a junction (X crossings).
    crossings = 0
    for i, (a1, a2) in enumerate(wires):
        for b1, b2 in wires[i + 1:]:
            if _crosses(a1, a2, b1, b2):
                crossings += 1
    if crossings > 4:
        add("SR-CROSSINGS", f"{crossings} wire crossings", (0, 0), None,
            "labels read better than long wires that cross others")

    # Nets drawn only with local labels whose ends sit close together.
    by_net: dict[str, list] = defaultdict(list)
    for t in texts:
        if t["kind"] == "label":
            by_net[t["text"]].append(t["anchor"])
    for net, anchors in by_net.items():
        if 2 <= len(anchors) <= 3:
            span = max(math.dist(a, b) for a in anchors for b in anchors)
            if span <= short_label_mm:
                add("SR-SHORT-LABEL-NET",
                    f"net {net}: {len(anchors)} labels within {span:.1f} mm",
                    anchors[0], [net], "a wire between the pins reads better")

    # Off-grid placement.
    for body in bodies:
        fr = g.Frame(body["node"])
        if abs(g.snap(fr.x) - fr.x) > 0.01 or abs(g.snap(fr.y) - fr.y) > 0.01:
            add("SR-OFF-GRID", f"{body['ref']} origin is off the 1.27 mm grid",
                (fr.x, fr.y), [body["ref"]], "move it onto the grid")

    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f["rule"]] += 1
    order = {"warning": 0, "info": 1}
    findings.sort(key=lambda f: (order[f["severity"]], f["rule"]))
    joined, multi = label_joined_nets(tree)
    return {
        "counts": dict(sorted(counts.items())),
        "label_only_nets": {"label_joined": joined, "signal_nets": multi,
                            "ratio": round(joined / multi, 2) if multi else 0.0},
        "total": len(findings),
        "findings": findings[:max_findings],
        "truncated": len(findings) > max_findings,
        "page_mm": list(g.page_size(tree)),
        "note": "Text extents are estimates of KiCAD's font; confirm visually.",
    }


def _inside(a, b) -> bool:
    return a[0] >= b[0] - 0.1 and a[1] >= b[1] - 0.1 and a[2] <= b[2] + 0.1 and a[3] <= b[3] + 0.1


def _crosses(a1, a2, b1, b2) -> bool:
    """Proper X crossing of two axis-aligned segments (not at an end)."""
    ah = abs(a1[1] - a2[1]) < 1e-6
    bh = abs(b1[1] - b2[1]) < 1e-6
    if ah == bh:
        return False
    h1, h2, v1, v2 = (a1, a2, b1, b2) if ah else (b1, b2, a1, a2)
    y, x = h1[1], v1[0]
    return (min(h1[0], h2[0]) + 1e-6 < x < max(h1[0], h2[0]) - 1e-6
            and min(v1[1], v2[1]) + 1e-6 < y < max(v1[1], v2[1]) - 1e-6)


# --------------------------------------------------------------------------- #
# Move with drag
# --------------------------------------------------------------------------- #


def _pin_map(tree: list, s_node: list) -> dict[str, tuple[float, float]]:
    return {p["number"]: p["point"] for p in g.pins(tree, s_node)}


def _same(a, b, tol: float = 1e-4) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _lookup(moves: list, pt):
    for old, new in moves:
        if _same(pt, old):
            return old, new
    return None


def _drag(tree: list, moves: list, skip: list) -> tuple[int, list]:
    """Carry what sits on each `old` point to its `new` point, all in one
    pass (a pin's new point can be another pin's old one). Wires keep the
    segment at the pin straight and bend toward their fixed end; power ports
    move whole. Returns (items moved, other symbols' pins left behind)."""
    moves = list(moves) + _stub_ends(tree, moves, skip)
    moved = 0
    broken: list[str] = []
    for node in list(tree[1:]):
        if not isinstance(node, list) or any(node is s for s in skip):
            continue
        head = head_of(node)
        if head == "wire":
            pts = find_child(node, "pts")
            xys = find_children(pts, "xy") if pts else []
            if len(xys) != 2:
                continue
            hits = [(i, _lookup(moves, (float(xy[1]), float(xy[2])))) for i, xy in enumerate(xys)]
            hits = [(i, h) for i, h in hits if h]
            if len(hits) == 2:  # both ends on moved pins: the wire moves whole
                for i, (_old, new) in hits:
                    xys[i][1], xys[i][2] = round(new[0], 4), round(new[1], 4)
                moved += 1
                continue
            for i, (old, new) in hits:
                fixed = xys[1 - i]
                fx, fy = float(fixed[1]), float(fixed[2])
                horizontal = abs(fy - old[1]) < 1e-4
                xys[i][1], xys[i][2] = round(new[0], 4), round(new[1], 4)
                if not (abs(fx - new[0]) < 1e-4 or abs(fy - new[1]) < 1e-4):
                    corner = (fx, new[1]) if horizontal else (new[0], fy)
                    other = (new[0], fy) if horizontal else (fx, new[1])
                    if _overlaps_wire(tree, node, [((fx, fy), corner), (corner, new)]) and                             not _overlaps_wire(tree, node, [((fx, fy), other), (other, new)]):
                        corner = other  # do not lay the bend on top of another wire
                    xys[i][1], xys[i][2] = round(corner[0], 4), round(corner[1], 4)
                    ed.add_wire(tree, corner[0], corner[1], new[0], new[1])
                moved += 1
        elif head in ("label", "global_label", "hierarchical_label", "no_connect", "junction"):
            at = find_child(node, "at")
            hit = _lookup(moves, (float(at[1]), float(at[2]))) if at and len(at) >= 3 else None
            if hit:
                at[1], at[2] = round(hit[1][0], 4), round(hit[1][1], 4)
                moved += 1
        elif head == "symbol" and find_child(node, "lib_id"):
            touching = [(p, _lookup(moves, p["point"])) for p in g.pins(tree, node)]
            touching = [(p, h) for p, h in touching if h]
            if not touching:
                continue
            ref = ed.get_symbol_property(node, "Reference") or "?"
            if g.is_power(tree, node):
                old, new = touching[0][1]
                fr = g.Frame(node)
                ed.move_symbol(tree, ref, fr.x + new[0] - old[0], fr.y + new[1] - old[1])
                moved += 1
            else:
                broken += [f"{ref}.{p['number']}" for p, _h in touching]
    return moved, broken


def _overlaps_wire(tree: list, own: list, segments: list) -> bool:
    """True if one of `segments` runs along another wire for some length.

    Two collinear wires that overlap are one piece of copper to KiCAD, and
    the sheet shows a single line where two wires sit: a drag must not lay
    its bend there."""
    for node in tree[1:]:
        if node is own or head_of(node) != "wire":
            continue
        xys = find_children(find_child(node, "pts"), "xy")
        if len(xys) != 2:
            continue
        a = (float(xys[0][1]), float(xys[0][2]))
        b = (float(xys[1][1]), float(xys[1][2]))
        for u, v in segments:
            for axis in (0, 1):
                k = 1 - axis  # the fixed coordinate of a line along `axis`
                if abs(u[k] - v[k]) < 1e-4 and abs(a[k] - b[k]) < 1e-4 and abs(u[k] - a[k]) < 1e-4:
                    lo = max(min(u[axis], v[axis]), min(a[axis], b[axis]))
                    hi = min(max(u[axis], v[axis]), max(a[axis], b[axis]))
                    if hi - lo > 1e-4:
                        return True
    return False


def _stub_ends(tree: list, moves: list, skip: list) -> list:
    """Far ends of stubs that should travel with their pin.

    A wire from a moved pin to a point that carries only labels, power ports
    or flags (no other wire, no other part's pin) is a stub: it moves whole,
    with what it carries, instead of stretching across the moved part.
    """
    wire_ends: dict = defaultdict(int)
    walls: set = set()
    for node in tree[1:]:
        if head_of(node) == "wire":
            xys = find_children(find_child(node, "pts"), "xy")
            for xy in (xys[0], xys[-1]) if xys else ():
                wire_ends[(round(float(xy[1]), 4), round(float(xy[2]), 4))] += 1
        elif head_of(node) == "symbol" and find_child(node, "lib_id")                 and not any(node is s for s in skip) and not g.is_power(tree, node):
            for p in g.pins(tree, node):
                walls.add(p["point"])
    extra = []
    for node in tree[1:]:
        if head_of(node) != "wire":
            continue
        xys = find_children(find_child(node, "pts"), "xy")
        if len(xys) != 2:
            continue
        ends = [(round(float(xy[1]), 4), round(float(xy[2]), 4)) for xy in xys]
        hits = [_lookup(moves, e) for e in ends]
        if sum(1 for h in hits if h) != 1:
            continue
        i = 0 if hits[0] else 1
        old, new = hits[i]
        far = ends[1 - i]
        if wire_ends[far] == 1 and far not in walls and not _lookup(moves, far):
            extra.append((far, (round(far[0] + new[0] - old[0], 4),
                                round(far[1] + new[1] - old[1], 4))))
    return extra


def move_symbol(tree: list, reference: str, x_mm: float, y_mm: float,
                rotation: float | None = None, drag: bool = False,
                mirror: str | None = None) -> dict:
    """Move / rotate / mirror a symbol; with `drag` its connections follow.

    A changed rotation or mirror re-runs field autoplace (KiCAD does the
    same for symbols whose fields were autoplaced). `mirror`: "x", "y", or
    "" to clear; None leaves it."""
    s_node = ed.find_symbol_by_reference(tree, reference)
    if s_node is None:
        raise KeyError(f"no symbol with reference {reference!r}")
    old_rot = g.Frame(s_node).rot
    before = _pin_map(tree, s_node) if drag else {}
    ed.move_symbol(tree, reference, x_mm, y_mm, rotation)
    mirrored = mirror is not None and ed.set_symbol_mirror(s_node, mirror)
    out: dict = {}
    if mirrored or (rotation is not None and float(rotation) % 360 != old_rot % 360):
        out["fields_autoplaced"] = autoplace_fields(tree, s_node)["placed"]
    if drag:
        after = _pin_map(tree, s_node)
        moves = [(old, after[num]) for num, old in before.items()
                 if num in after and not _same(old, after[num])]
        moved, broken = _drag(tree, moves, skip=[s_node]) if moves else (0, [])
        out["dragged"] = moved
        if broken:
            out["contacts_broken"] = sorted(set(broken))
    return out


_ANCHORED = ("label", "global_label", "hierarchical_label", "no_connect", "junction", "text")


def _shift_at(node: list, dx: float, dy: float) -> None:
    at = find_child(node, "at")
    if at is not None and len(at) >= 3:
        at[1], at[2] = round(float(at[1]) + dx, 4), round(float(at[2]) + dy, 4)


def move_region(tree: list, box: tuple[float, float, float, float],
                dx: float, dy: float) -> dict:
    """Translate everything inside `box` by (dx, dy).

    Symbols go when their origin is inside (fields with them); labels,
    no-connects, junctions and notes when their anchor is. Wire ends inside
    the box move, except an end on the pin of a symbol that stays; a wire
    with one moving end is rubber-banded (bent as a drag bends it), and a
    power port outside the box on a moved pin follows. Returns counts and
    the other parts' pins a moved pin left (`contacts_broken`)."""
    x1, y1, x2, y2 = min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3])

    def inside(pt) -> bool:
        return x1 - 1e-4 <= pt[0] <= x2 + 1e-4 and y1 - 1e-4 <= pt[1] <= y2 + 1e-4

    symbols, staying_pins = [], set()
    for s_node in ed.iter_instance_symbols(tree):
        fr = g.Frame(s_node)
        if inside((fr.x, fr.y)):
            symbols.append(s_node)
        else:
            staying_pins |= {p["point"] for p in g.pins(tree, s_node)}
    points = {p["point"] for s_node in symbols for p in g.pins(tree, s_node)}
    for node in tree[1:]:
        if g.is_wire(node):
            for xy in find_children(find_child(node, "pts"), "xy"):
                pt = (round(float(xy[1]), 4), round(float(xy[2]), 4))
                if inside(pt) and pt not in staying_pins:
                    points.add(pt)
    loose = []
    for node in tree[1:]:
        if head_of(node) in _ANCHORED:
            at = find_child(node, "at")
            if at is not None and len(at) >= 3 and inside((float(at[1]), float(at[2]))):
                loose.append((node, (float(at[1]), float(at[2]))))

    for s_node in symbols:
        _shift_at(s_node, dx, dy)
        for prop in find_children(s_node, "property"):
            _shift_at(prop, dx, dy)
    moves = [(pt, (round(pt[0] + dx, 4), round(pt[1] + dy, 4))) for pt in sorted(points)]
    dragged, broken = _drag(tree, moves, skip=symbols) if moves else (0, [])
    shifted = 0
    for node, old in loose:
        at = find_child(node, "at")
        if _same((float(at[1]), float(at[2])), old):  # not carried by the drag
            _shift_at(node, dx, dy)
            shifted += 1
    out = {"symbols": len(symbols), "wires_and_items": dragged + shifted}
    if broken:
        out["contacts_broken"] = sorted(set(broken))
    return out


# --------------------------------------------------------------------------- #
# Page and title block
# --------------------------------------------------------------------------- #

TITLE_FIELDS = ("title", "date", "rev", "company")


def set_page(tree: list, paper: str | None = None, portrait: bool | None = None,
             width_mm: float | None = None, height_mm: float | None = None,
             title_block: dict | None = None) -> dict:
    """Set `(paper ...)` and `(title_block ...)` the way KiCAD writes them.

    `title_block` keys: title, date, rev, company, comments {1..9: text}. A
    key left out is unchanged; an empty string removes that entry.
    """
    paper_node = find_child(tree, "paper")
    if paper is not None or portrait is not None:
        name = paper or (str(paper_node[1]) if paper_node and len(paper_node) > 1 else "A4")
        if name != "User" and name not in g.PAPER_MM:
            raise ValueError(f"paper must be one of {sorted(g.PAPER_MM)} or User")
        if portrait is None:
            portrait = bool(paper_node) and any(str(x) == "portrait" for x in paper_node[2:])
        new = [sym("paper"), name]
        if name == "User":
            if width_mm is None or height_mm is None:
                raise ValueError("paper User needs width_mm and height_mm")
            new += [float(width_mm), float(height_mm)]
        elif portrait:
            new.append(sym("portrait"))
        if paper_node is None:
            at = next((i for i, c in enumerate(tree) if is_call(c, "uuid")), 1) + 1
            tree.insert(at, new)
        else:
            paper_node[:] = new
            new = paper_node
        paper_node = new
    if title_block:
        tb = find_child(tree, "title_block")
        if tb is None:
            tb = [sym("title_block")]
            at = next((i for i, c in enumerate(tree) if c is paper_node), len(tree) - 1) + 1
            tree.insert(at, tb)
        entries: dict = {}
        for c in tb[1:]:
            h = head_of(c)
            if h == "comment" and len(c) >= 3:
                entries[("comment", int(c[1]))] = str(c[2])
            elif h in TITLE_FIELDS and len(c) >= 2:
                entries[(h, 0)] = str(c[1])
        for key in TITLE_FIELDS:
            if title_block.get(key) is not None:
                entries[(key, 0)] = str(title_block[key])
        for num, text in (title_block.get("comments") or {}).items():
            n = int(num)
            if not 1 <= n <= 9:
                raise ValueError("comment numbers are 1..9")
            entries[("comment", n)] = str(text)
        body: list = [[sym(k), entries[(k, 0)]] for k in TITLE_FIELDS if entries.get((k, 0))]
        body += [[sym("comment"), n, entries[("comment", n)]]
                 for n in range(1, 10) if entries.get(("comment", n))]
        tb[1:] = body
    w, h = g.page_size(tree)
    tb = find_child(tree, "title_block")
    pn = find_child(tree, "paper")
    return {
        "paper": str(pn[1]) if pn else "A4",
        "page_mm": [w, h],
        "title_block": {(f"comment{c[1]}" if head_of(c) == "comment" else head_of(c)): str(c[-1])
                        for c in (tb[1:] if tb else [])},
    }
