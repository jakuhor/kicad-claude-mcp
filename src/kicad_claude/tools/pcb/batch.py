"""`edit_board`: many board edits in one guarded write.

One parse, the ops in order, one `save_tree`. All or nothing: an op that
fails raises with its index and nothing is written. Pads are addressed as
"REF.PAD", so a track can run pad to pad without looking up coordinates,
and takes the pad's net.
"""

from __future__ import annotations

import logging
from typing import Any

from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import pcb_netlist
from kicad_claude.tools.common import on_board
from kicad_claude.tools.pcb import layout as pcb

logger = logging.getLogger("kicad-claude.tools.pcb.batch")

OPS = ("move", "track", "via", "zone", "zone_set", "text", "remove")


class OpError(ValueError):
    pass


def _xy(op: dict, kx: str = "x_mm", ky: str = "y_mm") -> tuple[float, float]:
    try:
        return float(op[kx]), float(op[ky])
    except KeyError as exc:
        raise OpError(f"missing {exc.args[0]!r}") from None


class _Board:
    def __init__(self, tree: list):
        self.tree = tree
        self._pads: list[dict] | None = None

    def pad(self, addr: Any) -> dict:
        if not isinstance(addr, str) or "." not in addr:
            raise OpError(f"pad address must be 'REF.PAD', got {addr!r}")
        if self._pads is None:  # positions change only through `move`
            self._pads = pcb_netlist.list_pads(self.tree)
        ref, num = addr.split(".", 1)
        for p in self._pads:
            if p["ref"] == ref and p["pad"] == num:
                return p
        raise OpError(f"no pad {addr!r}")

    def moved(self) -> None:
        self._pads = None

    def point(self, value: Any) -> tuple[tuple[float, float], str]:
        """[x, y] or "REF.PAD" -> (point, the pad's net or "")."""
        if isinstance(value, str):
            p = self.pad(value)
            return (float(p["point"][0]), float(p["point"][1])), p["net_name"]
        try:
            return (float(value[0]), float(value[1])), ""
        except (TypeError, ValueError, IndexError):
            raise OpError(f"a point is [x, y] or 'REF.PAD', got {value!r}") from None


def _op_move(b: _Board, op: dict) -> dict:
    if "near" in op:
        refs = op.get("refs") or [op["ref"]]
        if op.get("layer"):
            for ref in refs:
                fp = ed.find_footprint_by_reference(b.tree, ref)
                if fp is None:
                    raise OpError(f"no footprint {ref!r}")
                at = ed.find_child(fp, "at")
                ed.move_footprint(b.tree, ref, *ed.file_to_pcb_xy(float(at[1]), float(at[2])),
                                  layer=op["layer"])
        placed = ed.place_near(b.tree, refs, op["near"], op.get("side", "right"),
                               gap_mm=float(op.get("gap_mm", 0.25)),
                               rotation=op.get("rotation"))
        b.moved()
        return {"placed": placed}
    x, y = _xy(op)
    ed.move_footprint(b.tree, op["ref"], x, y, rotation=op.get("rotation"),
                      layer=op.get("layer"))
    b.moved()
    return {}


def _op_track(b: _Board, op: dict) -> dict:
    raw: list = []
    if "from" in op:
        raw.append(op["from"])
    raw.extend(op.get("points") or [])
    if "to" in op:
        raw.append(op["to"])
    if len(raw) < 2:
        raise OpError("track needs from / to and / or points (2 or more in all)")
    pts, nets = [], set()
    for value in raw:
        pt, net = b.point(value)
        pts.append(pt)
        if net:
            nets.add(net)
    net = op.get("net", "")
    if not net:
        if len(nets) > 1:
            raise OpError(f"pads on different nets {sorted(nets)}: pass `net`")
        net = next(iter(nets), "")
    elif nets and any(n != net for n in nets):
        raise OpError(f"net {net!r} differs from the pads' {sorted(nets)}")
    width = float(op.get("width_mm", 0.25))
    layer = op.get("layer", "F.Cu")
    for a, c in zip(pts, pts[1:]):
        if a != c:
            ed.add_track(b.tree, a[0], a[1], c[0], c[1], width_mm=width, layer=layer, net=net)
    return {"net": net} if net and "net" not in op else {}


