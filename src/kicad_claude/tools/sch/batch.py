"""`edit_schematic`: many schematic edits in one guarded write.

One parse, the ops in order, one `save_tree`. All or nothing: an op that
fails raises with its index and nothing is written. Pin-addressed ops
(`connect`, `wire` from/to, `no_connect`, `label` / `power` with `pin`)
take "REF.PIN" so no coordinates have to be looked up first.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_geometry as g
from kicad_claude.adapters import sch_layout as layout
from kicad_claude.adapters.sch_io import find_child, find_children
from kicad_claude.tools.common import on_sheet
from kicad_claude.tools.library import index as lib_tools
from kicad_claude.tools.sch import schematic as sch
from kicad_claude.utils.geometry import file_to_sch_xy, normalize_rotation

logger = logging.getLogger("kicad-claude.tools.sch.batch")

_DIR_TO_ORIENT = {(1, 0): "right", (-1, 0): "left", (0, -1): "up", (0, 1): "down"}
OPS = ("symbol", "wire", "label", "power", "pwr_flag", "connect", "name_net", "no_connect",
       "junction", "text", "move", "move_region", "join_power", "place_chain", "remove",
       "property", "field", "autoplace", "hier_label", "sheet_pin")


class OpError(ValueError):
    pass


class _Batch:
    """State shared by the ops of one call."""

    def __init__(self, tree: list):
        self.tree = tree
        self.proj = state.get_active()
        self.instance_path = sch._instance_path()
        refs = list(ed.all_references(tree))
        refs.extend(ed.all_references_in_hierarchy(self.proj.sch_path))
        self.refs = refs
        self.warnings: list[str] = []
        self._footprints = None
        self.ported: set[str] = set()       # nets this batch drew ports on
        self._flagged: set[str] | None = None

    def flagged(self, net: str) -> bool:
        """The net carries a PWR_FLAG already (on the sheet or from this batch)."""
        if self._flagged is None:
            from kicad_claude.adapters import sch_netlist

            self._flagged = {
                name for name, n in sch_netlist.build_sheet_nets(self.tree).nets.items()
                if any(pin["ref"].startswith("#FLG") for pin in n["pins"])}
        return net in self._flagged

    # ----- helpers -------------------------------------------------------- #

    def symbol_node(self, ref: str) -> list:
        node = ed.find_symbol_by_reference(self.tree, ref)
        if node is None:
            raise OpError(f"no symbol {ref!r}")
        return node

    def pin(self, addr: str) -> dict:
        if not isinstance(addr, str) or "." not in addr:
            raise OpError(f"pin address must be 'REF.PIN', got {addr!r}")
        ref, num = addr.split(".", 1)
        for p in g.pins(self.tree, self.symbol_node(ref)):
            if p["number"] == num:
                return p
        raise OpError(f"{ref} has no pin {num!r}")

    def point(self, op: dict, prefix: str = "") -> tuple[float, float]:
        x, y = op.get(f"x{prefix}_mm"), op.get(f"y{prefix}_mm")
        if x is None or y is None:
            raise OpError(f"needs x{prefix}_mm and y{prefix}_mm")
        return sch._snap(float(x), float(y), op.get("snap_to_grid", True))

    def place(self, lib_id: str, ref: str, value: str, x: float, y: float, rotation: float,
              footprint: str = "", fields: dict | None = None) -> list:
        lib_path, sym_name, meta = sch._resolve_lib_symbol(lib_id)
        sym_def = ed.fetch_symbol_def(lib_path, sym_name)
        if footprint:
            if self._footprints is None:
                self._footprints = lib_tools._ensure_index().get("footprints") or {}
            if self._footprints and footprint not in self._footprints:
                hint = lib_tools.suggest_id(footprint, self._footprints)
                self.warnings.append(f"{ref}: footprint {footprint!r} is not in the library index"
                                     + (f"; did you mean {hint!r}?" if hint else ""))
        node = ed.add_symbol(
            self.tree, qualified_lib_id=lib_id, reference=ref, value=value,
            x_mm=x, y_mm=y, rotation=rotation, sym_def_node=sym_def,
            project_name=self.proj.name, instance_path=self.instance_path,
            footprint=footprint or meta.get("default_footprint", ""),
            datasheet=meta.get("datasheet", "~"), description=meta.get("description", ""),
        )
        for name, val in (fields or {}).items():
            if name in ("Reference", "Value", "Footprint"):
                raise OpError(f"set {name} with its own key, not in fields")
            if ed.get_symbol_property(node, name) is None:
                ed.add_symbol_property(node, name, str(val))
            else:
                ed.set_symbol_property(node, name, str(val))
        self.refs.append(ref)
        return node

    def power_port(self, net: str, x: float, y: float, rotation: float | None,
                   out: tuple[int, int] | None, base_symbol: str = "",
                   trial: bool = False) -> str:
        candidate, _custom, _warns = sch._power_choice(self.tree, net, base_symbol)
        if net != "PWR_FLAG" and not trial:
            self.ported.add(net)  # the driver reminder is judged on the result
        lib_path, sym_name, meta = sch._resolve_lib_symbol(candidate)
        sym_def = ed.fetch_symbol_def(lib_path, sym_name)
        ref = ed.next_virtual_reference(self.refs, ed.library_reference_prefix(sym_def))
        node = ed.add_symbol(
            self.tree, qualified_lib_id=candidate, reference=ref, value=net,
            x_mm=x, y_mm=y, rotation=0, sym_def_node=sym_def,
            project_name=self.proj.name, instance_path=self.instance_path,
            footprint=meta.get("default_footprint", ""),
            datasheet=meta.get("datasheet", "~"), description=meta.get("description", ""),
        )
        self.refs.append(ref)
        if rotation is None and out is not None:
            from kicad_claude.adapters.pcb_geometry import is_ground_net

            rotation = _port_rotation(self.tree, node, out, ground_like=is_ground_net(net))
        if rotation:
            ed.move_symbol(self.tree, ref, x, y, normalize_rotation(rotation))
            if out is not None:
                # Name beyond the tip, reading horizontally, off the wire.
                layout.autoplace_fields(self.tree, node, side=g.side_of(out))
        if not trial:
            # KiCAD joins a pin to a wire only at the wire's end, or under a
            # junction: a port dropped mid-wire needs the dot.
            ed.junction_mid_wire_pins(self.tree, node)
        return ref

    def is_power_net(self, net: str) -> bool:
        from kicad_claude.adapters.pcb_geometry import is_ground_net

        if is_ground_net(net) or f"power:{net}" in lib_tools._ensure_index()["symbols"]:
            return True
        return any(
            (ed.get_symbol_property(s, "Reference") or "").startswith("#PWR")
            and ed.get_symbol_property(s, "Value") == net
            for s in ed.iter_instance_symbols(self.tree)
        )


def _port_rotation(tree: list, node: list, out: tuple[int, int], ground_like: bool) -> int:
    """Rotation that points a power port's graphic along `out` (away from the
    pin it sits on). Uses the drawn body; a port without graphics falls back
    to ground pointing down, supplies up."""
    box = g.body_box(tree, node)
    fr = g.Frame(node)
    if box is not None:
        cx, cy = (box[0] + box[2]) / 2 - fr.x, (box[1] + box[3]) / 2 - fr.y
        base = (0, 1) if abs(cy) >= abs(cx) and cy > 0 else (0, -1) if abs(cy) >= abs(cx) \
            else (1, 0) if cx > 0 else (-1, 0)
    else:
        base = (0, 1) if ground_like else (0, -1)
    # Screen CCW rotation of a sheet vector (Y down): (x, y) -> (y, -x) per 90.
    v = base
    for rot in (0, 90, 180, 270):
        if v == tuple(out):
            return rot
        v = (v[1], -v[0])
    return 0


def _route(b: _Batch, a: dict, c: dict) -> list[tuple[float, float]]:
    """Manhattan path from pin `a` to pin `c`: straight, or the L whose legs
    leave each pin outward and cross no symbol body."""
    pts, hits = _l_route(b, a, c)
    if hits:
        b.warnings.append(f"wire {a['number']}->{c['number']}: no clean L route, crosses a symbol")
    return pts


def _l_route(b: _Batch, a: dict, c: dict) -> tuple[list[tuple[float, float]], int]:
    """The best Manhattan path from pin `a` to pin `c` and how many symbol
    bodies it crosses."""
    p, q = a["point"], c["point"]
    bodies = [g.inset(bx, 0.3) for s in ed.iter_instance_symbols(b.tree)
              if (bx := g.body_box(b.tree, s)) is not None and not g.is_power(b.tree, s)]
    if abs(p[0] - q[0]) < 1e-4 or abs(p[1] - q[1]) < 1e-4:
        return [p, q], sum(g.segment_hits_box(p, q, bx) for bx in bodies)

    def score(corner):
        legs = [(p, corner), (corner, q)]
        hits = sum(g.segment_hits_box(u, v, bx) for u, v in legs for bx in bodies)
        d1 = (corner[0] - p[0], corner[1] - p[1])
        d2 = (corner[0] - q[0], corner[1] - q[1])
        outward = (d1[0] * a["out"][0] + d1[1] * a["out"][1] > 0) + \
                  (d2[0] * c["out"][0] + d2[1] * c["out"][1] > 0)
        return hits * 10 - outward

    corners = [(q[0], p[1]), (p[0], q[1])]
    best = min(corners, key=score)
    return [p, best, q], sum(g.segment_hits_box(u, v, bx)
                                 for u, v in ((p, best), (best, q)) for bx in bodies)


def _add_path(b: _Batch, pts: list) -> int:
    n = 0
    for u, v in zip(pts, pts[1:]):
        if abs(u[0] - v[0]) > 1e-6 or abs(u[1] - v[1]) > 1e-6:
            ed.add_wire(b.tree, u[0], u[1], v[0], v[1])
            n += 1
    for corner in pts[1:-1]:
        _junction_if_tee(b.tree, corner)
    for end in (pts[0], pts[-1]):
        _junction_if_tee(b.tree, end)
    return n


def _junction_if_tee(tree: list, pt) -> None:
    """KiCAD draws a dot where three or more wire ends meet, or a wire ends
    on another's interior; add it so the sheet shows what is connected."""
    ends, interior = 0, False
    for node in tree[1:]:
        if not g.is_wire(node):
            continue
        xys = find_children(find_child(node, "pts"), "xy")
        a, c = (float(xys[0][1]), float(xys[0][2])), (float(xys[-1][1]), float(xys[-1][2]))
        if layout._same(a, pt) or layout._same(c, pt):
            ends += 1
        elif (min(a[0], c[0]) - 1e-4 <= pt[0] <= max(a[0], c[0]) + 1e-4
              and min(a[1], c[1]) - 1e-4 <= pt[1] <= max(a[1], c[1]) + 1e-4
              and (abs(a[0] - c[0]) < 1e-4 or abs(a[1] - c[1]) < 1e-4)):
            interior = True
    if ends >= 3 or (ends >= 1 and interior):
        if ed.find_point_item(tree, "junction", pt[0], pt[1]) is None:
            ed.add_junction(tree, pt[0], pt[1])


