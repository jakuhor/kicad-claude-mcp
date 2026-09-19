"""Phase 3 — schematic editing tools.

Tools (all operate on the active project's `.kicad_sch`):
    add_symbol, remove_symbol, move_symbol, replace_symbol
    add_wire, remove_wire, add_label, add_no_connect, add_power_symbol
    add_junction, remove_junction, remove_no_connect, remove_items_in_box
    get_symbol_properties, set_symbol_property, remove_symbol_property
    set_dnp, set_in_bom, set_on_board
    add_text, list_texts, set_text, remove_text, rename_label, move_item
    list_sch_nets, trace_net, get_pin_net, find_dangling
    list_pins, get_pin_position
    list_components_detailed (richer than Phase 1's list_components)

Coordinates: millimetres, KiCAD-native — Y axis points DOWN, origin at the
page's top-left, exactly like the numbers in the KiCAD GUI (see utils/geometry).
Every placement snaps to the 1.27 mm grid unless `snap_to_grid=False`; KiCAD
only connects items whose endpoints share a grid point.
Rotations: 0 / 90 / 180 / 270 only.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import safe_write
from kicad_claude.adapters import sch_io
from kicad_claude.adapters import sch_netlist as netlist
from kicad_claude.templates.blank import write_blank_schematic
from kicad_claude.tools import library as lib_tools
from kicad_claude.utils.geometry import round_mm, snap_xy
from kicad_claude.utils.kicad_strings import normalize_name

logger = logging.getLogger("kicad-claude.tools.schematic")


def _snap(x_mm: float, y_mm: float, snap_to_grid: bool) -> tuple[float, float]:
    """Snap a point to the 1.27 mm schematic grid unless the caller opts out."""
    if not snap_to_grid:
        return round_mm(x_mm), round_mm(y_mm)
    return snap_xy(x_mm, y_mm)


def _candidate_points(x_mm: float, y_mm: float, snap_to_grid: bool) -> list[tuple[float, float]]:
    """Points to try when removing an item: as given, then snapped.

    A wire drawn to a pin is off the 1.27 mm grid more often than not, and
    `snap_to_grid=True` on the remove call used to move the lookup somewhere
    the wire never was. The exact point is therefore tried first, whatever the
    caller asked for, and the snapped one only as a fallback.
    """
    exact = (round_mm(x_mm), round_mm(y_mm))
    if not snap_to_grid:
        return [exact]
    snapped = snap_xy(x_mm, y_mm)
    return [exact] if snapped == exact else [exact, snapped]


# --------------------------------------------------------------------------- #
# Helpers shared across tools
# --------------------------------------------------------------------------- #


def _load_active_schematic() -> tuple[list, Path]:
    """Return (tree, sch_path) for the currently active sheet (root by default)."""
    sch_path = state.get_active_sheet_path()
    tree = sch_io.parse_file(sch_path)
    return tree, sch_path


def _save_with_backup(tree: list, sch_path: Path) -> Path | None:
    """Write the sheet through the guarded path: lock check, backup, verify."""
    return safe_write.save_tree(tree=tree, path=sch_path)


def _root_uuid(proj_sch_path: Path) -> str:
    root = sch_io.parse_file(proj_sch_path)
    u = sch_io.find_child(root, "uuid")
    if u and len(u) >= 2 and isinstance(u[1], str):
        return u[1]
    raise RuntimeError(f"root schematic {proj_sch_path} has no uuid")


def _instance_path() -> str:
    """Build the KiCAD instance path string for the currently active context.

    - Root sheet:  `/{root_uuid}`
    - Child sheet: `/{root_uuid}/{sheet_in_root_uuid}`
    """
    proj = state.get_active()
    root_uuid = _root_uuid(proj.sch_path)
    active_filename = state.get_active_sheet_filename()
    if active_filename is None:
        return f"/{root_uuid}"

    root_tree = sch_io.parse_file(proj.sch_path)
    sheet_node = ed.find_sheet_by_filename(root_tree, active_filename)
    if sheet_node is None:
        raise RuntimeError(
            f"active sheet {active_filename!r} is not registered in the root "
            f"schematic. Was it removed manually?"
        )
    return f"/{root_uuid}/{ed.get_sheet_uuid(sheet_node)}"


def _resolve_lib_symbol(lib_id: str) -> tuple[Path, str, dict]:
    """Look up `lib_id` in the indexer and return (lib_file, symbol_name, meta)."""
    idx = lib_tools._ensure_index()
    meta = idx["symbols"].get(lib_id)
    if meta is None:
        raise KeyError(
            f"unknown lib_id {lib_id!r} — did you call index_libraries first?"
        )
    for d in idx.get("symbol_dirs", []):
        path = Path(d) / f"{meta['lib']}.kicad_sym"
        if path.is_file():
            return path, meta["name"], meta
    raise FileNotFoundError(
        f"source .kicad_sym for {lib_id!r} not found; index may be stale"
    )


def _next_power_reference(tree: list) -> str:
    """Auto-increment a `#PWR####` reference, picking the smallest unused number.

    Scans the whole hierarchy, not just the active sheet: `#PWR` references
    must be unique across all sheets or KiCAD reports annotation errors.
    """
    refs = list(ed.all_references(tree))
    proj = state.get_active_or_none()
    if proj is not None:
        refs.extend(ed.all_references_in_hierarchy(proj.sch_path))
    used = set()
    for ref in refs:
        if not ref:
            continue
        m = re.fullmatch(r"#PWR0*(\d+)", ref)
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"#PWR{n:04d}"


# --------------------------------------------------------------------------- #
# Tool registration
# --------------------------------------------------------------------------- #


def register(mcp) -> None:
    """Register Phase 3 tools on the FastMCP instance."""

    @mcp.tool()
    def add_symbol(
        lib_id: str,
        reference: str,
        value: str,
        x_mm: float,
        y_mm: float,
        rotation: float = 0,
        snap_to_grid: bool = True,
    ) -> dict:
        """Add a symbol from the indexed KiCAD libraries to the active schematic.

        Args:
            lib_id: e.g. "Device:R" or "RF_Module:ESP32-S3-WROOM-1"
            reference: schematic-unique reference designator (e.g. "R1", "U2")
            value: human-visible value ("10k", "100uF", ...)
            x_mm, y_mm: position in KiCAD coords (Y down, origin top-left)
            rotation: 0/90/180/270 degrees CCW
            snap_to_grid: snap the position to the 1.27 mm grid (default True)

        Returns the placed symbol's identity, with the snapped position.
        Refuses if `reference` already exists.
        """
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        lib_path, sym_name, meta = _resolve_lib_symbol(lib_id)
        sym_def = ed.fetch_symbol_def(lib_path, sym_name)

        proj = state.get_active()
        ed.add_symbol(
            tree,
            qualified_lib_id=lib_id,
            reference=reference,
            value=value,
            x_mm=x_mm,
            y_mm=y_mm,
            rotation=rotation,
            sym_def_node=sym_def,
            project_name=proj.name,
            instance_path=_instance_path(),
            footprint=meta.get("default_footprint", ""),
            datasheet=meta.get("datasheet", "~"),
            description=meta.get("description", ""),
        )
        backup = _save_with_backup(tree, path)
        logger.info(
            "added %s (%s) on sheet=%s at (%s, %s)",
            reference, lib_id, state.get_active_sheet_filename() or "root", x_mm, y_mm
        )
        return {
            "reference": reference,
            "lib_id": lib_id,
            "value": value,
            "position_mm": [x_mm, y_mm],
            "rotation": rotation,
            "pin_count": meta.get("pin_count", 0),
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def remove_symbol(reference: str, remove_connected_wires: bool = False) -> dict:
        """Remove the symbol with the given reference from the active schematic.

        With `remove_connected_wires=True`, also removes the no-connect markers
        and junctions that sat on the symbol's pins, plus wire stubs that ended
        on those pins and connect to nothing else. Wires that still reach
        another pin, label or junction stay.
        """
        tree, path = _load_active_schematic()
        result = ed.remove_symbol_with_wires(
            tree, reference, remove_connected_wires=remove_connected_wires
        )
        if not result["removed"]:
            raise KeyError(f"no symbol with reference {reference!r}")
        backup = _save_with_backup(tree, path)
        return {
            "removed": reference,
            "removed_wires": result["wires"],
            "removed_junctions": result["junctions"],
            "removed_no_connects": result["no_connects"],
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def remove_wire(
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
        kind: str = "wire",
        snap_to_grid: bool = True,
    ) -> dict:
        """Remove the wire (or bus) segment running between two points.

        Endpoint order does not matter. `kind` is "wire" or "bus". Points are
        matched with a 0.01 mm tolerance; pass the same coordinates used to
        create the segment, or read them back with `list_pins`.

        The coordinates are tried exactly as given first, and only then snapped
        to the grid — a wire drawn to a pin usually does not sit on it.
        """
        if kind not in ("wire", "bus"):
            raise ValueError(f"kind must be 'wire' or 'bus' (got {kind!r})")
        tree, path = _load_active_schematic()
        tried: list[tuple] = []
        for (x1, y1), (x2, y2) in zip(
            _candidate_points(x1_mm, y1_mm, snap_to_grid),
            _candidate_points(x2_mm, y2_mm, snap_to_grid),
        ):
            tried.append(((x1, y1), (x2, y2)))
            if ed.remove_wire(tree, x1, y1, x2, y2, kind=kind):
                x1_mm, y1_mm, x2_mm, y2_mm = x1, y1, x2, y2
                break
        else:
            raise KeyError(
                f"no {kind} between any of the points tried: "
                + "; ".join(f"{a} to {b}" for a, b in tried)
            )
        backup = _save_with_backup(tree, path)
        return {
            "removed": kind,
            "from_mm": [x1_mm, y1_mm],
            "to_mm": [x2_mm, y2_mm],
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_junction(x_mm: float, y_mm: float, snap_to_grid: bool = True) -> dict:
        """Add a junction dot at a point — the T-joint marker KiCAD needs.

        Idempotent: a junction already at that point is reused.
        """
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        existed = ed.find_point_item(tree, "junction", x_mm, y_mm) is not None
        ed.add_junction(tree, x_mm, y_mm)
        backup = _save_with_backup(tree, path)
        return {
            "position_mm": [x_mm, y_mm],
            "created": not existed,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def remove_junction(x_mm: float, y_mm: float, snap_to_grid: bool = True) -> dict:
        """Remove the junction dot at a point."""
        tree, path = _load_active_schematic()
        points = _candidate_points(x_mm, y_mm, snap_to_grid)
        for x, y in points:
            if ed.remove_junction(tree, x, y):
                x_mm, y_mm = x, y
                break
        else:
            raise KeyError(f"no junction at any of {points}")
        backup = _save_with_backup(tree, path)
        return {
            "removed": "junction",
            "position_mm": [x_mm, y_mm],
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def remove_no_connect(x_mm: float, y_mm: float, snap_to_grid: bool = True) -> dict:
        """Remove the no-connect marker at a point.

        Use `get_pin_position` to find the point of a pin's marker.
        """
        tree, path = _load_active_schematic()
        points = _candidate_points(x_mm, y_mm, snap_to_grid)
        for x, y in points:
            if ed.remove_no_connect(tree, x, y):
                x_mm, y_mm = x, y
                break
        else:
            raise KeyError(f"no no-connect marker at any of {points}")
        backup = _save_with_backup(tree, path)
        return {
            "removed": "no_connect",
            "position_mm": [x_mm, y_mm],
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def remove_items_in_box(
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
        kinds: list[str] | None = None,
    ) -> dict:
        """Remove every item fully inside a rectangle of the active sheet.

        `x1,y1` and `x2,y2` are opposite corners in KiCAD coords. A wire or bus
        is removed only when BOTH endpoints are inside, so a segment crossing
        the box survives.

        `kinds` defaults to everything but symbols: wire, bus, junction,
        no_connect, label, global_label, hierarchical_label, bus_entry, text.
        Pass ["symbol"] explicitly to delete symbols by area.
        """
        tree, path = _load_active_schematic()
        removed = ed.remove_items_in_box(tree, x1_mm, y1_mm, x2_mm, y2_mm, kinds=kinds)
        backup = _save_with_backup(tree, path)
        return {
            "removed": removed,
            "removed_total": sum(removed.values()),
            "box_mm": [[x1_mm, y1_mm], [x2_mm, y2_mm]],
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def move_symbol(
        reference: str,
        x_mm: float,
        y_mm: float,
        rotation: float | None = None,
        snap_to_grid: bool = True,
    ) -> dict:
        """Move (and optionally rotate) an existing symbol. Absolute positioning."""
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        ed.move_symbol(tree, reference, x_mm, y_mm, rotation)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "position_mm": [x_mm, y_mm],
            "rotation": rotation,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_wire(
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
        snap_to_grid: bool = True,
    ) -> dict:
        """Add a straight wire segment between two points.

        Both endpoints snap to the 1.27 mm grid by default — an off-grid
        endpoint does not connect to anything in KiCAD.
        """
        x1_mm, y1_mm = _snap(x1_mm, y1_mm, snap_to_grid)
        x2_mm, y2_mm = _snap(x2_mm, y2_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        ed.add_wire(tree, x1_mm, y1_mm, x2_mm, y2_mm)
        backup = _save_with_backup(tree, path)
        return {
            "from_mm": [x1_mm, y1_mm],
            "to_mm": [x2_mm, y2_mm],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_label(
        net_name: str,
        x_mm: float,
        y_mm: float,
        orientation: str = "right",
        snap_to_grid: bool = True,
    ) -> dict:
        """Add a net label at a point. orientation ∈ {right, up, left, down}."""
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        ed.add_label(tree, net_name, x_mm, y_mm, orientation)
        backup = _save_with_backup(tree, path)
        return {
            "net": net_name,
            "position_mm": [x_mm, y_mm],
            "orientation": orientation,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_power_symbol(
        net: str, x_mm: float, y_mm: float, snap_to_grid: bool = True
    ) -> dict:
        """Place a power symbol (e.g. +5V, +3V3, GND) from the `power` library.

        Auto-assigns a `#PWR####` reference. The library symbol id is
        `power:{net}`; if that doesn't exist in the index, the call fails with
        a hint listing valid power nets.
        """
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        candidate = f"power:{net}"
        idx = lib_tools._ensure_index()
        if candidate not in idx["symbols"]:
            available = sorted(
                k.split(":", 1)[1] for k in idx["symbols"] if k.startswith("power:")
            )
            raise KeyError(
                f"unknown power net {net!r} (looked up {candidate!r}); "
                f"available: {available[:20]}{'...' if len(available) > 20 else ''}"
            )

        tree, path = _load_active_schematic()
        ref = _next_power_reference(tree)
        lib_path, sym_name, meta = _resolve_lib_symbol(candidate)
        sym_def = ed.fetch_symbol_def(lib_path, sym_name)
        proj = state.get_active()
        ed.add_symbol(
            tree,
            qualified_lib_id=candidate,
            reference=ref,
            value=net,
            x_mm=x_mm,
            y_mm=y_mm,
            rotation=0,
            sym_def_node=sym_def,
            project_name=proj.name,
            instance_path=_instance_path(),
            footprint=meta.get("default_footprint", ""),
            datasheet=meta.get("datasheet", "~"),
            description=meta.get("description", ""),
        )
        backup = _save_with_backup(tree, path)
        return {
            "net": net,
            "reference": ref,
            "position_mm": [x_mm, y_mm],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_no_connect(reference: str, pin: str) -> dict:
        """Mark a pin as no-connect by placing a NC marker at its position."""
        tree, path = _load_active_schematic()
        x, y = ed.get_pin_position(tree, reference, pin)
        ed.add_no_connect(tree, x, y)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "pin": pin,
            "position_mm": [x, y],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def list_pins(reference: str) -> list[dict]:
        """List pins of a placed symbol with their absolute positions (KiCAD coords)."""
        tree, _ = _load_active_schematic()
        return ed.list_pins_for_symbol(tree, reference)

    @mcp.tool()
    def get_pin_position(reference: str, pin: str) -> dict:
        """Return absolute (x, y) of one pin in KiCAD coordinates (Y down)."""
        tree, _ = _load_active_schematic()
        x, y = ed.get_pin_position(tree, reference, pin)
        return {"reference": reference, "pin": pin, "position_mm": [x, y]}

    @mcp.tool()
    def replace_symbol(
        reference: str,
        lib_id: str,
        pin_map: dict[str, str] | None = None,
        value: str | None = None,
    ) -> dict:
        """Swap a placed symbol's library part, keeping its identity and wiring.

        Kept: reference, position, rotation, mirror, unit, uuid, the
        `(instances ...)` block and the dnp / in_bom / on_board flags. The
        symbol stays the same symbol to KiCAD, so the PCB keeps its footprint
        association. Value, Footprint, Datasheet and Description carry over
        unless `value` overrides.

        `pin_map` maps old pin number to new pin number, e.g. `{"1": "2"}`.
        Wires, junctions, no-connects and labels on a mapped pin move to that
        pin's new position. Omit it to map every pin number the two parts share.

        An old pin with no mapping keeps whatever was attached where it is,
        which normally leaves it dangling — the result lists those under
        `left_dangling` rather than dropping them. `new_pins_unconnected` lists
        pins of the new part nothing reached. Check both, then `run_erc`.
        """
        tree, path = _load_active_schematic()
        lib_path, sym_name, meta = _resolve_lib_symbol(lib_id)
        sym_def = ed.fetch_symbol_def(lib_path, sym_name)

        result = ed.replace_symbol(
            tree,
            reference,
            qualified_lib_id=lib_id,
            sym_def_node=sym_def,
            pin_map=pin_map,
            value=value,
        )
        backup = _save_with_backup(tree, path)
        logger.info(
            "replaced %s: %s -> %s (%d pins reconnected, %d left dangling)",
            reference, result["from_lib_id"], lib_id,
            len(result["reconnected"]), len(result["left_dangling"]),
        )
        result["sheet"] = state.get_active_sheet_filename() or "root"
        result["backup"] = str(backup) if backup else None
        return result

    # ----- Connectivity (derived, read-only) ------------------------------ #

    @mcp.tool()
    def list_sch_nets(scope: str = "active", include_power_pins: bool = False) -> dict:
        """Every net of the schematic, derived from wire and pin geometry.

        A `.kicad_sch` stores no net list — nets are geometry, so this rebuilds
        them the way KiCAD does: wires join their endpoints, a junction joins
        everything meeting at a point, labels name the result.

        `scope="active"` reads the active sheet; `"all"` walks the hierarchy and
        merges global labels, power nets and sheet ports across sheets.

        Power symbols (`#PWR`) and power flags (`#FLG`) are real connections but
        virtual parts, so their pins are hidden unless `include_power_pins=True`
        — this matches what `kicad-cli sch export netlist` reports.
        """
        if scope not in ("active", "all"):
            raise ValueError(f"scope must be 'active' or 'all' (got {scope!r})")

        if scope == "active":
            path = state.get_active_sheet_path()
            nets = netlist.build_sheet_nets(sch_io.parse_file(path), path).nets
        else:
            nets = netlist.build_hierarchy_nets(state.get_active().sch_path)

        out = []
        for name in sorted(nets):
            entry = nets[name]
            pins = [
                p for p in entry["pins"] if include_power_pins or not p["power"]
            ]
            out.append(
                {
                    "name": name,
                    "pin_count": len(pins),
                    "pins": pins,
                    "labels": entry["labels"],
                    "sheets": entry["sheets"],
                    "no_connect": entry["no_connect"],
                }
            )
        return {"scope": scope, "net_count": len(out), "nets": out}

    @mcp.tool()
    def trace_net(name: str, scope: str = "active") -> dict:
        """Everything on one net: pins, labels and sheet ports.

        The name is matched against the derived net names from `list_sch_nets`,
        including generated ones like `Net-(R1-Pad1)`.
        """
        if scope not in ("active", "all"):
            raise ValueError(f"scope must be 'active' or 'all' (got {scope!r})")
        if scope == "active":
            path = state.get_active_sheet_path()
            nets = netlist.build_sheet_nets(sch_io.parse_file(path), path).nets
        else:
            nets = netlist.build_hierarchy_nets(state.get_active().sch_path)

        wanted = normalize_name(name)
        for net_name, entry in nets.items():
            if normalize_name(net_name) == wanted:
                return {
                    "name": net_name,
                    "pin_count": len([p for p in entry["pins"] if not p["power"]]),
                    "pins": entry["pins"],
                    "labels": entry["labels"],
                    "sheet_ports": entry["sheet_ports"],
                    "sheets": entry["sheets"],
                    "no_connect": entry["no_connect"],
                }
        raise KeyError(
            f"no net named {name!r} in scope {scope!r}; "
            f"call list_sch_nets to see the derived names"
        )

    @mcp.tool()
    def get_pin_net(reference: str, pin: str, scope: str = "active") -> dict:
        """The net a given pin sits on.

        `connected` is False when the pin reaches no other pin. KiCAD still
        names such a net — `unconnected-(R1-Pad1)` — so read `connected`, not
        the presence of a name. `net` is None only when the reference or pin
        does not exist on the sheet.
        """
        if scope not in ("active", "all"):
            raise ValueError(f"scope must be 'active' or 'all' (got {scope!r})")
        if scope == "active":
            path = state.get_active_sheet_path()
            nets = netlist.build_sheet_nets(sch_io.parse_file(path), path).nets
        else:
            nets = netlist.build_hierarchy_nets(state.get_active().sch_path)

        ref = normalize_name(reference)
        for net_name, entry in nets.items():
            for p in entry["pins"]:
                if normalize_name(p["ref"]) == ref and p["pin"] == pin:
                    others = [
                        q for q in entry["pins"]
                        if not (q["ref"] == p["ref"] and q["pin"] == p["pin"])
                    ]
                    return {
                        "reference": reference,
                        "pin": pin,
                        "net": net_name,
                        "connected": any(not q["power"] for q in others),
                        "connected_to": others,
                    }
        return {
            "reference": reference,
            "pin": pin,
            "net": None,
            "connected": False,
            "connected_to": [],
        }

    @mcp.tool()
    def find_dangling(scope: str = "active") -> dict:
        """Wire ends and pins that connect to nothing.

        The same class of defect ERC reports, without a `kicad-cli` round trip.
        Pins carrying a `no_connect` marker are not reported.
        """
        if scope not in ("active", "all"):
            raise ValueError(f"scope must be 'active' or 'all' (got {scope!r})")
        if scope == "active":
            paths = [state.get_active_sheet_path()]
        else:
            paths = ed.hierarchy_sch_paths(state.get_active().sch_path)

        items: list[dict] = []
        for p in paths:
            sheet = netlist.build_sheet_nets(sch_io.parse_file(p), p)
            items.extend(sheet.dangling)
        return {"scope": scope, "count": len(items), "items": items}

    # ----- Text notes, label renaming, generic moves ---------------------- #

    @mcp.tool()
    def add_text(
        x_mm: float,
        y_mm: float,
        text: str,
        size_mm: float = 1.27,
        rotation: float = 0,
        snap_to_grid: bool = True,
    ) -> dict:
        """Add a free text note to the active sheet.

        A note is documentation — it carries no connectivity. Newlines are
        allowed. Returns the note's `uuid`, which `set_text`, `remove_text` and
        `move_item` take.
        """
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        node = ed.add_text(tree, text, x_mm, y_mm, size_mm=size_mm, rotation=rotation)
        backup = _save_with_backup(tree, path)
        return {
            "uuid": ed.get_uuid(node),
            "text": text,
            "position_mm": [x_mm, y_mm],
            "size_mm": size_mm,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def list_texts() -> list[dict]:
        """Every free text note on the active sheet, with uuid and position."""
        tree, _ = _load_active_schematic()
        items = ed.list_texts(tree)
        for item in items:
            item["text"] = normalize_name(item["text"])
        return items

    @mcp.tool()
    def set_text(uuid: str, text: str) -> dict:
        """Replace a text note's content. `uuid` comes from `list_texts`."""
        tree, path = _load_active_schematic()
        previous = ed.set_text(tree, uuid, text)
        backup = _save_with_backup(tree, path)
        return {
            "uuid": uuid,
            "text": text,
            "previous": previous,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def remove_text(uuid: str) -> dict:
        """Delete a text note by uuid."""
        tree, path = _load_active_schematic()
        previous = ed.remove_text(tree, uuid)
        backup = _save_with_backup(tree, path)
        return {
            "uuid": uuid,
            "removed_text": previous,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def rename_label(old_name: str, new_name: str, scope: str = "all") -> dict:
        """Rename a net label everywhere it appears.

        Rewrites `label`, `global_label`, `hierarchical_label` and the matching
        `(pin ...)` of every `(sheet ...)`. `scope="all"` (the default) walks
        the whole hierarchy; `scope="active"` touches only the active sheet.

        The default is `"all"` on purpose: a global label or sheet pin left
        behind on another sheet splits one net into two, and nothing reports it
        until ERC.
        """
        if scope not in ("active", "all"):
            raise ValueError(f"scope must be 'active' or 'all' (got {scope!r})")
        if not new_name:
            raise ValueError("new_name must not be empty")

        if scope == "active":
            paths = [state.get_active_sheet_path()]
        else:
            paths = ed.hierarchy_sch_paths(state.get_active().sch_path)

        total = {"label": 0, "global_label": 0, "hierarchical_label": 0, "sheet_pin": 0}
        per_sheet: list[dict] = []
        backups: list[str] = []
        for p in paths:
            tree = sch_io.parse_file(p)
            counts = ed.rename_label_in_tree(tree, old_name, new_name)
            if not any(counts.values()):
                continue
            backup = _save_with_backup(tree, p)
            if backup:
                backups.append(str(backup))
            for k, v in counts.items():
                total[k] += v
            per_sheet.append({"sheet": p.name, **counts})

        if not per_sheet:
            raise KeyError(f"no label named {old_name!r} found in scope {scope!r}")
        return {
            "old_name": old_name,
            "new_name": new_name,
            "scope": scope,
            "renamed": total,
            "sheets": per_sheet,
            "backups": backups,
        }

    @mcp.tool()
    def move_item(uuid: str, x_mm: float, y_mm: float, snap_to_grid: bool = True) -> dict:
        """Move one item on the active sheet to an absolute position.

        Works for wire, bus, label, global_label, hierarchical_label, junction,
        no_connect, text, symbol and sheet. A wire or bus moves as a whole: its
        first point lands on the target and the rest follow by the same delta.

        Get a `uuid` from `list_texts`, or read it from the file. Moving a wire
        away from a pin breaks that connection — re-check with `run_erc`.
        """
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        result = ed.move_item(tree, uuid, x_mm, y_mm)
        backup = _save_with_backup(tree, path)
        return {
            "uuid": uuid,
            **result,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    # ----- Symbol properties and flags ------------------------------------ #

    @mcp.tool()
    def get_symbol_properties(reference: str) -> dict:
        """Every property and flag of a placed symbol.

        `properties` holds the text fields (Value, Footprint, MPN, …) with their
        exact names. `flags` holds the booleans KiCAD stores as nodes: dnp,
        in_bom, on_board, exclude_from_sim.
        """
        tree, _ = _load_active_schematic()
        s_node = ed.find_symbol_by_reference(tree, reference)
        if s_node is None:
            raise KeyError(f"no symbol with reference {reference!r}")
        props = {}
        for prop in sch_io.find_children(s_node, "property"):
            i = sch_io.property_name_index(prop)
            if len(prop) > i + 1 and isinstance(prop[i], str):
                props[prop[i]] = normalize_name(str(prop[i + 1]))
        return {
            "reference": reference,
            "properties": props,
            "flags": {f: ed.get_symbol_flag(s_node, f) for f in ed.SYMBOL_FLAGS},
            "sheet": state.get_active_sheet_filename() or "root",
        }

    @mcp.tool()
    def set_symbol_property(
        reference: str, name: str, value: str, create: bool = False
    ) -> dict:
        """Set a text property (Value, Footprint, MPN, …) on a placed symbol.

        With `create=True` a property that does not exist yet is added, hidden,
        positioned like the symbol's Value field.

        `Reference` is refused: renaming a designator here would leave the
        symbol's `(instances ...)` paths pointing at the old one. Use
        `annotate_schematic`.
        """
        if name == "Reference":
            raise ValueError(
                "refusing to set 'Reference' — it would desync the symbol's "
                "(instances ...) paths; use annotate_schematic instead"
            )
        tree, path = _load_active_schematic()
        s_node = ed.find_symbol_by_reference(tree, reference)
        if s_node is None:
            raise KeyError(f"no symbol with reference {reference!r}")

        previous = ed.get_symbol_property(s_node, name)
        if previous is None:
            if not create:
                raise KeyError(
                    f"symbol {reference!r} has no property {name!r}; "
                    f"pass create=True to add it"
                )
            ed.add_symbol_property(s_node, name, value)
        else:
            ed.set_symbol_property(s_node, name, value)

        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "property": name,
            "value": value,
            "previous": previous,
            "created": previous is None,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def remove_symbol_property(reference: str, name: str) -> dict:
        """Remove a text property from a placed symbol.

        KiCAD's five mandatory fields (Reference, Value, Footprint, Datasheet,
        Description) are refused — removing one makes the symbol invalid.
        """
        mandatory = {"Reference", "Value", "Footprint", "Datasheet", "Description"}
        if name in mandatory:
            raise ValueError(f"{name!r} is a mandatory KiCAD field and cannot be removed")
        tree, path = _load_active_schematic()
        s_node = ed.find_symbol_by_reference(tree, reference)
        if s_node is None:
            raise KeyError(f"no symbol with reference {reference!r}")
        previous = ed.get_symbol_property(s_node, name)
        if not ed.remove_symbol_property(s_node, name):
            raise KeyError(f"symbol {reference!r} has no property {name!r}")
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "property": name,
            "previous": previous,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    def _set_flag(reference: str, flag: str, value: bool) -> dict:
        tree, path = _load_active_schematic()
        s_node = ed.find_symbol_by_reference(tree, reference)
        if s_node is None:
            raise KeyError(f"no symbol with reference {reference!r}")
        previous = ed.get_symbol_flag(s_node, flag)
        ed.set_symbol_flag(s_node, flag, value)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "flag": flag,
            "value": value,
            "previous": previous,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def set_dnp(reference: str, dnp: bool = True) -> dict:
        """Mark a symbol Do Not Populate (or clear the mark).

        DNP parts stay on the board and in the netlist; fabrication BOM and
        position exports drop them.
        """
        return _set_flag(reference, "dnp", dnp)

    @mcp.tool()
    def set_in_bom(reference: str, in_bom: bool = True) -> dict:
        """Include or exclude a symbol from the BOM (mounting holes, logos, …)."""
        return _set_flag(reference, "in_bom", in_bom)

    @mcp.tool()
    def set_on_board(reference: str, on_board: bool = True) -> dict:
        """Include or exclude a symbol from the board — `update_pcb_from_schematic`
        will not push an excluded symbol to the PCB."""
        return _set_flag(reference, "on_board", on_board)

    # ----- Buses (visual grouping of multiple nets) ----------------------- #

    @mcp.tool()
    def add_bus(
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
        snap_to_grid: bool = True,
    ) -> dict:
        """Add a bus line to the active sheet.

        A bus is the thick line that visually groups several wires (e.g.
        an 8-bit data bus). Each wire that taps off the bus uses
        `add_bus_entry` and a `add_label` with the individual net name.

        Bus naming uses KiCAD conventions:
          - Bracket form: `DATA[0..7]` for 8 wires
          - Alias form: declare with `add_bus_alias("DATA", ["D0", "D1", ...])`
        """
        x1_mm, y1_mm = _snap(x1_mm, y1_mm, snap_to_grid)
        x2_mm, y2_mm = _snap(x2_mm, y2_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        ed.add_bus_segment(tree, x1_mm, y1_mm, x2_mm, y2_mm)
        backup = _save_with_backup(tree, path)
        return {
            "from_mm": [x1_mm, y1_mm],
            "to_mm": [x2_mm, y2_mm],
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_bus_entry(
        x_mm: float,
        y_mm: float,
        direction: str = "right_down",
        snap_to_grid: bool = True,
    ) -> dict:
        """Add a `(bus_entry ...)` — the diagonal connector from a bus to a wire.

        `direction` is one of: right_down, right_up, left_down, left_up.
        Place at the point where the bus meets the entry; KiCAD draws a
        diagonal line from there to the wire side.
        """
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        ed.add_bus_entry(tree, x_mm, y_mm, direction=direction)
        backup = _save_with_backup(tree, path)
        return {
            "position_mm": [x_mm, y_mm],
            "direction": direction,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_bus_alias(alias_name: str, members: list[str]) -> dict:
        """Declare a bus alias: short name → list of member net names.

        Example: `add_bus_alias("MEM", ["MA0","MA1","MA2","MA3","MA4","MA5","MA6","MA7"])`.
        After this, the bus can be labelled "MEM" and KiCAD expands it to MA0..MA7.
        """
        tree, path = _load_active_schematic()
        ed.add_bus_alias(tree, alias_name, members)
        backup = _save_with_backup(tree, path)
        return {
            "alias": alias_name,
            "members": members,
            "member_count": len(members),
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    # ----- Hierarchical sheets ------------------------------------------- #

    @mcp.tool()
    def add_sheet(
        sheet_name: str,
        sheet_filename: str | None = None,
        x_mm: float = 50,
        y_mm: float = 50,
        width_mm: float = 30,
        height_mm: float = 20,
        snap_to_grid: bool = True,
    ) -> dict:
        """Create a child sub-sheet on the ROOT schematic.

        (`x_mm`, `y_mm`) is the sheet block's TOP-LEFT corner.

        Adds a `(sheet ...)` placeholder to the root and creates a fresh
        blank child `.kicad_sch` file. The active context stays on root —
        call `set_active_sheet` to switch into the new child.

        If `sheet_filename` is omitted, it defaults to a slugified
        `{sheet_name}.kicad_sch`.
        """
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        proj = state.get_active()
        if sheet_filename is None:
            slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in sheet_name).strip("_")
            sheet_filename = f"{slug.lower() or 'subsheet'}.kicad_sch"
        if not sheet_filename.endswith(".kicad_sch"):
            raise ValueError("sheet_filename must end with .kicad_sch")

        # Always operate on root — sheets must live in the root.
        previous_sheet = state.get_active_sheet_filename()
        state.set_active_sheet(None)
        try:
            tree, root_path = _load_active_schematic()
            ed.add_sheet_node(
                tree,
                sheet_name=sheet_name,
                sheet_filename=sheet_filename,
                x_mm=x_mm,
                y_mm=y_mm,
                width_mm=width_mm,
                height_mm=height_mm,
                project_name=proj.name,
            )
            backup = _save_with_backup(tree, root_path)
            child_path = proj.path / sheet_filename
            write_blank_schematic(child_path)
        finally:
            if previous_sheet is not None:
                state.set_active_sheet(previous_sheet)
        logger.info("added sheet %s -> %s", sheet_name, sheet_filename)
        return {
            "sheet_name": sheet_name,
            "sheet_filename": sheet_filename,
            "position_mm": [x_mm, y_mm],
            "child_path": str(child_path),
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def set_active_sheet(sheet: str = "") -> dict:
        """Switch the editing context to a sub-sheet (or back to root).

        `sheet` can be:
          - "" or "root" — operate on the root schematic
          - "<name>.kicad_sch" — operate on that child sheet
          - the sheet's name as registered via `add_sheet`
        """
        proj = state.get_active()
        if not sheet or sheet.lower() == "root":
            state.set_active_sheet(None)
            return {"active_sheet": "root", "path": str(proj.sch_path)}

        # Accept either filename or sheet_name
        if not sheet.endswith(".kicad_sch"):
            root_tree = sch_io.parse_file(proj.sch_path)
            node = ed.find_sheet_by_name(root_tree, sheet)
            if node is None:
                raise KeyError(f"no sheet named {sheet!r} in root")
            sheet = ed.get_sheet_filename(node) or ""
            if not sheet:
                raise RuntimeError(f"sheet {sheet!r} has no Sheetfile")

        state.set_active_sheet(sheet)
        return {
            "active_sheet": sheet,
            "path": str(state.get_active_sheet_path()),
        }

    @mcp.tool()
    def get_active_sheet() -> dict:
        """Return the currently active sub-sheet (or 'root')."""
        active = state.get_active_sheet_filename()
        return {
            "active_sheet": active or "root",
            "path": str(state.get_active_sheet_path()),
        }

    @mcp.tool()
    def list_sheets() -> dict:
        """List the root schematic and every registered child sheet."""
        proj = state.get_active()
        root = sch_io.parse_file(proj.sch_path)
        children = ed.list_sheets(root)
        return {
            "root": {"name": "root", "path": str(proj.sch_path)},
            "children": children,
            "count": len(children),
        }

    @mcp.tool()
    def add_hierarchical_label(
        net_name: str,
        x_mm: float,
        y_mm: float,
        shape: str = "input",
        orientation: str = "right",
        snap_to_grid: bool = True,
    ) -> dict:
        """Add a hierarchical label on the active SUB-sheet.

        Hierarchical labels are how a child sheet exposes a net to the
        parent. Each label must be matched by a sheet pin of the same name
        in the parent's `(sheet ...)` block — see `add_sheet_pin`.

        `shape`: input | output | bidirectional | tri_state | passive
        `orientation`: right | up | left | down
        """
        if state.get_active_sheet_filename() is None:
            raise RuntimeError(
                "hierarchical labels belong on a sub-sheet; call set_active_sheet "
                "first or use add_label for the root."
            )
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        ed.add_hierarchical_label(
            tree, net_name=net_name, x_mm=x_mm, y_mm=y_mm,
            shape=shape, orientation=orientation,
        )
        backup = _save_with_backup(tree, path)
        return {
            "net": net_name,
            "shape": shape,
            "orientation": orientation,
            "position_mm": [x_mm, y_mm],
            "sheet": state.get_active_sheet_filename(),
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()
    def add_sheet_pin(
        sheet_name: str,
        pin_name: str,
        shape: str,
        x_mm: float,
        y_mm: float,
        orientation: str = "right",
        snap_to_grid: bool = True,
    ) -> dict:
        """Add a sheet pin to a child sheet's placeholder on the ROOT.

        The pin must match a `hierarchical_label` of the same name inside
        the child sheet — that's how KiCAD bridges the nets.

        `pin_name` is the net name to expose (e.g., "+5V").
        `shape`: input | output | bidirectional | tri_state | passive
        """
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        previous = state.get_active_sheet_filename()
        state.set_active_sheet(None)
        try:
            tree, root_path = _load_active_schematic()
            sheet_node = ed.find_sheet_by_name(tree, sheet_name)
            if sheet_node is None:
                raise KeyError(f"no sheet named {sheet_name!r}")
            ed.add_sheet_pin(
                sheet_node,
                pin_name=pin_name,
                shape=shape,
                x_mm=x_mm,
                y_mm=y_mm,
                orientation=orientation,
            )
            backup = _save_with_backup(tree, root_path)
        finally:
            if previous is not None:
                state.set_active_sheet(previous)
        return {
            "sheet": sheet_name,
            "pin": pin_name,
            "shape": shape,
            "orientation": orientation,
            "position_mm": [x_mm, y_mm],
            "backup": str(backup) if backup else None,
        }
