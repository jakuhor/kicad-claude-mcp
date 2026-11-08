"""Design review tools (open_points_4 R7, phase P-C).

Tools:
    analyze_power_tree — regulators, rails, enable/PG sequencing and
                         datasheet-free power checks, from KiCAD's own netlist
    review_connectivity — IC power pins vs rails, decoupling per supply net,
                          open-drain pull-ups, I2C pull-ups / address strapping
"""

from __future__ import annotations

import logging
from pathlib import Path

from kicad_claude import state
from kicad_claude.adapters import connectivity_review, kicad_cli, power_review

logger = logging.getLogger("kicad-claude.tools.review")


def project_netlist(netlist_path: str = "") -> Path:
    """`netlist_path` if given, else a fresh kicadsexpr export of the active
    project to `fab/<name>-netlist.net`."""
    if netlist_path:
        p = Path(netlist_path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(p)
        return p
    proj = state.get_active()
    out = proj.path / "fab" / f"{proj.name}-netlist.net"
    out.parent.mkdir(parents=True, exist_ok=True)
    kicad_cli.export_netlist(proj.sch_path, out, fmt="kicadsexpr")
    return out


def _datasheet_values(nl) -> dict:
    """Per regulator name: Vref (+ negative) and ANY-OUT weights from
    `datasheets/specs/<MPN>.json`, each as (value, "datasheet MPN p.N (file)").
    Channel values (regulator.channels.<ch>) win over part-wide ones; stale
    records are ignored."""
    from kicad_claude.adapters import datasheet_index as dsi
    from kicad_claude.adapters import datasheet_specs as dss

    proj = state.get_active_or_none()
    if proj is None:
        return {}
    ds_dir = dsi.datasheets_dir(proj.path)
    out: dict = {}
    for reg in power_review.detect_regulators(nl):
        ref, ch = reg["ref"], reg["channel"]
        mpn = reg["comp"]["fields"].get("MPN", "")
        rec = dss.load(ds_dir, mpn) if mpn else None
        if not rec or rec.get("stale"):
            continue
        r = rec.get("regulator") or {}
        chan = (r.get("channels") or {}).get(ch) or {}

        def src(sv, mpn=mpn, rec=rec):
            return f"datasheet {mpn} p.{sv['page']} ({rec['pdf']['file']})"

        vals: dict = {}
        for key in ("vref", "vref_negative", "set_current"):
            sv = chan.get(key) or r.get(key)
            v = dss.value(sv)
            if v is not None:
                vals[key] = (v, src(sv))
        ao = chan.get("anyout") or r.get("anyout")
        if ao and dss.value(ao.get("base")) is not None:
            vals["anyout"] = {"base": (dss.value(ao["base"]), src(ao["base"])),
                              "pins": {n: (dss.value(sv), src(sv))
                                       for n, sv in ao["pins"].items()
                                       if dss.value(sv) is not None}}
        lim = {}
        absmax, rec_ = rec.get("absolute_max") or {}, rec.get("recommended") or {}
        for key, sv in (("abs_vin", absmax.get("VIN")), ("rec_vin", rec_.get("VIN")),
                        ("abs_vout", absmax.get("VOUT")), ("rec_vout", rec_.get("VOUT")),
                        ("abs_en", absmax.get("EN")),
                        ("en_on", chan.get("en_threshold_rising") or r.get("en_threshold_rising")),
                        ("dropout", chan.get("dropout") or r.get("dropout"))):
            if sv:
                lim[key] = (sv.get("min"), sv.get("typ"), sv.get("max"), src(sv))
        if lim:
            vals["limits"] = lim
        if vals:
            out[f"{ref}{'/ch' + ch if ch else ''}"] = vals
    return out


def _specs_by_ref(nl) -> dict:
    """{ref: spec record} for every part whose MPN has a fresh extraction."""
    from kicad_claude.adapters import datasheet_index as dsi
    from kicad_claude.adapters import datasheet_specs as dss

    proj = state.get_active_or_none()
    if proj is None:
        return {}
    ds_dir = dsi.datasheets_dir(proj.path)
    cache: dict = {}
    out = {}
    for ref, comp in nl.components.items():
        mpn = comp["fields"].get("MPN", "")
        if not mpn:
            continue
        if mpn not in cache:
            cache[mpn] = dss.load(ds_dir, mpn)
        rec = cache[mpn]
        if rec and not rec.get("stale"):
            out[ref] = rec
    return out


def register(mcp) -> None:
    @mcp.tool()
    def analyze_power_tree(
        vref: dict[str, float] | None = None,
        netlist_path: str = "",
        rail_voltages: dict[str, float] | None = None,
        include_details: bool = True,
    ) -> dict:
        """Review the power architecture of the active schematic (whole hierarchy).

        Built on `kicad-cli sch export netlist` (kicadsexpr), so connectivity is
        exactly KiCAD's. Returns:
          - `regulators`: one record per regulator channel — topology
            (switching / linear), input and output rail, enable state
            (`tied_to_input`, `divider_from_input`, `driven` by another part's
            pin, `tied_to_ground`, `floating`, …), power-good use, FB mode and
            divider (resistor values, ratio), SET resistor, switch-node
            inductor, input/output capacitors to ground;
          - `tree`: rails from each root through regulators and through series
            L / ferrite / 0 Ω links (`via`, flagged `dnp`) to the outputs, with
            power-input pin counts and connectors per rail;
          - `sequencing`: regulators whose EN is driven by another part (e.g.
            a PG output);
          - `findings` (error / warning / info) with rule ids `PT-*`.

        No datasheet constant is assumed from general knowledge. Vref comes
        from the caller (`vref={"U1": 0.8}` or per channel `{"U2/ch1": 1.25}`,
        `vref_source: "caller"`) or else from the project's extracted
        datasheet specs (`save_datasheet_extraction`; source names MPN, page
        and PDF; stale records are ignored). Without either, the FB ratio is
        reported with a `PT-VOUT-VREF` note.

        Datasheet limit checks (`PT-DS-*`, from extracted specs): input vs
        abs-max / recommended VIN, output vs VOUT range, LDO headroom vs
        dropout, EN pin voltage (divider / pull-up, PG released) vs EN abs max
        and turn-on threshold. Rail voltages come from computed Vouts carried
        through series links; pass the ones nothing regulates (an input such
        as a USB-PD contract) as `rail_voltages={"/VBUS_+20V": 20}`.

        `netlist_path`: use an existing kicadsexpr netlist instead of exporting.
        `include_details=False` drops the per-regulator capacitor lists.
        """
        nl = power_review.parse_netlist(project_netlist(netlist_path))
        result = power_review.analyze(nl, vref=vref or {},
                                      datasheet=_datasheet_values(nl),
                                      rail_voltages=rail_voltages or {})
        if not include_details:
            for r in result["regulators"]:
                r["input_caps"] = len(r["input_caps"])
                r["output_caps"] = len(r["output_caps"])
        result["note"] = ("Heuristic, pin-name driven review; confirm each finding "
                          "against the schematic and datasheet before acting.")
        return result

    @mcp.tool()
    def review_connectivity(
        checks: str = "power_pins,decoupling,open_drain,i2c,datasheet_pins,pin_abs_max,"
                      "cap_voltage",
        netlist_path: str = "",
        rail_voltages: dict[str, float] | None = None,
        cap_derating: float = 0.8,
    ) -> dict:
        """Datasheet-free connectivity review of the active schematic (whole
        hierarchy, from `kicad-cli sch export netlist`).

        `checks` (comma list, default all):
          - `power_pins`: each active part's `power_in` pins with the rail
            they sit on and how that rail is sourced (regulator, power_out
            pin, connector, or a series L / ferrite / R / diode / FET path).
            Findings: CR-PWR-NC (unconnected), CR-PWR-UNSRC (no source),
            CR-PWR-DNP (source only through a DNP part), CR-PWR-GND (supply
            pin on ground), CR-GND-RAIL (ground pin on a non-ground net),
            CR-GND-MIX (ground pins on several ground nets);
          - `decoupling`: capacitors to ground on every IC supply net
            (regulator VIN/VOUT are left to `analyze_power_tree`).
            CR-DEC-NONE when none is fitted. Netlist only — placement is
            `validate_decoupling_caps`;
          - `open_drain`: open-collector / open-emitter nets without a pull
            resistor or other driver (CR-OD-PULL);
          - `i2c`: buses from SDA/SCL pin names; pull-ups per line
            (CR-I2C-PULLUP, CR-I2C-RAIL) and identical parts with identical
            address-pin strapping (CR-I2C-ADDR). A part with a single fixed
            address in its extracted specs gets it (`address`), and different
            parts at one address are CR-I2C-ADDR too; the rest CR-I2C-UNRES;
          - `datasheet_pins` (needs extracted specs): symbol pin numbers and
            names against the datasheet pin table (CR-PIN-MISSING / -EXTRA /
            -NAME; exposed pads ignored), and pins the datasheet says to leave
            unconnected that share a net (CR-PIN-NC);
          - `pin_abs_max` (needs extracted specs): every pin matching an
            `absolute_max` key (VIN~IN, EN~EN/UV, CC~CC1 ...) against its net
            voltage — rail voltages from `analyze_power_tree` (datasheet
            Vouts, plus `rail_voltages` for unregulated inputs) and resistor
            dividers (CR-PIN-ABS);
          - `cap_voltage`: DC voltage across each fitted capacitor against its
            `Voltage` field (or a `/50V` suffix in Value): CR-CAP-V above the
            rating, CR-CAP-DERATE above `cap_derating` x rating. The rating is
            the schematic field, not the fitted MPN's datasheet.

        The pin -> rail map is returned so it can be checked against each
        datasheet; whether a rail is the *right* one is not judged here.
        """
        wanted = tuple(c.strip() for c in checks.split(",") if c.strip())
        bad = [c for c in wanted if c not in connectivity_review.CHECKS]
        if bad:
            raise ValueError(f"unknown check(s) {bad}; valid: {list(connectivity_review.CHECKS)}")
        nl = power_review.parse_netlist(project_netlist(netlist_path))
        specs = _specs_by_ref(nl)
        volts = {}
        if {"pin_abs_max", "cap_voltage"} & set(wanted):
            pt = power_review.analyze(nl, datasheet=_datasheet_values(nl),
                                      rail_voltages=rail_voltages or {})
            volts = {n: (v["volts"], v["source"]) for n, v in pt["rail_voltages"].items()}
        result = connectivity_review.analyze(nl, wanted, specs=specs, volts=volts,
                                             cap_derating=cap_derating)
        result["note"] = ("Heuristic, pin-name/pin-type driven review; confirm each "
                          "finding against the schematic and datasheet before acting.")
        return result