# --------------------------------------------------------------------------- #
# Ops
# --------------------------------------------------------------------------- #


def _pin_positions(tree: list, ref: str) -> dict[str, list[float]]:
    """Pin number -> sheet position, returned after a mirror so the caller
    sees where the pins landed."""
    return {p["number"]: p["position_mm"] for p in ed.list_pins_for_symbol(tree, ref)}


def _op_symbol(b: _Batch, op: dict) -> dict:
    x, y = b.point(op)
    rot = normalize_rotation(op.get("rotation", 0))
    node = b.place(op["lib_id"], op["ref"], op.get("value", ""), x, y, rot,
                   op.get("footprint", ""), op.get("fields"))
    mirrored = ed.set_symbol_mirror(node, op.get("mirror") or "")
    if op.get("autoplace", bool(rot) or mirrored):
        layout.autoplace_fields(b.tree, node, force=bool(op.get("autoplace") == "force"))
    ed.junction_mid_wire_pins(b.tree, node)
    if op.get("mirror"):
        return {"ref": op["ref"], "pins": _pin_positions(b.tree, op["ref"])}
    return {"ref": op["ref"]}


def _op_wire(b: _Batch, op: dict) -> dict:
    if "from" in op or "to" in op:
        pts = _route(b, b.pin(op["from"]), b.pin(op["to"]))
    elif "points" in op:
        pts = [sch._snap(float(px), float(py), op.get("snap_to_grid", True))
               for px, py in op["points"]]
    else:
        pts = [b.point(op, "1"), b.point(op, "2")]
    existing = [_ends(n) for n in b.tree[1:] if g.is_wire(n)]
    hits = {e for u, v in zip(pts, pts[1:]) for e in existing if _collinear_overlap(e, (u, v))}
    for a, c in sorted(hits):
        b.warnings.append(f"wire runs along the existing wire {list(a)}-{list(c)} "
                          f"(collinear overlap; ERC sees one wire, a label on it "
                          f"may touch both)")
    return {"segments": _add_path(b, pts)}


def _ends(node: list) -> tuple:
    xys = find_children(find_child(node, "pts"), "xy")
    return (float(xys[0][1]), float(xys[0][2])), (float(xys[-1][1]), float(xys[-1][2]))


def _collinear_overlap(s: tuple, t: tuple, tol: float = 1e-4) -> bool:
    """Two segments on one line sharing more than a point."""
    (ax, ay), (cx, cy) = s
    dx, dy = cx - ax, cy - ay
    length = math.hypot(dx, dy)
    if length < tol:
        return False
    # Both ends of t on s's line ...
    if any(abs(dx * (py - ay) - dy * (px - ax)) / length > tol for px, py in t):
        return False
    # ... and their projections overlap by more than a point.
    proj = sorted(((px - ax) * dx + (py - ay) * dy) / length for px, py in t)
    return min(proj[1], length) - max(proj[0], 0.0) > tol


