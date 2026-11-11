"""Schematic editing tools.

Tools (all operate on the active project's `.kicad_sch`):
    add_symbol, remove_symbol, replace_symbol, move_item
    add_wire, add_label, add_no_connect, add_power_symbol, add_junction
    remove_item, remove_items_in_box
    get_symbol_properties, set_symbol_property, remove_symbol_property
    set_symbol_flags
    add_text, list_texts, set_text, rename_label
    list_sch_nets, trace_net, get_pin_net, find_dangling
    list_lib_symbols, refresh_lib_symbols
    list_pins, get_pin_position

Coordinates: millimetres, KiCAD-native — Y axis points DOWN, origin at the
page's top-left, exactly like the numbers in the KiCAD GUI (see utils/geometry).
Every placement snaps to the 1.27 mm grid unless `snap_to_grid=False`; KiCAD
only connects items whose endpoints share a grid point.
Rotations: 0 / 90 / 180 / 270 only.
"""

from __future__ import annotations

import logging
from typing import Literal
from pathlib import Path

from kicad_claude import state
from kicad_claude.tools.common import on_sheet
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import safe_write
from kicad_claude.adapters import sch_io
from kicad_claude.adapters import sch_netlist as netlist
from kicad_claude.templates.blank import write_blank_schematic
from kicad_claude.tools.library import index as lib_tools
from kicad_claude.utils.geometry import round_mm, snap_xy
from kicad_claude.utils.kicad_strings import normalize_name

logger = logging.getLogger("kicad-claude.tools.sch.schematic")


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
    if active_filename is None or active_filename == Path(proj.sch_path).name:
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


def _next_virtual_reference(tree: list, prefix: str = "#PWR") -> str:
    """Smallest unused `<prefix>####` across the whole hierarchy, not just this sheet."""
    refs = list(ed.all_references(tree))
    proj = state.get_active_or_none()
    if proj is not None:
        refs.extend(ed.all_references_in_hierarchy(proj.sch_path))
    return ed.next_virtual_reference(refs, prefix)


def _fresh_lib_def(qualified_lib_id: str) -> list:
    """Read a symbol definition straight from the library file on disk."""
    lib_path, sym_name, _meta = _resolve_lib_symbol(qualified_lib_id)
    return ed.fetch_symbol_def(lib_path, sym_name)


# --------------------------------------------------------------------------- #
# Tool registration
# --------------------------------------------------------------------------- #


