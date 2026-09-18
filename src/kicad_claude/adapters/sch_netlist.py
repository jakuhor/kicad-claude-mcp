"""Derive connectivity from a parsed `.kicad_sch` tree.

A KiCAD schematic holds no net list. Nets are geometry: a wire joins the two
points it runs between, a pin joins the point it sits on, and a junction joins
everything that meets at one point. This module reconstructs that, so the
server can answer "what is connected to what" without a `kicad-cli` round trip.

Read-only. Nothing here mutates the tree.

Connection rules, matching KiCAD's own connectivity:
  - A wire connects its two endpoints.
  - Two wires whose endpoints coincide are connected.
  - A wire ending on another wire's interior (a T) is connected. A wire merely
    crossing another (an X) is not, unless a `(junction ...)` sits there.
  - A pin connects where it touches a wire endpoint, a junction, or a wire's
    interior.
  - Labels name the set they sit on. A `no_connect` marks a pin as deliberately
    unconnected.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters.sch_io import (
    find_child,
    find_children,
    head_of,
    is_call,
)
from kicad_claude.utils.kicad_strings import normalize_name

# KiCAD stores nanometre-exact millimetres. Round before hashing a point, or
# two coordinates that are equal on the sheet compare unequal as floats.
_PLACES = 4
_EPS = 1e-4

# Node kinds whose first atom is a net name.
LABEL_KINDS = ("label", "global_label", "hierarchical_label")


Point = tuple[float, float]


def _pt(x: Any, y: Any) -> Point:
    return (round(float(x), _PLACES), round(float(y), _PLACES))


# --------------------------------------------------------------------------- #
# Union-find
# --------------------------------------------------------------------------- #


class _Union:
    """Disjoint sets over hashable keys, with path compression."""

    def __init__(self) -> None:
        self._parent: dict[Any, Any] = {}

    def add(self, key: Any) -> Any:
        if key not in self._parent:
            self._parent[key] = key
        return key

    def find(self, key: Any) -> Any:
        self.add(key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def groups(self) -> dict[Any, list[Any]]:
        out: dict[Any, list[Any]] = {}
        for key in self._parent:
            out.setdefault(self.find(key), []).append(key)
        return out


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    """True if `p` lies on the segment a-b (endpoints included)."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > _EPS:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < -_EPS:
        return False
    length_sq = (bx - ax) ** 2 + (by - ay) ** 2
    return dot <= length_sq + _EPS


# --------------------------------------------------------------------------- #
# Pin positions
# --------------------------------------------------------------------------- #


def _unit_of(sub_symbol_name: str) -> int | None:
    """Unit number encoded in a lib sub-symbol name, `NAME_<unit>_<style>`."""
    m = re.search(r"_(\d+)_(\d+)$", sub_symbol_name)
    return int(m.group(1)) if m else None


def pins_of_instance(tree: list, s_node: list) -> list[dict]:
    """Absolute pin positions of one placed `(symbol ...)` node.

    Unlike `sch_editor.list_pins_for_symbol`, which takes a reference, this
    works per instance node and honours `(unit N)` and `(mirror x|y)`. A
    multi-unit part has one instance node per unit, each carrying only its own
    pins.
    """
    lib_id_node = find_child(s_node, "lib_id")
    if not lib_id_node or len(lib_id_node) < 2:
        return []
    sym_def = ed.find_lib_symbol_def(tree, lib_id_node[1])
    if sym_def is None:
        return []

    at = find_child(s_node, "at")
    if not at or len(at) < 3:
        return []
    sx, sy = float(at[1]), float(at[2])
    srot = float(at[3]) if len(at) > 3 else 0.0

    unit_node = find_child(s_node, "unit")
    want_unit = int(unit_node[1]) if unit_node and len(unit_node) > 1 else 1

    mirror = find_child(s_node, "mirror")
    mirror_axis = str(mirror[1]) if mirror and len(mirror) > 1 else ""

    out: list[dict] = []
    for pin_node, parent in ed._iter_pins(sym_def):
        if parent is not None:
            unit = _unit_of(str(parent[1])) if len(parent) > 1 else None
            # Unit 0 holds the graphics and pins common to every unit.
            if unit is not None and unit not in (0, want_unit):
                continue
        lx, ly, _ = ed._pin_local_at(pin_node)

        # Library coords are Y down, like the sheet. Mirror first, then rotate.
        if mirror_axis == "y":
            lx = -lx
        elif mirror_axis == "x":
            ly = -ly
        rx, ry = ed.rotate_xy(lx, ly, srot)
        px, py = sx + rx, sy - ry

        number, name = ed._pin_id(pin_node)
        out.append(
            {
                "number": number,
                "name": normalize_name(name),
                "point": _pt(px, py),
            }
        )
    return out