def _anchor(b: _Batch, op: dict) -> tuple[tuple[float, float], tuple[int, int] | None]:
    if "pin" in op:
        p = b.pin(op["pin"])
        return p["point"], p["out"]
    return b.point(op), None


def _op_label(b: _Batch, op: dict) -> dict:
    (x, y), out = _anchor(b, op)
    orient = op.get("orientation") or (_DIR_TO_ORIENT.get(tuple(out)) if out else "right")
    if op.get("label_type", "local") == "global":
        ed.add_global_label(b.tree, op["net"], x, y, orient, op.get("shape", "bidirectional"))
    else:
        ed.add_label(b.tree, op["net"], x, y, orient)
    return {}


def _op_hier_label(b: _Batch, op: dict) -> dict:
    """A hierarchical label on a child sheet: the net it shares with the
    parent's sheet pin of the same name."""
    if state.get_active_sheet_filename() is None:
        raise OpError("hierarchical labels belong on a child sheet; pass sheet=")
    (x, y), out = _anchor(b, op)
    orient = op.get("orientation") or (_DIR_TO_ORIENT.get(tuple(out)) if out else "right")
    ed.add_hierarchical_label(b.tree, net_name=op["net"], x_mm=x, y_mm=y,
                              shape=op.get("shape", "input"), orientation=orient)
    return {}


def _op_sheet_pin(b: _Batch, op: dict) -> dict:
    """A pin on a sheet block of this sheet, on the block's edge; the side
    gives the orientation unless one is passed."""
    node = ed.find_sheet_by_name(b.tree, op["sheet"])
    if node is None:
        raise OpError(f"no sheet block named {op['sheet']!r} on this sheet")
    at, size = find_child(node, "at"), find_child(node, "size")
    x0, y0 = file_to_sch_xy(float(at[1]), float(at[2]))
    w, h = float(size[1]), float(size[2])
    x, y = b.point(op)
    sides = {"left": abs(x - x0), "right": abs(x - x0 - w),
             "up": abs(y - y0), "down": abs(y - y0 - h)}
    side = min(sides, key=sides.get)
    inside = x0 - 1e-3 <= x <= x0 + w + 1e-3 and y0 - 1e-3 <= y <= y0 + h + 1e-3
    if sides[side] > 1e-3 or not inside:
        raise OpError(f"sheet pin {op['name']!r} at {[x, y]} is not on the edge of "
                      f"{op['sheet']!r} (box {[x0, y0]} + {[w, h]})")
    ed.add_sheet_pin(node, pin_name=op["name"], shape=op.get("shape", "input"),
                     x_mm=x, y_mm=y, orientation=op.get("orientation") or side)
    return {}


def _op_power(b: _Batch, op: dict) -> dict:
    (x, y), out = _anchor(b, op)
    return {"ref": b.power_port(op["net"], x, y, op.get("rotation"), out,
                                op.get("base_symbol", ""))}


def _stub_from(b: _Batch, pin_pt, out) -> tuple[float, float] | None:
    """Far end of a straight wire leaving `pin_pt` along `out`, if any."""
    for node in b.tree[1:]:
        if not g.is_wire(node):
            continue
        xys = find_children(find_child(node, "pts"), "xy")
        ends = [(float(xy[1]), float(xy[2])) for xy in (xys[0], xys[-1])]
        for i in (0, 1):
            far = ends[1 - i]
            d = (far[0] - pin_pt[0], far[1] - pin_pt[1])
            along = (d[0] * out[0] > 0 and abs(d[1]) < 1e-4) or \
                    (d[1] * out[1] > 0 and abs(d[0]) < 1e-4)
            if layout._same(ends[i], pin_pt) and along:
                return far
    return None


def _best_port(b: _Batch, net: str, candidates: list, base_symbol: str = "") -> str:
    """Place a port at the candidate (point, rotation | None, out) the
    readability check likes best; the first one wins ties."""
    best = None
    for k, (pt, rot, out) in enumerate(candidates):
        ref = b.power_port(net, pt[0], pt[1], rot, out, base_symbol, trial=True)
        hits = layout.check_readability(b.tree, max_findings=0)["total"]
        if best is None or hits < best[0]:
            best = (hits, k)
        ed.remove_symbol(b.tree, ref)
        b.refs.remove(ref)
    pt, rot, out = candidates[best[1]]
    return b.power_port(net, pt[0], pt[1], rot, out, base_symbol)


def _op_pwr_flag(b: _Batch, op: dict) -> dict:
    (x, y), out = _anchor(b, op)
    if op.get("rotation") is not None or out is None:
        return {"ref": b.power_port("PWR_FLAG", x, y, op.get("rotation"), (0, -1))}
    # Beside the connection, not on the port that names the net: square to
    # the pin, on the pin or halfway along its stub (on the grid), on the
    # side and spot where the flag collides least.
    sides = [(out[1], -out[0]), (-out[1], out[0])]
    spots = [(x, y)]
    far = _stub_from(b, (x, y), out)
    if far is not None:
        mid = sch._snap((x + far[0]) / 2, (y + far[1]) / 2, True)
        if not layout._same(mid, (x, y)) and not layout._same(mid, far):
            spots.append(mid)
    return {"ref": _best_port(b, "PWR_FLAG", [(pt, None, side) for pt in spots
                                              for side in sides])}


JOIN_SPAN_MM = 25.4  # pins this close are wired together, not labelled


def _wire_close_pins(b: _Batch, pins: list[str],
                     span_mm: float = JOIN_SPAN_MM) -> list[list[str]]:
    """Wire pins that sit within `span_mm` of each other: each one to its
    nearest already-wired pin of its group, by a clean straight / L path.
    Returns the groups, first pin first; a pin with no clean path is a group
    of its own."""
    pts = {a: b.pin(a) for a in pins}
    rest, groups = list(pins), []
    while rest:
        group = [rest.pop(0)]
        groups.append(group)
        while True:
            near = [(w, r) for w in group for r in rest
                    if math.dist(pts[w]["point"], pts[r]["point"]) <= span_mm + 1e-6]
            if not near:
                break
            a, c = min(near, key=lambda e: math.dist(pts[e[0]]["point"], pts[e[1]]["point"]))
            rest.remove(c)
            path, hits = _l_route(b, pts[a], pts[c])
            if hits:
                groups.append([c])  # no clean path: its own stub and label
                continue
            _add_path(b, path)
            group.append(c)
    return groups


def _group_segments(tree: list, start: tuple[float, float]) -> list[tuple]:
    """The wire segments connected to `start` (a wire end), as (a, c)."""
    from kicad_claude.adapters import sch_netlist

    key = sch_netlist._pt(*start)
    segs = sch_netlist._wire_segments(tree)
    if not any(key in (a, c) for a, c, _ in segs):
        return []
    uf = sch_netlist.build_sheet_graph(tree)["uf"]
    root = uf.find(key)
    return [(a, c) for a, c, _ in segs if uf.find(a) == root]


