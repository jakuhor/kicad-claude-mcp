"""Create custom symbols and footprints from MCP.

Tools:
    create_symbol      — emit a new (symbol ...) into <project>/lib/<lib>.kicad_sym
    create_footprint   — emit a new .kicad_mod into <project>/lib/<lib>.pretty/

Both register the lib in sym-lib-table / fp-lib-table so KiCAD picks it up
on next open. Run `index_libraries(force=True)` afterwards to make the new
lib_ids searchable via `search_symbol` / `search_footprint`.
"""

from __future__ import annotations

import logging
from typing import Literal

from kicad_claude import state
from kicad_claude.adapters import library_create as lc

logger = logging.getLogger("kicad-claude.tools.library.create")


def register(mcp) -> None:
    @mcp.tool()
    def create_symbol(
        lib_name: str,
        symbol_name: str,
        pins: list,
        body_width_mm: float = 5.08,
        body_height_mm: float = 5.08,
        reference_prefix: str = "U",
        value: str = "",
        footprint: str = "",
        datasheet: str = "~",
        description: str = "",
        keywords: str = "",
        fields: dict | None = None,
    ) -> dict:
        """Create a symbol in `<project>/lib/<lib_name>.kicad_sym`, placeable with
        `add_symbol` at once. `pins`: list of dicts with `number`, `name`,
        `x_mm`, `y_mm` (electrical end, symbol-local), `length_mm` (2.54),
        `angle_deg` (KiCAD's pin angle, from the electrical end toward the
        body: 0 left-side pin, 180 right, 270 top, 90 bottom), `type` (input/output/passive/power_in/...), `shape`. `fields`
        adds library properties (MPN, Manufacturer) that every placement
        inherits.

        Details:

        `fields` adds any other property to the library symbol itself —
        `{"MPN": "RC0603FR-0710KL", "Manufacturer": "Yageo"}`. A house part
        carries its order codes in the library, so every placement inherits
        them; `set_symbol_property` only changes one placed instance.

        The active project's libraries are indexed on every lookup, so the new
        symbol can be placed with `add_symbol` right away. Call
        `index_libraries(force=True)` only to refresh the global libraries.
        """
        proj = state.get_active()
        if not pins:
            raise ValueError("at least one pin required")
        return lc.create_symbol(
            proj.path,
            lib_name=lib_name, symbol_name=symbol_name,
            pins=list(pins),
            body_width_mm=body_width_mm, body_height_mm=body_height_mm,
            reference_prefix=reference_prefix,
            value=value, footprint=footprint, datasheet=datasheet,
            description=description, keywords=keywords,
            fields=dict(fields) if fields else None,
        )

    @mcp.tool()
    def create_footprint(
        lib_name: str,
        footprint_name: str,
        pads: list,
        description: str = "",
        tags: str = "",
        add_courtyard: bool = True,
        add_silk_outline: bool = True,
        graphics: list[dict] | None = None,
        courtyard_from: Literal["pads", "body"] = "pads",
    ) -> dict:
        """Create a footprint in `<project>/lib/<lib_name>.pretty/`. `pads`:
        dicts with `number`, `type`, `shape`, `x_mm`, `y_mm`, `size_x_mm`,
        `size_y_mm` (help). `graphics`: body outline / marks.

        Details:

        Pad keys: `type` smd | thru_hole | np_thru_hole, `shape` rect |
        circle | oval | roundrect, `x_mm` / `y_mm` the footprint-local
        centre, optional `drill_mm` (default size - 0.4), `layers`,
        `rotation_deg`.

        `graphics`: list of {kind: rect | line (x1_mm, y1_mm, x2_mm, y2_mm) |
        circle (x_mm, y_mm, r_mm) | text (text, x_mm, y_mm, size_mm,
        rotation_deg), layer (default F.Fab; silk, fab, courtyard and user
        layers, never copper), width_mm, fill}. `courtyard_from="body"` sizes
        the courtyard over the pads plus the F.Fab / B.Fab graphics (a body
        overhanging its pads); a courtyard drawn in `graphics` replaces the
        generated one.

        Auto-generates F.CrtYd courtyard + F.SilkS outline by default
        (toggle via flags). After running, call `index_libraries(force=True)`
        to make this footprint searchable via `search_footprint`.
        """
        proj = state.get_active()
        if not pads:
            raise ValueError("at least one pad required")
        return lc.create_footprint(
            proj.path,
            lib_name=lib_name, footprint_name=footprint_name,
            pads=list(pads),
            description=description, tags=tags,
            add_courtyard=add_courtyard,
            add_silk_outline=add_silk_outline,
            graphics=list(graphics or []), courtyard_from=courtyard_from,
        )