def _is_power_symbol(tree: list, s_node: list) -> bool:
    """A power symbol: `(power)` in its library definition, or a `#PWR` ref."""
    ref = ed.get_symbol_property(s_node, "Reference") or ""
    if ref.startswith("#PWR") or ref.startswith("#FLG"):
        return True
    lib_id_node = find_child(s_node, "lib_id")
    if lib_id_node and len(lib_id_node) > 1:
        sym_def = ed.find_lib_symbol_def(tree, lib_id_node[1])
        if sym_def is not None and find_child(sym_def, "power") is not None:
            return True
    return False


# --------------------------------------------------------------------------- #
# Net building
# --------------------------------------------------------------------------- #


class SheetNets:
    """The connectivity of one sheet.

    `nets` maps a net name to its members. A member is
    `{"ref", "pin", "pin_name"}` for a pin, and the net also records the
    labels, wires and junctions that formed it.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.nets: dict[str, dict] = {}
        self.dangling: list[dict] = []


def _wire_segments(tree: list) -> list[tuple[Point, Point, list]]:
    """Every `(wire ...)` as (start, end, node). Buses are excluded."""
    out = []
    for node in tree[1:]:
        if not is_call(node, "wire"):
            continue
        pts = find_child(node, "pts")
        xys = find_children(pts, "xy") if pts else []
        if len(xys) < 2:
            continue
        out.append((_pt(xys[0][1], xys[0][2]), _pt(xys[-1][1], xys[-1][2]), node))
    return out


def build_sheet_graph(tree: list) -> dict:
    """Union-find over one sheet. Returns the raw graph, before naming.

    Keys in the union are points. The result carries the point sets plus the
    pins, labels and no-connects that attach to each point.
    """
    uf = _Union()
    segments = _wire_segments(tree)

    for start, end, _node in segments:
        uf.add(start)
        uf.add(end)
        uf.union(start, end)

    # Junctions: join every wire that passes through the junction point.
    junctions = [
        _pt(find_child(n, "at")[1], find_child(n, "at")[2])
        for n in tree[1:]
        if is_call(n, "junction") and find_child(n, "at")
    ]
    for jp in junctions:
        uf.add(jp)
        for start, end, _node in segments:
            if _on_segment(jp, start, end):
                uf.union(jp, start)

    # A wire ending on another wire's interior is a T-connection, which KiCAD
    # treats as connected even without an explicit junction dot.
    endpoints = {p for seg in segments for p in seg[:2]}
    for ep in endpoints:
        for start, end, _node in segments:
            if ep in (start, end):
                continue
            if _on_segment(ep, start, end):
                uf.union(ep, start)

    # Pins.
    pins: dict[Point, list[dict]] = {}
    power_names: dict[Point, str] = {}
    for s_node in ed.iter_instance_symbols(tree):
        ref = ed.get_symbol_property(s_node, "Reference") or "?"
        is_power = _is_power_symbol(tree, s_node)
        value = ed.get_symbol_property(s_node, "Value") or ""
        for pin in pins_of_instance(tree, s_node):
            p = pin["point"]
            uf.add(p)
            # A pin landing mid-wire connects to that wire.
            for start, end, _node in segments:
                if _on_segment(p, start, end):
                    uf.union(p, start)
                    break
            pins.setdefault(p, []).append(
                {
                    "ref": ref,
                    "pin": pin["number"],
                    "pin_name": pin["name"],
                    "power": is_power,
                }
            )
            if is_power and value:
                power_names[p] = normalize_name(value)

    # Labels.
    labels: dict[Point, list[dict]] = {}
    for node in tree[1:]:
        head = head_of(node)
        if head not in LABEL_KINDS:
            continue
        at = find_child(node, "at")
        if not at or len(at) < 3 or len(node) < 2 or not isinstance(node[1], str):
            continue
        p = _pt(at[1], at[2])
        uf.add(p)
        for start, end, _node in segments:
            if _on_segment(p, start, end):
                uf.union(p, start)
                break
        labels.setdefault(p, []).append(
            {"kind": head, "name": normalize_name(node[1])}
        )

    # Sheet pins: a hierarchical sheet's ports behave like labels on this sheet.
    sheet_ports: dict[Point, list[dict]] = {}
    for node in tree[1:]:
        if not is_call(node, "sheet"):
            continue
        sheet_name = ed.get_symbol_property(node, "Sheetname") or "?"
        for pin in find_children(node, "pin"):
            at = find_child(pin, "at")
            if not at or len(at) < 3 or len(pin) < 2 or not isinstance(pin[1], str):
                continue
            p = _pt(at[1], at[2])
            uf.add(p)
            for start, end, _node in segments:
                if _on_segment(p, start, end):
                    uf.union(p, start)
                    break
            sheet_ports.setdefault(p, []).append(
                {"sheet": sheet_name, "name": normalize_name(pin[1])}
            )

    no_connects = set()
    for node in tree[1:]:
        if not is_call(node, "no_connect"):
            continue
        at = find_child(node, "at")
        if at and len(at) >= 3:
            p = _pt(at[1], at[2])
            uf.add(p)
            no_connects.add(p)

    return {
        "uf": uf,
        "segments": segments,
        "pins": pins,
        "labels": labels,
        "power_names": power_names,
        "sheet_ports": sheet_ports,
        "no_connects": no_connects,
        "junctions": junctions,
    }


def _generated_name(members: list[dict], connected: bool) -> str:
    """KiCAD's fallback name for an unlabelled net.

    KiCAD writes `Net-(R1-Pad1)` for a real net and `unconnected-(P8-Pad1)` for
    a pin that reaches nothing else. The tag is the pin's name, or `Pad<number>`
    when the name carries nothing extra — empty, `~`, or just the pin number
    repeated.
    """
    if not members:
        return "unconnected"
    first = sorted(members, key=lambda m: (m["ref"], m["pin"]))[0]
    name = first["pin_name"]
    meaningful = name not in ("", "~") and name != first["pin"]
    tag = name if meaningful else f"Pad{first['pin']}"
    prefix = "Net" if connected else "unconnected"
    return f"{prefix}-({first['ref']}-{tag})"


def build_sheet_nets(tree: list, path: Path | None = None) -> SheetNets:
    """Build the named nets of one sheet."""
    graph = build_sheet_graph(tree)
    uf: _Union = graph["uf"]

    members: dict[Any, dict] = {}
    for point, group in uf.groups().items():
        members[point] = {
            "points": group,
            "pins": [],
            "labels": [],
            "sheet_ports": [],
            "no_connect": False,
        }
    for p, pin_list in graph["pins"].items():
        members[uf.find(p)]["pins"].extend(pin_list)
    for p, label_list in graph["labels"].items():
        members[uf.find(p)]["labels"].extend(label_list)
    for p, port_list in graph["sheet_ports"].items():
        members[uf.find(p)]["sheet_ports"].extend(port_list)
    for p in graph["no_connects"]:
        members[uf.find(p)]["no_connect"] = True
    for p, name in graph["power_names"].items():
        members[uf.find(p)].setdefault("power_names", []).append(name)

    result = SheetNets(path)
    for root, data in members.items():
        if not data["pins"] and not data["labels"] and not data["sheet_ports"]:
            continue  # bare wire with nothing on it — reported as dangling below

        name = _name_for(data)
        entry = result.nets.setdefault(
            name,
            {
                "name": name,
                "pins": [],
                "labels": [],
                "sheet_ports": [],
                "no_connect": False,
                "sheets": [],
            },
        )
        for pin in data["pins"]:
            entry["pins"].append(
                {
                    "ref": pin["ref"],
                    "pin": pin["pin"],
                    "pin_name": pin["pin_name"],
                    # A power symbol or power flag: real connectivity, but a
                    # virtual part. KiCAD leaves these out of its netlist.
                    "power": pin["power"],
                }
            )
        entry["labels"].extend(data["labels"])
        entry["sheet_ports"].extend(data["sheet_ports"])
        entry["no_connect"] = entry["no_connect"] or data["no_connect"]
        if path is not None and path.name not in entry["sheets"]:
            entry["sheets"].append(path.name)

    result.dangling = _find_dangling(graph, members, path)
    return result


def _name_for(data: dict) -> str:
    """Pick a net's name. Priority: local, hierarchical, global, power, generated."""
    by_kind = {k: [] for k in LABEL_KINDS}
    for label in data["labels"]:
        by_kind[label["kind"]].append(label["name"])
    for kind in ("label", "hierarchical_label", "global_label"):
        if by_kind[kind]:
            return sorted(by_kind[kind])[0]
    powers = data.get("power_names") or []
    if powers:
        return sorted(powers)[0]
    if data["sheet_ports"]:
        return sorted(p["name"] for p in data["sheet_ports"])[0]
    # One pin and nothing else: KiCAD calls that unconnected, and still lists it.
    real = [p for p in data["pins"] if not p["power"]]
    return _generated_name(data["pins"], connected=len(real) > 1)