def _label_spot(b: _Batch, segs: list[tuple], net: str, kind: str = "label",
                shape: str = "bidirectional"):
    """Where one label for a wired net collides least: on its longest
    horizontal segment first, stepping 1.27 mm along it. Returns
    (point, orientation, collisions), or None for a net with no straight
    segment."""
    items = layout._items(b.tree)
    cands = []
    for a, c in segs:
        if abs(a[1] - c[1]) < 1e-4:
            cands.append((0, -abs(a[0] - c[0]), min(a, c), (1, 0), "right"))
        elif abs(a[0] - c[0]) < 1e-4:
            low = a if a[1] > c[1] else c  # the text runs up from the lower end
            cands.append((1, -abs(a[1] - c[1]), low, (0, -1), "up"))
    best = None
    for _vert, neg_len, start, step, orient in sorted(cands):
        for k in range(int(round(-neg_len / 1.27)) + 1):
            pt = (round(start[0] + step[0] * 1.27 * k, 4),
                  round(start[1] + step[1] * 1.27 * k, 4))
            node = (ed.add_global_label(b.tree, net, pt[0], pt[1], orient, shape)
                    if kind == "global" else ed.add_label(b.tree, net, pt[0], pt[1], orient))
            box = g.label_box(node)
            b.tree.remove(node)
            hits = (sum(g.overlap_area(box, t["box"]) > 0.15 for t in items["texts"])
                    + sum(g.overlap_area(g.shrink(box, 0.15), bd["box"]) > 0
                          for bd in items["bodies"])
                    + sum(g.segment_hits_box(u, v, g.shrink(box, 0.3))
                          for u, v in items["wires"]))
            if best is None or hits < best[2]:
                best = (pt, orient, hits)
            if hits == 0:
                return best
    return best


def _place_name(b: _Batch, start, net: str, kind: str, shape: str,
                only_if_clear: bool = False) -> dict | None:
    """Put one label on the wired net at `start`, on its clearest segment.
    None when `start` is on no wire (or, with `only_if_clear`, when every
    spot collides)."""
    segs = _group_segments(b.tree, start)
    spot = _label_spot(b, segs, net, kind, shape) if segs else None
    if spot is None or (only_if_clear and spot[2]):
        return None
    pt, orient, hits = spot
    if kind == "global":
        ed.add_global_label(b.tree, net, pt[0], pt[1], orient, shape)
    else:
        ed.add_label(b.tree, net, pt[0], pt[1], orient)
    return {"at": list(pt), "orientation": orient, "collisions": hits}


def _stub_label(b: _Batch, addr: str, net: str, kind: str, stub: float, shape: str) -> None:
    """A stub out of the pin and a label at its end, facing away from the body."""
    p = b.pin(addr)
    o = p["out"]
    end = (round(p["point"][0] + o[0] * stub, 4), round(p["point"][1] + o[1] * stub, 4))
    if stub > 0:
        ed.add_wire(b.tree, p["point"][0], p["point"][1], end[0], end[1])
    orient = _DIR_TO_ORIENT[tuple(o)]
    if kind == "global":
        ed.add_global_label(b.tree, net, end[0], end[1], orient, shape)
    else:
        ed.add_label(b.tree, net, end[0], end[1], orient)


def _op_connect(b: _Batch, op: dict) -> dict:
    """Tie pins to a net. Label style: pins close together are wired to each
    other and each wired group gets one label on its clearest segment; a lone
    pin gets a stub and a label facing away from the body. Power style: a
    stub and a port per pin."""
    pins = op.get("pins") or ([op["pin"]] if "pin" in op else [])
    if not pins:
        raise OpError("connect needs pin or pins")
    net = op["net"]
    style = op.get("style", "auto")
    if style == "auto":
        style = "power" if b.is_power_net(net) else "label"
    if style not in ("label", "global", "power"):
        raise OpError(f"style must be auto, label, global or power, got {style!r}")
    stub = float(op.get("stub_mm", 2.54))
    shape = op.get("shape", "bidirectional")
    made = []
    out: dict = {"style": style, "pins": len(pins)}
    if style == "power":
        for addr in pins:
            p = b.pin(addr)
            o = p["out"]
            end = (round(p["point"][0] + o[0] * stub, 4), round(p["point"][1] + o[1] * stub, 4))
            if stub > 0:
                ed.add_wire(b.tree, p["point"][0], p["point"][1], end[0], end[1])
            if o[1] == 0 and stub > 0:
                # A side pin: the port upright (as KiCAD draws them) unless
                # pointing it along the stub collides less.
                cands = [(end, 0, None), (end, None, o)]
                made.append(_best_port(b, net, cands, op.get("base_symbol", "")))
            else:
                made.append(b.power_port(net, end[0], end[1], None, o,
                                         op.get("base_symbol", "")))
    else:
        kind = "global" if style == "global" else "label"
        groups = [[a] for a in pins]
        if len(pins) > 1 and op.get("join", True):
            groups = _wire_close_pins(b, list(pins), float(op.get("join_mm", JOIN_SPAN_MM)))
        if len(groups) == 1 and len(groups[0]) > 1 and op.get("name", True) is False:
            groups = []  # all wired and no name wanted: KiCAD names the net
        for group in groups:
            # A crowded wired group falls back to the stub at its first pin.
            if len(group) < 2 or _place_name(b, b.pin(group[0])["point"], net, kind,
                                              shape, only_if_clear=True) is None:
                _stub_label(b, group[0], net, kind, stub, shape)
        out["groups"] = max(len(groups), 1)
        out["labels"] = len(groups)
    if op.get("flag") and not b.flagged(net):
        # One flag marks the whole net as driven.
        made.append(_op_pwr_flag(b, {"pin": pins[0]})["ref"])
        b._flagged.add(net)
    if made:
        out["refs"] = made
    return out


def _op_name_net(b: _Batch, op: dict) -> dict:
    """One label on an already-wired net, on its clearest segment."""
    start = b.pin(op["pin"])["point"] if "pin" in op else b.point(op)
    kind = "global" if op.get("label_type", "local") == "global" else "label"
    shape = op.get("shape", "bidirectional")
    placed = _place_name(b, start, op["net"], kind, shape)
    if placed is None:
        if "pin" not in op:
            raise OpError(f"no wire end at {start}: give a pin, or a wire end point")
        _stub_label(b, op["pin"], op["net"], kind, float(op.get("stub_mm", 2.54)), shape)
        return {"stub": True}
    if placed["collisions"]:
        b.warnings.append(f"name_net {op['net']!r}: no clear spot on its wires; the label "
                          f"at {placed['at']} touches {placed['collisions']} item(s)")
    return placed


def _op_no_connect(b: _Batch, op: dict) -> dict:
    pins = op.get("pins") or ([op["pin"]] if "pin" in op else [])
    points = [b.pin(a)["point"] for a in pins] or [b.point(op)]
    for x, y in points:
        ed.add_no_connect(b.tree, x, y)
    return {"count": len(points)}


