"""Component-level part of "Update PCB from Schematic".

`sync.update_pcb_from_schematic` used to do the netlist alone: it assigned
nets to pads that were already on the board and listed everything else as
`missing_in_pcb`, leaving the caller to place each footprint by hand and to
retype the lib_id the symbol already carried.

This module does the component side of the same job, the way KiCAD's own
dialog does it:

  - place a footprint for every schematic symbol that has none on the board,
    using the symbol's own `Footprint` property;
  - copy `Value` and the symbol's extra fields (`MPN`, `Manufacturer`, …) onto
    the footprint, which is what DRC's schematic-parity check compares;
  - optionally delete footprints whose symbol is gone.

Deleting is off by default. A board legitimately carries footprints that no
symbol knows about — mounting holes, fiducials, logos — and losing those to a
sync would be worse than leaving a stale part behind.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kicad_claude.adapters import pcb_editor as ped
from kicad_claude.adapters import sch_editor as sed
from kicad_claude.adapters import sch_io

logger = logging.getLogger("kicad-claude.tools.sync_components")

# Fields worth carrying to the footprint. `Reference` and `Value` are handled
# separately; `Footprint` is the identity of the footprint itself.
_SKIP_FIELDS = {"Reference", "Value", "Footprint", "ki_description", "ki_keywords"}


def collect_schematic_components(sch_paths: list[Path]) -> dict[str, dict]:
    """Every real symbol of the hierarchy, by reference.

    Power symbols (`#PWR`), net ties and other `#`-prefixed references are not
    parts and never reach the board, so they are left out.
    """
    out: dict[str, dict] = {}
    for path in sch_paths:
        tree = sch_io.parse_file(path)
        for symbol in sed.iter_instance_symbols(tree):
            ref = sed.get_symbol_property(symbol, "Reference") or ""
            if not ref or ref.startswith("#") or ref.endswith("?"):
                continue
            props: dict[str, str] = {}
            for prop in sch_io.find_children(symbol, "property"):
                i = sch_io.property_name_index(prop)
                if len(prop) <= i + 1:
                    continue
                name = str(prop[i])
                if name in _SKIP_FIELDS:
                    continue
                props[name] = str(prop[i + 1])
            out[ref] = {
                "reference": ref,
                "value": sed.get_symbol_property(symbol, "Value") or "",
                "footprint": sed.get_symbol_property(symbol, "Footprint") or "",
                "properties": props,
                "sheet": path.name,
            }
    return out


def _placement_origin(tree: list) -> tuple[float, float]:
    """Where to drop newly placed footprints: just below the board outline.

    KiCAD drops them wherever the cursor is; there is no cursor here. Below the
    outline keeps them off the board and out of each other's way, ready for
    `move_footprint` or `place_footprints_grid`.
    """
    poly = ped.get_board_outline_polygon_kicad(tree)
    if not poly:
        return 50.0, 50.0
    min_x = min(p[0] for p in poly)
    max_y = max(p[1] for p in poly)
    return float(min_x), float(max_y) + 10.0


def place_missing_footprints(
    tree: list,
    components: dict[str, dict],
    resolve_footprint,
    *,
    spacing_mm: float = 10.0,
    per_row: int = 10,
) -> dict[str, Any]:
    """Add a footprint for every component that has none on the board.

    `resolve_footprint(lib_id) -> Path` maps a footprint lib_id to its
    `.kicad_mod`; it is passed in so this module does not depend on the
    library index.

    Returns what was placed, what could not be (no `Footprint` property, or a
    lib_id the index does not know), and nothing is raised for either — a
    board with one unresolvable part should still get the other parts.
    """
    on_board = set(ped.all_footprint_references(tree))
    ox, oy = _placement_origin(tree)

    placed: list[dict] = []
    no_footprint: list[str] = []
    unresolved: list[dict] = []

    index = 0
    for ref in sorted(components):
        if ref in on_board:
            continue
        lib_id = components[ref]["footprint"]
        if not lib_id:
            no_footprint.append(ref)
            continue
        try:
            mod_path = resolve_footprint(lib_id)
            fp_def = ped.fetch_footprint_def(mod_path)
        except Exception as e:  # noqa: BLE001 — reported per part, not fatal
            unresolved.append({"reference": ref, "footprint": lib_id, "error": str(e)})
            continue

        x = ox + (index % per_row) * spacing_mm
        y = oy + (index // per_row) * spacing_mm
        ped.add_footprint(
            tree,
            qualified_lib_id=lib_id,
            reference=ref,
            value=components[ref]["value"],
            x_mm=x,
            y_mm=y,
            fp_def_node=fp_def,
        )
        placed.append({"reference": ref, "footprint": lib_id, "position_mm": [x, y]})
        index += 1

    return {"placed": placed, "no_footprint": no_footprint, "unresolved": unresolved}


def sync_footprint_fields(tree: list, components: dict[str, dict]) -> list[dict]:
    """Copy `Value` and the symbol's extra fields onto the matching footprint.

    Without this, `kicad-cli pcb drc --schematic-parity` reports one
    `footprint_symbol_field_mismatch` per field per part, and an assembly BOM
    taken from the board has no part numbers on it.
    """
    changed: list[dict] = []
    for fp in ped.iter_footprints(tree):
        ref = ped.get_footprint_reference(fp)
        comp = components.get(ref or "")
        if comp is None:
            continue
        fields: list[str] = []
        if ped.set_footprint_property(fp, "Value", comp["value"]):
            fields.append("Value")
        for name, value in sorted(comp["properties"].items()):
            if ped.set_footprint_property(fp, name, value):
                fields.append(name)
        if fields:
            changed.append({"reference": ref, "fields": fields})
    return changed


def remove_orphan_footprints(tree: list, components: dict[str, dict]) -> list[str]:
    """Delete every footprint whose reference has no symbol. Returns the refs."""
    orphans = [
        ref for ref in ped.all_footprint_references(tree)
        if ref and ref not in components
    ]
    for ref in orphans:
        ped.remove_footprint(tree, ref)
    return orphans


def list_orphan_footprints(tree: list, components: dict[str, dict]) -> list[str]:
    """Footprints on the board that no schematic symbol accounts for."""
    return [
        ref for ref in ped.all_footprint_references(tree)
        if ref and ref not in components
    ]