def _find_dangling(graph: dict, members: dict, path: Path | None) -> list[dict]:
    """Endpoints and pins that touch nothing else."""
    uf: _Union = graph["uf"]
    out: list[dict] = []
    sheet = path.name if path is not None else None

    for point, group in uf.groups().items():
        data = members.get(point, {})
        attached = len(data.get("pins", [])) + len(data.get("labels", [])) + len(
            data.get("sheet_ports", [])
        )
        if data.get("no_connect"):
            continue
        if attached == 0 and graph["segments"]:
            # A wire whose whole group carries nothing at all.
            for p in group:
                out.append({"kind": "wire_end", "position_mm": list(p), "sheet": sheet})
        elif attached == 1 and len(data.get("pins", [])) == 1:
            pin = data["pins"][0]
            out.append(
                {
                    "kind": "pin",
                    "ref": pin["ref"],
                    "pin": pin["pin"],
                    "position_mm": list(group[0]),
                    "sheet": sheet,
                }
            )
    return out


# --------------------------------------------------------------------------- #
# Hierarchy
# --------------------------------------------------------------------------- #


def build_hierarchy_nets(root_sch: Path) -> dict[str, dict]:
    """Merge the nets of every sheet the root reaches.

    Global labels and power symbols merge by name across sheets. A local label
    is scoped to its sheet, so two sheets using `SDA` locally stay separate —
    they are reported as `SDA` and `SDA~2`, in sheet order, rather than being
    silently joined.
    """
    merged: dict[str, dict] = {}
    global_names: set[str] = set()

    for path in ed.hierarchy_sch_paths(root_sch):
        try:
            tree = ed.sch_io.parse_file(path)
        except (OSError, ValueError):
            continue
        sheet = build_sheet_nets(tree, path)
        for name, entry in sheet.nets.items():
            is_global = any(
                label["kind"] == "global_label" for label in entry["labels"]
            ) or bool(entry["sheet_ports"]) or _looks_like_power(name)

            key = name
            if not is_global and name in merged and name not in global_names:
                n = 2
                while f"{name}~{n}" in merged:
                    n += 1
                key = f"{name}~{n}"
            if is_global:
                global_names.add(name)

            if key in merged:
                merged[key]["pins"].extend(entry["pins"])
                merged[key]["labels"].extend(entry["labels"])
                merged[key]["sheet_ports"].extend(entry["sheet_ports"])
                merged[key]["sheets"].extend(
                    s for s in entry["sheets"] if s not in merged[key]["sheets"]
                )
                merged[key]["no_connect"] = (
                    merged[key]["no_connect"] or entry["no_connect"]
                )
            else:
                merged[key] = dict(entry, name=key)
    return merged


def _looks_like_power(name: str) -> bool:
    """Power nets are global in KiCAD: GND, +3V3, VCC, VDD, …"""
    return bool(re.fullmatch(r"(GND\w*|AGND|DGND|[+-]?\d+V\d*|V(CC|DD|SS|BUS|IN|OUT)\w*)", name))