def _op_junction(b: _Batch, op: dict) -> dict:
    x, y = b.point(op)
    if ed.find_point_item(b.tree, "junction", x, y) is None:
        ed.add_junction(b.tree, x, y)
    return {}


def _op_text(b: _Batch, op: dict) -> dict:
    x, y = b.point(op)
    node = ed.add_text(b.tree, op["text"], x, y, size_mm=float(op.get("size_mm", 1.27)),
                       rotation=op.get("rotation", 0))
    return {"uuid": ed.get_uuid(node)}


def _pin_nets(tree: list) -> dict[tuple[str, str], str]:
    """(ref, pin) -> net name for the real (non-power) pins of the sheet."""
    from kicad_claude.adapters import sch_netlist

    return {(p["ref"], p["pin"]): name
            for name, n in sch_netlist.build_sheet_nets(tree).nets.items()
            for p in n["pins"] if not p["power"]}


def _merged_nets(before: dict, after: dict) -> list[list[str]]:
    """Groups of nets a change joined: two connected nets (named, or 2+
    pins), or a lone unconnected pin (`C8.1`) that landed on a connected or
    labelled net."""
    real: dict[str, int] = {}
    for name in before.values():
        real[name] = real.get(name, 0) + 1
    joined: dict[str, tuple[set, set]] = {}
    for key, name in after.items():
        old = before.get(key)
        if old is None:
            continue
        nets, lone = joined.setdefault(name, (set(), set()))
        if real[old] == 1 and old.startswith("unconnected"):
            lone.add(f"{key[0]}.{key[1]}")
        else:
            nets.add(old)
    out = []
    for name, (nets, lone) in joined.items():
        labelled = not name.startswith(("unconnected-", "Net-("))
        if len(nets) > 1 or (lone and (nets or labelled)):
            out.append(sorted(nets) + sorted(lone) + ([name] if labelled and not nets else []))
    return out


def _op_move(b: _Batch, op: dict) -> dict:
    x, y = b.point(op)
    if op.get("ref"):
        before = _pin_nets(b.tree)
        res = layout.move_symbol(b.tree, op["ref"], x, y, op.get("rotation"),
                                 drag=op.get("drag", True), mirror=op.get("mirror"))
        merged = _merged_nets(before, _pin_nets(b.tree))
        if merged and not op.get("allow_merge"):
            raise OpError(f"moving {op['ref']} merges nets "
                          f"{'; '.join(' + '.join(m) for m in merged)} (a wire or label "
                          f"now touches another net); move elsewhere, or pass "
                          f"allow_merge: true")
        if merged:
            res["nets_merged"] = merged
        if op.get("mirror"):
            res["pins"] = _pin_positions(b.tree, op["ref"])
        return res
    if op.get("uuid"):
        ed.move_item(b.tree, op["uuid"], x, y)
        return {}
    raise OpError("move needs ref or uuid")


def _op_move_region(b: _Batch, op: dict) -> dict:
    x1, y1 = b.point(op, "1")
    x2, y2 = b.point(op, "2")
    grid = 1.27  # the sheet grid: a shift off it puts every pin off grid
    dx = round(round(float(op.get("dx_mm", 0)) / grid) * grid, 4)
    dy = round(round(float(op.get("dy_mm", 0)) / grid) * grid, 4)
    if not dx and not dy:
        raise OpError("move_region needs dx_mm and / or dy_mm (snapped to 1.27 mm)")
    before = _pin_nets(b.tree)
    res = layout.move_region(b.tree, (x1, y1, x2, y2), dx, dy)
    merged = _merged_nets(before, _pin_nets(b.tree))
    if merged and not op.get("allow_merge"):
        raise OpError(f"moving the region merges nets "
                      f"{'; '.join(' + '.join(m) for m in merged)}; move elsewhere, or "
                      f"pass allow_merge: true")
    if merged:
        res["nets_merged"] = merged
    return res


def _ports_on(b: _Batch, net: str, pt, out) -> list[str]:
    """Power ports of `net` drawn on the pin at `pt`: on it, or at the end of
    its straight stub."""
    spots = [pt] + ([far] if (far := _stub_from(b, pt, out)) else [])
    refs = []
    for s_node in ed.iter_instance_symbols(b.tree):
        if not g.is_power(b.tree, s_node) or ed.get_symbol_property(s_node, "Value") != net:
            continue
        if any(layout._same(p["point"], q) for p in g.pins(b.tree, s_node) for q in spots):
            refs.append(ed.get_symbol_property(s_node, "Reference"))
    return refs


def _op_join_power(b: _Batch, op: dict) -> dict:
    """Pins of one power net (VEE on 1 / 4 / 9 / 12 / EP): ports already on
    them go, the pins of each side get stubs to one spine wire, and the spine
    gets one port (ground-like nets at its bottom end, others at its top)."""
    from kicad_claude.adapters.pcb_geometry import is_ground_net

    pins = op.get("pins") or []
    if len(pins) < 2:
        raise OpError("join_power needs pins (two or more)")
    net = op["net"]
    stub = float(op.get("stub_mm", 2.54))
    if stub <= 0:
        raise OpError("join_power needs stub_mm > 0 (the spine sits beyond the pins)")
    resolved = [b.pin(a) for a in pins]
    removed = 0
    for p in resolved:
        for ref in _ports_on(b, net, p["point"], p["out"]):
            ed.remove_symbol_with_wires(b.tree, ref, remove_connected_wires=True)
            b.refs = [x for x in b.refs if x != ref]
            removed += 1
    sides: dict[tuple, list] = {}
    for p in resolved:
        sides.setdefault(tuple(p["out"]), []).append(p["point"])
    ground = is_ground_net(net)
    ports = []
    for out, points in sides.items():
        axis = 0 if out[0] else 1  # the coordinate the stubs run along
        reach = [pt[axis] + out[axis] * stub for pt in points]
        spine = max(reach) if out[axis] > 0 else min(reach)
        ends = []
        for pt in points:
            end = (spine, pt[1]) if axis == 0 else (pt[0], spine)
            end = (round(end[0], 4), round(end[1], 4))
            ed.add_wire(b.tree, pt[0], pt[1], end[0], end[1])
            ends.append(end)
        ends.sort(key=lambda e: (e[1 - axis], e[axis]))
        if len(ends) > 1:
            ed.add_wire(b.tree, ends[0][0], ends[0][1], ends[-1][0], ends[-1][1])
            for e in ends:
                _junction_if_tee(b.tree, e)
        if axis == 0:  # a vertical spine: the port at its bottom or top end
            at, direction = (ends[-1], (0, 1)) if ground else (ends[0], (0, -1))
        else:          # a horizontal spine: the port at its first end, outward
            at, direction = ends[0], out
        ports.append(b.power_port(net, at[0], at[1], None, direction, op.get("base_symbol", "")))
        ed.junction_mid_wire_pins(b.tree, ed.find_symbol_by_reference(b.tree, ports[-1]))
    out_res = {"refs": ports, "sides": len(sides)}
    if removed:
        out_res["ports_removed"] = removed
    return out_res


