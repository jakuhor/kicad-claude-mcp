"""Design review tools.

Tools:
    analyze_power_tree — regulators, rails, enable/PG sequencing and
                         datasheet-free power checks, from KiCAD's own netlist
    review_connectivity — IC power pins vs rails, decoupling per supply net,
                          open-drain pull-ups, I2C pull-ups / address strapping
"""

from __future__ import annotations

import logging
from typing import Literal

from kicad_claude.arg_types import CircuitGroup, ConnectivityCheck, DeratingProfile, clean
from kicad_claude.tools.common import datasheet_values, project_netlist, specs_by_ref
from kicad_claude.adapters import connectivity_review, power_review

logger = logging.getLogger("kicad-claude.tools.review.design")


def register(mcp) -> None:
    @mcp.tool()
    def analyze_power_tree(
        vref: dict[str, float] | None = None,
        netlist_path: str = "",
        rail_voltages: dict[str, float] | None = None,
        include_details: bool = True,
    ) -> dict:
        """Power architecture of the active schematic (whole hierarchy, KiCAD
        netlist): regulators (topology, rails, enable, FB divider, inductor,
        caps), the rail tree, sequencing and `PT-*` findings with datasheet
        limit checks. Vref from `vref={"U1": 0.8}` or extracted datasheet
        specs, never assumed. Pass unregulated inputs as
        `rail_voltages={"/VBUS": 20}`. `include_details=False` drops capacitor
        lists.

        Details:

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
                                      datasheet=datasheet_values(nl),
                                      rail_voltages=rail_voltages or {},
                                      specs=specs_by_ref(nl))
        if not include_details:
            for r in result["regulators"]:
                r["input_caps"] = len(r["input_caps"])
                r["output_caps"] = len(r["output_caps"])
        result["note"] = ("Heuristic, pin-name driven review; confirm each finding "
                          "against the schematic and datasheet before acting.")
        return result

    @mcp.tool()
    def review_circuits(
        groups: list[CircuitGroup] | None = None,
        rail_voltages: dict[str, float] | None = None,
        internal_connectors: list[str] | None = None,
        audio_f_low_hz: float = 20.0,
        fb_z_max_ohm: float = 100e3,
        i2c_mode: Literal["standard", "fast", "fast_plus"] = "fast",
        i2c_bus_capacitance_pf: dict[str, float] | None = None,
        source_ohm: float | None = None,
        inrush_limit_a: float | None = None,
        netlist_path: str = "",
    ) -> dict:
        """Circuit-level detectors on the active schematic (whole hierarchy) from
        pin names and types plus the datasheet spec cache. `groups` (default
        all): power, analog, digital, misc, interfaces, power_conversion,
        drives, rf; e.g. USB-C CC, ESD, op-amp bias, I2C pull-ups, LED limits,
        Ethernet magnetics, chargers, motor drivers, RF DC block (rule IDs in
        help). Rail voltages: `analyze_power_tree` plus `rail_voltages`.
        Heuristic: confirm each finding.

        Details:

        `groups` (default all):
          - `power`: USB Type-C CC termination (PW-USBC-CC: Rd 5.1 k per CC
            pin, Rp value vs its rail, a controller pin, CC1+CC2 on one
            resistor), D+ / D- of both orientations joined (PW-USBC-DATA),
            clamps on connector supply nets (PW-VBUS-TVS) and a ground pin
            (PW-CONN-GND), ESD clamps on external connector signal nets
            (PW-ESD; `internal_connectors="J2,J5"` excludes board-to-board
            and test connectors), reverse-polarity protection on connector
            supplies that reach ICs - series diode, drain-upstream FET or
            ideal-diode IC, found topology in `reverse_polarity`; USB and
            internal connectors skipped (PW-REVPOL), inrush (PW-INRUSH:
            capacitance before the first regulator; hot-plug peak V /
            `source_ohm` vs `inrush_limit_a`, the connector rating and a fuse
            in the path; USB VBUS above 10 uF, USB 2.0 7.2.4.1; soft-start
            charge C x Vout / `regulator.t_ss`; info without source_ohm),
            enable loops /
            self-enable (PW-SEQ-*), reset
            nets (PW-RESET), FB divider impedance (PW-FB-Z, above
            `fb_z_max_ohm`) and FB bias current error (PW-FB-IB, spec cache);
          - `analog`: op-amp inputs with no DC path (AU-BIAS), input coupling
            high-pass corner vs `audio_f_low_hz` (AU-HPF), output coupling cap
            to a connector without a bleed resistor (AU-BLEED), unused
            channels (AU-UNUSED); converter (ADC / DAC / codec) reference /
            VCOM caps (AD-VREF), analog supply decoupling (AD-SUPPLY), split
            grounds with no join (AD-GND), inputs without RC (AD-AAF); jacks
            driven straight from an output (HP-JACK); level shifters (LS-*);
          - `digital`: inputs with nothing driving them (DG-FLOAT; pins the
            datasheet says may stay open are skipped, DG-PINTYPE when the
            datasheet calls the pin an output), boot / mode straps (DG-BOOT),
            I2C pull-ups stronger than the `i2c_mode` sink (3 mA; 20 mA
            fast_plus; DG-I2C-RPU) and rise time 0.8473 R C against the mode
            limit (1000 / 300 / 120 ns, NXP UM10204; C = devices' extracted
            C_in + `i2c_bus_capacitance_pf={net: pF}`; info when unknown;
            DG-I2C-RISE), SPI chip
            selects shared / without pull-up (DG-SPI-CS), driver vs receiver
            supply (DG-LEVEL; UART TX -> RX through series resistors:
            DG-UART-LEVEL), branched clocks without series R
            (DG-CLK-TERM), USB data ESD (DG-USB-ESD), SWD pins reachable
            (DG-SWD);
          - `misc`: LEDs without a current limit (LE-NORES), LED driver set
            pins (LE-ISET), display charge-pump / VCOMH caps (DS-CAP), RTC
            backup pin (RTC-VBAT), buttons without pull-up (KB-PULL), switch
            matrices without diodes (KB-GHOST);
          - `interfaces`: Ethernet magnetics / Bob Smith / centre-tap caps
            (ETH-*), HDMI / DVI ESD, DDC pull-ups, HPD, +5V protection
            (HDMI-*), LVDS receiver termination (LVDS-TERM), CAN terminations
            (CAN-TERM: 120 Ohm or split 2 x 60 Ohm + cap; none with a
            connector / more than two) and CAN ESD (CAN-ESD), flash WP / HOLD
            and DDR VREF (MEM-*), radio-module antenna path and pi pad (WL-*);
          - `power_conversion`: charger set resistor and thermistor (CHG-*),
            BMS cell-sense RC and balancing (BMS-*), cell-protection sense
            (PROT-CS), opto + shunt-reference feedback (TF-*), series
            supercap balancing (SC-BAL), isolator grounds bridged / Y-caps /
            decoupling (ISO-*);
          - `drives`: motor-driver bootstrap, sense resistor, VM caps,
            freewheel diodes on discrete low-side switches (MD-*),
            thermocouple filter and RTD reference resistor (TC-*, RTD-*),
            addressable LED chains, decoupling, data level and series R
            (ALED-*);
          - `rf`: RF pins to connectors without DC block (RF-DCBLOCK),
            matching networks (RF-MATCH, RF-MATCH-OPEN) and the standards the
            detected circuits point to (CERT-*).

        Rail voltages come from `analyze_power_tree` plus `rail_voltages`.
        Heuristic: confirm each finding against the schematic and datasheet.
        """
        from kicad_claude.adapters import circuit_review

        wanted = tuple(clean(groups, circuit_review.GROUPS))
        bad = [g for g in wanted if g not in circuit_review.GROUPS]
        if bad:
            raise ValueError(f"unknown group(s) {bad}; valid: {list(circuit_review.GROUPS)}")
        nl = power_review.parse_netlist(project_netlist(netlist_path))
        pt = power_review.analyze(nl, datasheet=datasheet_values(nl),
                                  rail_voltages=rail_voltages or {})
        internal = set(clean(internal_connectors))
        return circuit_review.analyze(nl, pt, specs_by_ref(nl), wanted, internal,
                                      audio_f_low_hz, fb_z_max_ohm, i2c_mode,
                                      i2c_bus_capacitance_pf, source_ohm, inrush_limit_a)

    @mcp.tool()
    def review_part_ratings(
        loads: dict[str, float] | None = None,
        rail_voltages: dict[str, float] | None = None,
        efficiency: dict[str, float] | None = None,
        default_efficiency: float = 0.85,
        ambient_c: float | None = None,
        fsw: dict[str, float] | None = None,
        tj_margin_c: float | None = None,
        derating_profile: DeratingProfile = "commercial",
        sleep_states: dict[str, str] | None = None,
        netlist_path: str = "",
    ) -> dict:
        """Check parts against datasheet ratings with a per-rail current model.
        `loads={"+5VD": 0.5}` (A per net) is carried up the power tree with
        `efficiency` per regulator (else `default_efficiency`). Checks inductor
        Isat / Irms, CMC, connector, regulator IOUT / ILIM, Tj (`ambient_c`,
        default 40 C), FET / diode / LED currents (`RT-*` IDs, each citing the
        spec page). First-pass estimate.

        Details:

        `loads` is the current drawn from each rail in amps, by net name
        (`{"+5VD": 0.5, "/+18V": 0.15}`; a bare name matches the net's last
        path part). It is carried up the power tree: series links unchanged,
        linear regulators Iin = Iout, switchers by power with `efficiency`
        per regulator (`{"U1": 0.9}` or `{"U2/ch1": 0.8}`; others use
        `default_efficiency`, listed as assumed), and from each root rail
        through the series path (inductors, CMC winding, <= 1 Ohm resistors,
        FET dies, series diodes) to the power-entry connector.

        Checks, each citing the spec record's page: RT-L-ISAT (peak, with buck
        ripple from L and fsw - `fsw` per regulator or the datasheet's) and
        RT-L-IRMS for inductors; RT-CMC-I; RT-CONN-I (per contact);
        RT-REG-IOUT (recommended IOUT / current limit) and RT-REG-ILIM (buck
        inductor peak vs switch limit); RT-TJ (Ta + rth_ja P vs Tj max, for
        regulators - channels of one IC summed -, FETs I^2 RDS(on), series
        diodes, CCRs; `ambient_c`, default 40 C stated as assumed); RT-FET-ID,
        RT-D-IF; RT-LED (LED current from series R and VF or a CCR's I_reg vs
        IF max, CCR headroom); RT-FUSE (fuse / PTC I_hold, I_trip or
        I_rated / `Current` field vs the rail current); RT-OVERSPEC (info:
        cap >= 4x its voltage at >= 50 V, fuse >= 5x, regulator IOUT >= 10x
        the load); RT-MODEL (a regulator whose voltages are unknown);
        RT-NODATA (carries current, nothing extracted).

        `derating_profile` (commercial / industrial / automotive / military,
        policy values in `adapters/derating.py`) sets the Tj margin
        (`tj_margin_c` default: 25 / 25 / 30 / 40 C) and the resistor power
        fraction (50 %): RT-R-P compares each resistor's dissipation with its
        rating (extracted datasheet or `Power` field; never by package).

        `power_budget`: per rail voltage, current and power; per regulator
        Iin / Iout / loss; input current and total input power at the root
        rails. `sleep_current`: standby draw per rail - regulators' Iq (or
        I_shdn when `sleep_states={"U4": "off"}`, its rails then at 0 V) from
        the spec cache, and resistor currents leaving each rail in the sleep
        DC operating point (`sleep_states` also sets switches, `{"Q3":
        "off"}`); missing Iq in `nodata`.

        `dissipation`: every part that heats, largest first, with the method -
        regulators, FETs, diodes, CCRs from the checks above, resistors from
        I^2 R (rail series path, shunts) or dV^2 / R (DC operating point).
        Without an extracted rth_ja, Tj uses the package-typical value
        (`rth_source: package_typical (assumed)`, finding at most a warning).

        First-pass estimate: rth_ja is the datasheet's test board, efficiency
        is a number you supply, ripple is computed for bucks only.
        """
        from kicad_claude.adapters import part_ratings

        nl = power_review.parse_netlist(project_netlist(netlist_path))
        pt = power_review.analyze(nl, datasheet=datasheet_values(nl),
                                  rail_voltages=rail_voltages or {})
        result = part_ratings.check(nl, pt, specs_by_ref(nl), loads or {}, efficiency,
                                    default_efficiency, ambient_c, fsw, tj_margin_c,
                                    derating_profile, sleep_states)
        if not loads:
            result["note"] = ("No loads given: every current is zero except what the "
                              "schematic implies (LED strings). Pass loads={rail: amps}.")
        return result

    @mcp.tool()
    def review_connectivity(
        checks: list[ConnectivityCheck] | None = None,
        netlist_path: str = "",
        rail_voltages: dict[str, float] | None = None,
        cap_derating: float | None = None,
        derating_profile: DeratingProfile = "commercial",
        switch_states: Literal["auto", "all", "single", "both", "on", "off", "none"] = "auto",
        switch_overrides: dict[str, str] | None = None,
        max_switch_combos: int = 16,
    ) -> dict:
        """Datasheet-free connectivity review of the active schematic (KiCAD
        netlist, whole hierarchy). `checks` (default all):
        power_pins, decoupling, open_drain, i2c, datasheet_pins, pin_abs_max,
        cap_voltage (`CR-*` IDs). Net voltages from rails, `rail_voltages`,
        resistor networks and a DC model over transistor-switch scenarios
        (`switch_states`, `switch_overrides`). Returns the pin -> rail map.

        Details:

        `checks` (default all):
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
            dividers (CR-PIN-ABS; CR-PIN-DERATE beyond the derating
            profile's fraction of the limit: 90 / 85 / 85 / 80 %);
          - `cap_voltage`: DC voltage across each fitted capacitor against its
            rating: the fitted MPN's extracted datasheet (`absolute_max.V`)
            when there is one, else the `Voltage` field (or a `/50V` suffix in
            Value). CR-CAP-V above the rating, CR-CAP-DERATE above the
            derating fraction: `cap_derating` when given, else the
            `derating_profile`'s fraction for the dielectric family read from
            the symbol (ceramic / electrolytic / tantalum / unknown:
            commercial 50 / 80 / 80 / 50 %, industrial 50 / 75 / 70 / 50,
            automotive 50 / 70 / 60 / 50, military 40 / 60 / 50 / 40 - policy
            values, `adapters/derating.py`), CR-CAP-FIELD when the field
            disagrees with the datasheet.

        The pin -> rail map is returned so it can be checked against each
        datasheet; whether a rail is the *right* one is not judged here.

        Net voltages for `pin_abs_max` / `cap_voltage` come from the rails
        plus a DC operating-point model: every resistor network solved,
        datasheet pin models (bias current, internal clamp, trip current) and
        zener clamps, and transistor switches (FETs / BJTs with D-S-G or C-E-B
        pins) in each `switch_states` scenario: `all` (every on / off
        combination), `both` (all on, all off), `on`, `off`, `none` (switch
        nets left unknown), `single` (all on, all off, and each switch flipped
        alone: 2n + 2 cases); `auto` (default) is `all` up to
        `max_switch_combos` combinations, else `single`. On: the channel joins
        drain and source and the gate driver pulls to its on level (current
        limited by the pin's datasheet drive rating); off: body diode only
        (VSD from the spec cache), far side at 0 V, driver released or at its
        off level. Findings seen in only some scenarios are tagged with them;
        `pin_abs_max_worst` / `cap_voltage_worst` give each pin's voltage
        range over all scenarios. `switch_overrides={"Q3": "off"}` fixes single
        switches in every scenario. The solve adds DC-CLAMP /
        DC-TRIP (internal clamp current at its trip level, e.g. OVLO),
        DC-ZENER-P (zener power vs Ptot) and DC-DRIVE (gate-driver pin current).
        """
        wanted = tuple(clean(checks, connectivity_review.CHECKS))
        bad = [c for c in wanted if c not in connectivity_review.CHECKS]
        if bad:
            raise ValueError(f"unknown check(s) {bad}; valid: {list(connectivity_review.CHECKS)}")
        nl = power_review.parse_netlist(project_netlist(netlist_path))
        specs = specs_by_ref(nl)
        volts = {}
        if switch_states not in ("auto", "all", "single", "both", "on", "off", "none"):
            raise ValueError("switch_states: auto / all / single / both / on / off / none")
        need_v = {"pin_abs_max", "cap_voltage"} & set(wanted)
        if need_v:
            pt = power_review.analyze(nl, datasheet=datasheet_values(nl),
                                      rail_voltages=rail_voltages or {})
            volts = {n: (v["volts"], v["source"]) for n, v in pt["rail_voltages"].items()}
        if not need_v:
            result = connectivity_review.analyze(nl, wanted, specs=specs, volts=volts,
                                                 cap_derating=cap_derating, profile=derating_profile)
        else:
            import itertools

            from kicad_claude.adapters import dc_model
            sw = [r for r in dc_model.switches_in(nl) if r not in (switch_overrides or {})]
            mode = switch_states
            if mode == "auto":
                mode = "all" if sw and 2 ** len(sw) <= max_switch_combos else "single"
            if not sw:
                combos = [None]
            elif mode == "all":
                combos = [dict(zip(sw, st)) for st in itertools.product(("on", "off"),
                                                                        repeat=len(sw))]
            elif mode == "single":
                # all on, all off, and each switch flipped alone from both
                combos = [{r: "on" for r in sw}, {r: "off" for r in sw}]
                for s in sw:
                    combos.append({r: ("off" if r == s else "on") for r in sw})
                    combos.append({r: ("on" if r == s else "off") for r in sw})
                combos = list({tuple(sorted(c.items())): c for c in combos}.values())
            else:
                combos = {"both": [{r: "on" for r in sw}, {r: "off" for r in sw}],
                          "on": [{r: "on" for r in sw}], "off": [{r: "off" for r in sw}],
                          "none": [None]}[mode]
            base = [c for c in wanted if c not in need_v]
            result = connectivity_review.analyze(nl, tuple(base), specs=specs, volts=volts,
                                                 cap_derating=cap_derating, profile=derating_profile)
            findings = result["findings"]
            scen_out = {}
            seen: dict = {}
            worst_pin: dict = {}
            worst_cap: dict = {}
            sw_nets = {n for r in dc_model.switches_in(nl) for d in dc_model.switch_dies(nl, r)
                       for n in d["main"]}
            for combo in combos:
                swmap = dict(combo or {})
                swmap.update(switch_overrides or {})
                dc = dc_model.solve(nl, volts, specs, swmap)
                sv = {**dc["volts"], **volts} if swmap else {
                    **volts, **{n: v for n, v in dc["volts"].items() if n not in sw_nets}}
                part = connectivity_review.analyze(nl, tuple(need_v), specs=specs, volts=sv,
                                                   cap_derating=cap_derating, profile=derating_profile)
                part["findings"] += dc_model.findings(dc)
                if not swmap:
                    name = "as drawn"
                elif mode in ("both", "on", "off") and len(set(swmap.values())) == 1                         and not switch_overrides:
                    name = f"switches {next(iter(swmap.values()))}"
                else:
                    name = ", ".join(f"{r} {s}" for r, s in sorted(swmap.items()))
                rows = part.get("pin_abs_max") or []
                scen_out[name] = {"switches": swmap,
                                  "pin_abs_max_failing": [x for x in rows if not x["ok"]],
                                  "net_voltages": {n: round(v, 4) for n, (v, _) in
                                                   sorted(sv.items()) if n in sw_nets
                                                   or n not in volts and n in dc["volts"]}}
                for x in rows:
                    k = (x["ref"], x["pin"], x["limit"])
                    w = worst_pin.setdefault(k, {**{q: x[q] for q in ("ref", "pin", "net", "limit",
                                                                       "min", "max", "page")},
                                                 "v_low": x["volts"], "v_high": x["volts"],
                                                 "failing_in": []})
                    w["v_low"], w["v_high"] = min(w["v_low"], x["volts"]), max(w["v_high"],
                                                                               x["volts"])
                    if not x["ok"]:
                        w["failing_in"].append(name)
                for x in (part.get("cap_voltage") or {}).get("checked", []):
                    w = worst_cap.setdefault(x["ref"], {**x, "worst_in": name})
                    if x["use"] > w["use"]:
                        worst_cap[x["ref"]] = {**x, "worst_in": name}
                for f in part["findings"]:
                    key = (f["rule"], f["ref"], f["message"].split(" [")[0])
                    if key in seen:
                        seen[key]["scenarios"].append(name)
                    else:
                        f = {**f, "scenarios": [name]}
                        seen[key] = f
                        findings.append(f)
            for f in findings:
                sc = f.get("scenarios")
                if not (len(combos) > 1 and sc and len(sc) < len(combos)):
                    continue
                # the switch states every occurrence shares; if exactly the
                # scenarios with those states produce it, say "when ..."
                common = set.intersection(*(set(scen_out[s]["switches"].items()) for s in sc))
                match = [s for s, o in scen_out.items() if common <= set(o["switches"].items())]
                if common and sorted(match) == sorted(sc):
                    f["message"] += " [when " + ", ".join(f"{r} {s}" for r, s in
                                                          sorted(common)) + "]"
                else:
                    f["message"] += " [" + " | ".join(sc[:3]) + (
                        f" | +{len(sc) - 3} more" if len(sc) > 3 else "") + "]"
            result["switch_mode"] = mode
            result["scenarios"] = scen_out
            result["pin_abs_max_worst"] = sorted(worst_pin.values(),
                                                 key=lambda w: (not w["failing_in"], w["ref"]))
            result["cap_voltage_worst"] = sorted(worst_cap.values(), key=lambda w: -w["use"])
            order = {"error": 0, "warning": 1, "info": 2}
            findings.sort(key=lambda f: (order[f["severity"]], f["rule"], f["ref"]))
            result["counts"] = {k: sum(1 for f in findings if f["severity"] == k) for k in order}
        result["note"] = ("Heuristic, pin-name/pin-type driven review; confirm each "
                          "finding against the schematic and datasheet before acting.")
        return result
