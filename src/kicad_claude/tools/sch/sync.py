"""Schematic↔PCB sync tools.

Tools:
    annotate_schematic        — auto-number ?-suffixed references in all sheets
    update_pcb_from_schematic — place missing footprints, copy the symbols'
                                fields onto them, and assign every pad's net
                                from the schematic netlist
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from kicad_claude import state
from kicad_claude.tools.common import on_board
from kicad_claude.adapters import annotation, kicad_cli, kicad_python, safe_write
from kicad_claude.adapters import pcb_editor, sch_io
from kicad_claude.tools.pcb import layout as pcb_tools
from kicad_claude.adapters import sch_sync

logger = logging.getLogger("kicad-claude.tools.sch.sync")


def _all_schematic_paths() -> list[Path]:
    """Root + every sub-sheet file of the active project, nested ones included."""
    from kicad_claude.adapters import sch_editor

    return sch_editor.hierarchy_sch_paths(state.get_active().sch_path)


def _netlist_timeout(component_count: int) -> float:
    """Timeout for the netlist stage, scaled with the design.

    Measured through a stdio session (2026-11-14): 55 footprints in 3 s,
    KiCAD's cm5_minima demo (~200 symbols, 611 pads) in 18 s. The two to four
    minutes once seen for 189 footprints was the pcbnew child inheriting the
    server's stdin (now `subprocess.DEVNULL` everywhere); the generous limit
    stays as a safety margin for very large boards.
    """
    return max(180.0, 120.0 + 2.5 * component_count)


def _timeout_result(error: Exception, timeout: float, component_count: int) -> dict:
    """What the update managed before the netlist stage ran out of time."""
    return {
        "timed_out": True,
        "error": str(error),
        "stage": "netlist",
        "completed": (
            "footprints placed and saved; pad nets NOT assigned "
            "(the netlist stage did not finish)"
        ),
        "timeout_seconds": timeout,
        "schematic_components": component_count,
        "hint": (
            "Re-run with a larger timeout_seconds — the placement step is "
            f"idempotent, so nothing is duplicated. Try {int(timeout * 2)}."
        ),
    }


def _field_summary(changed: list[dict]) -> dict:
    """{"parts": n, "fields": {name: parts}} from the per-part change list."""
    per_field: dict[str, int] = {}
    for entry in changed:
        for name in entry["fields"]:
            per_field[name] = per_field.get(name, 0) + 1
    return {"parts": len(changed), "fields": dict(sorted(per_field.items()))}


def register(mcp) -> None:
    """Register the sync tools."""

    @mcp.tool()
    def annotate_schematic(sort_by_position: bool = True) -> dict:
        """Auto-number every `?`-suffixed reference (R?, U?, C?, …) in the project.

        Walks the root and every hierarchical child sheet. Numbers continue
        consistently across sheets — R1 in root and R2 in a child won't
        collide. Existing numbered refs are preserved.

        Returns per-sheet stats and the total assignments made.
        """
        paths = _all_schematic_paths()
        result = annotation.annotate_sheets(paths, sort_by_position=sort_by_position,
                                            project=state.get_active().name)
        return result

    @mcp.tool()

    @on_board
    def update_pcb_from_schematic(
        place_missing: bool = True,
        sync_fields: bool = True,
        remove_extra: bool = False,
        timeout_seconds: float = 0.0,
        scope: Literal["full", "fields"] = "full",
        verbose: bool = False,
    ) -> dict:
        """Bring the active PCB in line with the schematic. `scope="full"`
        (default): place missing footprints below the outline
        (`place_missing`), copy fields (`sync_fields`), delete orphans only
        with `remove_extra`, assign nets via KiCAD's pcbnew (~20 s at 200
        parts; `timeout_seconds=0` scales with part count).
        `scope="fields"`: field copy only, seconds.

        Details:

        Result: `placed_count` and `fields_synced` are compact by default
        (parts changed and count per field, BOM / DNP / simulation flags as
        `attr:<flag>`). `verbose=True` adds `placed_footprints` (with
        positions) and the field names per part.

        `scope="fields"` does step 2 only: copy Value / MPN / LCSC / … onto the
        footprints in pure Python and save. No footprint placement, no deletion,
        no netlist export and no pcbnew run, so it takes about a second. Use it after a field-only schematic edit (value swap,
        sourcing update). `scope="full"` (default) runs every step below.

        Process:
        1. Place a footprint for every schematic symbol that has none on the
           board, taken from the symbol's `Footprint` property and dropped in
           a grid just below the board outline (`place_missing`). Arrange them
           afterwards with `move_footprint` or `place_footprints_grid`. Set the
           outline first (`set_board_outline`): without one they land on a
           10 mm grid from (50, 50) mm, where a later outline may cover them.
        2. Copy `Value` and the symbol's other fields (`MPN`, `Manufacturer`,
           …) onto the matching footprint (`sync_fields`). This is what
           `run_drc(schematic_parity=True)` compares, and what an assembly BOM
           taken from the board needs.
        3. Delete footprints whose symbol is gone (`remove_extra`, off by
           default). A board also carries footprints no symbol knows about —
           mounting holes, fiducials, logos — and they would go too, so they
           are only ever reported as `orphan_footprints` unless this is on.
        4. Export the schematic netlist (kicadsexpr, which keeps net names in
           KiCAD's escaped form such as `Net-(U5-NR{slash}SS)`) and use KiCAD's
           Python (`pcbnew`) to add the missing nets and assign every pad's
           net. The board is re-saved only if a net was added or a pad changed.
           The netlist stays at `fab/<project>-netlist.net`, where the review
           tools read it.

        A symbol with no `Footprint` property is reported in `no_footprint`; a
        footprint lib_id the library index does not know is reported in
        `unresolved` — neither stops the rest of the update.

        After this, Freerouting can actually route the board (pads have
        net assignments).

        `timeout_seconds` defaults to 0, which means "scale with the board":
        120 s plus 2.5 s per schematic symbol (a 189-part board gets ~590 s).
        The netlist stage runs KiCAD's own Python on the whole board (18 s for
        a 200-symbol design). When it does time out, the footprints are
        already placed and saved; the response says so in `stage` and
        `timed_out` instead of only raising.
        """
        if scope not in ("full", "fields"):
            raise ValueError(f"scope must be 'full' or 'fields', got {scope!r}")
        proj = state.get_active()

        # 0) Component side: place, sync fields, optionally delete.
        sch_paths = _all_schematic_paths()
        components = sch_sync.collect_schematic_components(sch_paths)
        pcb_path = state.get_active_board_path()
        tree = sch_io.parse_file(pcb_path)

        if scope == "fields":
            fields_changed = sch_sync.sync_footprint_fields(tree, components)
            backup = safe_write.save_tree(tree=tree, path=pcb_path) if fields_changed else None
            on_board = set(pcb_editor.all_footprint_references(tree))
            missing = sorted(ref for ref in components if ref not in on_board)
            out = {
                "scope": "fields",
                "pcb_path": str(pcb_path),
                "fields_synced": fields_changed if verbose else _field_summary(fields_changed),
                "missing_in_pcb": missing,
                "backup": str(backup) if backup else None,
            }
            if missing:
                out["hint"] = ("Not on the board (scope='fields' does not place footprints): "
                               + ", ".join(missing) + ". Run scope='full' to add them.")
            return out

        placement: dict = {"placed": [], "no_footprint": [], "unresolved": []}
        if place_missing:
            placement = sch_sync.place_missing_footprints(
                tree, components, pcb_tools._resolve_footprint
            )
        fields_changed: list = []
        if sync_fields:
            fields_changed = sch_sync.sync_footprint_fields(tree, components)
        if remove_extra:
            orphans = sch_sync.remove_orphan_footprints(tree, components)
        else:
            orphans = sch_sync.list_orphan_footprints(tree, components)

        backup = None
        if placement["placed"] or fields_changed or (remove_extra and orphans):
            backup = safe_write.save_tree(tree=tree, path=pcb_path)

        # A fixed 90 s cannot cover both a 10-part board and a 200-part one.
        timeout = float(timeout_seconds) if timeout_seconds else _netlist_timeout(
            len(components)
        )

        # 1) Export the netlist as kicadsexpr. Not kicadxml: the XML export
        #    decodes `{slash}` to `/`, and pcbnew then creates a second net whose
        #    bare `/` reads as a sheet-path separator.
        netlist_path = proj.path / "fab" / f"{proj.name}-netlist.net"
        netlist_path.parent.mkdir(parents=True, exist_ok=True)
        kicad_cli.export_netlist(
            proj.sch_path, netlist_path, fmt="kicadsexpr", timeout=timeout
        )

        # 2) Apply via pcbnew Python
        try:
            pcb = state.get_active_board_path()
            result, net_backup = safe_write.guard_external(
                pcb, lambda: kicad_python.apply_netlist(pcb, netlist_path, timeout=timeout))
            result["timed_out"] = False
            backup = backup or net_backup
        except kicad_python.KicadPythonError as e:
            if "timed out" not in str(e):
                raise
            result = _timeout_result(e, timeout, len(components))

        # 3) Surface the artifacts and whatever could not be done automatically
        result["scope"] = "full"
        result["netlist_path"] = str(netlist_path)
        result["pcb_path"] = str(pcb_path)
        result["placed_count"] = len(placement["placed"])
        if verbose:
            result["placed_footprints"] = placement["placed"]
        result["no_footprint"] = placement["no_footprint"]
        result["unresolved"] = placement["unresolved"]
        result["fields_synced"] = fields_changed if verbose else _field_summary(fields_changed)
        result["orphan_footprints"] = orphans
        result["orphans_removed"] = bool(remove_extra and orphans)
        if backup:
            result["backup"] = str(backup)

        hints: list[str] = []
        if result.get("timed_out"):
            hints.append(result.pop("hint"))
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