def _op_via(b: _Board, op: dict) -> dict:
    if "at" in op:
        (x, y), pad_net = b.point(op["at"])
    else:
        (x, y), pad_net = _xy(op), ""
    net = op.get("net") or pad_net
    ed.add_via(b.tree, x, y, drill_mm=float(op.get("drill_mm", 0.4)),
               diameter_mm=float(op.get("diameter_mm", 0.8)), net=net)
    return {}


def _op_zone(b: _Board, op: dict) -> dict:
    poly = [(float(p[0]), float(p[1])) for p in op.get("polygon_mm") or []]
    ed.add_zone(
        b.tree, net_name=op.get("net", ""), layer=op.get("layer", "F.Cu"), polygon_mcp=poly,
        fill_clearance_mm=float(op.get("fill_clearance_mm", 0.5)),
        min_thickness_mm=float(op.get("min_thickness_mm", 0.25)),
        thermal_gap_mm=float(op.get("thermal_gap_mm", 0.5)),
        thermal_bridge_mm=float(op.get("thermal_bridge_mm", 0.5)),
        name=op.get("name", ""), priority=int(op.get("priority", 0)),
        pad_connection=op.get("pad_connection", "thermal"),
    )
    return {}


def _op_zone_set(b: _Board, op: dict) -> dict:
    zones = ed.find_zones(b.tree, item_uuid=op.get("uuid", ""), name=op.get("name", ""),
                          net=op.get("net", ""), layer=op.get("layer", ""))
    if not zones:
        raise OpError("no zone matches")
    keys = ("pad_connection", "priority", "clearance_mm", "min_thickness_mm",
            "thermal_gap_mm", "thermal_bridge_mm")
    args = {k: op[k] for k in keys if k in op}
    if not args:
        raise OpError(f"nothing to set; keys: {', '.join(keys)}")
    for z in zones:
        ed.set_zone(z, **args)
    return {"zones": len(zones)} if "uuid" not in op else {}


def _op_text(b: _Board, op: dict) -> dict:
    if op.get("uuid"):
        ed.edit_text(b.tree, op["uuid"], text=op.get("text"), x_mm=op.get("x_mm"),
                     y_mm=op.get("y_mm"), rotation=op.get("rotation"),
                     layer=op.get("layer"), size_mm=op.get("size_mm"))
        return {}
    x, y = _xy(op)
    node = ed.add_silk_text(b.tree, text=op["text"], x_mm=x, y_mm=y,
                            layer=op.get("layer", "F.SilkS"),
                            size_mm=float(op.get("size_mm", 1.0)),
                            rotation=op.get("rotation", 0),
                            thickness_mm=op.get("thickness_mm"))
    return {"uuid": str(ed.find_child(node, "uuid")[1])}


def _op_remove(b: _Board, op: dict) -> dict:
    if op.get("ref"):
        if not ed.remove_footprint(b.tree, op["ref"]):
            raise OpError(f"no footprint {op['ref']!r}")
        b.moved()
        return {"removed": 1}
    gone = ed.remove_board_items(
        b.tree, item_uuid=op.get("uuid", ""), net=op.get("net", ""),
        kinds=op.get("kinds") or ("track", "arc", "via"), layer=op.get("layer", ""))
    return {"removed": len(gone)}


_HANDLERS = {"move": _op_move, "track": _op_track, "via": _op_via,
             "zone": _op_zone, "zone_set": _op_zone_set, "text": _op_text,
             "remove": _op_remove}


