"""PCB editing tools.

Tools (all operate on the active project's `.kicad_pcb`):
    set_board_outline
    list_footprints
    add_footprint                 (extends the spec — needed for headless flows;
                                   in the spec'd workflow, footprints arrive via
                                   "Update PCB from Schematic" in KiCAD GUI)
    move_footprint
    place_footprints_grid
    add_track
    add_via
    list_unrouted, get_pad_position, list_pads, net_route_status

Coordinates: millimetres, KiCAD-native — Y axis points DOWN, the numbers the
KiCAD PCB editor shows (same convention as the schematic tools).
Rotations: 0 / 90 / 180 / 270.
"""

from __future__ import annotations

import logging
from typing import Literal
from pathlib import Path


from kicad_claude import state
from kicad_claude.arg_types import ZonePadConnection
from kicad_claude.tools.common import on_board
from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import project_settings as ps
from kicad_claude.adapters import pcb_netlist
from kicad_claude.adapters import safe_write
from kicad_claude.adapters import sch_io
from kicad_claude.templates.blank import write_blank_pcb
from kicad_claude.tools.library import index as lib_tools
from kicad_claude.utils.kicad_strings import normalize_name

logger = logging.getLogger("kicad-claude.tools.pcb.layout")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_active_pcb() -> tuple[list, Path]:
    pcb_path = state.get_active_board_path()
    tree = sch_io.parse_file(pcb_path)
    return tree, pcb_path


def _save_with_backup(tree: list, pcb_path: Path) -> Path | None:
    """Write the board through the guarded path: lock check, backup, verify."""
    return safe_write.save_tree(tree=tree, path=pcb_path)


def _resolve_footprint(lib_id: str) -> Path:
    """Find the source `.kicad_mod` for a given footprint lib_id via the index."""
    idx = lib_tools._ensure_index()
    fp_meta = idx["footprints"].get(lib_id)
    if fp_meta is None:
        raise KeyError(
            f"unknown footprint lib_id {lib_id!r} — call index_libraries first or check spelling"
        )
    if ":" not in lib_id:
        raise ValueError(f"lib_id must be 'Lib:Name' (got {lib_id!r})")
    lib_name, fp_name = lib_id.split(":", 1)

    # Search project libs first, then official.
    for d in idx.get("footprint_dirs", []):
        path = Path(d) / f"{lib_name}.pretty" / f"{fp_name}.kicad_mod"
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"source .kicad_mod for {lib_id!r} not found; index may be stale"
    )


# --------------------------------------------------------------------------- #
# Tool registration
# --------------------------------------------------------------------------- #