def register(mcp) -> None:
    """Register the tools."""

    @mcp.tool()

    @on_sheet
    def add_symbol(
        lib_id: str,
        reference: str,
        value: str,
        x_mm: float,
        y_mm: float,
        rotation: float = 0,
        snap_to_grid: bool = True,
        footprint: str = "",
        fields: dict[str, str] | None = None,
    ) -> dict:
        """Place a library symbol on the active sheet. `lib_id` "Lib:Name";
        `reference` must be unique; `x_mm` / `y_mm` KiCAD coords (Y down),
        snapped to 1.27 mm unless `snap_to_grid=False`; `rotation` 0/90/180/270
        CCW; `footprint` "Lib:FP" (empty = library default); `fields` extra
        properties (new ones hidden).

        Details:

        Args:
            lib_id: e.g. "Device:R" or "RF_Module:ESP32-S3-WROOM-1"
            reference: schematic-unique reference designator (e.g. "R1", "U2")
            value: human-visible value ("10k", "100uF", ...)
            x_mm, y_mm: position in KiCAD coords (Y down, origin top-left)
            rotation: 0/90/180/270 degrees CCW
            snap_to_grid: snap the position to the 1.27 mm grid (default True)
            footprint: "Lib:Footprint" for the Footprint field. Empty uses the
                library symbol's default footprint (generic passives have none).
            fields: extra fields to write in the same pass, e.g.
                {"MPN": "RC0603FR-0710KL", "Manufacturer": "Yageo",
                 "Tolerance": "1%"}. Existing fields (Datasheet, Description)
                are overwritten; new ones are added hidden. Reference, Value
                and Footprint go through their own arguments.

        Returns the placed symbol's identity, with the snapped position.
        Refuses if `reference` already exists.
        """
        fields = dict(fields or {})
        reserved = {"Reference", "Value", "Footprint"} & set(fields)
        if reserved:
            raise ValueError(
                f"set {sorted(reserved)} with the reference/value/footprint arguments, "
                "not in fields"
            )
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        lib_path, sym_name, meta = _resolve_lib_symbol(lib_id)
        sym_def = ed.fetch_symbol_def(lib_path, sym_name)

        warnings = []
        if footprint:
            known = lib_tools._ensure_index().get("footprints") or {}
            if known and footprint not in known:
                warnings.append(f"footprint {footprint!r} is not in the library index")

        proj = state.get_active()
        node = ed.add_symbol(
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
            footprint=footprint or meta.get("default_footprint", ""),
            datasheet=meta.get("datasheet", "~"),
            description=meta.get("description", ""),
        )
        for name, val in fields.items():
            if ed.get_symbol_property(node, name) is None:
                ed.add_symbol_property(node, name, str(val))
            else:
                ed.set_symbol_property(node, name, str(val))
        junctions = ed.junction_mid_wire_pins(tree, node)
        backup = _save_with_backup(tree, path)
        logger.info(
            "added %s (%s) on sheet=%s at (%s, %s)",
            reference, lib_id, state.get_active_sheet_filename() or "root", x_mm, y_mm
        )
        result = {
            "reference": reference,
            "lib_id": lib_id,
            "value": value,
            "footprint": ed.get_symbol_property(node, "Footprint") or "",
            "fields": sorted(fields),
            "position_mm": [x_mm, y_mm],
            "rotation": rotation,
            "pin_count": meta.get("pin_count", 0),
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }
        if junctions:
            result["junctions_added"] = junctions
        if not result["footprint"] and not reference.startswith("#"):  # #PWR, #FLG: none
            warnings.append("no footprint set; pass footprint= or set it before PCB sync")
        if warnings:
            result["warnings"] = warnings
        return result

    @mcp.tool()

    @on_sheet
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

    @on_sheet
    def remove_item(
        uuid: str = "",
        kind: Literal["wire", "bus", "junction", "no_connect"] | None = None,
        x_mm: float | None = None,
        y_mm: float | None = None,
        x1_mm: float | None = None,
        y1_mm: float | None = None,
        x2_mm: float | None = None,
        y2_mm: float | None = None,
        snap_to_grid: bool = True,
    ) -> dict:
        """Remove one item from the active sheet, by `uuid` or by position.

        `uuid`: any wire, bus, label, junction, no_connect, text or bus entry.
        By position: `kind` junction / no_connect at (`x_mm`, `y_mm`); wire /
        bus between (`x1_mm`, `y1_mm`) and (`x2_mm`, `y2_mm`), either order.
        Symbols: `remove_symbol`; areas: `remove_items_in_box`.

        Details:

        Points match with a 0.01 mm tolerance. They are tried exactly as given
        first and only then snapped to the grid, because a wire drawn to a pin
        usually does not sit on the grid. Text uuids come from `list_texts`.
        """
        tree, path = _load_active_schematic()
        sheet = state.get_active_sheet_filename() or "root"
        point = (x_mm, y_mm)
        ends = (x1_mm, y1_mm, x2_mm, y2_mm)
        if uuid:
            if kind is not None or any(v is not None for v in point + ends):
                raise ValueError("pass uuid alone, or kind with a position")
            result = ed.remove_by_uuid(tree, uuid)
            backup = _save_with_backup(tree, path)
            return {"removed": result["kind"], "uuid": uuid,
                    **({"removed_text": result["text"]} if "text" in result else {}),
                    "sheet": sheet, "backup": str(backup) if backup else None}
        if kind is None:
            raise ValueError("pass uuid, or kind with a position")
        if kind in ("wire", "bus"):
            if any(v is None for v in ends) or any(v is not None for v in point):
                raise ValueError(f"a {kind} takes both ends: x1_mm, y1_mm, x2_mm, y2_mm")
            tried: list[tuple] = []
            for (x1, y1), (x2, y2) in zip(
                _candidate_points(x1_mm, y1_mm, snap_to_grid),
                _candidate_points(x2_mm, y2_mm, snap_to_grid),
            ):
                tried.append(((x1, y1), (x2, y2)))
                if ed.remove_wire(tree, x1, y1, x2, y2, kind=kind):
                    break
            else:
                raise KeyError(f"no {kind} between any of the points tried: "
                               + "; ".join(f"{a} to {b}" for a, b in tried))
            backup = _save_with_backup(tree, path)
            return {"removed": kind, "from_mm": [x1, y1], "to_mm": [x2, y2],
                    "sheet": sheet, "backup": str(backup) if backup else None}
        if any(v is None for v in point) or any(v is not None for v in ends):
            raise ValueError(f"a {kind} is a point: x_mm, y_mm")
        remover = ed.remove_junction if kind == "junction" else ed.remove_no_connect
        points = _candidate_points(x_mm, y_mm, snap_to_grid)
        for x, y in points:
            if remover(tree, x, y):
                break
        else:
            raise KeyError(f"no {kind} at any of {points}")
        backup = _save_with_backup(tree, path)
        return {"removed": kind, "position_mm": [x, y],
                "sheet": sheet, "backup": str(backup) if backup else None}

    @mcp.tool()

    @on_sheet
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

    @on_sheet
    def remove_items_in_box(
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
        kinds: list[Literal["wire", "bus", "junction", "no_connect", "label",
                            "global_label", "hierarchical_label", "bus_entry",
                            "text", "symbol"]] | None = None,
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

    @on_sheet
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

    @on_sheet
    def add_label(
        net: str,
        x_mm: float,
        y_mm: float,
        orientation: Literal["right", "up", "left", "down"] = "right",
        snap_to_grid: bool = True,
    ) -> dict:
        """Add a net label at a point. orientation ∈ {right, up, left, down}.

        `net` is the label text.
        """
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        ed.add_label(tree, net, x_mm, y_mm, orientation)
        backup = _save_with_backup(tree, path)
        return {
            "net": net,
            "position_mm": [x_mm, y_mm],
            "orientation": orientation,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_sheet
    def add_power_symbol(
        net: str,
        x_mm: float,
        y_mm: float,
        snap_to_grid: bool = True,
        base_symbol: str = "",
    ) -> dict:
        """Place a power port (+5V, GND, ...) for `net`; the `#PWR` / `#FLG`
        reference is auto-assigned. Uses `power:{net}`, else builds the port on
        `base_symbol` (default `power:GND` for ground-like names, `power:VCC`
        otherwise) with Value = net. A new power net needs a `PWR_FLAG` or a
        driving pin, or ERC reports `power_pin_not_driven`.

        Details:

        Auto-assigns the reference the library part asks for: `#PWR####` for a
        rail or ground, `#FLG####` for `PWR_FLAG`. The two are numbered
        independently, as KiCAD numbers them.

        The library symbol is `power:{net}` when it exists. For a net with no
        stock symbol (`GND_USB`, `+18V`, `VBUS_+20V`, …) the port is built on
        `base_symbol` — `power:GND` by default for a ground-like name (GND,
        AGND, PGND, VSS, 0V, …), `power:VCC` otherwise — with its Value set to
        `net`. A power port's net is its Value, so this creates the net without
        a custom library symbol. Pass `base_symbol` (e.g. "power:GNDPWR",
        "+5V") to choose the graphic; it also overrides an existing
        `power:{net}`. A new power net needs its own `PWR_FLAG` (or a driving
        power-output pin) or ERC reports `power_pin_not_driven`.
        """
        from kicad_claude.adapters.pcb_geometry import is_ground_net

        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        idx = lib_tools._ensure_index()
        power_names = sorted(
            k.split(":", 1)[1] for k in idx["symbols"] if k.startswith("power:")
        )
        warnings: list[str] = []
        if base_symbol:
            candidate = base_symbol if ":" in base_symbol else f"power:{base_symbol}"
        elif f"power:{net}" in idx["symbols"]:
            candidate = f"power:{net}"
        else:
            candidate = "power:GND" if is_ground_net(net) else "power:VCC"
        if candidate not in idx["symbols"]:
            raise KeyError(
                f"power symbol {candidate!r} not in the library index; "
                f"available: {power_names[:20]}{'...' if len(power_names) > 20 else ''}"
            )
        custom = candidate != f"power:{net}"
        if custom and net != "PWR_FLAG":
            warnings.append(
                f"net {net!r} is drawn with {candidate!r} (Value overridden). "
                "Add a PWR_FLAG on it unless a power-output pin drives it."
            )

        tree, path = _load_active_schematic()
        lib_path, sym_name, meta = _resolve_lib_symbol(candidate)
        sym_def = ed.fetch_symbol_def(lib_path, sym_name)
        # The prefix comes from the library part: PWR_FLAG wants #FLG, the
        # rails want #PWR, and the two are numbered separately.
        ref = _next_virtual_reference(tree, ed.library_reference_prefix(sym_def))
        proj = state.get_active()
        node = ed.add_symbol(
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
        junctions = ed.junction_mid_wire_pins(tree, node)
        backup = _save_with_backup(tree, path)
        result = {
            "net": net,
            "reference": ref,
            "lib_id": candidate,
            "custom_net": custom,
            "position_mm": [x_mm, y_mm],
            "backup": str(backup) if backup else None,
        }
        if junctions:
            result["junctions_added"] = junctions
        if warnings:
            result["warnings"] = warnings
        return result

    @mcp.tool()

    @on_sheet
    def add_no_connect(
        reference: str = "",
        pin: str = "",
        x_mm: float | None = None,
        y_mm: float | None = None,
    ) -> dict:
        """Mark a pin as no-connect by placing a NC marker at its position.

        Give `reference` + `pin` (preferred: the marker lands exactly on the
        pin), or `x_mm` + `y_mm` for a raw sheet point, like the other markers.
        A point mode marker is snapped to the 1.27 mm grid.
        """
        by_pin = bool(reference and pin)
        by_xy = x_mm is not None and y_mm is not None
        if by_pin == by_xy:
            raise ValueError("give either reference + pin, or x_mm + y_mm")
        tree, path = _load_active_schematic()
        if by_pin:
            x, y = ed.get_pin_position(tree, reference, pin)
        else:
            x, y = _snap(x_mm, y_mm, True)
        ed.add_no_connect(tree, x, y)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "pin": pin,
            "position_mm": [x, y],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_sheet
    def list_pins(reference: str) -> list[dict]:
        """List pins of a placed symbol with their absolute positions (KiCAD coords)."""
        tree, _ = _load_active_schematic()
        return ed.list_pins_for_symbol(tree, reference)

    @mcp.tool()

    @on_sheet
    def get_pin_position(reference: str, pin: str) -> dict:
        """Return absolute (x, y) of one pin in KiCAD coordinates (Y down)."""
        tree, _ = _load_active_schematic()
        x, y = ed.get_pin_position(tree, reference, pin)
        return {"reference": reference, "pin": pin, "position_mm": [x, y]}

    @mcp.tool()

    @on_sheet
    def replace_symbol(
        reference: str,
        lib_id: str,
        pin_map: dict[str, str] | None = None,
        value: str | None = None,
    ) -> dict:
        """Swap a placed symbol's library part, keeping reference, position, uuid,
        instances and flags (the PCB association stays). `pin_map={"old":
        "new"}` moves wiring on mapped pins; by default shared pin numbers map.
        Returns `left_dangling` and `new_pins_unconnected`: check both, then
        `run_erc`.

        Details:

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

    # ----- Cached library definitions ------------------------------------- #

    @mcp.tool()

    @on_sheet
    def list_lib_symbols(scope: Literal["active", "all"] = "active") -> dict:
        """The library definitions a sheet has cached in its `(lib_symbols ...)`.

        Each sheet carries its own copy of every part it places, so it opens
        without the libraries. `stale: true` means the copy no longer matches
        the library on disk — see `refresh_lib_symbols`.
        """
        if scope not in ("active", "all"):
            raise ValueError(f"scope must be 'active' or 'all' (got {scope!r})")
        paths = (
            [state.get_active_sheet_path()]
            if scope == "active"
            else ed.hierarchy_sch_paths(state.get_active().sch_path)
        )

        out: list[dict] = []
        for path in paths:
            tree = sch_io.parse_file(path)
            for lib_id in ed.lib_symbol_ids(tree):
                entry = {"sheet": path.name, "lib_id": lib_id, "stale": None}
                try:
                    fresh = _fresh_lib_def(lib_id)
                except (KeyError, FileNotFoundError) as exc:
                    entry["error"] = str(exc)
                else:
                    diff = ed.diff_lib_symbol(tree, lib_id, fresh)
                    entry["stale"] = diff["changed"]
                    entry["pins_added"] = diff["pins_added"]
                    entry["pins_removed"] = diff["pins_removed"]
                    entry["pins_moved"] = diff["pins_moved"]
                out.append(entry)
        return {"scope": scope, "count": len(out), "symbols": out}

    @mcp.tool()

    @on_sheet
    def refresh_lib_symbols(
        lib_id: str = "",
        scope: Literal["active", "all"] = "active",
        update_fields: list[str] | None = None,
        dry_run: bool = True,
    ) -> dict:
        """Re-copy library definitions into a sheet's `lib_symbols` cache after a
        library change. `dry_run=True` (default) only reports; check
        `connections_at_risk` first, because moved pins leave wires dangling.
        `lib_id` empty = every part. `update_fields` copies named library
        properties onto the placements.

        Details:

        A sheet caches each part it places in `(lib_symbols ...)`, and nothing
        updates that copy when the library changes — so pin positions the tools
        report can drift from the library, and a part placed after the change
        still gets the old geometry.

        `dry_run=True` (the default) reports what would change and writes
        nothing. Read `connections_at_risk` before turning it off: a pin that
        moves or disappears leaves whatever was wired to it behind, exactly
        where it was, and `find_dangling` will then show it.

        `lib_id` empty refreshes every cached part; naming one refreshes that
        one. `update_fields` copies named properties from the library onto the
        placements — nothing is copied by default, because Reference, Value and
        Footprint belong to the placement, not the library.
        """
        if scope not in ("active", "all"):
            raise ValueError(f"scope must be 'active' or 'all' (got {scope!r})")
        paths = (
            [state.get_active_sheet_path()]
            if scope == "active"
            else ed.hierarchy_sch_paths(state.get_active().sch_path)
        )

        reports: list[dict] = []
        changed: list[tuple[Path, list]] = []
        for path in paths:
            tree = sch_io.parse_file(path)
            wanted = [lib_id] if lib_id else ed.lib_symbol_ids(tree)
            if lib_id and not ed.lib_symbols_has(tree, lib_id):
                continue

            touched = False
            for one in wanted:
                try:
                    fresh = _fresh_lib_def(one)
                except (KeyError, FileNotFoundError) as exc:
                    reports.append({"sheet": path.name, "lib_id": one, "error": str(exc)})
                    continue
                report = (
                    ed.diff_lib_symbol(tree, one, fresh)
                    if dry_run
                    else ed.refresh_lib_symbol(
                        tree, one, fresh, update_fields=update_fields
                    )
                )
                report["sheet"] = path.name
                reports.append(report)
                touched = touched or (report["changed"] or bool(update_fields))

            if not dry_run and touched:
                changed.append((path, tree))
        backups = [str(b) for b in safe_write.save_trees(changed) if b]

        if lib_id and not reports:
            raise KeyError(f"{lib_id!r} is not cached on any sheet in scope {scope!r}")

        changed = [r for r in reports if r.get("changed")]
        return {
            "dry_run": dry_run,
            "scope": scope,
            "checked": len(reports),
            "changed": len(changed),
            "reports": reports,
            "backups": backups,
        }

    # ----- Connectivity (derived, read-only) ------------------------------ #

    @mcp.tool()

    @on_sheet
    def list_sch_nets(scope: Literal["active", "all"] = "active", include_power_pins: bool = False) -> dict:
        """Every schematic net, rebuilt from wire, label and pin geometry the way
        KiCAD does. `scope`: "active" sheet or "all" (merges global labels,
        power nets and sheet ports). Power-symbol pins are hidden unless
        `include_power_pins=True`.

        Details:

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

    @on_sheet
    def trace_net(name: str, scope: Literal["active", "all"] = "active") -> dict:
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

    @on_sheet
    def get_pin_net(reference: str, pin: str, scope: Literal["active", "all"] = "active") -> dict:
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

    @on_sheet
    def find_dangling(scope: Literal["active", "all"] = "active") -> dict:
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

    @on_sheet
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
        allowed. Returns the note's `uuid`, which `set_text`, `remove_item` and
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

    @on_sheet
    def list_texts() -> list[dict]:
        """Every free text note on the active sheet, with uuid and position."""
        tree, _ = _load_active_schematic()
        items = ed.list_texts(tree)
        for item in items:
            item["text"] = normalize_name(item["text"])
        return items

    @mcp.tool()

    @on_sheet
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

    @on_sheet
    def rename_label(old_name: str, new_name: str, scope: Literal["active", "all"] = "all") -> dict:
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
        changed: list[tuple[Path, list]] = []
        for p in paths:
            tree = sch_io.parse_file(p)
            counts = ed.rename_label_in_tree(tree, old_name, new_name)
            if not any(counts.values()):
                continue
            changed.append((p, tree))
            for k, v in counts.items():
                total[k] += v
            per_sheet.append({"sheet": p.name, **counts})

        if not per_sheet:
            raise KeyError(f"no label named {old_name!r} found in scope {scope!r}")
        # One edit: a label renamed on some sheets only would split the net.
        backups = [str(b) for b in safe_write.save_trees(changed) if b]
        return {
            "old_name": old_name,
            "new_name": new_name,
            "scope": scope,
            "renamed": total,
            "sheets": per_sheet,
            "backups": backups,
        }

    @mcp.tool()

    @on_sheet
    def move_item(
        x_mm: float,
        y_mm: float,
        reference: str = "",
        uuid: str = "",
        rotation: float | None = None,
        snap_to_grid: bool = True,
    ) -> dict:
        """Move a symbol (by `reference`) or any item (by `uuid`) to an absolute point.

        `rotation` (0/90/180/270) applies to symbols only; their fields move and
        turn with them. A wire or bus moves whole: its first point lands on the
        target. Moving an item off a pin breaks that connection: re-check with
        `run_erc`.

        Details:

        Items by uuid: wire, bus, label, global_label, hierarchical_label,
        junction, no_connect, text, symbol and sheet. Text uuids come from
        `list_texts`.
        """
        if bool(reference) == bool(uuid):
            raise ValueError("pass exactly one of reference or uuid")
        x_mm, y_mm = _snap(x_mm, y_mm, snap_to_grid)
        tree, path = _load_active_schematic()
        if uuid:
            node = ed.find_by_uuid(tree, uuid)
            if node is not None and sch_io.is_call(node, "symbol"):
                reference = ed.get_symbol_property(node, "Reference") or ""
            elif rotation is not None:
                raise ValueError("rotation applies to symbols only")
        if reference:
            ed.move_symbol(tree, reference, x_mm, y_mm, rotation)
            result = {"kind": "symbol", "reference": reference,
                      "position_mm": [x_mm, y_mm], "rotation": rotation}
        else:
            result = ed.move_item(tree, uuid, x_mm, y_mm)
        backup = _save_with_backup(tree, path)
        return {
            **({"uuid": uuid} if uuid else {}),
            **result,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    # ----- Symbol properties and flags ------------------------------------ #

    @mcp.tool()

    @on_sheet
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

    @on_sheet
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

    @on_sheet
    def set_field_position(
        reference: str,
        field: str,
        x_mm: float | None = None,
        y_mm: float | None = None,
        angle: float | None = None,
        visible: bool | None = None,
    ) -> dict:
        """Move, turn, show or hide one field (Reference, Value, MPN, …) of a symbol.

        x_mm / y_mm are absolute sheet coordinates (KiCAD native, Y down) — the
        same frame as `move_item`; give both or neither. `angle` is the
        field's text angle as KiCAD stores it (0 or 90; KiCAD reads text at
        those two only, so 180 → 0 and 270 → 90). `visible` shows or hides it.
        Arguments left as None are unchanged.
        """
        if (x_mm is None) != (y_mm is None):
            raise ValueError("give both x_mm and y_mm, or neither")
        tree, path = _load_active_schematic()
        s_node = ed.find_symbol_by_reference(tree, reference)
        if s_node is None:
            raise KeyError(f"no symbol with reference {reference!r}")
        before = ed.get_field_placement(s_node, field)
        after = ed.set_field_placement(
            s_node, field, x_mm=x_mm, y_mm=y_mm, angle=angle, visible=visible)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "field": field,
            "previous": before,
            "now": after,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_sheet
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

    @mcp.tool()

    @on_sheet
    def set_symbol_flags(
        reference: str,
        dnp: bool | None = None,
        in_bom: bool | None = None,
        on_board: bool | None = None,
        exclude_from_sim: bool | None = None,
    ) -> dict:
        """Set a symbol's flags; only the ones passed change.

        `dnp`: Do Not Populate (stays on board and netlist, dropped from fab BOM
        and placement). `in_bom=False`: mounting holes, logos. `on_board=False`:
        `update_pcb_from_schematic` skips it. `exclude_from_sim`: SPICE skips it.
        """
        wanted = {"dnp": dnp, "in_bom": in_bom, "on_board": on_board,
                  "exclude_from_sim": exclude_from_sim}
        wanted = {k: v for k, v in wanted.items() if v is not None}
        if not wanted:
            raise ValueError("pass at least one flag")
        tree, path = _load_active_schematic()
        s_node = ed.find_symbol_by_reference(tree, reference)
        if s_node is None:
            raise KeyError(f"no symbol with reference {reference!r}")
        previous = {k: ed.get_symbol_flag(s_node, k) for k in wanted}
        for k, v in wanted.items():
            ed.set_symbol_flag(s_node, k, v)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "flags": wanted,
            "previous": previous,
            "sheet": state.get_active_sheet_filename() or "root",
            "backup": str(backup) if backup else None,
        }

    # ----- Buses (visual grouping of multiple nets) ----------------------- #

    @mcp.tool()

    @on_sheet
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

    @on_sheet
    def add_bus_entry(
        x_mm: float,
        y_mm: float,
        direction: Literal["right_down", "right_up", "left_down", "left_up"] = "right_down",
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

    @on_sheet
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
            # Child first: a root sheet entry must never point at a missing
            # file. If the root save fails, the new child goes again.
            child_path = proj.path / sheet_filename
            write_blank_schematic(child_path)  # refuses an existing file
            try:
                backup = _save_with_backup(tree, root_path)
            except Exception:
                child_path.unlink(missing_ok=True)
                raise
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

        state.set_active_sheet(sheet)  # the root's own filename maps to root
        return {
            "active_sheet": state.get_active_sheet_filename() or "root",
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

    @on_sheet
    def add_hierarchical_label(
        net: str,
        x_mm: float,
        y_mm: float,
        shape: Literal["input", "output", "bidirectional", "tri_state", "passive"] = "input",
        orientation: Literal["right", "up", "left", "down"] = "right",
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
            tree, net_name=net, x_mm=x_mm, y_mm=y_mm,
            shape=shape, orientation=orientation,
        )
        backup = _save_with_backup(tree, path)
        return {
            "net": net,
            "shape": shape,
            "orientation": orientation,
            "position_mm": [x_mm, y_mm],
            "sheet": state.get_active_sheet_filename(),
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_sheet
    def add_sheet_pin(
        sheet_name: str,
        pin_name: str,
        shape: Literal["input", "output", "bidirectional", "tri_state", "passive"],
        x_mm: float,
        y_mm: float,
        orientation: Literal["right", "up", "left", "down"] = "right",
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