def apply_ops(tree: list, ops: list[dict]) -> tuple[dict, list[dict]]:
    """Apply `ops` in order. Returns (count per kind, notes for ops that
    carry something the caller did not pass). Raises OpError("op N ...")."""
    b = _Board(tree)
    counts: dict[str, int] = {}
    notes: list[dict] = []
    for i, op in enumerate(ops):
        kind = op.get("op") if isinstance(op, dict) else None
        if kind not in _HANDLERS:
            raise OpError(f"op {i}: unknown op {kind!r}; use one of {', '.join(OPS)}")
        try:
            extra = _HANDLERS[kind](b, op)
        except OpError as exc:
            raise OpError(f"op {i} ({kind}): {exc}") from None
        except (KeyError, ValueError, TypeError) as exc:
            msg = exc.args[0] if exc.args else exc
            raise OpError(f"op {i} ({kind}): {type(exc).__name__}: {msg}") from None
        counts[kind] = counts.get(kind, 0) + 1
        if extra:
            notes.append({"i": i, **extra})
    return counts, notes


def register(mcp) -> None:
    @mcp.tool()
    @on_board
    def edit_board(ops: list[dict[str, Any]], dry_run: bool = False) -> dict:
        """Many board edits in ONE write, all or nothing; prefer it to
        single move_footprint / add_track / add_via / add_zone calls. Ops:
        move (or "near":"U1.2","side":"left"), track, via, zone, zone_set,
        text, remove. Pads are "REF.PAD": {"op":"track","from":"U1.3",
        "to":"C5.1"} takes the pads' net. help() lists every op's keys.

        Details:

        Ops (coordinates in mm, KiCAD board coords, Y down):
          move    ref, x_mm, y_mm, rotation, layer (pads, footprint zones
                  follow) | ref or refs [...], near "REF" or "REF.PAD",
                  side left / right / top / bottom (default right), gap_mm
                  (0.25, courtyard to courtyard), rotation, layer: beside
                  the anchor, the first centred on it (or its pad), the
                  rest stacked along that side; returns the positions
          track   from "REF.PAD" | [x, y], points [[x, y], ...], to …;
                  width_mm (0.25), layer (F.Cu), net (default: the pads'
                  net; pads on two nets are refused)
          via     x_mm, y_mm | at "REF.PAD"; net (default: that pad's),
                  drill_mm (0.4), diameter_mm (0.8)
          zone    net, layer, polygon_mm, pad_connection (thermal / solid /
                  thru_hole_only / none), priority, fill_clearance_mm,
                  min_thickness_mm, thermal_gap_mm, thermal_bridge_mm, name
          zone_set  uuid | name | net (+ layer); pad_connection,
                  priority, clearance_mm, min_thickness_mm, thermal_gap_mm,
                  thermal_bridge_mm (changes a pour in place; refill after)
          text    text, x_mm, y_mm, layer (F.SilkS), size_mm (1.0),
                  rotation, thickness_mm: adds, returns its uuid | uuid +
                  any of text, x_mm, y_mm, rotation, layer, size_mm: edits.
                  Back-layer text is mirrored
          remove  ref (a footprint) | uuid (track, arc, via, zone, text) |
                  net (+ kinds [track, arc, via, zone], layer)

        Returns `applied` (count per op kind), `notes` only for what the
        caller did not pass (a track's net taken from its pads, items
        removed, new text uuids, near-move positions), and `backup`. An op that fails raises "op N (kind): ..."
        and nothing is written. `dry_run=True` applies in memory only.
        Zones need `run_drc(refill_zones=True)` afterwards.
        """
        tree, path = pcb._load_active_pcb()
        try:
            counts, notes = apply_ops(tree, ops)
        except OpError as exc:
            raise ValueError(f"{exc} — nothing written") from None
        out: dict = {"applied": counts}
        if notes:
            out["notes"] = notes
        if dry_run:
            out["dry_run"] = True
        else:
            backup = pcb._save_with_backup(tree, path)
            out["backup"] = str(backup) if backup else None
        logger.info("edit_board: %d ops", len(ops))
        return out
