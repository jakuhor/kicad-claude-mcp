"""Sheet layout tools: page setup, field autoplace, readability check."""

from __future__ import annotations

from typing import Literal

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_layout as layout
from kicad_claude.tools.common import on_sheet
from kicad_claude.tools.sch import schematic as sch

Paper = Literal["A0", "A1", "A2", "A3", "A4", "A5", "USLetter", "USLegal", "USLedger",
                "A", "B", "C", "D", "E", "User"]


def register(mcp) -> None:
    @mcp.tool()
    def render_sheet(
        page: int = 1,
        region_mm: list[float] | None = None,
        dpi: float = 150,
        out_path: str = "",
        sheet: str = "",
    ):
        """2D PNG of one schematic page (kicad-cli plot of the whole
        hierarchy, page order as in KiCAD's PDF) returned as an image, to
        check readability by eye. `sheet` (name or file) picks its page
        instead of `page`; the result names the sheet. `region_mm=[x1, y1,
        x2, y2]` (sheet coords) zooms in; raise `dpi` with it.

        Details:

        The longest side is capped at 4000 px (dpi lowered to fit). The PNG
        is written to `out_path`, else `<temp>/kicad-claude-renders/`.
        """
        from mcp.server.fastmcp import Image
        from kicad_claude.adapters import render2d
        from kicad_claude.tools.common import render_out

        if region_mm is not None and len(region_mm) != 4:
            raise ValueError("region_mm is [x1, y1, x2, y2]")
        proj = state.get_active()
        tag = "".join(c if c.isalnum() else "_" for c in sheet) if sheet else f"page{page}"
        png = render_out(out_path, f"{proj.name}-{tag}")
        info = render2d.render_sheet(proj.sch_path, png, page=page, sheet=sheet, dpi=dpi,
                                     region_mm=tuple(region_mm) if region_mm else None)
        return [Image(path=png), info]

    @mcp.tool()
    @on_sheet
    def set_page(
        paper: Paper | None = None,
        portrait: bool | None = None,
        title_block: dict[str, str] | None = None,
        width_mm: float | None = None,
        height_mm: float | None = None,
    ) -> dict:
        """Paper size / orientation and title block of the sheet. Only what
        is passed changes. `title_block` keys: title, date, rev, company,
        comment1..comment9; "" clears one. `paper="User"` takes width_mm /
        height_mm.
        """
        tb: dict = {}
        for key, value in (title_block or {}).items():
            if key.startswith("comment") and key[7:].isdigit():
                tb.setdefault("comments", {})[key[7:]] = value
            elif key in layout.TITLE_FIELDS:
                tb[key] = value
            else:
                raise ValueError(f"title_block key {key!r}: use title, date, rev, "
                                 "company or comment1..comment9")
        tree, path = sch._load_active_schematic()
        result = layout.set_page(tree, paper=paper, portrait=portrait, width_mm=width_mm,
                                 height_mm=height_mm, title_block=tb or None)
        backup = sch._save_with_backup(tree, path)
        result["sheet"] = state.get_active_sheet_filename() or "root"
        result["backup"] = str(backup) if backup else None
        return result

    @mcp.tool()
    @on_sheet
    def autoplace_fields(references: list[str] | None = None, force: bool = False) -> dict:
        """KiCAD-style field autoplace: Reference / Value beside the body on
        the side with no pins, horizontal, on grid. `references` empty = every
        part. Parts with pins on every side keep their library placement
        unless `force=True`.
        """
        tree, path = sch._load_active_schematic()
        if references:
            nodes = []
            for r in references:
                n = ed.find_symbol_by_reference(tree, r)
                if n is None:
                    raise KeyError(f"no symbol {r!r}")
                nodes.append(n)
        else:
            nodes = list(ed.iter_instance_symbols(tree))
        done = [layout.autoplace_fields(tree, n, force=force) for n in nodes]
        placed = [d["reference"] for d in done if d["placed"]]
        kept = [{"reference": d["reference"], "reason": d["reason"]}
                for d in done if not d["placed"] and d.get("reason") != "power port"]
        backup = sch._save_with_backup(tree, path) if placed else None
        return {"placed": placed, "kept": kept,
                "sheet": state.get_active_sheet_filename() or "root",
                "backup": str(backup) if backup else None}

    @mcp.tool()
    @on_sheet
    def check_schematic_readability(max_findings: int = 40,
                                    short_label_mm: float = 15.0) -> dict:
        """Read-only layout review (`SR-*`): overlaps, labels or ports drawn
        into parts, frame / title-block hits, wires through parts or texts,
        label nets a wire would show better.

        Details:

        Run after placing, fix, re-run.

        Rules: SR-OVERLAP, SR-TEXT-ON-BODY (a field spilling over its own
        symbol; text fully inside the body is deliberate and not reported),
        SR-LABEL-INTO-BODY, SR-PORT-INTO-BODY, SR-FRAME, SR-TITLE-BLOCK,
        SR-WIRE-THROUGH-BODY, SR-TEXT-OVER-WIRE, SR-SHORT-LABEL-NET (2-3
        local labels within `short_label_mm`, default 15), SR-CROSSINGS (more
        than 4 X crossings), SR-OFF-GRID. Each finding has `at_mm`, `items`
        and a `fix` hint. A wire or port counts as hitting a body only on one
        of its graphics, not on the box that spans decoration (an LED's
        emission arrows). `label_only_nets` gives the signal nets with 2+
        pins (no power port) whose pins sit in 2+ wire groups joined only by
        a label name, and their ratio: one number for label overuse.
        Text extents are estimated from KiCAD's stroke font (0.9 x size per
        character), so a near miss can be reported; confirm by eye.
        """
        tree, _ = sch._load_active_schematic()
        result = layout.check_readability(tree, max_findings=max_findings,
                                          short_label_mm=short_label_mm)
        result["sheet"] = state.get_active_sheet_filename() or "root"
        return result
