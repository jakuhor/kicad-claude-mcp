"""Phase 10 — schematic↔PCB sync tools.

Tools:
    annotate_schematic        — auto-number ?-suffixed references in all sheets
    update_pcb_from_schematic — place missing footprints, copy the symbols'
                                fields onto them, and assign every pad's net
                                from the schematic netlist
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import annotation, kicad_cli, kicad_python, safe_write
from kicad_claude.adapters import sch_io
from kicad_claude.tools import pcb as pcb_tools
from kicad_claude.tools import sync_components

logger = logging.getLogger("kicad-claude.tools.sync")


def _all_schematic_paths() -> list[Path]:
    """Return root + every registered child .kicad_sch in the active project."""
    proj = state.get_active()
    paths = [proj.sch_path]
    root = sch_io.parse_file(proj.sch_path)
    for entry in root[1:]:
        if not sch_io.is_call(entry, "sheet"):
            continue
        sheetfile = sch_io.get_property(entry, "Sheetfile")
        if sheetfile:
            child = proj.path / sheetfile
            if child.is_file():
                paths.append(child)
    return paths


def register(mcp) -> None:
    """Register Phase 10 sync tools."""

    @mcp.tool()
    def annotate_schematic(sort_by_position: bool = True) -> dict:
        """Auto-number every `?`-suffixed reference (R?, U?, C?, …) in the project.

        Walks the root and every hierarchical child sheet. Numbers continue
        consistently across sheets — R1 in root and R2 in a child won't
        collide. Existing numbered refs are preserved.

        Returns per-sheet stats and the total assignments made.
        """
        paths = _all_schematic_paths()
        result = annotation.annotate_sheets(paths, sort_by_position=sort_by_position)
        return result

    @mcp.tool()
    def update_pcb_from_schematic(
        place_missing: bool = True,
        sync_fields: bool = True,
        remove_extra: bool = False,
        timeout_seconds: float = 90.0,
    ) -> dict:
        """Bring the active PCB in line with the schematic — KiCAD's own update.

        Process:
        1. Place a footprint for every schematic symbol that has none on the
           board, taken from the symbol's `Footprint` property and dropped in
           a grid just below the board outline (`place_missing`). Arrange them
           afterwards with `move_footprint` or `place_footprints_grid`.
        2. Copy `Value` and the symbol's other fields (`MPN`, `Manufacturer`,
           …) onto the matching footprint (`sync_fields`). This is what
           `run_drc(schematic_parity=True)` compares, and what an assembly BOM
           taken from the board needs.
        3. Delete footprints whose symbol is gone (`remove_extra`, off by
           default). A board also carries footprints no symbol knows about —
           mounting holes, fiducials, logos — and they would go too, so they
           are only ever reported as `orphan_footprints` unless this is on.
        4. Export the schematic netlist as kicadxml and use KiCAD's Python
           (`pcbnew`) to add the missing nets and assign every pad's net.

        A symbol with no `Footprint` property is reported in `no_footprint`; a
        footprint lib_id the library index does not know is reported in
        `unresolved` — neither stops the rest of the update.

        After this, Freerouting can actually route the board (pads have
        net assignments).
        """
        proj = state.get_active()

        # 0) Component side: place, sync fields, optionally delete.
        sch_paths = _all_schematic_paths()
        components = sync_components.collect_schematic_components(sch_paths)
        pcb_path = state.get_active_board_path()
        tree = sch_io.parse_file(pcb_path)

        placement: dict = {"placed": [], "no_footprint": [], "unresolved": []}
        if place_missing:
            placement = sync_components.place_missing_footprints(
                tree, components, pcb_tools._resolve_footprint
            )
        fields_changed: list = []
        if sync_fields:
            fields_changed = sync_components.sync_footprint_fields(tree, components)
        if remove_extra:
            orphans = sync_components.remove_orphan_footprints(tree, components)
        else:
            orphans = sync_components.list_orphan_footprints(tree, components)

        backup = None
        if placement["placed"] or fields_changed or (remove_extra and orphans):
            backup = safe_write.save_tree(tree=tree, path=pcb_path)

        # 1) Export the netlist as kicadxml (different format than DSN)
        netlist_xml = proj.path / "fab" / f"{proj.name}-netlist.xml"
        netlist_xml.parent.mkdir(parents=True, exist_ok=True)
        kicad_cli.export_netlist(
            proj.sch_path, netlist_xml, fmt="kicadxml", timeout=timeout_seconds
        )

        # 2) Apply via pcbnew Python
        result = kicad_python.apply_netlist(
            state.get_active_board_path(), netlist_xml, timeout=timeout_seconds
        )

        # 3) Surface the artifacts and whatever could not be done automatically
        result["netlist_path"] = str(netlist_xml)
        result["pcb_path"] = str(pcb_path)
        result["placed_footprints"] = placement["placed"]
        result["no_footprint"] = placement["no_footprint"]
        result["unresolved"] = placement["unresolved"]
        result["fields_synced"] = fields_changed
        result["orphan_footprints"] = orphans
        result["orphans_removed"] = bool(remove_extra and orphans)
        if backup:
            result["backup"] = str(backup)

        hints: list[str] = []
        if result.get("missing_in_pcb"):
            hints.append(
                "Some schematic references are still not on the PCB: "
                + ", ".join(result["missing_in_pcb"])
            )
        if placement["no_footprint"]:
            hints.append(
                "No Footprint property on: "
                + ", ".join(placement["no_footprint"])
                + ". Set it with set_symbol_property and re-run."
            )
        if placement["unresolved"]:
            hints.append(
                "Footprint library not found for: "
                + ", ".join(u["reference"] for u in placement["unresolved"])
            )
        if orphans and not remove_extra:
            hints.append(
                "On the board but not in the schematic: "
                + ", ".join(orphans)
                + ". Pass remove_extra=True to delete them."
            )
        if hints:
            result["hint"] = " ".join(hints)
        return result
