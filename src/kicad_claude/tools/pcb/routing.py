"""Autorouting tools.

Tools:
    autoroute_pcb     — full pipeline: export DSN → run Freerouting → import SES
    export_dsn        — export-only (use the GUI of Freerouting if you want)
    import_ses        — import an existing .ses back into the active PCB

The DSN export and SES import shell out to KiCAD's bundled Python 3.9 because
`kicad-cli` v10 doesn't expose Specctra interchange. Freerouting itself is
invoked as a JAR via `java`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.tools.common import on_board
from kicad_claude.adapters import (
    freerouting, kicad_cli, kicad_python, pcb_netlist, safe_write, sch_io)

logger = logging.getLogger("kicad-claude.tools.pcb.routing")


def export_dsn_without_pours(pcb_path: Path, dsn_path: Path) -> int:
    """Export the DSN from a copy of the board with its copper pours removed,
    so Freerouting routes pour nets as signals. The copy sits next to a copy
    of the project file (net classes, rules). Returns the pours left out."""
    import shutil
    import tempfile

    tree = sch_io.parse_file(pcb_path)
    keep = [n for n in tree if not pcb_netlist.is_pour(n, tree)]
    dropped = len(tree) - len(keep)
    with tempfile.TemporaryDirectory(prefix="kc-dsn-") as tmp:
        copy = Path(tmp) / pcb_path.name
        sch_io.write_file(copy, keep)
        pro = pcb_path.with_suffix(".kicad_pro")
        if pro.is_file():
            shutil.copy2(pro, copy.with_suffix(".kicad_pro"))
        kicad_python.export_dsn(copy, dsn_path)
    return dropped


def register(mcp) -> None:
    """Register the tools."""

    @mcp.tool()

    @on_board
    def export_dsn(output_path: str | None = None) -> dict:
        """Export the active PCB to Specctra DSN.

        If `output_path` is None, writes alongside the active .kicad_pcb with
        the same stem and `.dsn` extension.
        """
        pcb_path = state.get_active_board_path()
        out = (
            Path(output_path).expanduser()
            if output_path
            else pcb_path.with_suffix(".dsn")
        )
        kicad_python.export_dsn(pcb_path, out)
        return {"dsn_path": str(out), "size_bytes": out.stat().st_size}

    @mcp.tool()

    @on_board
    def import_ses(ses_path: str) -> dict:
        """Apply a Freerouting `.ses` session into the active `.kicad_pcb` (in-place).

        A backup of the PCB is saved under `<project>/.backups/` before the import.
        """
        pcb_path = state.get_active_board_path()
        ses = Path(ses_path).expanduser()
        _, backup = safe_write.guard_external(
            pcb_path, lambda: kicad_python.import_ses(pcb_path, ses))
        return {
            "pcb_path": str(pcb_path),
            "ses_path": str(ses),
            "backup": str(backup) if backup else None,
        }

    @mcp.tool()

    @on_board
    def autoroute_pcb(
        passes: int = 100,
        timeout_seconds: float = 300.0,
        threads: int | None = None,
        check_after: bool = True,
        route_pour_nets: bool = False,
    ) -> dict:
        """Autoroute: export DSN -> Freerouting -> import SES. `check_after`
        refills zones and reports what is still open (`after_import`, the
        number to trust). Pads of a pour's net beyond the pour stay unrouted
        (`pour_guard`); `route_pour_nets=True` routes pour nets as signals.

        Details:

        `passes` is Freerouting's `router.autorouter.max_passes` (higher =
        better routes, slower); `timeout_seconds` hard-kills it; `threads`
        sets `-mt`. Freerouting counts only the connections it was given:
        routed tracks can cut a pour into islands, and it does not route a
        pad to a pour of its own net it cannot reach ("-> conductionarea" in
        its log). `pour_guard`: per pour net, the pads outside every outline
        of that net, checked before routing (a pad above a pour on another
        layer is fine: Freerouting drops a via). `route_pour_nets=True`
        exports the DSN from a copy without the pours, so every net is
        routed with tracks; the pours stay on the board and are refilled
        after the import. `after_import` runs `kicad-cli pcb drc --refill-zones`
        (the board is saved with fresh fills, inside the same backup) and
        `list_unrouted` on the result.
        """
        pcb_path = state.get_active_board_path()
        dsn_path = pcb_path.with_suffix(".dsn")
        ses_path = pcb_path.with_suffix(".ses")

        # 1) Export DSN (pour nets: guard, or route them as signals)
        guard = pcb_netlist.pads_outside_pours(sch_io.parse_file(pcb_path))
        if route_pour_nets:
            pours_left_out = export_dsn_without_pours(pcb_path, dsn_path)
        else:
            pours_left_out = 0
            kicad_python.export_dsn(pcb_path, dsn_path)

        # 2) Run Freerouting
        route_result = freerouting.route(
            dsn_path,
            ses_path,
            passes=passes,
            threads=threads,
            timeout_seconds=timeout_seconds,
        )

        # 3) Backup, import SES back into the PCB, refill zones
        def _import_and_check():
            kicad_python.import_ses(pcb_path, ses_path)
            if not check_after:
                return None
            try:
                return kicad_cli.run_drc(pcb_path, schematic_parity=False, refill_zones=True)
            except Exception as exc:  # the routing is in; only the check failed
                return {"error": f"{type(exc).__name__}: {exc}"}

        drc, backup = safe_write.guard_external(pcb_path, _import_and_check)

        after = None
        if drc is not None and "error" in drc:
            after = {"error": drc["error"]}
        elif drc is not None:
            unrouted = pcb_netlist.list_unrouted(sch_io.parse_file(pcb_path))
            by_net: dict[str, int] = {}
            for item in unrouted["unrouted"]:
                by_net[item["net"]] = by_net.get(item["net"], 0) + 1
            for isl in unrouted["islands"]:
                by_net[isl["net"]] = by_net.get(isl["net"], 0) + 1
            after = {
                "drc_unconnected": drc["unconnected_items_count"],
                "drc_errors": drc["errors"],
                "missing_links": unrouted["count"],
                "copper_islands": len(unrouted["islands"]),
                "by_net": dict(sorted(by_net.items(), key=lambda kv: -kv[1])),
                "drc_raw_path": drc["raw_path"],
            }

        return {
            "pcb_path": str(pcb_path),
            "dsn_path": str(dsn_path),
            "ses_path": str(ses_path),
            "passes": passes,
            "pour_guard": {
                "pads_outside_pours": guard,
                "route_pour_nets": route_pour_nets,
                "pours_left_out_of_dsn": pours_left_out,
                **({"warning": "Freerouting treats a pour as a plane: these pads "
                    "stay unrouted. Extend the pour, or rerun with route_pour_nets=True."}
                   if guard and not route_pour_nets else {}),
            },
            "stats": route_result["stats"],
            "after_import": after,
            "freerouting_returncode": route_result["returncode"],
            "freerouting_stdout_tail": route_result["stdout_tail"],
            "backup": str(backup) if backup else None,
        }
