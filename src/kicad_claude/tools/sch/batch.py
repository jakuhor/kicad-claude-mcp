"""`edit_schematic`: many schematic edits in one guarded write.

One parse, the ops in order, one `save_tree`. All or nothing: an op that
fails raises with its index and nothing is written. Pin-addressed ops
(`connect`, `wire` from/to, `no_connect`, `label` / `power` with `pin`)
take "REF.PIN" so no coordinates have to be looked up first.
"""

from __future__ import annotations

import logging
from typing import Any

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_geometry as g
from kicad_claude.adapters import sch_layout as layout
from kicad_claude.adapters.sch_io import find_child, find_children
from kicad_claude.tools.common import on_sheet
from kicad_claude.tools.library import index as lib_tools
from kicad_claude.tools.sch import schematic as sch
from kicad_claude.utils.geometry import normalize_rotation

logger = logging.getLogger("kicad-claude.tools.sch.batch")

_DIR_TO_ORIENT = {(1, 0): "right", (-1, 0): "left", (0, -1): "up", (0, 1): "down"}
OPS = ("symbol", "wire", "label", "power", "pwr_flag", "connect", "no_connect",
       "junction", "text", "move", "remove", "property", "field", "autoplace")


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
                self.warnings.append(f"{ref}: footprint {footprint!r} is not in the library index")
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
        candidate, _custom, warns = sch._power_choice(self.tree, net, base_symbol)
        self.warnings += warns
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
    p, q = a["point"], c["point"]
    if abs(p[0] - q[0]) < 1e-4 or abs(p[1] - q[1]) < 1e-4:
        return [p, q]
    bodies = [g.inset(bx, 0.3) for s in ed.iter_instance_symbols(b.tree)
              if (bx := g.body_box(b.tree, s)) is not None and not g.is_power(b.tree, s)]

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
    if score(best) >= 10:
        b.warnings.append(f"wire {a['number']}->{c['number']}: no clean L route, crosses a symbol")
    return [p, best, q]


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


def _op_symbol(b: _Batch, op: dict) -> dict:
    x, y = b.point(op)
    rot = normalize_rotation(op.get("rotation", 0))
    node = b.place(op["lib_id"], op["ref"], op.get("value", ""), x, y, rot,
                   op.get("footprint", ""), op.get("fields"))
    mirrored = ed.set_symbol_mirror(node, op.get("mirror") or "")
    if op.get("autoplace", bool(rot) or mirrored):
        layout.autoplace_fields(b.tree, node, force=bool(op.get("autoplace") == "force"))
    ed.junction_mid_wire_pins(b.tree, node)
    return {"ref": op["ref"]}


def _op_wire(b: _Batch, op: dict) -> dict:
    if "from" in op or "to" in op:
        pts = _route(b, b.pin(op["from"]), b.pin(op["to"]))
    elif "points" in op:
        pts = [sch._snap(float(px), float(py), op.get("snap_to_grid", True))
               for px, py in op["points"]]
    else:
        pts = [b.point(op, "1"), b.point(op, "2")]
    return {"segments": _add_path(b, pts)}


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


def _op_connect(b: _Batch, op: dict) -> dict:
    """Tie pins to a net: a stub wire out of each pin, then a label or a
    power port at its end, facing away from the body."""
    pins = op.get("pins") or ([op["pin"]] if "pin" in op else [])
    if not pins:
        raise OpError("connect needs pin or pins")
    net = op["net"]
    style = op.get("style", "auto")
    if style == "auto":
        style = "power" if b.is_power_net(net) else "label"
    stub = float(op.get("stub_mm", 2.54))
    made = []
    for addr in pins:
        p = b.pin(addr)
        o = p["out"]
        end = (round(p["point"][0] + o[0] * stub, 4), round(p["point"][1] + o[1] * stub, 4))
        if stub > 0:
            ed.add_wire(b.tree, p["point"][0], p["point"][1], end[0], end[1])
        if style == "power":
            if o[1] == 0 and stub > 0:
                # A side pin: the port upright (as KiCAD draws them) unless
                # pointing it along the stub collides less.
                cands = [(end, 0, None), (end, None, o)]
                made.append(_best_port(b, net, cands, op.get("base_symbol", "")))
            else:
                made.append(b.power_port(net, end[0], end[1], None, o,
                                         op.get("base_symbol", "")))
        elif style in ("label", "global"):
            orient = _DIR_TO_ORIENT[tuple(o)]
            if style == "global":
                ed.add_global_label(b.tree, net, end[0], end[1], orient,
                                    op.get("shape", "bidirectional"))
            else:
                ed.add_label(b.tree, net, end[0], end[1], orient)
        else:
            raise OpError(f"style must be auto, label, global or power, got {style!r}")
        if op.get("flag"):
            made.append(_op_pwr_flag(b, {"pin": addr})["ref"])
    out = {"style": style, "pins": len(pins)}
    if made:
        out["refs"] = made
    return out


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