_DIRS = {"right": (1, 0), "left": (-1, 0), "down": (0, 1), "up": (0, -1)}
SHUNT_PITCH_MM = 7.62  # between the shunts hung from one node


def _step(pt, vec, d: float) -> tuple[float, float]:
    return round(pt[0] + vec[0] * d, 4), round(pt[1] + vec[1] * d, 4)


def _place_along(b: _Batch, spec: dict, at, vec) -> tuple[tuple, tuple]:
    """Place a part with its in pin on `at` and its out pin along `vec`
    (rotation found by trying the four). Returns (in point, out point)."""
    for key in ("lib_id", "ref"):
        if key not in spec:
            raise OpError(f"chain part needs {key}: {spec}")
    ref, pin_in, pin_out = spec["ref"], str(spec.get("in_pin", "1")), str(spec.get("out_pin", "2"))
    node = b.place(spec["lib_id"], ref, spec.get("value", ""), at[0], at[1], 0,
                   spec.get("footprint", ""), spec.get("fields"))
    fr = g.Frame(node)
    for rot in (0, 90, 180, 270):
        ed.move_symbol(b.tree, ref, fr.x, fr.y, rot)
        a, c = b.pin(f"{ref}.{pin_in}")["point"], b.pin(f"{ref}.{pin_out}")["point"]
        d = (c[0] - a[0], c[1] - a[1])
        if (d[0] * vec[0] + d[1] * vec[1]) > 1e-4 and abs(d[0] * vec[1] - d[1] * vec[0]) < 1e-4:
            break
    else:
        ed.move_symbol(b.tree, ref, fr.x, fr.y, 0)
        b.warnings.append(f"{ref}: pins {pin_in} -> {pin_out} cannot lie along the chain")
    a = b.pin(f"{ref}.{pin_in}")["point"]
    fr = g.Frame(node)
    ed.move_symbol(b.tree, ref, round(fr.x + at[0] - a[0], 4), round(fr.y + at[1] - a[1], 4))
    layout.autoplace_fields(b.tree, node)
    return b.pin(f"{ref}.{pin_in}")["point"], b.pin(f"{ref}.{pin_out}")["point"]


def _op_place_chain(b: _Batch, op: dict) -> dict:
    """Series parts inline, shunts hung from the nodes, all wired."""
    parts = op.get("parts") or []
    if not parts:
        raise OpError("place_chain needs parts (the series parts, in order)")
    direction = op.get("direction", "right")
    if direction not in _DIRS:
        raise OpError(f"direction must be one of {list(_DIRS)}, got {direction!r}")
    vec = _DIRS[direction]
    side = op.get("shunt_side") or ("below" if vec[1] == 0 else "right")
    perp = {"below": (0, 1), "above": (0, -1), "right": (1, 0), "left": (-1, 0)}.get(side)
    if perp is None or abs(perp[0] * vec[0] + perp[1] * vec[1]) > 0:
        raise OpError(f"shunt_side {side!r} is not across a {direction} chain")
    shunts = {int(k): v for k, v in (op.get("shunts") or {}).items()}
    names = {int(k): v for k, v in (op.get("nets") or {}).items()}
    n = len(parts)
    if any(k < 0 or k > n for k in list(shunts) + list(names)):
        raise OpError(f"node numbers run 0 (input) .. {n} (output)")
    gap_mm = float(op.get("gap_mm", 5.08))
    drop = float(op.get("shunt_offset_mm", 5.08))

    def layout_of(k: int) -> tuple[float, float]:
        """(wire length, first tap offset) of node k. An end node's free end
        must carry something, or ERC calls it dangling: its label when the
        node is named, else its outermost shunt's tap."""
        m = len(shunts.get(k, []))
        span = (m - 1) * SHUNT_PITCH_MM if m else 0.0
        if k in (0, n) and k not in names:
            if not m:
                return 0.0, 0.0  # an end with nothing on it: no lead wire
            return span + 2.54, (0.0 if k == 0 else 2.54)
        return (max(gap_mm, 2 * 2.54 + span) if m else gap_mm), 2.54

    refs, nodes = [], {}
    a = b.point(op)
    for k in range(n + 1):
        length, first = layout_of(k)
        end = _step(a, vec, length)
        if length:
            ed.add_wire(b.tree, a[0], a[1], end[0], end[1])
        nodes[k] = (a, end)
        for j, spec in enumerate(shunts.get(k, [])):
            tap = _step(a, vec, first + j * SHUNT_PITCH_MM)
            top, _bottom = _place_along(b, spec, _step(tap, perp, drop), perp)
            ed.add_wire(b.tree, tap[0], tap[1], top[0], top[1])
            _junction_if_tee(b.tree, tap)
            refs.append(spec["ref"])
            if spec.get("to"):
                res = _op_connect(b, {"pin": f"{spec['ref']}.{spec.get('out_pin', '2')}",
                                      "net": spec["to"]})
                refs += res.get("refs", [])
        if k < n:
            _in, a = _place_along(b, parts[k], end, vec)
            refs.append(parts[k]["ref"])
    back = (-vec[0], -vec[1])
    for k, net in sorted(names.items()):
        if k in (0, n):  # on the lead's free end, facing out of the chain
            pt, facing = (nodes[0][0], back) if k == 0 else (nodes[n][1], vec)
            ed.add_label(b.tree, net, pt[0], pt[1], _DIR_TO_ORIENT[facing])
        elif _place_name(b, nodes[k][0], net, "label", "bidirectional") is None:
            b.warnings.append(f"place_chain: node {k} has no wire to name {net!r} on")
    return {"refs": refs, "nodes": {str(k): list(v[0]) for k, v in nodes.items()}}


def _op_remove(b: _Batch, op: dict) -> dict:
    refs = list(op.get("refs") or ([op["ref"]] if op.get("ref") else []))
    if op.get("pattern"):
        import fnmatch

        refs += [r for r in (ed.get_symbol_property(s, "Reference") or ""
                             for s in ed.iter_instance_symbols(b.tree))
                 if fnmatch.fnmatchcase(r, op["pattern"]) and r not in refs]
        if not refs:
            raise OpError(f"no symbol matches {op['pattern']!r}")
    if refs:
        wires = 0
        for ref in refs:
            r = ed.remove_symbol_with_wires(b.tree, ref,
                                            remove_connected_wires=op.get("wires", True))
            if not r["removed"]:
                raise OpError(f"no symbol {ref!r}")
            b.refs = [x for x in b.refs if x != ref]
            wires += r["wires"]
        return {"wires": wires, "removed": len(refs)}
    if op.get("label"):
        pt = b.point(op) if "x_mm" in op else None
        points = ed.remove_labels(b.tree, op["label"], pt)
        if not points:
            raise OpError(f"no label {op['label']!r}" + (f" at {pt}" if pt else ""))
        wires = (ed.remove_stub_chains(b.tree, points, keep_pin_links=False)
                 if op.get("wires", True) else 0)
        return {"removed": len(points), "wires": wires}
    uuids = list(op.get("uuids") or ([op["uuid"]] if op.get("uuid") else []))
    if uuids:
        for u in uuids:
            ed.remove_by_uuid(b.tree, u)
        return {"removed": len(uuids)}
    raise OpError("remove needs ref, refs, pattern, label or uuid / uuids")


