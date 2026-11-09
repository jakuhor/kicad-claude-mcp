"""BOM lifecycle tools.

Tools:
    diff_bom             — BOM of two revisions, per designator and per line
    export_fab_bom       — JLCPCB / PCBWay BOM + CPL in the fab's columns
    update_symbol_fields — set fields on many symbols across all sheets
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import bom_tools, kicad_cli, power_review, safe_write
from kicad_claude.tools.release import _project_files, _snapshot
from kicad_claude.tools.review import project_netlist


def register(mcp) -> None:
    @mcp.tool()
    def diff_bom(old: str = "git:HEAD", new: str = "", extra_fields: str = "",
                 max_items: int = 300) -> dict:
        """BOM difference between two revisions of the active project.

        `old` / `new`: `git:<ref>`, a project folder, or empty for the working
        tree (default: last commit vs working tree). Per designator: added,
        removed, and changed Value / Footprint / MPN / Manufacturer / LCSC /
        Mouser / DigiKey / Farnell / Description / DNP / exclude-from-BOM
        (plus `extra_fields="Tolerance,Voltage"`). Per BOM line (MPN, or
        value + footprint without one): quantity changes with the
        designators that moved, MPNs that are new or gone, line and fitted
        part counts. Read-only.
        """
        proj = state.get_active()
        extra = tuple(f.strip() for f in extra_fields.split(",") if f.strip())
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            nls = []
            for tag, spec in (("old", old), ("new", new)):
                folder = _snapshot(spec, tmp)
                sch, _ = _project_files(folder, proj.name)
                net = tmp / f"{tag}.net"
                kicad_cli.export_netlist(sch, net, fmt="kicadsexpr")
                nls.append(power_review.parse_netlist(net))
        out = bom_tools.diff_bom(nls[0], nls[1], extra)
        for k in ("added", "removed", "changed", "line_qty_changes"):
            if len(out[k]) > max_items:
                out[f"{k}_omitted"] = len(out[k]) - max_items
                out[k] = out[k][:max_items]
        out["old"], out["new"] = old or "working tree", new or "working tree"
        return out

    @mcp.tool()
    def export_fab_bom(fab: str = "jlcpcb", rotation_offsets: dict[str, float] | None = None,
                       exclude_refs: str = "", out_dir: str = "", name: str = "",
                       netlist_path: str = "") -> dict:
        """Write the assembly BOM and placement (CPL) files in the fab's own
        format to `<project>/fab/` (or `out_dir`).

        `fab`: `jlcpcb` — BOM `Comment, Designator, Footprint, LCSC Part #`
        grouped by value + footprint + LCSC (FB-NO-LCSC for parts without
        one); `pcbway` — BOM `Item #, Designator, Qty, Manufacturer, Mfg Part
        #, Description / Value, Package/Footprint, Type, Your Instructions /
        Notes` grouped by MPN (FB-NO-MPN, FB-NO-MFR). CPL for both:
        `Designator, Mid X, Mid Y, Layer, Rotation` from `kicad-cli pcb export
        pos` (mm, KiCAD's pos coordinates).

        DNP parts, parts excluded from the BOM, test points, holes and
        fiducials are left out. Rotation = KiCAD rotation + an offset: the
        first `rotation_offsets` regex matching the footprint name
        (`{"^SOT-23": 180, "QFN": 90}`), else a `JLCPCB Rotation Offset` /
        `Rotation Offset` symbol field. No fab offset table is assumed —
        check the fab's placement preview, and record each correction in
        `rotation_offsets` or the field. Parts in the BOM but not on the board
        are FB-NO-POS. `exclude_refs="U2,J1"` leaves consigned or hand-fitted
        parts out of both files (listed in the result).
        """
        fab = fab.lower()
        if fab not in bom_tools.FABS:
            raise ValueError(f"fab must be one of {list(bom_tools.FABS)}")
        proj = state.get_active()
        nl = power_review.parse_netlist(project_netlist(netlist_path))
        out = Path(out_dir).expanduser().resolve() if out_dir else proj.path / "fab"
        out.mkdir(parents=True, exist_ok=True)
        stem = name or f"{proj.name}-{fab}"
        with tempfile.TemporaryDirectory() as tmp:
            posf = Path(tmp) / "pos.csv"
            kicad_cli.export_pos(state.get_active_board_path(), posf, side="both", fmt="csv", units="mm")
            pos = bom_tools.read_pos(posf)
            smd = None
            smdf = Path(tmp) / "smd.csv"
            try:
                kicad_cli.export_pos(state.get_active_board_path(), smdf, side="both", fmt="csv", units="mm",
                                     smd_only=True)
                smd = set(bom_tools.read_pos(smdf))
            except kicad_cli.KicadCliError:
                pass
        excl = {r.strip() for r in exclude_refs.split(",") if r.strip()}
        unknown = sorted(excl - set(nl.components))
        if unknown:
            raise ValueError(f"exclude_refs not in the schematic: {unknown}")
        bom, f1 = bom_tools.fab_bom(nl, fab, smd, excl)
        cpl, f2 = bom_tools.fab_cpl(nl, pos, fab, rotation_offsets, excl)
        bom_path, cpl_path = out / f"{stem}-bom.csv", out / f"{stem}-cpl.csv"
        bom_path.write_text(bom_tools.to_csv(bom), encoding="utf-8")
        cpl_path.write_text(bom_tools.to_csv(cpl), encoding="utf-8")
        findings = f1 + f2
        return {"fab": fab, "bom": str(bom_path), "cpl": str(cpl_path),
                "bom_lines": len(bom) - 1, "placements": len(cpl) - 1,
                "excluded": sorted(excl),
                "findings": findings,
                "counts": {k: sum(1 for f in findings if f["severity"] == k)
                           for k in ("error", "warning", "info")}}

    @mcp.tool()
    def update_symbol_fields(updates: dict[str, dict[str, str]] | None = None,
                             csv_path: str = "", dry_run: bool = True,
                             create: bool = True) -> dict:
        """Set fields on many symbols at once, across the whole sheet
        hierarchy: `updates={"R7": {"MPN": "RC0603FR-07100KL", "Manufacturer":
        "Yageo"}, ...}` and / or `csv_path` (a Reference / Designator column —
        one ref or a comma list per row — plus one column per field; empty
        cells are skipped).

        `dry_run=True` (default) returns the old -> new list and writes
        nothing; run again with `dry_run=False` to write (each changed sheet
        through the guarded writer: lock check, backup, re-parse). Every unit
        of a multi-unit symbol gets the value. `create=False` skips fields a
        symbol does not have. A symbol on a reused sheet is shared by every
        instance — listed under `reused_sheets`. `Reference` is refused (use
        `annotate_schematic`). KiCAD must be closed while writing.
        """
        proj = state.get_active()
        ups: dict = {}
        if csv_path:
            text = Path(csv_path).expanduser().read_text(encoding="utf-8-sig")
            ups.update(bom_tools.parse_updates_csv(text))
        for ref, fields in (updates or {}).items():
            ups.setdefault(ref, {}).update({k: str(v) for k, v in fields.items()})
        if not ups:
            raise ValueError("give updates or csv_path")
        return bom_tools.write_fields(
            proj.sch_path, ups, create=create, dry_run=dry_run,
            save=lambda tree, path: safe_write.save_tree(tree=tree, path=path))