def _op_move(b: _Batch, op: dict) -> dict:
    x, y = b.point(op)
    if op.get("ref"):
        return layout.move_symbol(b.tree, op["ref"], x, y, op.get("rotation"),
                                  drag=op.get("drag", True), mirror=op.get("mirror"))
    if op.get("uuid"):
        ed.move_item(b.tree, op["uuid"], x, y)
        return {}
    raise OpError("move needs ref or uuid")


def _op_remove(b: _Batch, op: dict) -> dict:
    if op.get("ref"):
        r = ed.remove_symbol_with_wires(b.tree, op["ref"],
                                        remove_connected_wires=op.get("wires", True))
        if not r["removed"]:
            raise OpError(f"no symbol {op['ref']!r}")
        b.refs = [x for x in b.refs if x != op["ref"]]
        return {"wires": r["wires"]}
    if op.get("uuid"):
        return {"kind": ed.remove_by_uuid(b.tree, op["uuid"])["kind"]}
    raise OpError("remove needs ref or uuid")


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
    ed.set_field_placement(b.symbol_node(op["ref"]), op["field"], x_mm=op.get("x_mm"),
                           y_mm=op.get("y_mm"), angle=op.get("angle"),
                           visible=op.get("visible"))
    return {}


def _op_autoplace(b: _Batch, op: dict) -> dict:
    refs = op.get("refs", "all")
    nodes = (list(ed.iter_instance_symbols(b.tree)) if refs == "all"
             else [b.symbol_node(r) for r in refs])
    done = [layout.autoplace_fields(b.tree, n, force=op.get("force", False)) for n in nodes]
    return {"placed": sum(1 for d in done if d["placed"])}


_DISPATCH = {name: globals()[f"_op_{name}"] for name in OPS}


def apply_ops(tree: list, ops: list[dict]) -> tuple[list[dict], list[str]]:
    """Apply `ops` to `tree` in order. Raises `OpError` naming the failing op."""
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
    return results, _drop_settled_flag_warnings(tree, b.warnings)


def _drop_settled_flag_warnings(tree: list, warnings: list[str]) -> list[str]:
    """Judge the PWR_FLAG reminder on the batch's result: a net that ended up
    with a flag on it (placed by a later op) needs no reminder."""
    import re

    from kicad_claude.adapters import sch_netlist

    pending = [(w, m.group(1)) for w in warnings
               if "PWR_FLAG" in w and (m := re.match(r"net '([^']+)'", w))]
    if not pending:
        return warnings
    flagged = {name for name, net in sch_netlist.build_sheet_nets(tree).nets.items()
               if any(pin["ref"].startswith("#FLG") for pin in net["pins"])}
    drop = {w for w, net in pending if net in flagged}
    return [w for w in warnings if w not in drop]


# Result keys the caller cannot know from its own op.
_NEW_INFO = {"power": ("ref",), "pwr_flag": ("ref",), "connect": ("refs",),
             "text": ("uuid",), "remove": ("wires", "kind"),
             "move": ("contacts_broken",)}


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
        field, autoplace. Pins are "REF.PIN": {"op":"connect","pins":["R1.1",
        "C1.1"],"net":"VIN"} draws stubs + label / power port; {"op":"wire",
        "from":"R1.2","to":"C1.2"} routes an L. `check=True` adds readability
        counts. help() lists every op's keys.

        Details:

        Ops (keys; coordinates `x_mm`, `y_mm`, snapped to 1.27 mm):
          symbol      lib_id, ref, value, x_mm, y_mm, rotation, mirror (x / y),
                      footprint, fields {name: value}, autoplace (default:
                      when rotated or mirrored; "force" also for large parts)
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
                      0 = on the pin), flag (also a PWR_FLAG), shape
          no_connect  pin | pins | x_mm, y_mm
          junction    x_mm, y_mm
          text        text, x_mm, y_mm, size_mm, rotation (multi-line text
                      grows upward from its anchor)
          move        ref | uuid, x_mm, y_mm, rotation, mirror (x / y / ""
                      clears), drag (default true: wires, labels, ports on
                      the pins follow)
          remove      ref (wires: remove its pin stubs, default true) | uuid
          property    ref, name, value, create
          field       ref, field, x_mm, y_mm, angle, visible
          autoplace   refs [...] | "all", force

        Returns `applied` (op count), `by_op` (count per kind), `results`
        only for what the caller did not pass (auto-assigned `#PWR` / `#FLG`
        refs, text uuids, removed items, broken contacts; `verbose=True`:
        one entry per op that produced anything), `warnings`, `backup`. An op that fails
        raises "op N (kind): ..." and nothing is written. `dry_run=True`
        applies in memory only. `check=True` runs
        `check_schematic_readability` on the result and returns its counts.
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
            out["readability"] = layout.check_readability(tree, max_findings=0)["counts"]
        if dry_run:
            out["dry_run"] = True
        else:
            out["backup"] = str(backup) if backup else None
        logger.info("edit_schematic: %d ops on %s", len(ops), out["sheet"])
        return out