def _op_property(b: _Batch, op: dict) -> dict:
    if op["name"] == "Reference":
        raise OpError("Reference is set by annotate_schematic")
    node = b.symbol_node(op["ref"])
    if ed.get_symbol_property(node, op["name"]) is None:
        if not op.get("create", False):
            raise OpError(f"{op['ref']} has no property {op['name']!r}; pass create: true")
        ed.add_symbol_property(node, op["name"], str(op["value"]))
    else:
        ed.set_symbol_property(node, op["name"], str(op["value"]))
    return {}


def _op_field(b: _Batch, op: dict) -> dict:
    node = b.symbol_node(op["ref"])
    ed.set_field_placement(node, op["field"], x_mm=op.get("x_mm"),
                           y_mm=op.get("y_mm"), angle=op.get("angle"),
                           visible=op.get("visible"))
    prop = ed._find_field(node, op["field"])
    if op.get("justify"):
        if op["justify"] not in ("left", "right", "center"):
            raise OpError(f"justify must be left, right or center, got {op['justify']!r}")
        # `justify` is how it reads on the sheet; KiCAD flips the stored
        # one on a rotated / mirrored symbol, so store it pre-flipped.
        at = find_child(prop, "at")
        flipped = g.is_text_flipped(g.Frame(node), float(at[3]) if len(at) > 3 else 0.0)[1]
        layout._set_justify(prop, {"left": "right", "right": "left"}.get(op["justify"])
                            if flipped and op["justify"] != "center" else op["justify"])
    box = g.field_box(node, prop)
    return {"bbox_mm": [round(v, 3) for v in box]} if box else {}


def _op_autoplace(b: _Batch, op: dict) -> dict:
    refs = op.get("refs", "all")
    nodes = (list(ed.iter_instance_symbols(b.tree)) if refs == "all"
             else [b.symbol_node(r) for r in refs])
    done = [layout.autoplace_fields(b.tree, n, force=op.get("force", False)) for n in nodes]
    out = {"placed": sum(1 for d in done if d["placed"])}
    crowded = {d["reference"]: d["collisions"] for d in done if d.get("collisions")}
    if crowded:
        out["collisions"] = crowded  # no clear spot found for these
    return out


_DISPATCH = {name: globals()[f"_op_{name}"] for name in OPS}


def _check_lib_ids(ops: list[dict]) -> None:
    """Refuse a batch naming unknown symbols before any op runs, all of them
    at once with a did-you-mean each, so one typo costs one retry."""
    wanted = [(i, op["lib_id"]) for i, op in enumerate(ops)
              if isinstance(op, dict) and op.get("op") == "symbol" and op.get("lib_id")]
    if not wanted:
        return
    known = lib_tools._ensure_index()["symbols"]
    bad = []
    for i, lib_id in wanted:
        if lib_id not in known:
            hint = lib_tools.suggest_id(lib_id, known)
            bad.append(f"op {i}: unknown lib_id {lib_id!r}"
                       + (f" (did you mean {hint!r}?)" if hint else ""))
    if bad:
        raise OpError("; ".join(bad))


def apply_ops(tree: list, ops: list[dict]) -> tuple[list[dict], list[str]]:
    """Apply `ops` to `tree` in order. Raises `OpError` naming the failing op."""
    _check_lib_ids(ops)
    b = _Batch(tree)
    results = []
    for i, op in enumerate(ops):
        kind = op.get("op") if isinstance(op, dict) else None
        if kind not in _DISPATCH:
            raise OpError(f"op {i}: unknown op {kind!r}; one of {list(OPS)}")
        try:
            res = _DISPATCH[kind](b, op)
        except OpError as exc:
            raise OpError(f"op {i} ({kind}): {exc}") from None
        except (KeyError, ValueError, TypeError) as exc:
            raise OpError(f"op {i} ({kind}): {type(exc).__name__}: {exc}") from None
        if res:
            results.append({"i": i, **res})
    return results, b.warnings + _driver_reminders(tree, b.ported)


def _driver_reminders(tree: list, ported: set[str]) -> list[str]:
    """Judge the PWR_FLAG reminder on the batch's result, for every net the
    batch drew ports on (stock symbols too): one with no PWR_FLAG and no
    power-output pin fails ERC `power_pin_not_driven`."""
    if not ported:
        return []
    from kicad_claude.adapters import sch_netlist

    undriven = sch_netlist.undriven_power_nets(tree)
    return [f"net {net!r} has power ports but no driver: add a pwr_flag on it, "
            f"unless a power-output pin on another sheet drives it"
            for net in sorted(ported & undriven)]


# Result keys the caller cannot know from its own op.
_NEW_INFO = {"power": ("ref",), "pwr_flag": ("ref",), "connect": ("refs",),
             "text": ("uuid",), "remove": ("wires",), "symbol": ("pins",),
             "field": ("bbox_mm",), "autoplace": ("collisions",),
             "name_net": ("at", "orientation", "collisions", "stub"),
             "move": ("contacts_broken", "nets_merged", "pins"),
             "move_region": ("symbols", "wires_and_items", "contacts_broken", "nets_merged"),
             "join_power": ("refs", "ports_removed"), "place_chain": ("refs", "nodes")}


def _new_info(ops: list[dict], results: list[dict]) -> list[dict]:
    """Per-op results cut to what an op made up itself (auto references,
    uuids, removed items, broken contacts)."""
    out = []
    for r in results:
        keys = _NEW_INFO.get(ops[r["i"]].get("op"), ())
        kept = {k: r[k] for k in keys if r.get(k)}
        if kept:
            out.append({"i": r["i"], **kept})
    return out


