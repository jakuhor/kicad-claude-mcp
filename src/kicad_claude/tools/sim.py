"""SPICE simulation hooks.

Tools:
    export_spice_netlist      — KiCAD schematic → SPICE netlist
    run_spice                 — run ngspice or LTspice on a netlist + analysis
    simulate_subcircuits      — detect dividers / RC / LC-pi filters in the
                                schematic and check them (worst case, SPICE
                                AC sweep, Monte Carlo)

Simulation needs `ngspice` on PATH or LTspice installed
(`adapters/spice_backends.py`: discovery and the .raw reader).

The simulator output is captured verbatim — for plotting, parse the printed
data points yourself or pipe it into another tool.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Literal
from pathlib import Path

from kicad_claude import state
from kicad_claude.tools.common import on_board
from kicad_claude.arg_types import SubcircuitType, clean
from kicad_claude.adapters import kicad_cli

logger = logging.getLogger("kicad-claude.tools.sim")


def register(mcp) -> None:
    @mcp.tool()
    def export_spice_netlist(
        output_path: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Export the active project's schematic as a SPICE netlist.

        Default output: `<project>/fab/<project>-spice.cir`. Run
        `run_spice` on the result to perform DC/AC/transient
        analysis.

        Note: for SPICE to be meaningful, schematic symbols must have
        `Spice_Model` / `Spice_Lib_File` / `Spice_Primitive` fields set.
        Symbols from KiCAD's official Simulation library (`Simulation_SPICE`)
        already include these.
        """
        proj = state.get_active()
        out = (
            Path(output_path).expanduser()
            if output_path
            else proj.path / "fab" / f"{proj.name}-spice.cir"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        kicad_cli.export_netlist(
            proj.sch_path, out, fmt="spice", timeout=timeout_seconds,
        )
        return {
            "netlist_path": str(out),
            "size_bytes": out.stat().st_size,
            "format": "spice",
        }

    @mcp.tool()
    def run_spice(
        netlist_path: str | None = None,
        analysis: str = ".op",
        simulator: Literal["auto", "ngspice", "ltspice"] = "auto",
        output_path: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> dict:
        """Run a SPICE netlist in batch mode with `analysis` appended (`.op`,
        `.dc V1 0 5 0.1`, `.ac dec 10 1 1G`, `.tran 1u 10m`). `simulator`:
        auto (ngspice, else LTspice), ngspice or ltspice. Returns the output
        tail and, for LTspice, the .raw / .log paths.

        Details:

        Default netlist: `<project>/fab/<project>-spice.cir`
        (`export_spice_netlist`). The netlist is copied with the directive
        and `.end` appended, so the original is never changed. ngspice: also
        `.print all`, output in `<project>/fab/<project>-spice-out.txt`.
        LTspice (launched by its full path: `LTSPICE_PATH`, absolute PATH
        entries, or the default install folder; `-b` batch): the `.raw`
        (binary, read it with any raw reader) and `.log` beside the driver
        deck. Use LTspice syntax there (no `.control` blocks).
        """
        from kicad_claude.adapters import spice_backends

        sim = spice_backends.detect(simulator)
        if sim is None:
            raise FileNotFoundError(f"no {'SPICE simulator' if simulator == 'auto' else simulator}"
                                    " found: install ngspice (on PATH) or LTspice, or set "
                                    "LTSPICE_PATH")
        proj = state.get_active()
        if netlist_path is None:
            netlist_path = str(proj.path / "fab" / f"{proj.name}-spice.cir")
        nl_path = Path(netlist_path).expanduser().resolve()
        if not nl_path.is_file():
            raise FileNotFoundError(
                f"netlist not found at {nl_path}. Run export_spice_netlist first."
            )
        out = (Path(output_path).expanduser() if output_path
               else proj.path / "fab" / f"{proj.name}-spice-out.txt")
        out.parent.mkdir(parents=True, exist_ok=True)
        body = nl_path.read_text(encoding="utf-8", errors="replace")
        if sim.name == "ltspice":
            folder = out.parent / f"{out.stem}-ltspice"
            folder.mkdir(exist_ok=True)
            try:
                raw, log = sim.run(f"* kicad-claude SPICE driver\n{body}\n{analysis}\n.end\n",
                                   folder, timeout_seconds)
            except subprocess.TimeoutExpired as e:
                raise TimeoutError(f"LTspice timed out after {timeout_seconds}s") from e
            return {"simulator": "ltspice", "simulator_path": sim.path,
                    "driver_path": str(folder / "deck.cir"),
                    "raw_path": str(raw) if raw.is_file() else None,
                    "log_path": str(folder / "deck.log"), "analysis": analysis,
                    "log_tail": log[-2000:]}
        composed = ("* kicad-claude SPICE driver\n" + body
                    + f"\n{analysis}\n.print all\n.end\n")
        driver_path = out.with_suffix(".cir")
        driver_path.write_text(composed, encoding="utf-8")
        try:
            r = subprocess.run(
                [sim.path, "-b", str(driver_path), "-o", str(out)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"ngspice timed out after {timeout_seconds}s") from e
        return {
            "simulator": "ngspice",
            "returncode": r.returncode,
            "simulator_path": sim.path,
            "driver_path": str(driver_path),
            "output_path": str(out) if out.is_file() else None,
            "analysis": analysis,
            "stdout_tail": r.stdout[-2000:] if r.stdout else "",
            "stderr_tail": r.stderr[-2000:] if r.stderr else "",
        }

    @mcp.tool()

    @on_board
    def simulate_subcircuits(
        types: list[SubcircuitType] | None = None,
        fsw: dict[str, float] | None = None,
        freqs: list[float] | None = None,
        monte_carlo: int = 0,
        rail_voltages: dict[str, float] | None = None,
        peaking_warn_db: float = 6.0,
        loads: dict[str, float] | None = None,
        source_ohm: dict[str, float] | None = None,
        pcb_parasitics: bool = False,
        c_stray_pf: float | None = None,
        opamp_models: dict | None = None,
        supplies: dict | None = None,
        seed: int = 1,
        simulator: Literal["auto", "ngspice", "ltspice"] = "auto",
        netlist_path: str = "",
        include_decks: bool = False,
    ) -> dict:
        """Check the schematic's subcircuits against intent: dividers (ratio,
        worst case, FB Vout), RC corners, LC / pi ladders (SPICE AC: peaking,
        attenuation at `freqs`, `fsw` harmonics), op-amp stages (gain,
        bandwidth; `opamp_models`), crystal load caps. `monte_carlo=N`,
        `pcb_parasitics`. `SP-*` IDs. LC: ngspice or LTspice (`simulator`).

        Details:

        - `divider` — resistor dividers into an active pin. Ratio, tapped
          voltage (rails from `analyze_power_tree`), worst case from each
          resistor's `Tolerance` field; for a regulator FB divider, Vout typ
          and worst case using the datasheet Vref min / max from the spec
          cache (SP-DIV-VOUT), plus Monte Carlo with `monte_carlo=N`.
          SP-TOL when a resistor has no Tolerance field.
        - `rc` — RC low-pass nodes feeding an active input: corner frequency
          with the Thevenin resistance of every resistor on the node; and
          first-order high-pass nodes (a series cap into resistance to ground
          or a rail, no other DC path; feedback nodes skipped) in
          `rc_highpass`, with `loads` on the node in the corner (else
          "open").
        - `lc` — LC / ferrite / pi ladders (series L between nodes with
          capacitors to ground; switch nodes of converters excluded), with
          R-C damping branches, run as an ngspice AC sweep from the ladder's
          source end to its far end: resonance peaks (SP-LC-PEAK above
          `peaking_warn_db`) and attenuation (SP-LC-ATT) at `freqs` and at
          the 1st-3rd harmonic of every `fsw={"U1": 500e3}` value. Monte
          Carlo varies L and C within their Tolerance fields.
          `loads={"/VBUS_+20V": 20}` adds resistive loads and
          `source_ohm={"<input net>": 0.1}` a source resistance; with an open
          load the peaking is an upper bound and SP-LC-PEAK is only info.

        - `opamp` — op-amp stages from pin names (+IN / IN+ / 1IN- / OUTA / + / - …; an unnamed output pin is OUT):
          follower / non-inverting / inverting, gain and noise gain from the
          feedback resistors, feedback-cap pole, and closed-loop bandwidth
          GBW / noise gain when the spec cache has the op-amp's `GBW`
          (SP-OPAMP). With a vendor model — `opamp_models={"U1": {"lib":
          "OPA1612.lib", "subckt": "OPA1612", "pins": ["+IN", "-IN", "V+",
          "V-", "OUT"]}}` (roles in .subckt node order; omitted roles are
          read from node names), or the symbol's KiCAD `Sim.Library` /
          `Sim.Name` / `Sim.Pins` fields — each stage is simulated with
          ngspice AC on its real feedback network and supplies (rail
          voltages, or `supplies={"U1": {"V+": 15, "V-": -15}}`): gain,
          -3 dB frequency, peaking. Without one the amplifier is ideal and
          the result says so.
        - `crystal` — crystals with a load cap on each pin: effective load
          C1·C2/(C1+C2) + `c_stray_pf` (none assumed) against the crystal's
          `CL` from the spec cache (SP-XTAL-CL; warning beyond ±10 %).

        `pcb_parasitics=True` adds each ladder capacitor's mounting
        inductance in series with it: ground-via barrel from the cap's layer
        to the first ground plane (L = 0.2 h (ln(4h/d) + 1) nH) plus the
        pad-to-via strip over the plane (track width, or the pad width for
        a pour; Hammerstad microstrip inductance).

        Parts are ideal unless the datasheet spec cache gives ESR / ESL / DCR;
        ferrite beads without an inductance value are a short (noted). Open
        load, zero source impedance — the passive network, not the
        regulator loop. `lc` and vendor op-amp models need a simulator:
        `simulator` auto (ngspice, else LTspice), ngspice or ltspice; the
        result names the one used.
        """
        from kicad_claude.adapters import connectivity_review, power_review, spice_auto
        from kicad_claude.adapters import spice_backends
        from kicad_claude.adapters import datasheet_index as dsi
        from kicad_claude.adapters import datasheet_specs as dss
        from kicad_claude.tools.common import datasheet_values, project_netlist

        wanted = tuple(clean(types, spice_auto.TYPES))
        bad = [t for t in wanted if t not in spice_auto.TYPES]
        if bad:
            raise ValueError(f"unknown type(s) {bad}; valid: {list(spice_auto.TYPES)}")
        nl = power_review.parse_netlist(project_netlist(netlist_path))
        power = power_review.analyze(nl, datasheet=datasheet_values(nl),
                                     rail_voltages=rail_voltages or {})
        volts = {k: v["volts"] for k, v in power["rail_voltages"].items()}
        # datasheet Vref range per FB net
        vref: dict = {}
        proj = state.get_active_or_none()
        ds_dir = dsi.datasheets_dir(proj.path) if proj else None
        for r in power["regulators"]:
            div = (r.get("feedback") or {}).get("divider")
            mpn = (nl.components.get(r["ref"], {}).get("fields") or {}).get("MPN", "")
            rec = dss.load(ds_dir, mpn) if ds_dir and mpn else None
            if not div or not rec or rec.get("stale"):
                continue
            reg = rec.get("regulator") or {}
            chan = (reg.get("channels") or {}).get(r["channel"] or "") or {}
            key = "vref_negative" if power_review.is_negative_rail(div["sense_net"]) else "vref"
            sv = chan.get(key) or reg.get(key)
            if sv:
                vref[r["feedback"]["net"]] = {
                    "min": sv.get("min"), "typ": sv.get("typ"), "max": sv.get("max"),
                    "source": f"datasheet {mpn} p.{sv.get('page')}"}
        specs = {}
        if ds_dir:
            for ref, comp in nl.components.items():
                mpn = comp["fields"].get("MPN", "")
                if mpn and kind_is_passive(ref) or (mpn and _re_ic(ref)):
                    rec = dss.load(ds_dir, mpn)
                    if rec and not rec.get("stale"):
                        specs[ref] = rec
        f_list = list(freqs or [])
        for f in (fsw or {}).values():
            f_list += [f, 2 * f, 3 * f]
        mount = {}
        if pcb_parasitics and "lc" in wanted:
            from kicad_claude.adapters import sch_io as _sch_io
            board = _sch_io.parse_file(state.get_active_board_path())
            caps = [r for r in nl.components if r.startswith("C")]
            mount = {c: v["l_h"] for c, v in spice_auto.mounting_inductance(board, caps).items()}
        if opamp_models:
            proj_dir = proj.path if proj else Path.cwd()
            for m in opamp_models.values():
                if not Path(m["lib"]).is_absolute():
                    m["lib"] = str(proj_dir / m["lib"])
        result = spice_auto.analyze(
            nl, wanted, volts=volts, vref=vref, regulators=power["regulators"],
            sources=connectivity_review.sourced_nets(nl), freqs=sorted(set(f_list)),
            mc=monte_carlo, seed=seed, specs=specs, peaking_warn_db=peaking_warn_db,
            loads=loads or {}, source_ohm=source_ohm or {}, mount_l=mount,
            c_stray_pf=c_stray_pf, opamp_models=opamp_models or {}, supplies=supplies or {},
            sim=spice_backends.detect(simulator) or "")
        if not include_decks:
            for lad in result.get("ladders", []):
                lad.pop("deck", None)
        result["note"] = ("First-pass check of the passive networks with ideal parts unless "
                          "the spec cache has ESR / DCR; not a substitute for the vendor "
                          "models in a full converter simulation.")
        return result


def kind_is_passive(ref: str) -> bool:
    import re as _re
    return bool(_re.match(r"(L|C|FB|R)\d", ref))


def _re_ic(ref: str) -> bool:
    import re as _re
    return bool(_re.match(r"(U|IC|Y|X)\d", ref))