def register(mcp) -> None:
    """Register the tools."""

    @mcp.tool()

    @on_board
    def set_layer_count(n: int) -> dict:
        """Reconfigure the active PCB to have `n` copper layers.

        Details:

        `n` must be even, 2-32. The `(layers ...)` block and the
        `(setup (stackup ...) ...)` sub-block are regenerated to match.

        Call this BEFORE adding tracks/vias on inner layers — existing items
        on layers that no longer exist will trigger DRC errors.
        """
        tree, path = _load_active_pcb()
        result = ed.set_copper_layer_count(tree, n)
        backup = _save_with_backup(tree, path)
        result["backup"] = str(backup) if backup else None
        return result

    @mcp.tool()

    @on_board
    def set_board_outline(
        width_mm: float,
        height_mm: float,
        shape: Literal["rect"] = "rect",
        origin_x_mm: float = 10.0,
        origin_y_mm: float = 10.0,
    ) -> dict:
        """Replace the Edge.Cuts outline with a width × height rectangle.

        Coordinates are KiCAD-native (Y down): the board's TOP-left corner
        sits at (origin_x_mm, origin_y_mm) and it extends right and down.
        Shape currently supports 'rect' only; rounded corners come later.
        """
        tree, path = _load_active_pcb()
        result = ed.set_board_outline(
            tree, width_mm, height_mm, shape=shape,
            origin_x_mcp=origin_x_mm, origin_y_mcp=origin_y_mm,
        )
        backup = _save_with_backup(tree, path)
        result["backup"] = str(backup) if backup else None
        return result

    @mcp.tool()

    @on_board
    def list_footprints() -> list[dict]:
        """Placed footprints: reference, value, lib_id, layer, position_mm,
        rotation, courtyard_mm [x1, y1, x2, y2] (board coords, rotation
        applied; null without a courtyard) for placement and spacing."""
        tree, _ = _load_active_pcb()
        return ed.list_footprints_summary(tree)

    @mcp.tool()

    @on_board
    def add_footprint(
        lib_id: str,
        reference: str,
        value: str,
        x_mm: float,
        y_mm: float,
        rotation: float = 0,
        layer: str = "F.Cu",
    ) -> dict:
        """Place a footprint from the indexed KiCAD libraries.

        Outside the spec'd workflow (Update PCB from Schematic in KiCAD GUI),
        this is the way to populate a fresh PCB with footprints from this MCP.
        """
        tree, path = _load_active_pcb()
        mod_path = _resolve_footprint(lib_id)
        fp_def = ed.fetch_footprint_def(mod_path)
        ed.add_footprint(
            tree,
            qualified_lib_id=lib_id,
            reference=reference,
            value=value,
            x_mm=x_mm,
            y_mm=y_mm,
            rotation=rotation,
            layer=layer,
            fp_def_node=fp_def,
        )
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "lib_id": lib_id,
            "value": value,
            "position_mm": [x_mm, y_mm],
            "rotation": rotation,
            "layer": layer,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_board
    def move_footprint(
        reference: str,
        x_mm: float,
        y_mm: float,
        rotation: float | None = None,
        layer: str | None = None,
    ) -> dict:
        """Reposition (and optionally rotate / flip layer of) a placed footprint."""
        tree, path = _load_active_pcb()
        ed.move_footprint(tree, reference, x_mm, y_mm, rotation=rotation, layer=layer)
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "position_mm": [x_mm, y_mm],
            "rotation": rotation,
            "layer": layer,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_board
    def remove_footprint(reference: str) -> dict:
        """Delete a placed footprint from the active PCB.

        Details:

        Tracks and vias that were routed to its pads stay where they are —
        KiCAD does the same — so re-run `run_drc` afterwards to see what the
        removal left dangling. To clear out every footprint whose symbol is
        gone, use `update_pcb_from_schematic(remove_extra=True)`.
        """
        tree, path = _load_active_pcb()
        if not ed.remove_footprint(tree, reference):
            raise KeyError(f"no footprint with reference {reference!r} on this board")
        backup = _save_with_backup(tree, path)
        return {
            "removed": reference,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_board
    def remove_board_items(
        uuid: str = "",
        net: str = "",
        kinds: list[Literal["track", "arc", "via", "zone"]] | None = None,
        layer: str = "",
        dry_run: bool = False,
    ) -> dict:
        """Remove routing from the active PCB: one item by `uuid`, or a net's items.

        By `net`: every item of `kinds` (default track, arc, via; add "zone"
        explicitly) on that net — a rip-up. `layer` keeps it to one layer and
        then spares vias. `dry_run=True` lists the matches only. DRC reports
        carry item uuids.
        """
        tree, path = _load_active_pcb()
        removed = ed.remove_board_items(
            tree, item_uuid=uuid, net=net, kinds=kinds or ("track", "arc", "via"),
            layer=layer, dry_run=dry_run,
        )
        counts: dict[str, int] = {}
        for item in removed:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        backup = None
        if removed and not dry_run:
            backup = _save_with_backup(tree, path)
        return {
            "dry_run": dry_run,
            "removed_count": len(removed),
            "by_kind": counts,
            "items": removed,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_board
    def place_footprints_grid(
        spacing_mm: float = 10.0,
        columns: int = 5,
        origin_x_mm: float = 15.0,
        origin_y_mm: float = 15.0,
        only_unplaced: bool = True,
    ) -> dict:
        """Lay footprints out on a regular grid, sorted by reference.

        Details:

        By default only the footprints sitting at (0, 0) are moved — where
        KiCAD leaves a part it has no position for. Pass
        `only_unplaced=False` to re-arrange every footprint on the board,
        which is what `update_pcb_from_schematic` needs: it drops new parts in
        a row below the board outline rather than at the origin.
        """
        tree, path = _load_active_pcb()
        result = ed.place_footprints_grid(
            tree,
            spacing_mm=spacing_mm,
            columns=columns,
            origin_mcp=(origin_x_mm, origin_y_mm),
            only_unplaced=only_unplaced,
        )
        backup = _save_with_backup(tree, path)
        result["backup"] = str(backup) if backup else None
        return result

    @mcp.tool()

    @on_board
    def add_track(
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
        width_mm: float = 0.25,
        layer: str = "F.Cu",
        net: str | int = "",
    ) -> dict:
        """Add a track segment between two points on a copper layer, on `net`
        (net name as `list_nets` shows it; "" = no net).

        Details:

        `net` may also be a legacy integer index, accepted only on a board
        that still has a net table (pre-KiCAD 10). A name the board does not
        know is refused: a track on a net nothing else carries is invisible
        to KiCAD. Many segments at once: `edit_board`.
        """
        tree, path = _load_active_pcb()
        ed.add_track(tree, x1_mm, y1_mm, x2_mm, y2_mm,
                     width_mm=width_mm, layer=layer, net=net)
        backup = _save_with_backup(tree, path)
        return {
            "from_mm": [x1_mm, y1_mm],
            "to_mm": [x2_mm, y2_mm],
            "width_mm": width_mm,
            "layer": layer,
            "net": net,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_board
    def add_zone(
        net: str,
        layer: str,
        polygon_mm: list,
        fill_clearance_mm: float = 0.5,
        min_thickness_mm: float = 0.25,
        name: str = "",
        pad_connection: ZonePadConnection = "thermal",
        thermal_gap_mm: float = 0.5,
        thermal_bridge_mm: float = 0.5,
        priority: int = 0,
    ) -> dict:
        """Add a copper zone (pour) to `net` on `layer` ("F.Cu", "*.Cu", …)
        over `polygon_mm` ([[x, y], …], KiCAD coords, ≥ 3 points).
        `pad_connection`: thermal / solid (power paths) / thru_hole_only /
        none. Overlapping pours: the higher `priority` wins. Then
        `run_drc(refill_zones=True)`.

        Details:

        `thermal_gap_mm` / `thermal_bridge_mm` set the relief of `thermal`
        (and of THT pads under `thru_hole_only`). A large SMD power pad on a
        thermal pour often gets one spoke only: DRC `starved_thermal`, and
        the pad may stay unconnected — use `solid` there. A zone of higher
        `priority` is filled first and cut out of lower ones, so a power
        pour inside a GND pour on the same layer needs the higher priority.
        """
        polygon = [(float(p[0]), float(p[1])) for p in polygon_mm]
        tree, path = _load_active_pcb()
        ed.add_zone(
            tree,
            net_name=net, layer=layer, polygon_mcp=polygon,
            fill_clearance_mm=fill_clearance_mm,
            min_thickness_mm=min_thickness_mm,
            thermal_gap_mm=thermal_gap_mm,
            thermal_bridge_mm=thermal_bridge_mm,
            name=name,
            priority=priority,
            pad_connection=pad_connection,
        )
        backup = _save_with_backup(tree, path)
        return {
            "net": net,
            "layer": layer,
            "vertices": len(polygon),
            "pad_connection": pad_connection,
            "priority": priority,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_board
    def add_ground_plane(
        layer: str = "B.Cu",
        net: str = "GND",
        fill_clearance_mm: float = 0.5,
        pad_connection: ZonePadConnection = "thermal",
        priority: int = 0,
    ) -> dict:
        """Pour a ground plane covering the whole board on `layer`.

        Reads the board outline (must exist — call `set_board_outline` first),
        creates a zone polygon matching it, and assigns it to `net`.
        Defaults: B.Cu / GND (the most common pattern). `pad_connection` and
        `priority` as in `add_zone`.
        """
        tree, path = _load_active_pcb()
        ed.add_ground_plane(
            tree, layer=layer, net_name=net,
            fill_clearance_mm=fill_clearance_mm,
            pad_connection=pad_connection, priority=priority,
        )
        backup = _save_with_backup(tree, path)
        return {
            "layer": layer,
            "net": net,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_board
    def add_silk_text(
        text: str,
        x_mm: float,
        y_mm: float,
        layer: str = "F.SilkS",
        size_mm: float = 1.0,
        rotation: float = 0,
    ) -> dict:
        """Add silkscreen text to the PCB.

        `layer`: F.SilkS / B.SilkS (silkscreen) or F.Fab / B.Fab (fab notes).
        For text on copper, use F.Cu / B.Cu — handy for IDs etched into copper.
        """
        tree, path = _load_active_pcb()
        ed.add_silk_text(
            tree, text=text, x_mm=x_mm, y_mm=y_mm,
            layer=layer, size_mm=size_mm, rotation=rotation,
        )
        backup = _save_with_backup(tree, path)
        return {
            "text": text,
            "position_mm": [x_mm, y_mm],
            "layer": layer,
            "size_mm": size_mm,
            "rotation": rotation,
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_board
    def render_board(
        layers: list[str] | None = None,
        region_mm: list[float] | None = None,
        bottom: bool = False,
        dpi: float = 400,
        out_path: str = "",
    ):
        """2D PNG of the board (kicad-cli plot, KiCAD colours) returned as an
        image, to check placement, silkscreen and routing by eye. `layers`
        default F/B.Cu, F/B.SilkS, F.Fab, Edge.Cuts; `region_mm=[x1, y1, x2,
        y2]` zooms (board coords), else the outline + 1 mm; `bottom=True`
        mirrors to view from below.

        Details:

        The longest side is capped at 4000 px (dpi lowered to fit). The PNG
        is written to `out_path`, else `<temp>/kicad-claude-renders/`.
        """
        from mcp.server.fastmcp import Image
        from kicad_claude.adapters import render2d
        from kicad_claude.tools.common import render_out

        if region_mm is not None and len(region_mm) != 4:
            raise ValueError("region_mm is [x1, y1, x2, y2]")
        tree, path = _load_active_pcb()
        box = None
        outline = ed.get_board_outline_polygon_kicad(tree)
        if outline:
            xs, ys = [p[0] for p in outline], [p[1] for p in outline]
            box = (min(xs), min(ys), max(xs), max(ys))
        else:
            cy = [b for fp in ed.iter_footprints(tree) for b in [ed.footprint_courtyard_bbox(fp)] if b]
            if cy:
                box = (min(b[0] for b in cy), min(b[1] for b in cy),
                       max(b[2] for b in cy), max(b[3] for b in cy))
        png = render_out(out_path, f"{path.stem}-{'bottom' if bottom else 'top'}")
        info = render2d.render_board(path, png, layers=layers,
                                     region_mm=tuple(region_mm) if region_mm else None,
                                     board_box=box, mirror=bottom, dpi=dpi)
        return [Image(path=png), info]

    @mcp.tool()

    @on_board
    def set_footprint_field(
        references: list[str],
        field: str = "Reference",
        visible: bool | None = None,
        x_mm: float | None = None,
        y_mm: float | None = None,
        rotation: float | None = None,
        size_mm: float | None = None,
        layer: str | None = None,
    ) -> dict:
        """Show / hide, move, rotate, resize or re-layer a placed footprint's
        field (Reference, Value or a user field) on the board. `x_mm`/`y_mm`
        are the board position (one reference only), `rotation` the text's
        board angle. Returns each field after the edit.

        Details:

        `references` takes several parts for a bulk change (`visible`,
        `size_mm` - thickness scales with it -, `layer`, `rotation`). A
        position needs exactly one reference. `layer` e.g. F.SilkS / F.Fab.
        For a whole-board pass use `tidy_footprint_fields`.
        """
        from kicad_claude.adapters import pcb_fields

        refs = list(references or [])
        if not refs:
            raise ValueError("references is empty")
        if (x_mm is None) != (y_mm is None):
            raise ValueError("give both x_mm and y_mm")
        if x_mm is not None and len(refs) != 1:
            raise ValueError("a position needs exactly one reference")
        tree, path = _load_active_pcb()
        out, changed = [], False
        for ref in refs:
            fp = ed.find_footprint_by_reference(tree, ref)
            if fp is None:
                raise KeyError(f"no footprint with reference {ref!r} on this board")
            changed |= pcb_fields.set_field(
                fp, field, visible=visible,
                position=(x_mm, y_mm) if x_mm is not None else None,
                angle=rotation, size=size_mm, layer=layer)
            out.append({"reference": ref,
                        **pcb_fields.field_info(fp, pcb_fields.field_node(fp, field))})
        backup = _save_with_backup(tree, path) if changed else None
        return {"fields": out, "changed": changed,
                "backup": str(backup) if backup else None}

    @mcp.tool()

    @on_board
    def tidy_footprint_fields(
        references: list[str] | None = None,
        hide_values: bool = True,
        size_mm: float | None = None,
        clearance_mm: float = 0.2,
        hide_unplaced: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Keep silkscreen references off copper: move each footprint's
        Reference (default all) to the first free spot around its courtyard,
        clear of pads, silk graphics, other references and the board edge,
        and hide Values that sit on silkscreen. Lists parts with no free spot
        (`hide_unplaced=True` hides those). Run DRC after.

        Details:

        Spots tried: above, below, right, left, then the four corners, each
        at `clearance_mm` from the courtyard and then 0.5 / 1.0 mm further.
        Text is set horizontal. `size_mm` resizes the references first
        (thickness scales). Text extents are an estimate of KiCAD's stroke
        font, so confirm with `run_drc` (silk_overlap, silk_over_copper).
        `dry_run=True` reports without writing.
        """
        from kicad_claude.adapters import pcb_fields

        tree, path = _load_active_pcb()
        result = pcb_fields.tidy_fields(tree, references, hide_values, size_mm,
                                        clearance_mm, hide_unplaced)
        changed = any(result[k] for k in ("moved", "values_hidden", "references_hidden"))
        backup = _save_with_backup(tree, path) if changed and not dry_run else None
        result.update(dry_run=dry_run, backup=str(backup) if backup else None)
        return result

    @mcp.tool()

    @on_board
    def add_mounting_hole(
        x_mm: float,
        y_mm: float,
        diameter_mm: float = 3.2,
        plated: bool = False,
        reference: str | None = None,
    ) -> dict:
        """Add a mounting hole at (x_mm, y_mm) by drill diameter.

        Details:

        Common sizes:
          - 2.2 mm → M2 screw
          - 2.7 mm → M2.5 screw
          - 3.2 mm → M3 screw
          - 4.3 mm → M4 screw
          - 5.3 mm → M5 screw

        `plated=True` picks a `_Pad` variant (annular ring around the hole,
        useful for grounding the chassis). `plated=False` picks the bare
        non-plated through-hole.
        """
        idx = lib_tools._ensure_index()
        diam_str = f"{diameter_mm:g}"  # 3.2 → "3.2", 3.0 → "3"
        prefix = f"MountingHole:MountingHole_{diam_str}mm"

        candidates = [k for k in idx["footprints"] if k.startswith(prefix)]
        if plated:
            preferred = [
                k for k in candidates
                if "_Pad" in k
                and "TopOnly" not in k
                and "TopBottom" not in k
                and "Via" not in k
            ]
        else:
            preferred = [k for k in candidates if "_Pad" not in k]
        chosen = preferred or candidates
        if not chosen:
            available = sorted({
                f.split(":", 1)[1].split("_M")[0]
                for f in idx["footprints"]
                if f.startswith("MountingHole:MountingHole_")
            })
            raise FileNotFoundError(
                f"no MountingHole footprint for diameter {diameter_mm} mm "
                f"(plated={plated}). Available diameters: {available}"
            )
        lib_id = min(chosen, key=len)  # simplest matching name

        if reference is None:
            tree, _ = _load_active_pcb()
            existing_h = [
                ref for ref in ed.all_footprint_references(tree)
                if ref and ref.upper().startswith("H")
                and ref[1:].isdigit()
            ]
            n = len(existing_h) + 1
            reference = f"H{n}"

        mod_path = _resolve_footprint(lib_id)
        fp_def = ed.fetch_footprint_def(mod_path)
        tree, path = _load_active_pcb()
        ed.add_footprint(
            tree,
            qualified_lib_id=lib_id,
            reference=reference,
            value=f"{diameter_mm}mm",
            x_mm=x_mm, y_mm=y_mm,
            rotation=0, layer="F.Cu",
            fp_def_node=fp_def,
        )
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "lib_id": lib_id,
            "diameter_mm": diameter_mm,
            "plated": plated,
            "position_mm": [x_mm, y_mm],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_board
    def add_fiducial(
        x_mm: float,
        y_mm: float,
        size: str = "1mm",
        layer: str = "F.Cu",
        reference: str | None = None,
    ) -> dict:
        """Add a fiducial marker (for pick-and-place camera registration).

        Details:

        `size` ∈ {"0.5mm", "0.75mm", "1mm", "1.5mm"}. Standard PnP machines
        need 3 fiducials per side, ideally near board corners. Layer defines
        which side: F.Cu (top fiducial) or B.Cu (bottom fiducial).
        """
        if size not in {"0.5mm", "0.75mm", "1mm", "1.5mm"}:
            raise ValueError(f"size must be one of 0.5mm, 0.75mm, 1mm, 1.5mm (got {size!r})")
        lib_id = f"Fiducial:Fiducial_{size}_Mask{size.replace('mm', '')}mm"
        # Several fiducial naming conventions exist; try the simpler one first.
        idx = lib_tools._ensure_index()
        if lib_id not in idx["footprints"]:
            # Common alternative names
            for cand in (
                f"Fiducial:Fiducial_{size}_Mask{size.replace('mm','')}mm",
                f"Fiducial:Fiducial_{size}_Mask2mm",
                f"Fiducial:Fiducial_{size}_CopperTop",
            ):
                if cand in idx["footprints"]:
                    lib_id = cand
                    break
            else:
                raise FileNotFoundError(
                    f"no Fiducial footprint matches size {size!r} in the index. "
                    f"Try add_footprint with an explicit Fiducial:* lib_id."
                )

        if reference is None:
            tree, _ = _load_active_pcb()
            existing = [
                ref for ref in ed.all_footprint_references(tree)
                if ref and ref.startswith("FID")
            ]
            n = len(existing) + 1
            reference = f"FID{n}"

        mod_path = _resolve_footprint(lib_id)
        fp_def = ed.fetch_footprint_def(mod_path)
        tree, path = _load_active_pcb()
        ed.add_footprint(
            tree,
            qualified_lib_id=lib_id,
            reference=reference,
            value=size,
            x_mm=x_mm, y_mm=y_mm,
            rotation=0, layer=layer,
            fp_def_node=fp_def,
        )
        backup = _save_with_backup(tree, path)
        return {
            "reference": reference,
            "lib_id": lib_id,
            "size": size,
            "layer": layer,
            "position_mm": [x_mm, y_mm],
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_board
    def list_diff_pair_candidates() -> dict:
        """Auto-detect differential pairs by net name conventions.

        Details:

        Recognizes `_P/_N`, `+/-`, and `DP/DM` (USB-style) suffixes. Only
        returns pairs where BOTH members exist on the PCB. Useful before
        autorouting — pairs detected here will be routed coupled by
        Freerouting if the assigned net class has diff_pair_width / gap.
        """
        tree, _ = _load_active_pcb()
        return {"pairs": ed.find_diff_pair_candidates(tree)}

    @mcp.tool()

    @on_board
    def list_nets() -> dict:
        """List every net on the PCB, with its table index when it has one.

        Details:

        KiCAD 10 (board format `20260206`) stores a net as its **name** on each
        pad, track, via and zone — `(net "GND")` — and writes no top-level net
        table, so `index` is None. The numbered `(net 5 "GND")` form plus a
        `(net 5 "GND")` table is board format 9 and earlier; both are read, the
        current one is written. A KiCAD 10 board that an older parser reports as
        having no nets is not broken.
        """
        tree, _ = _load_active_pcb()
        return {"nets": ed.list_nets(tree)}

    # ----- Copper connectivity (derived, read-only) ----------------------- #

    @mcp.tool()

    @on_board
    def list_unrouted(summary: bool = False, max_items: int = 0, net: str = "") -> dict:
        """Missing connections (the ratsnest), longest first, from tracks, vias
        and filled zones; each names the closest pad pair. `summary=True`
        returns only `count` and `by_net`; `max_items` caps the list (0 = all);
        `net` filters one net. `islands`: copper reaching no pad. Unfilled
        zones (`zones_filled=False`): re-run after `run_drc(refill_zones=True)`.

        Details:

        Each filled polygon is its own island, so a pour cut in two by tracks
        shows as a missing link. A pad joins a pour only where the fill
        overlaps the pad outline: a thermal relief without a spoke does not
        connect. `islands` lists tracks / vias / pour pieces of a net that
        reach none of its pads (router stubs); DRC counts each as an
        unconnected item, so `count + island_count` matches its total.

        A `.kicad_pcb` says which net each pad belongs to but not whether copper
        actually joins them. This walks tracks, vias and filled zones to find
        out. One entry per missing link, naming the closest pad pair it would
        join.

        `zones_filled` is False when some zone carries no fill data. An unfilled
        ground pour connects nothing, so treat the result as pessimistic and
        re-run after `run_drc(refill_zones=True)`.

        An unrouted board has hundreds of these, which is a lot of JSON to
        answer "how many?". `summary=True` returns only `count` and
        `by_net`; `max_items` caps the list (0 = all) and `omitted` says how
        many were dropped; `net` restricts the result to one net.
        """
        tree, _ = _load_active_pcb()
        result = pcb_netlist.list_unrouted(tree)
        result.setdefault("islands", [])
        items = result["unrouted"]
        if net:
            wanted = normalize_name(net)
            items = [i for i in items if normalize_name(i["net"]) == wanted]
            result["net"] = net
            result["count"] = len(items)
            result["islands"] = [i for i in result["islands"]
                                 if normalize_name(i["net"]) == wanted]
        result["island_count"] = len(result["islands"])

        by_net: dict[str, int] = {}
        for item in items:
            by_net[item["net"]] = by_net.get(item["net"], 0) + 1
        result["by_net"] = sorted(
            [{"net": n, "missing_links": c} for n, c in by_net.items()],
            key=lambda d: (-d["missing_links"], d["net"]),
        )

        if summary:
            result["unrouted"] = []
            result["islands"] = []
            result["omitted"] = len(items)
        elif max_items and len(items) > max_items:
            result["unrouted"] = items[:max_items]
            result["omitted"] = len(items) - max_items
        else:
            result["unrouted"] = items
        return result

    @mcp.tool()

    @on_board
    def get_pad_position(reference: str, pad: str) -> dict:
        """Absolute position, layers and net of one pad.

        The primitive behind "put this decoupling cap at its IC pin": read the
        IC pad's position, then `move_footprint` the capacitor next to it.
        """
        tree, _ = _load_active_pcb()
        found = pcb_netlist.find_pad(tree, reference, pad)
        if found is None:
            raise KeyError(f"no pad {pad!r} on footprint {reference!r}")
        return {
            "reference": found["ref"],
            "pad": found["pad"],
            "position_mm": list(found["point"]),
            "layers": found["layers"],
            "net": found["net_name"],
            "net_number": found["net"],
            "type": found["type"],
        }

    @mcp.tool()

    @on_board
    def list_pads(reference: str = "") -> list[dict]:
        """Every pad on the board, or only those of one footprint: position,
        layers, net, type, shape, size_mm [w, h] (before the pad's rotation)."""
        tree, _ = _load_active_pcb()
        pads = pcb_netlist.list_pads(tree)
        if reference:
            pads = [p for p in pads if p["ref"] == reference]
            if not pads:
                raise KeyError(f"no footprint with reference {reference!r}")
        return [
            {
                "reference": p["ref"],
                "pad": p["pad"],
                "position_mm": list(p["point"]),
                "layers": p["layers"],
                "net": p["net_name"],
                "type": p["type"],
                "shape": p.get("shape", ""),
                "size_mm": p.get("size_mm", []),
            }
            for p in pads
        ]

    @mcp.tool()

    @on_board
    def net_route_status(net: str) -> dict:
        """Whether one net is fully routed, and how its pads are grouped.

        `connected_groups` holds one list per island of joined pads. A routed
        net has exactly one.
        """
        tree, _ = _load_active_pcb()
        return pcb_netlist.net_route_status(tree, net)

    @mcp.tool()

    @on_board
    def compute_trace_length(net: str) -> dict:
        """Total trace length on a net (mm), summed across all layers/segments."""
        tree, _ = _load_active_pcb()
        return ed.compute_trace_length(tree, net)

    @mcp.tool()

    @on_board
    def validate_diff_pair_length_match(
        positive_net: str,
        negative_net: str,
        tolerance_mm: float = 0.5,
    ) -> dict:
        """Check that two diff pair nets are within `tolerance_mm` of each other.

        Returns lengths, skew, and `within_tolerance` boolean. Use after
        autoroute to decide whether length tuning (add_meander) is needed.
        """
        tree, _ = _load_active_pcb()
        p = ed.compute_trace_length(tree, positive_net)
        n = ed.compute_trace_length(tree, negative_net)
        skew = abs(p["total_mm"] - n["total_mm"])
        return {
            "positive_net": positive_net,
            "negative_net": negative_net,
            "positive_length_mm": p["total_mm"],
            "negative_length_mm": n["total_mm"],
            "skew_mm": round(skew, 4),
            "tolerance_mm": tolerance_mm,
            "within_tolerance": skew <= tolerance_mm,
            "longer_net": positive_net if p["total_mm"] > n["total_mm"] else negative_net,
        }

    @mcp.tool()

    @on_board
    def add_meander(
        x1_mm: float,
        y1_mm: float,
        x2_mm: float,
        y2_mm: float,
        target_length_mm: float,
        amplitude_mm: float = 1.5,
        side: Literal["up", "down"] = "up",
        width_mm: float = 0.25,
        layer: str = "F.Cu",
        net: str | None = None,
    ) -> dict:
        """Add a triangular meander trace from (x1,y1) to (x2,y2) totalling
        `target_length_mm`, for length tuning. `side` (up / down) picks the
        side of the endpoint line. The straight segment it replaces must
        already be gone. Raises if the target is shorter than the straight
        distance or the meander does not fit (raise the amplitude).

        Details:

        For length tuning of high-speed signals (DDR, USB, Ethernet diff
        pairs). The meander is placed perpendicular to the line connecting
        the endpoints; `side` chooses which side ("up" / "down" relative to
        the perpendicular direction).

        Typical workflow:
          1. autoroute_pcb → traces routed
          2. validate_diff_pair_length_match(p, n) → reports skew
          3. compute_trace_length(longer_net), compute_trace_length(shorter_net)
          4. Manually delete a straight segment of the shorter net in KiCAD
             GUI, then call add_meander between its endpoints with target =
             longer_length.

        Raises ValueError if `target_length_mm` is shorter than the straight
        distance, or the meander region doesn't fit (increase amplitude).
        """
        tree, path = _load_active_pcb()
        segs = ed.add_meander_segments(
            tree,
            start_mm=(x1_mm, y1_mm),
            end_mm=(x2_mm, y2_mm),
            target_length_mm=target_length_mm,
            amplitude_mm=amplitude_mm,
            side=side,
            width_mm=width_mm,
            layer=layer,
            net_name=net,
        )
        backup = _save_with_backup(tree, path)
        # Total achieved length
        total = 0.0
        import math
        for seg in segs:
            start = sch_io.find_child(seg, "start")
            end = sch_io.find_child(seg, "end")
            total += math.hypot(
                float(end[1]) - float(start[1]),
                float(end[2]) - float(start[2]),
            )
        return {
            "from_mm": [x1_mm, y1_mm],
            "to_mm": [x2_mm, y2_mm],
            "target_length_mm": target_length_mm,
            "achieved_length_mm": round(total, 4),
            "segment_count": len(segs),
            "amplitude_mm": amplitude_mm,
            "side": side,
            "layer": layer,
            "net": net or "(unconnected)",
            "backup": str(backup) if backup else None,
        }

    # ----- Multi-board management ---------------------------------------- #

    @mcp.tool()
    def add_board(name: str) -> dict:
        """Create an additional `.kicad_pcb` file in the project (multi-board).

        Details:

        Useful for projects with several physical boards (main + breakout +
        debugger, for instance). The new board file is registered in the
        project's `boards` list and made the active board.

        Standard project flow continues to use the original `.kicad_pcb`
        unless you switch via `set_active_board`.
        """
        proj = state.get_active()
        if not name.endswith(".kicad_pcb"):
            filename = f"{name}.kicad_pcb"
        else:
            filename = name
        target = proj.path / filename
        if target.is_file():
            raise FileExistsError(f"{target} already exists")
        write_blank_pcb(target)

        # Register the board in the .kicad_pro `boards` array so KiCAD's
        # Project Manager shows it.
        pro = ps.load_pro(proj.pro_path)
        boards = pro.setdefault("boards", [])
        if filename not in boards:
            boards.append(filename)
        ps.save_pro(proj.pro_path, pro)

        state.set_active_board(filename)
        return {
            "filename": filename,
            "path": str(target),
            "active": True,
        }

    @mcp.tool()
    def list_boards() -> dict:
        """List every `.kicad_pcb` in the project directory + which is active."""
        proj = state.get_active()
        files = sorted(p.name for p in proj.path.glob("*.kicad_pcb"))
        active = state.get_active_board_filename()
        # Active resolves to the main board if not explicitly set
        main = proj.pcb_path.name
        return {
            "boards": files,
            "main_board": main,
            "active_board": active or main,
        }

    @mcp.tool()
    def set_active_board(filename: str = "") -> dict:
        """Switch the active PCB. Empty string returns to the project's main board.

        Subsequent PCB tools (`add_footprint`, `add_track`, `run_drc`, etc.)
        operate on the active board.
        """
        if not filename or filename.lower() == "main":
            state.set_active_board(None)
        else:
            if not filename.endswith(".kicad_pcb"):
                filename = f"{filename}.kicad_pcb"
            state.set_active_board(filename)
        state.remember()
        active_path = state.get_active_board_path()
        return {
            "active_board": state.get_active_board_filename() or "main",
            "path": str(active_path),
        }

    @mcp.tool()

    @on_board
    def add_via(
        x_mm: float,
        y_mm: float,
        drill_mm: float = 0.4,
        diameter_mm: float = 0.8,
        net: str | int = "",
    ) -> dict:
        """Add a through-hole via from F.Cu to B.Cu on `net` (net name as
        `list_nets` shows it; "" = no net).

        Details:

        `net` may also be a legacy integer index, accepted only on a board
        that still has a net table (pre-KiCAD 10). A name the board does not
        know is refused.
        """
        tree, path = _load_active_pcb()
        ed.add_via(tree, x_mm, y_mm, drill_mm=drill_mm, diameter_mm=diameter_mm, net=net)
        backup = _save_with_backup(tree, path)
        return {
            "position_mm": [x_mm, y_mm],
            "drill_mm": drill_mm,
            "diameter_mm": diameter_mm,
            "net": net,
            "backup": str(backup) if backup else None,
        }