def register(mcp) -> None:
    @mcp.tool()
    @on_sheet
    def edit_schematic(ops: list[dict[str, Any]], dry_run: bool = False,
                       check: bool = False, verbose: bool = False) -> dict:
        """Many sheet edits in ONE write, all or nothing; prefer it to single
        add_* calls. Each op is a dict with "op": symbol, wire, label, power,
        pwr_flag, connect, no_connect, junction, text, move, remove, property,
        field, autoplace, hier_label, sheet_pin. Pins are "REF.PIN":
        {"op":"connect","pins":["R1.1","C1.1"],"net":"VIN"} draws stubs +
        label / power port; {"op":"wire",
        "from":"R1.2","to":"C1.2"} routes an L. `check=True` adds readability
        counts. help() lists every op's keys.

        Details:

        Ops (keys; coordinates `x_mm`, `y_mm`, snapped to 1.27 mm):
          symbol      lib_id, ref, value, x_mm, y_mm, rotation, mirror (x / y),
                      footprint, fields {name: value}, autoplace (default:
                      when rotated or mirrored; "force" also for large parts);
                      mirrored: the result lists `pins` {number: [x, y]}
          wire        x1_mm, y1_mm, x2_mm, y2_mm | points [[x, y], ...] |
                      from "REF.PIN", to "REF.PIN" (straight or L route that
                      leaves each pin outward and avoids symbol bodies);
                      junction dots are added at tees
          label       net, pin | x_mm, y_mm, orientation (default: away from
                      the pin), label_type local|global, shape
          power       net, pin | x_mm, y_mm, rotation (default: pointing away
                      from the pin), base_symbol
          pwr_flag    pin | x_mm, y_mm, rotation: PWR_FLAG beside the
                      connection, on the side and spot that collides least
          connect     pin | pins [...], net, style auto|label|global|power
                      (auto: power port for GND / stock power nets / nets
                      already drawn with ports, else label), stub_mm (2.54;
                      0 = on the pin), flag (one PWR_FLAG for the net),
                      shape, join (default true: label pins within join_mm,
                      default 25.4, are wired pin to pin by a clean L route
                      and each wired group gets one label on its clearest
                      segment; a lone pin, or a group with no clear spot,
                      gets a stub and label), name (false: all pins wired
                      into one group get no label, KiCAD names the net)
          name_net    net, pin | x_mm, y_mm (a wire end), label_type
                      local|global, shape: one label on an already-wired
                      net, on its longest free segment (horizontal first);
                      a pin on no wire gets a stub and label; no clear spot
                      places the least-colliding one and warns
          no_connect  pin | pins | x_mm, y_mm
          junction    x_mm, y_mm
          text        text, x_mm, y_mm, size_mm, rotation (multi-line text
                      grows upward from its anchor)
          move        ref | uuid, x_mm, y_mm, rotation, mirror (x / y / ""
                      clears), drag (default true: wires, labels, ports on
                      the pins follow); a move that joins two nets, or
                      drops a lone pin onto a net, is refused unless
                      allow_merge: true; with mirror the
                      result lists `pins` {number: [x, y]}
          move_region x1_mm, y1_mm, x2_mm, y2_mm (the box), dx_mm, dy_mm
                      (snapped to 1.27): symbols whose origin is inside,
                      labels, no-connects, junctions, notes and wire ends
                      inside move; wires crossing the edge are rubber-banded,
                      ports on moved pins follow; a shift that merges nets
                      is refused unless allow_merge: true
          join_power  pins [...], net, stub_mm (2.54), base_symbol: pins of
                      one power net drawn with one port: ports of that net
                      already on them go, each side's pins get stubs to one
                      spine wire, and each side one port (ground-like nets
                      at the spine's bottom, others at its top); the fix for
                      SR-OVERLAP "ports ... are stacked"
          place_chain x_mm, y_mm (the input end), parts [{lib_id, ref, value,
                      footprint, fields, in_pin "1", out_pin "2"}] in series,
                      direction right|left|down|up, shunts {node: [part +
                      to NET]}, nets {node: NAME}, gap_mm (5.08),
                      shunt_side, shunt_offset_mm (5.08): parts inline (the
                      rotation that lays in -> out along the chain), node k
                      is the wire before part k (0 input, len(parts)
                      output), shunts hang from taps on it 7.62 apart and
                      their far pin goes to `to` as `connect` would; each
                      named node gets one label on its wire
          remove      ref | refs [...] | pattern ("#PWR*", fnmatch on
                      references; wires: remove their pin stubs, default
                      true) | label NET (+ x_mm, y_mm: only that one;
                      wires: also its stub, default true) |
                      uuid | uuids [...]
          hier_label  net, pin | x_mm, y_mm, shape (input | output |
                      bidirectional | tri_state | passive), orientation
                      (default: away from the pin); child sheets only
          sheet_pin   sheet (block name on this sheet), name, shape, x_mm,
                      y_mm on the block's edge, orientation (default: the
                      edge's side); pair each with a hier_label of the same
                      name in the child
          property    ref, name, value, create
          field       ref, field, x_mm, y_mm, angle, visible, justify
                      left|right|center (as it reads on the sheet, flip of a
                      rotated / mirrored symbol handled); returns `bbox_mm`
                      [x1, y1, x2, y2] of the rendered text (estimate)
          autoplace   refs [...] | "all", force (KiCAD's side when it is
                      clear, else the side and grid offset whose texts hit
                      the fewest other texts, bodies, ports and wires;
                      `collisions` names parts with no clear spot)

        Mirror, as KiCAD writes `(mirror x|y)`: applied after the rotation,
        about the sheet axes. `x` flips about the horizontal axis (pins above
        and below the body swap, left / right stay); `y` flips about the
        vertical axis (left and right pins swap). So rotation 180 + mirror x
        equals mirror y, and rotation 180 + mirror y equals mirror x. A
        symbol whose pins are symmetric about one axis looks unchanged under
        the matching mirror; read the returned `pins` rather than predict.

        Returns `applied` (op count), `by_op` (count per kind), `results`
        only for what the caller did not pass (auto-assigned `#PWR` / `#FLG`
        refs, text uuids, removed items, broken contacts; `verbose=True`:
        one entry per op that produced anything), `warnings`, `backup`. An op that fails
        raises "op N (kind): ..." and nothing is written. `dry_run=True`
        applies in memory only. `check=True` runs
        `check_schematic_readability` on the result and returns its counts,
        plus `dangling`: wire ends that touch nothing (positions; ERC's
        `unconnected_wire_endpoint`) and the count of unconnected pins.
        """
        tree, path = sch._load_active_schematic()
        try:
            results, warnings = apply_ops(tree, ops)
        except OpError as exc:
            raise ValueError(f"{exc} — nothing written") from None
        backup = None if dry_run else sch._save_with_backup(tree, path)
        by_op: dict[str, int] = {}
        for op in ops:
            by_op[op["op"]] = by_op.get(op["op"], 0) + 1
        if not verbose:
            results = _new_info(ops, results)
        out: dict = {"applied": len(ops), "by_op": by_op,
                     "sheet": state.get_active_sheet_filename() or "root"}
        if results:
            out["results"] = results
        if warnings:
            out["warnings"] = warnings
        if check:
            from kicad_claude.adapters import sch_netlist

            out["readability"] = layout.check_readability(tree, max_findings=0)["counts"]
            dangling = sch_netlist.build_sheet_nets(tree, path).dangling
            ends = [d["position_mm"] for d in dangling if d["kind"] == "wire_end"]
            out["dangling"] = {"wire_ends": ends,
                               "pins": sum(d["kind"] != "wire_end" for d in dangling)}
        if dry_run:
            out["dry_run"] = True
        else:
            out["backup"] = str(backup) if backup else None
        logger.info("edit_schematic: %d ops on %s", len(ops), out["sheet"])
        return out
