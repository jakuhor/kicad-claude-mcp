"""Manufacturing outputs and visualization.

Tools (2):
    export             — one output by kind: gerbers, drill, pos, bom, netlist,
                         render, step, svg (the per-kind functions below)
    export_fab_package — bundled gerbers + drill + pos + bom + render in <project>/fab/

All tools default to writing into `<project>/fab/` so a single project ends
up with a clean, fab-house-ready directory.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, Literal

from kicad_claude import state
from kicad_claude.tools.common import on_board
from kicad_claude.arg_types import clean
from kicad_claude.adapters import kicad_cli

logger = logging.getLogger("kicad-claude.tools.fab.exports")

# Columns of the BOM inside a fab package: KiCAD's own set plus what an
# assembly house needs to buy the parts.
DEFAULT_BOM_FIELDS = ("Reference", "Value", "Footprint", "MPN", "Manufacturer", "Datasheet",
                      "${QUANTITY}", "${DNP}")


def _fab_dir(subdir: str | None = None) -> Path:
    """Default output directory for manufacturing artifacts."""
    proj = state.get_active()
    base = proj.path / "fab"
    if subdir:
        base = base / subdir
    base.mkdir(parents=True, exist_ok=True)
    return base


def register(mcp) -> None:
    """Register the fab tools on the FastMCP instance."""

    # ----- Individual exports (dispatched by `export`) ----------------------- #

    def export_gerbers(
        output_dir: str | None = None,
        layers: list[str] | None = None,
        timeout_seconds: float = 120.0,
    ) -> dict:
        """Export Gerber files for fabrication.

        `layers` is an optional subset (e.g. ["F.Cu", "B.Cu", "Edge.Cuts"]).
        When omitted, KiCAD uses the project's plot settings.
        Defaults to `<project>/fab/gerbers/`.
        """
        out = Path(output_dir).expanduser() if output_dir else _fab_dir("gerbers")
        layer_list = clean(layers) or None
        return kicad_cli.export_gerbers(
            state.get_active_board_path(), out, layers=layer_list, timeout=timeout_seconds
        )

    def export_drill(
        output_dir: str | None = None,
        format: str = "excellon",
        generate_map: bool = True,
        map_format: str = "pdf",
        separate_pth_npth: bool = True,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export drill files (Excellon by default) + drill map.

        `separate_pth_npth=True` writes PTH and NPTH holes to separate files,
        which is what most fab houses prefer.
        """
        out = Path(output_dir).expanduser() if output_dir else _fab_dir("drill")
        return kicad_cli.export_drill(
            state.get_active_board_path(), out,
            drill_format=format,
            generate_map=generate_map,
            map_format=map_format,
            excellon_separate_th=separate_pth_npth,
            timeout=timeout_seconds,
        )

    def export_pos(
        output_path: str | None = None,
        side: str = "both",
        format: str = "csv",
        units: str = "mm",
        smd_only: bool = False,
        exclude_dnp: bool = False,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export pick-and-place position file (for PCB assembly).

        `side`: front | back | both
        `format`: csv | ascii | gerber
        Defaults to `<project>/fab/<project>-pos.csv`.
        """
        proj = state.get_active()
        if output_path:
            out = Path(output_path).expanduser()
        else:
            ext = "csv" if format == "csv" else ("pos" if format == "ascii" else "gbr")
            out = _fab_dir() / f"{proj.name}-pos.{ext}"
        return kicad_cli.export_pos(
            state.get_active_board_path(), out,
            side=side, fmt=format, units=units,
            smd_only=smd_only, exclude_dnp=exclude_dnp,
            timeout=timeout_seconds,
        )

    def export_bom(
        output_path: str | None = None,
        fields: list[str] | None = None,
        group_by: str | None = "Value",
        exclude_dnp: bool = False,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export the Bill of Materials from the schematic as CSV.

        `fields`: column list, e.g. ["Reference", "Value", "Footprint"].
        Default is the standard KiCAD set.
        """
        proj = state.get_active()
        out = (
            Path(output_path).expanduser()
            if output_path
            else _fab_dir() / f"{proj.name}-bom.csv"
        )
        return kicad_cli.export_bom(
            proj.sch_path, out,
            fields=",".join(clean(fields)) or None, group_by=group_by,
            exclude_dnp=exclude_dnp,
            timeout=timeout_seconds,
        )

    def export_netlist(
        output_path: str | None = None,
        format: str = "kicadsexpr",
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export a netlist from the schematic.

        `format`: kicadsexpr | kicadxml | cadstar | orcadpcb2 | spice | spicemodel | pads | allegro
        """
        proj = state.get_active()
        ext = {
            "kicadsexpr": "net", "kicadxml": "xml",
            "spice": "cir", "spicemodel": "lib",
            "orcadpcb2": "net", "cadstar": "frp",
            "pads": "asc", "allegro": "txt",
        }.get(format, "txt")
        out = (
            Path(output_path).expanduser()
            if output_path
            else _fab_dir() / f"{proj.name}-netlist.{ext}"
        )
        return kicad_cli.export_netlist(
            proj.sch_path, out, fmt=format, timeout=timeout_seconds
        )

    def render_pcb_3d(
        output_path: str | None = None,
        side: str = "top",
        width: int = 1600,
        height: int = 900,
        quality: str = "basic",
        rotate: str | None = None,
        perspective: bool = False,
        timeout_seconds: float = 180.0,
    ) -> dict:
        """Render the PCB in 3D as a PNG (or JPEG if path ends with .jpg).

        `side`: top | bottom | left | right | front | back
        `quality`: basic | high   (high is much slower but prettier)
        `rotate`: 'X,Y,Z' degrees, e.g. '-30,0,45' for an isometric look.
        """
        proj = state.get_active()
        out = (
            Path(output_path).expanduser()
            if output_path
            else _fab_dir() / f"{proj.name}-render-{side}.png"
        )
        return kicad_cli.render_pcb(
            state.get_active_board_path(), out,
            side=side, width=width, height=height,
            quality=quality, rotate=rotate, perspective=perspective,
            timeout=timeout_seconds,
        )

    def export_step_3d(
        output_path: str | None = None,
        include_components: bool = True,
        include_tracks: bool = False,
        include_zones: bool = False,
        include_silkscreen: bool = False,
        include_soldermask: bool = False,
        no_dnp: bool = False,
        component_filter: str | None = None,
        timeout_seconds: float = 240.0,
    ) -> dict:
        """Export the PCB as a STEP 3D file (for Fusion 360 / SolidWorks / FreeCAD).

        Default: board body + 3D component models. Toggle include_tracks,
        include_zones, include_silkscreen, include_soldermask to add copper
        and graphical layers to the STEP.

        `component_filter` accepts wildcards (e.g. "U*,R1,R2").
        Big boards with everything included can take several minutes.
        """
        proj = state.get_active()
        out = (
            Path(output_path).expanduser()
            if output_path
            else _fab_dir() / f"{proj.name}.step"
        )
        return kicad_cli.export_step(
            state.get_active_board_path(), out,
            include_components=include_components,
            include_tracks=include_tracks,
            include_zones=include_zones,
            include_silkscreen=include_silkscreen,
            include_soldermask=include_soldermask,
            no_dnp=no_dnp,
            component_filter=component_filter,
            timeout=timeout_seconds,
        )

    def export_pcb_svg(
        output_dir: str | None = None,
        layers: list[str] | None = None,
        fit_page_to_board: bool = True,
        black_and_white: bool = False,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export one SVG per layer for documentation/preview.

        `layers`: subset (e.g. ["F.Cu", "B.Cu", "F.Silkscreen"]). When
        omitted, the documentation set is written: both copper layers, both
        silkscreens, both solder masks and Edge.Cuts. `kicad-cli` itself has no
        "every layer" default and refuses to run without a layer list.
        """
        out = Path(output_dir).expanduser() if output_dir else _fab_dir("svg")
        layer_list = clean(layers) or None
        return kicad_cli.export_pcb_svg(
            state.get_active_board_path(), out,
            layers=layer_list,
            fit_page_to_board=fit_page_to_board,
            black_and_white=black_and_white,
            timeout=timeout_seconds,
        )

    exporters = {
        "gerbers": export_gerbers, "drill": export_drill, "pos": export_pos,
        "bom": export_bom, "netlist": export_netlist, "render": render_pcb_3d,
        "step": export_step_3d, "svg": export_pcb_svg,
    }

    @mcp.tool()

    @on_board
    def export(
        kind: Literal["gerbers", "drill", "pos", "bom", "netlist", "render", "step", "svg"],
        output: str = "",
        options: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict:
        """Export one manufacturing / documentation output via kicad-cli.

        `kind`: gerbers, drill, pos, bom, netlist, render (3D PNG), step, svg.
        `output`: folder (gerbers, drill, svg) or file; default under
        `<project>/fab/`. `options`: per-kind settings (see help), e.g.
        `{"layers": ["F.Cu", "B.Cu"]}`, `{"side": "front"}`, `{"format": "spice"}`.
        For the whole fab set use `export_fab_package`.

        Details:

        Options per kind (defaults in brackets):
          - gerbers: layers (project plot settings) — list of layer names.
          - drill: format [excellon], generate_map [true], map_format [pdf],
            separate_pth_npth [true].
          - pos: side [both] front|back|both, format [csv] csv|ascii|gerber,
            units [mm], smd_only [false], exclude_dnp [false].
          - bom: fields (KiCAD's set) list, group_by [Value],
            exclude_dnp [false].
          - netlist: format [kicadsexpr] kicadsexpr|kicadxml|cadstar|orcadpcb2|
            spice|spicemodel|pads|allegro.
          - render: side [top] top|bottom|left|right|front|back, width [1600],
            height [900], quality [basic] basic|high, rotate ('X,Y,Z' degrees),
            perspective [false]. A `.jpg` output writes JPEG.
          - step: include_components [true], include_tracks, include_zones,
            include_silkscreen, include_soldermask, no_dnp [all false],
            component_filter (wildcards, "U*,R1").
          - svg: layers (copper, silk, mask, Edge.Cuts), fit_page_to_board
            [true], black_and_white [false].

        Default timeouts: render 180 s, step 240 s, gerbers 120 s, others 60 s.
        """
        fn = exporters[kind]
        params = inspect.signature(fn).parameters
        out_arg = "output_dir" if "output_dir" in params else "output_path"
        allowed = sorted(set(params) - {out_arg, "timeout_seconds"})
        opts = dict(options or {})
        unknown = sorted(set(opts) - set(allowed))
        if unknown:
            raise ValueError(f"export {kind}: unknown option(s) {unknown}; "
                             f"accepted: {allowed}")
        if output:
            opts[out_arg] = output
        if timeout_seconds is not None:
            opts["timeout_seconds"] = timeout_seconds
        return {"kind": kind, **fn(**opts)}

    # ----- Bundled fab package --------------------------------------------- #

    @mcp.tool()

    @on_board
    def export_fab_package(
        output_dir: str | None = None,
        include_render: bool = True,
        include_svg: bool = False,
        bom_fields: list[str] | None = None,
        timeout_seconds: float = 240.0,
    ) -> dict:
        """One-shot fab export to `<project>/fab/`: gerbers, drill, pos, BOM
        (`bom_fields` columns; the default includes MPN and Manufacturer) and
        an optional top render (`include_render`). Overwrites
        `<project>/fab/<name>-bom.csv`.

        Details:

        Produces a directory ready to zip and send to a fab house. If
        `include_render` is True, also writes a 3D render of the top side.

        `bom_fields` is the BOM's column list. The default carries the sourcing
        fields (`MPN`, `Manufacturer`) as well as the reference/value/footprint
        that KiCAD writes by default — an assembly house cannot quote a BOM
        without a part number.

        Note that this overwrites `<project>/fab/<name>-bom.csv`; export a BOM
        with other columns to a path of its own.
        """
        proj = state.get_active()
        base = Path(output_dir).expanduser() if output_dir else _fab_dir()
        base.mkdir(parents=True, exist_ok=True)

        results: dict = {"output_dir": str(base), "steps": {}}

        # Gerbers
        results["steps"]["gerbers"] = kicad_cli.export_gerbers(
            state.get_active_board_path(), base / "gerbers", timeout=timeout_seconds,
        )
        # Drill
        results["steps"]["drill"] = kicad_cli.export_drill(
            state.get_active_board_path(), base / "drill",
            generate_map=True, excellon_separate_th=True,
            timeout=timeout_seconds,
        )
        # Position file
        try:
            results["steps"]["pos"] = kicad_cli.export_pos(
                state.get_active_board_path(), base / f"{proj.name}-pos.csv",
                side="both", fmt="csv", units="mm",
                timeout=timeout_seconds,
            )
        except kicad_cli.KicadCliError as e:
            results["steps"]["pos"] = {"error": str(e)}
        # BOM. Assembly needs the part number, so the sourcing fields are
        # asked for by name; kicad-cli leaves a field blank when a symbol does
        # not carry it.
        try:
            results["steps"]["bom"] = kicad_cli.export_bom(
                proj.sch_path, base / f"{proj.name}-bom.csv",
                fields=",".join(clean(bom_fields, DEFAULT_BOM_FIELDS)),
                timeout=timeout_seconds,
            )
        except kicad_cli.KicadCliError as e:
            results["steps"]["bom"] = {"error": str(e)}
        # Render (optional)
        if include_render:
            try:
                results["steps"]["render"] = kicad_cli.render_pcb(
                    state.get_active_board_path(), base / f"{proj.name}-top.png",
                    side="top", timeout=timeout_seconds,
                )
            except kicad_cli.KicadCliError as e:
                results["steps"]["render"] = {"error": str(e)}
        if include_svg:
            try:
                results["steps"]["svg"] = kicad_cli.export_pcb_svg(
                    state.get_active_board_path(), base / "svg",
                    fit_page_to_board=True,
                    timeout=timeout_seconds,
                )
            except kicad_cli.KicadCliError as e:
                results["steps"]["svg"] = {"error": str(e)}

        # Summary counts
        all_files: list[str] = []
        for step in results["steps"].values():
            if not isinstance(step, dict) or "error" in step:
                continue
            if "files" in step:
                all_files.extend(step["files"])
            elif "output_path" in step:
                all_files.append(Path(step["output_path"]).name)
        results["total_artifact_count"] = len(all_files)
        return results
