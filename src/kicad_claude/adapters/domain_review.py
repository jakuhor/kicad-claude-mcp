"""Domain detectors, second set (OP-11): interfaces, power conversion,
drives & sensing, RF & certification hints.

Same rules as `circuit_review`: pin names and pin types, the datasheet spec
cache, no part-number keyword lists; every finding is a heuristic to
confirm. Scope follows kicad-happy 3.0.1 `domain_detectors.py` (MIT
License, Copyright (c) 2025 Andrew Klofas); the checks are this server's.

  interfaces        ETH-MAG (PHY MDI pins reach the RJ45 without magnetics),
                    ETH-BOBSMITH (no 75 Ohm + cap termination of the centre
                    taps / unused pairs), ETH-CT (PHY-side centre tap without
                    a cap); HDMI-ESD, HDMI-DDC (DDC without pull-ups),
                    HDMI-HPD, HDMI-5V; LVDS-TERM (receiver pair without 100
                    Ohm across it); MEM-WP / MEM-HOLD (flash protect / hold
                    pins floating), MEM-VREF (DDR VREF without divider / cap);
                    WL-ANT (radio module antenna pin not reaching an antenna
                    or connector), WL-MATCH (no pi pad on the antenna line);
  power_conversion  CHG-PROG (charge-current set pin without resistor),
                    CHG-TS (temperature-sense pin without thermistor),
                    BMS-RC (cell sense pins without RC filter), BMS-BAL;
                    PROT-CS (protection IC current-sense pin); TF-OPTO-R
                    (opto LED without series resistor), TF-431-BIAS (shunt
                    reference cathode without bias resistor), TF-COMP (no
                    compensation cap REF-K); SC-BAL (series supercaps without
                    balancing); ISO-BRIDGE (an isolator's two grounds joined
                    by a direct path), ISO-YCAP (Y-cap across the barrier),
                    ISO-DEC (a side's supply without decoupling);
  drives            MD-BOOT (bootstrap pin without cap to its phase node),
                    MD-SENSE (current-sense pin without resistor), MD-VM
                    (motor supply without bulk / decoupling), MD-FLYBACK
                    (low-side switch on an inductive load without freewheel
                    diode); TC-FILTER (thermocouple input without differential
                    cap), RTD-RREF (RTD converter without reference resistor /
                    a loose one); ALED-CHAIN, ALED-DEC, ALED-LEVEL, ALED-R
                    (addressable LED chains);
  rf                RF-DCBLOCK (RF pin of an active part to a connector without
                    a series cap), RF-MATCH (matching network found: topology
                    and values), RF-MATCH-OPEN (all-DNP matching pads);
                    CERT-* (standards the detected circuits point to).
"""

from __future__ import annotations

import re
from collections import defaultdict

from kicad_claude.adapters import circuit_review as cr
from kicad_claude.adapters.power_review import (
    Netlist,
    _two_terminal,
    is_ground,
    is_unconnected,
    kind,
    parse_value,
)

GROUPS = ("interfaces", "power_conversion", "drives", "rf")
_f = cr._f
pn = cr.pn
fitted = cr.fitted
parts_on = cr.parts_on


def _text(nl: Netlist, ref: str) -> str:
    c = nl.components.get(ref, {})
    return f"{c.get('lib_id', '')} {c.get('value', '')} {c.get('description', '')}"


def _pins(nl: Netlist, ref: str, with_nc: bool = False) -> dict[str, str]:
    return {pn(p): p.net for p in nl.pins.get(ref, []) if with_nc or not is_unconnected(p.net)}


def _reach(nl: Netlist, net: str, kinds=("R", "C", "L", "FB"), depth: int = 2,
           series_only: bool = True) -> dict[str, list[str]]:
    """Nets reachable from `net` through up to `depth` two-terminal parts of
    `kinds` (not to ground): {net: [refs on the way]}."""
    out = {net: []}
    frontier = [net]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for p in nl.nets.get(n, []):
                if kind(p.ref) not in kinds or not fitted(nl, p.ref):
                    continue
                o = cr.other_end(nl, p.ref, n)
                if o is None or (series_only and is_ground(o)) or o in out:
                    continue
                out[o] = out[n] + [p.ref]
                nxt.append(o)
        frontier = nxt
    return out


# ---- interfaces ------------------------------------------------------------- #

MDI = re.compile(r"^(TX[PN+-]|TXD?[+-]|TD[+-]|RX[PN+-]|RXD?[+-]|RD[+-]|MDI\d?[PN+-]|"
                 r"TRX\d[PN+-]|MX\d[PN+-]|TXO[PN]|RXI[PN])$")
RJ45 = re.compile(r"RJ45|8P8C|RJ-45|MagJack|Ethernet", re.I)
MAGNETIC_JACK = re.compile(r"MagJack|integrated magnetics|magnetics|HR9\d|LPJ\d|ARJ\d|J0G|"
                           r"J1B|RJMG", re.I)


def check_ethernet(nl: Netlist, findings: list) -> list[dict]:
    out = []
    jacks = [j for j in cr._connectors(nl) if RJ45.search(_text(nl, j))]
    for ref in cr.ics(nl):
        mdi = [p for p in nl.pins.get(ref, []) if MDI.match(pn(p)) and not is_unconnected(p.net)]
        if len(mdi) < 4:
            continue
        rec = {"phy": ref, "mdi_pins": [p.name for p in mdi], "path": {}}
        out.append(rec)
        for p in mdi:
            reach = _reach(nl, p.net, kinds=("R", "C", "L", "FB"), depth=2)
            hits = {n for n in reach for q in nl.nets.get(n, []) if q.ref in jacks}
            via_t = {q.ref for n in reach for q in nl.nets.get(n, [])
                     if kind(q.ref) in ("T", "TR") and fitted(nl, q.ref)}
            rec["path"][p.name] = {"transformer": sorted(via_t),
                                   "jack_direct": bool(hits)}
            if hits and not via_t and not any(MAGNETIC_JACK.search(_text(nl, j)) for j in jacks):
                findings.append(_f("error", "ETH-MAG", ref, f"MDI pin {p.name} ({p.net}) reaches "
                                   "the RJ45 without a transformer, and the jack is not marked as "
                                   "having integrated magnetics: no galvanic isolation (IEEE "
                                   "802.3 asks for 1500 V)."))
                break
        # PHY-side centre taps
        for t in sorted({q.ref for n in (p.net for p in mdi) for x in _reach(nl, n, depth=2)
                         for q in nl.nets.get(x, []) if kind(q.ref) in ("T", "TR")}):
            for q in nl.pins.get(t, []):
                if re.search(r"CT", pn(q)) and not is_unconnected(q.net) and not is_ground(q.net) \
                        and not cr.caps_to_ground(nl, q.net):
                    findings.append(_f("info", "ETH-CT", t, f"Transformer centre tap {q.name} "
                                       f"({q.net}) has no capacitor to ground."))
    for j in jacks:
        names = _pins(nl, j)
        bob = False
        for n in names.values():
            for r, o in parts_on(nl, n, "R"):
                v = parse_value(nl.components[r]["value"])
                if v and 49 <= v <= 76 and any(not is_ground(x) for x in [o]) and parts_on(
                        nl, o, "C"):
                    bob = True
        if not bob and not MAGNETIC_JACK.search(_text(nl, j)):
            findings.append(_f("info", "ETH-BOBSMITH", j, "No Bob Smith termination found (75 Ohm "
                               "per centre tap / unused pair into a high-voltage cap to chassis): "
                               "common-mode emissions and ESD return path."))
        out.append({"jack": j, "bob_smith": bob,
                    "integrated_magnetics": bool(MAGNETIC_JACK.search(_text(nl, j)))})
    return out


HDMI_TMDS = re.compile(r"^(TMDS)?_?(D[0-2]|DATA[0-2]|CK|CLK)[+-]$|^TMDS.*")


def check_hdmi(nl: Netlist, findings: list, specs: dict) -> list[dict]:
    out = []
    for j in cr._connectors(nl):
        names = _pins(nl, j)
        tmds = {k: v for k, v in names.items() if HDMI_TMDS.match(k)}
        has_hpd = any(k in ("HPD", "HOTPLUG", "HOT_PLUG_DETECT") for k in names)
        if len(tmds) < 6 and not (re.search(r"HDMI|DVI", _text(nl, j), re.I) and has_hpd):
            continue
        rec = {"ref": j, "tmds": sorted(tmds)}
        out.append(rec)
        bare = [k for k, n in tmds.items() if not cr.clamped(nl, n, specs)]
        if bare:
            findings.append(_f("warning", "HDMI-ESD", j, f"TMDS lines without an ESD clamp: "
                               f"{', '.join(sorted(bare)[:8])}."))
        for line in ("SCL", "SDA", "DDC_SCL", "DDC_SDA", "DDCCLK", "DDCDATA"):
            n = names.get(line)
            if n and not any(not is_ground(o) for _, o in parts_on(nl, n, "R")) and not any(
                    not is_ground(o) for x in _reach(nl, n, ("R",), 1) for _, o in
                    parts_on(nl, x, "R")):
                module = [q.ref for q in nl.nets.get(n, []) if q.ref != j
                          and cr.is_active(nl, q.ref) and kind(q.ref) not in ("U", "IC")]
                findings.append(_f("info" if module else "warning", "HDMI-DDC", j,
                                   f"DDC {line} ({n}) has no pull-up on this board (source: "
                                   "1.5-2.2 k to +5 V on the connector side)"
                                   + (f" - {module[0]} may provide it." if module else ".")))
        hpd = next((names[k] for k in ("HPD", "HOTPLUG", "HOT_PLUG_DETECT") if k in names), None)
        if hpd and not parts_on(nl, hpd, "R"):
            findings.append(_f("info", "HDMI-HPD", j, f"Hot-plug detect ({hpd}) without a "
                               "resistor (pull-down / series) - check the receiver side."))
        v5 = next((names[k] for k in ("+5V", "5V", "+5V_POWER", "5V_POWER") if k in names), None)
        if v5 and not any(kind(q.ref) in ("F", "FU", "PTC", "RT") or re.search(
                r"fuse|polyfuse|PTC", _text(nl, q.ref), re.I) for q in nl.nets.get(v5, [])):
            findings.append(_f("info", "HDMI-5V", j, "+5V pin without a fuse / PTC (HDMI source "
                               "must limit the +5V current to 0.5 A)."))
    return out


def _pairs(nl: Netlist, ref: str) -> list[tuple[str, str, str]]:
    """(base name, P net, N net) from pin names ..P/..N, ..+/..-."""
    pins = {pn(p): p for p in nl.pins.get(ref, []) if not is_unconnected(p.net)}
    out = []
    for name, p in pins.items():
        m = re.fullmatch(r"(.+?)(_?P|\+)", name)
        if not m:
            continue
        base = m.group(1)
        for neg in (base + "_N", base + "N", base + "-", base + "M"):
            if neg in pins and m.group(2) in ("_P", "P", "+"):
                out.append((base, p.net, pins[neg].net))
                break
    return out


LVDS_HINT = re.compile(r"LVDS|SERDES|SERIALIZER|DESERIALIZER|FPD-?LINK|DS90|SN65LVDS", re.I)


def check_lvds(nl: Netlist, findings: list, specs: dict) -> list[dict]:
    out = []
    for ref in cr.ics(nl):
        if not (LVDS_HINT.search(_text(nl, ref)) or (specs.get(ref) or {}).get("category") ==
                "interface" and "LVDS" in (specs.get(ref) or {}).get("description", "").upper()):
            continue
        pairs = _pairs(nl, ref)
        rx = [(b, a, c) for b, a, c in pairs
              if any(pn(p) in (b + "P", b + "_P", b + "+") and cr.btype(p) == "input"
                     for p in nl.pins.get(ref, []))]
        rec = {"ref": ref, "pairs": [b for b, _, _ in pairs], "receiver_pairs": [b for b, _, _ in rx]}
        out.append(rec)
        for b, a, c in rx:
            term = [r for r, o in parts_on(nl, a, "R") if o == c]
            if not term:
                findings.append(_f("info", "LVDS-TERM", ref, f"Receiver pair {b} ({a} / {c}) has "
                                   "no resistor across it: needs 100 Ohm at the receiver unless "
                                   "the part terminates internally (check the datasheet)."))
            elif not any(80 <= (parse_value(nl.components[r]["value"]) or 0) <= 120 for r in term):
                findings.append(_f("warning", "LVDS-TERM", term[0], f"Termination across {b} is "
                                   f"{nl.components[term[0]]['value']}, not ~100 Ohm."))
    return out


def check_memory(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for ref in cr.ics(nl):
        names = _pins(nl, ref)
        spi_flash = {"CS", "SCK", "CLK"} & set(names) and ({"SI", "SO", "DI", "DO", "IO0", "IO1",
                                                             "DQ0", "DQ1"} & set(names))
        ddr = sum(1 for k in names if re.fullmatch(r"DQ\d+", k)) >= 8 and (
            {"RAS", "CAS", "WE"} & set(names) or any(k.startswith("BA") for k in names))
        if not (spi_flash or ddr):
            continue
        rec = {"ref": ref, "type": "DDR / parallel DRAM" if ddr else "serial flash / SRAM"}
        out.append(rec)
        for p in nl.pins.get(ref, []):
            name = pn(p)
            if name in ("WP", "W", "HOLD", "RESET", "IO2", "IO3") and spi_flash:
                if is_unconnected(p.net):
                    if name in ("IO2", "IO3"):
                        continue
                    findings.append(_f("warning", "MEM-HOLD" if name == "HOLD" else "MEM-WP", ref,
                                       f"{p.name} is unconnected: tie it high (or drive it) - a "
                                       "floating write-protect / hold pin blocks writes or pauses "
                                       "the bus at random."))
                elif not cr.has_driver(nl, p.net, ref):
                    findings.append(_f("warning", "MEM-WP", ref, f"{p.name} ({p.net}) is not "
                                       "driven or pulled."))
            if ddr and name.startswith("VREF") and not is_unconnected(p.net):
                rs = parts_on(nl, p.net, "R")
                if not cr.caps_to_ground(nl, p.net) or (not rs and not cr.has_driver(nl, p.net, ref)):
                    findings.append(_f("warning", "MEM-VREF", ref, f"{p.name} ({p.net}): VREF "
                                       "needs a VDDQ/2 source (divider or regulator) and a cap."))
    return out


WL_HINT = re.compile(r"RF_Module|ESP32|ESP8266|nRF5|CC2\d\d\d|BLE|WiFi|Wi-Fi|LoRa|SX12\d\d|"
                     r"RFM9\d|GNSS|GPS|LTE|NB-IoT|BG9\d|SARA|Bluetooth|Zigbee|Thread", re.I)
ANT_PIN = re.compile(r"^(ANT\w*|RF_?(IN|OUT|IO|ANT)?|ANTENNA|RFIO\w*|AE\d?)$")


def check_wireless(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for ref in sorted(set(cr.ics(nl)) | {r for r in nl.components if kind(r) in ("M", "MOD")
                                         and fitted(nl, r)}):
        ant = [p for p in nl.pins.get(ref, []) if ANT_PIN.match(pn(p))]
        if not ant or not (WL_HINT.search(_text(nl, ref)) or ant):
            continue
        rec = {"ref": ref, "antenna_pins": [p.name for p in ant], "path": {}}
        out.append(rec)
        for p in ant:
            if is_unconnected(p.net):
                continue
            reach = _reach(nl, p.net, kinds=("R", "C", "L"), depth=3)
            ends = {q.ref for n in reach for q in nl.nets.get(n, [])
                    if kind(q.ref) in cr.CONN + ("AE", "ANT", "E")}
            shunts = {r for n in reach for k in ("C", "L") for r, o in parts_on(nl, n, k)
                      if is_ground(o)}
            series = {r for path in reach.values() for r in path}
            rec["path"][p.name] = {"antenna_or_connector": sorted(ends),
                                   "series": sorted(series), "shunt": sorted(shunts)}
            if not ends:
                findings.append(_f("warning", "WL-ANT", ref, f"Antenna pin {p.name} ({p.net}) "
                                   "reaches no antenna or RF connector within 3 parts."))
            elif not shunts:
                findings.append(_f("info", "WL-MATCH", ref, f"No pi pad (shunt parts) on the "
                                   f"{p.name} line: no room to tune the antenna match."))
    return out


# ---- power conversion ------------------------------------------------------- #

CHG_HINT = re.compile(r"charger|charge|Li-?ion|LiPo|BQ2\d|MCP738|TP4056|LTC40\d\d|MAX1\d\d\d",
                      re.I)
NTC = re.compile(r"NTC|thermistor|PTC", re.I)


def check_chargers(nl: Netlist, findings: list, specs: dict) -> list[dict]:
    out = []
    for ref in cr.ics(nl):
        names = _pins(nl, ref)
        cat = (specs.get(ref) or {}).get("category", "")
        bat = any(re.fullmatch(r"V?BAT\w*|BATT?\+?", k) for k in names)
        all_pins = _pins(nl, ref, with_nc=True)
        prog = [k for k in all_pins if re.fullmatch(
            r"PROG\d?|ISET\d?|IPRG|RPROG|ICHG|ILIM|ISET_?\w*", k)]
        cells = sorted(k for k in names if re.fullmatch(r"(VC|CELL|VCELL)\d{1,2}", k))
        if len(cells) < 3 and bat:      # "C1..Cn" counts only on a part with a battery pin
            cells = sorted(k for k in names if re.fullmatch(r"C\d{1,2}", k))
        if len(cells) >= 3 and (bat or cat in ("bms", "battery_monitor") or re.search(
                r"BMS|cell|AFE|battery", _text(nl, ref), re.I) or any(k.startswith("VC")
                                                                     for k in cells)):
            rec = {"ref": ref, "type": "BMS / cell monitor", "cell_pins": cells}
            out.append(rec)
            no_rc = [k for k in cells if not (parts_on(nl, names[k], "R") and
                                              cr.caps_to_ground(nl, names[k]) +
                                              [c for c, _ in parts_on(nl, names[k], "C")])]
            if no_rc:
                findings.append(_f("info", "BMS-RC", ref, "Cell sense pins without a series R + "
                                   f"cap filter: {', '.join(no_rc[:8])}."))
            bal = [k for k in names if re.fullmatch(r"(CB|BAL|S)\d{1,2}", k)]
            if bal and not any(parts_on(nl, names[k], "R") for k in bal):
                findings.append(_f("info", "BMS-BAL", ref, "Balance pins without balancing "
                                   "resistors."))
            continue
        if not (bat and (prog or CHG_HINT.search(_text(nl, ref)) or cat == "charger")):
            # single-cell protection (DW01-style): OD / OC / CS
            if {"OD", "OC"} <= set(names) and "CS" in names:
                rec = {"ref": ref, "type": "cell protection"}
                out.append(rec)
                if not parts_on(nl, names["CS"], "R"):
                    findings.append(_f("info", "PROT-CS", ref, "Current-sense pin CS without its "
                                       "series resistor (check the datasheet circuit)."))
            continue
        rec = {"ref": ref, "type": "charger", "prog_pins": prog}
        out.append(rec)
        for k in prog:
            rs = [] if is_unconnected(all_pins[k]) else parts_on(nl, all_pins[k], "R")
            rec.setdefault("prog_resistors", []).extend(r for r, _ in rs)
            if not rs:
                findings.append(_f("error", "CHG-PROG", ref, f"Charge-current set pin {k} has no "
                                   "resistor: the charge current is undefined."))
        ts = next((names[k] for k in ("TS", "NTC", "THERM", "TEMP", "TH") if k in names), None)
        if ts is not None:
            therm = [q.ref for q in nl.nets.get(ts, []) if kind(q.ref) in ("TH", "RT", "NTC")
                     or (kind(q.ref) == "R" and NTC.search(_text(nl, q.ref)))]
            rec["thermistor"] = therm
            if not therm:
                findings.append(_f("info", "CHG-TS", ref, f"Temperature-sense pin ({ts}) has no "
                                   "thermistor: charging is not temperature-qualified (JEITA)."))
    return out


OPTO = re.compile(r"opto|PC817|LTV|TLP\d|EL817|CNY17|HCPL|SFH6", re.I)


def check_feedback_isolated(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for ref in sorted(nl.components):
        if not fitted(nl, ref):
            continue
        names = _pins(nl, ref)
        if kind(ref) in ("U", "IC", "OK") and ({"A", "K", "C", "E"} <= set(names)) and OPTO.search(
                _text(nl, ref)):
            a, k = names["A"], names["K"]
            series = parts_on(nl, a, "R") + parts_on(nl, k, "R")
            out.append({"ref": ref, "type": "optocoupler", "led_series_r": [r for r, _ in series]})
            if not series and not any(kind(q.ref) in ("U", "IC") and q.ref != ref
                                      for n in (a, k) for q in nl.nets.get(n, [])):
                findings.append(_f("warning", "TF-OPTO-R", ref, "Optocoupler LED without a series "
                                   "resistor or driver: its current is not limited."))
        if {"REF", "K", "A"} <= set(names) and kind(ref) in ("U", "IC", "D", "Q", "VR"):
            k, rf = names["K"], names["REF"]
            bias = [r for r, o in parts_on(nl, k, "R") if not is_ground(o)]
            comp = [c for c, o in parts_on(nl, k, "C") if o == rf] + \
                   [c for c, o in parts_on(nl, rf, "C")
                    for x in [o] if any(o2 == k for _, o2 in parts_on(nl, x, "R"))]
            out.append({"ref": ref, "type": "shunt reference", "bias": bias, "comp": comp})
            opto_on_k = any(OPTO.search(_text(nl, q.ref)) for q in nl.nets.get(k, []))
            if not bias and not opto_on_k:
                findings.append(_f("warning", "TF-431-BIAS", ref, "Shunt reference cathode with "
                                   "no bias resistor: below its minimum cathode current it does "
                                   "not regulate."))
            if opto_on_k and not comp:
                findings.append(_f("info", "TF-COMP", ref, "Shunt reference in an opto feedback "
                                   "loop without a REF-K compensation cap."))
    return out


def check_supercaps(nl: Netlist, findings: list) -> list[dict]:
    caps = []
    for ref, c in nl.components.items():
        if kind(ref) != "C" or not fitted(nl, ref):
            continue
        v = parse_value(c["value"].replace("F", ""))
        if (v is not None and v >= 0.1) or re.search(r"supercap|EDLC|ultracap", _text(nl, ref), re.I):
            caps.append(ref)
    out = []
    on_net = defaultdict(list)
    for ref in caps:
        for n in _two_terminal(nl, ref) or ():
            on_net[n].append(ref)
    for net, cs in sorted(on_net.items()):
        if len(cs) < 2 or is_ground(net) or any(
                cr.btype(q) in ("power_in", "power_out") for q in nl.nets.get(net, [])):
            continue
        balanced = bool(parts_on(nl, net, "R")) or any(
            kind(q.ref) in ("U", "IC") for q in nl.nets.get(net, []))
        out.append({"mid_node": net, "caps": cs, "balanced": balanced})
        if not balanced:
            findings.append(_f("warning", "SC-BAL", cs[0], f"Supercaps {', '.join(cs)} in series "
                               f"(mid node {net}) without balancing: leakage mismatch over-volts "
                               "one cell."))
    return out


def check_isolation(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for ref in cr.ics(nl):
        names = _pins(nl, ref)
        g1 = next((names[k] for k in ("GND1", "GNDA", "VSS1", "GND_1", "GNDI") if k in names), None)
        g2 = next((names[k] for k in ("GND2", "GNDB", "VSS2", "GND_2", "GNDO", "GNDISO")
                   if k in names), None)
        if not g1 or not g2:
            continue
        rec = {"ref": ref, "grounds": [g1, g2]}
        out.append(rec)
        if g1 == g2:
            findings.append(_f("error", "ISO-BRIDGE", ref, f"Both sides of the isolator share "
                               f"ground {g1}: no isolation."))
            continue
        # direct joins through low-ohm R / L / net tie / jumper; Y-caps reported
        seen, frontier, path = {g1}, [g1], None
        while frontier and not path:
            n = frontier.pop()
            for p in nl.nets.get(n, []):
                k = kind(p.ref)
                if not fitted(nl, p.ref):
                    continue
                v = parse_value(nl.components[p.ref]["value"]) if k == "R" else None
                if k in ("L", "FB", "NT", "JP", "SJ") or (k == "R" and v is not None and v < 1e3):
                    o = cr.other_end(nl, p.ref, n)
                    if o == g2:
                        path = p.ref
                        break
                    if o and o not in seen:
                        seen.add(o)
                        frontier.append(o)
        if path:
            findings.append(_f("error", "ISO-BRIDGE", ref, f"Grounds {g1} and {g2} of the isolator "
                               f"are joined through {path}: the barrier is bypassed."))
        ycap = [c for c, o in parts_on(nl, g1, "C") if o == g2]
        if ycap:
            rec["y_caps"] = ycap
            findings.append(_f("info", "ISO-YCAP", ref, f"Y-cap {', '.join(ycap)} across the "
                               "barrier: check its rated voltage (Y1 / Y2 class) and the leakage "
                               "it allows."))
        for k in ("VDD1", "VDD2", "VCC1", "VCC2", "VDDA", "VDDB", "VISO"):
            if k in names and not is_ground(names[k]) and not cr.caps_to_ground(nl, names[k]) \
                    and not [c for c, o in parts_on(nl, names[k], "C") if o in (g1, g2)]:
                findings.append(_f("warning", "ISO-DEC", ref, f"Isolator supply {k} "
                                   f"({names[k]}) has no decoupling cap to its side's ground."))
    return out


# ---- drives & sensing ------------------------------------------------------- #

MOTOR_CONN = re.compile(r"motor|M\d?\b|stepper|BLDC|solenoid|relay|coil", re.I)


def check_motor_drivers(nl: Netlist, findings: list, reg_refs: set | None = None) -> list[dict]:
    out = []
    for ref in cr.ics(nl):
        if ref in (reg_refs or set()):
            continue          # a buck / boost controller's GH / GL / BOOT, not a motor bridge
        names = _pins(nl, ref)
        outs = [k for k in names if re.fullmatch(r"([AB]?OUT\d?|OUT[AB]\d?|[UVW]|SH[ABC]?|"
                                                 r"PHASE\w*|SW[ABC])", k)]
        gate = [k for k in names if re.fullmatch(r"G[HL][ABC]?|[HL]O\d?|GH\d|GL\d", k)]
        vm = next((names[k] for k in ("VM", "VMOT", "VBB", "VS", "PVDD") if k in names), None)
        boots = [k for k in names if re.fullmatch(r"BOOT\w*|CBOOT|BST\w*|VB\d?|BS[ABC]?", k)]
        phases = {re.sub(r"^(G[HL]|[HL]O|SH|BS|BST|BOOT|VB)", "", k) for k in gate + boots}
        if not ((len(outs) >= 2 and vm) or (gate and boots and len(phases - {""}) >= 2)):
            continue          # one phase of GH / GL / BOOT is a synchronous buck controller
        rec = {"ref": ref, "outputs": outs, "gate_pins": gate, "vm": vm, "bootstrap": boots}
        out.append(rec)
        for b in boots:
            caps = [o for c, o in parts_on(nl, names[b], "C")]
            if not caps:
                findings.append(_f("error", "MD-BOOT", ref, f"Bootstrap pin {b} ({names[b]}) has "
                                   "no capacitor: the high-side gate cannot be driven."))
            elif all(is_ground(o) for o in caps):
                findings.append(_f("warning", "MD-BOOT", ref, f"Bootstrap cap on {b} goes to "
                                   "ground, not to the phase / switch node."))
        for k in names:
            if re.fullmatch(r"(I?SEN(SE)?\w*|ISEN\w*|RSENSE\w*|CS[ABC]?|ISNS\w*)", k) \
                    and not is_ground(names[k]) and not parts_on(nl, names[k], "R"):
                findings.append(_f("warning", "MD-SENSE", ref, f"Current-sense pin {k} "
                                   f"({names[k]}) has no sense resistor."))
        if vm and not cr.caps_to_ground(nl, vm):
            findings.append(_f("warning", "MD-VM", ref, f"Motor supply {vm} has no capacitor to "
                               "ground: bulk + ceramic close to the driver absorb the recirculating "
                               "current."))
    # discrete low-side switch on an inductive load
    for q in sorted(r for r in nl.components if kind(r) == "Q" and fitted(nl, r)):
        from kicad_claude.adapters.dc_model import switch_dies
        for d in switch_dies(nl, q):
            dn, sn = d["main"]
            if not is_ground(sn):
                continue
            loads = [p.ref for p in nl.nets.get(dn, []) if (kind(p.ref) in ("L", "K", "M") or (
                kind(p.ref) in cr.CONN and MOTOR_CONN.search(_text(nl, p.ref))))
                and fitted(nl, p.ref)]
            if not loads:
                continue
            flyback = [p.ref for p in nl.nets.get(dn, []) if kind(p.ref) == "D" and fitted(nl, p.ref)]
            out.append({"switch": q, "load": loads, "freewheel": flyback, "discrete": True})
            if not flyback:
                findings.append(_f("warning", "MD-FLYBACK", q, f"Low-side switch {q} drives an "
                                   f"inductive load ({', '.join(loads)}) on {dn} with no freewheel "
                                   "diode: the turn-off spike hits the drain."))
    return out


def check_temperature_sensing(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for ref in cr.ics(nl):
        names = _pins(nl, ref)
        tc = [k for k in names if re.fullmatch(r"T[+-]|TC[+-]|THERM[+-]|TCP|TCN|T_?[PN]", k)]
        rtd_ref = [k for k in names if re.fullmatch(r"REFIN[+-]|RREF[+-]?|REF[+-]", k)]
        rtd = [k for k in names if re.fullmatch(r"RTDIN[+-]|FORCE[+-]?|FORCE2|RTD\w*", k)]
        if len(tc) >= 2:
            p, n = (names.get(k) for k in sorted(tc)[:2])
            rec = {"ref": ref, "type": "thermocouple", "pins": tc}
            out.append(rec)
            if not [c for c, o in parts_on(nl, p, "C") if o == n]:
                findings.append(_f("info", "TC-FILTER", ref, "Thermocouple inputs without a "
                                   "differential cap across them (and to ground): RF pickup on "
                                   "the long sensor leads."))
        if rtd:
            rec = {"ref": ref, "type": "RTD", "pins": rtd, "ref_pins": rtd_ref}
            out.append(rec)
            if len(rtd_ref) >= 2:
                a, b = names[rtd_ref[0]], names[rtd_ref[1]]
                rr = [r for r, o in parts_on(nl, a, "R") if o == b]
                rec["rref"] = rr
                if not rr:
                    findings.append(_f("error", "RTD-RREF", ref, "No reference resistor across "
                                       f"{rtd_ref[0]} / {rtd_ref[1]}: the RTD ratio has no "
                                       "reference."))
                else:
                    tol = nl.components[rr[0]]["fields"].get("Tolerance", "")
                    m = re.search(r"([\d.]+)\s*%", tol)
                    if not m or float(m.group(1)) > 0.1:
                        findings.append(_f("warning", "RTD-RREF", rr[0], f"Reference resistor "
                                           f"tolerance {tol or 'not stated'}: its error goes "
                                           "straight into the temperature (0.1 % or better)."))
    return out


def check_addressable_leds(nl: Netlist, findings: list, volts: dict) -> list[dict]:
    leds = []
    for ref, c in nl.components.items():
        if not fitted(nl, ref):
            continue
        names = _pins(nl, ref, with_nc=True)
        din = next((k for k in ("DIN", "DI", "SDI") if k in names), None)
        dout = next((k for k in ("DOUT", "DO", "SDO") if k in names), None)
        ci = "CI" in names or "CKI" in names
        if din and dout and (kind(ref) in ("D", "LED", "U") or ci) and any(
                k in names for k in ("VDD", "VCC", "V+", "VDD5")):
            leds.append((ref, names, din, dout))
    if not leds:
        return []
    by_din = {names[din]: ref for ref, names, din, dout in leds}
    out = []
    firsts = [ref for ref, names, din, dout in leds
              if not any(names[din] == n2[d2] for r2, n2, _, d2 in leds if r2 != ref)]
    for ref, names, din, dout in leds:
        vdd = next(names[k] for k in ("VDD", "VCC", "V+", "VDD5") if k in names)
        nxt = by_din.get(names[dout])
        if not cr.caps_to_ground(nl, vdd) and not [c for c, o in parts_on(nl, vdd, "C")]:
            findings.append(_f("info", "ALED-DEC", ref, "Addressable LED without a local cap "
                               "on its supply."))
        out.append({"ref": ref, "din": names[din], "dout": names[dout], "next": nxt})
    for ref in firsts:
        names = next(n for r, n, _, _ in leds if r == ref)
        din = next(k for k in ("DIN", "DI", "SDI") if k in names)
        net = names[din]
        series = parts_on(nl, net, "R")
        src_net = series[0][1] if series else net
        drv = [p for p in nl.nets.get(src_net, []) if cr.btype(p) in ("output", "bidirectional")
               and kind(p.ref) in ("U", "IC")]
        if not series:
            findings.append(_f("info", "ALED-R", ref, f"First LED data input {net} has no series "
                               "resistor (33-470 Ohm damps ringing and protects the input)."))
        vdd = next(names[k] for k in ("VDD", "VCC", "V+", "VDD5") if k in names)
        v_led = volts.get(vdd)
        for d in drv:
            v_drv = cr.supply_v(nl, d.ref, volts)
            if v_led and v_drv and v_drv < 0.7 * v_led:
                findings.append(_f("warning", "ALED-LEVEL", ref, f"Data from {d.ref} at "
                                   f"{v_drv:g} V into an LED supplied at {v_led:g} V: VIH is "
                                   f"0.7 VDD = {0.7 * v_led:.2g} V - add a level shifter or run "
                                   "the first LED from a lower supply."))
    chain = len(out)
    out.append({"chain_starts": firsts, "leds": chain})
    if len(firsts) > 1:
        findings.append(_f("info", "ALED-CHAIN", ",".join(firsts), f"{len(firsts)} separate "
                           "addressable LED chains (or a broken DOUT -> DIN link)."))
    return out


# ---- RF and certification --------------------------------------------------- #

RF_PIN = re.compile(r"^(RF_?(IN|OUT|IO|P|N)?\d?|RFIN\w*|RFOUT\w*|ANT\w*|LNA_?(IN|OUT)|PA_?OUT|"
                    r"TX_?RF|RX_?RF)$")
RF_CONN = re.compile(r"SMA|U\.?FL|IPEX|MMCX|MCX|BNC|N-type|Coax|RF", re.I)


def check_rf(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for ref in sorted(set(cr.ics(nl)) | {r for r in nl.components if kind(r) in ("M", "MOD")}):
        for p in nl.pins.get(ref, []):
            if not RF_PIN.match(pn(p)) or is_unconnected(p.net):
                continue
            reach = _reach(nl, p.net, kinds=("C", "L", "R"), depth=4)
            conns = {q.ref for n in reach for q in nl.nets.get(n, [])
                     if kind(q.ref) in cr.CONN + ("AE", "ANT") and (RF_CONN.search(
                         _text(nl, q.ref)) or kind(q.ref) in ("AE", "ANT"))}
            series = sorted({r for path in reach.values() for r in path})
            shunts = sorted({r for n in reach for k in ("C", "L") for r, o in parts_on(nl, n, k)
                             if is_ground(o)})
            rec = {"ref": ref, "pin": p.name, "net": p.net, "to": sorted(conns),
                   "series": series, "shunt": shunts}
            out.append(rec)
            # matching topology from shunt / series counts near the pin
            ns, nsh = len([s for s in series if kind(s) in ("C", "L")]), len(shunts)
            topo = ("pi" if nsh >= 2 and ns >= 1 else "T" if ns >= 2 and nsh >= 1 else
                    "L" if ns >= 1 and nsh >= 1 else None)
            if topo:
                vals = {r: nl.components[r]["value"] for r in series + shunts}
                dnp = [r for r in series + shunts if nl.components[r].get("dnp")]
                rec.update(matching=topo, values=vals, dnp=dnp)
                findings.append(_f("info", "RF-MATCH", ref, f"{topo}-match on {p.name}: "
                                   + ", ".join(f"{r} {v}" for r, v in vals.items()) + "."))
                if dnp and len(dnp) == len(series + shunts):
                    findings.append(_f("warning", "RF-MATCH-OPEN", ref, f"All matching parts on "
                                       f"{p.name} are DNP: the RF path is open."))
            if conns and not [s for s in series if kind(s) == "C"]:
                findings.append(_f("info", "RF-DCBLOCK", ref, f"{p.name} reaches "
                                   f"{', '.join(sorted(conns))} without a series cap: DC on "
                                   "the pin goes out to the connector (fine only if the pin "
                                   "is DC-blocked internally)."))
    return out


def certification_hints(nl: Netlist, findings: list, res: dict, pt: dict) -> list[dict]:
    hints = []

    def add(rule, std, why):
        hints.append({"rule": rule, "standards": std, "because": why})
        findings.append(_f("info", rule, "-", f"{std}: {why}"))
    if any(r["topology"] == "switching" for r in pt["regulators"]) or res.get("clocks_present"):
        add("CERT-EMC", "CISPR 32 / FCC Part 15B (EN 55032, EN 55035 immunity)",
            "switching regulators / clocks: unintentional radiator.")
    if res.get("wireless") or any(x.get("to") for x in res.get("rf", [])):
        add("CERT-RADIO", "RED 2014/53/EU (EN 300 328 / 301 489), FCC Part 15C",
            "intentional radiator (radio module or RF port).")
    if res.get("ethernet"):
        add("CERT-ETH", "IEEE 802.3 isolation (1500 V hipot), IEC 62368-1",
            "Ethernet port.")
    if res.get("chargers") or any(re.search(r"(^|/)V?BATT?([_+]|$)", n, re.I) for n in nl.nets):
        add("CERT-BATT", "IEC 62133-2, UN 38.3 (transport)", "lithium cell / charger.")
    if any(re.search(r"(^|/)(AC_?L|AC_?N|LINE|NEUTRAL|MAINS|L1|N)$", n, re.I) for n in nl.nets):
        add("CERT-MAINS", "IEC 62368-1 safety, EN 61000-3-2 / -3-3 harmonics",
            "mains-connected nets.")
    if res.get("usb_c") or any(re.search(r"VBUS", n) for n in nl.nets):
        add("CERT-USB", "USB-IF compliance (optional), USB PD if a PD controller is used",
            "USB port.")
    if res.get("isolation"):
        add("CERT-ISO", "IEC 60747-17 / UL 1577 (isolator), creepage per IEC 62368-1",
            "galvanic isolation barrier.")
    if any("ref" in m for m in res.get("motor_drivers") or []) or any(
            MOTOR_CONN.search(_text(nl, j)) and re.search(r"motor|stepper|BLDC", _text(nl, j), re.I)
            for j in cr._connectors(nl)):
        add("CERT-MOTOR", "IEC 61800-3 (drives EMC) where applicable", "motor driver.")
    return hints


def analyze(nl: Netlist, pt: dict, specs: dict, groups: tuple, findings: list,
            res: dict) -> None:
    volts = {n: v["volts"] for n, v in pt["rail_voltages"].items()}
    if "interfaces" in groups:
        res["ethernet"] = check_ethernet(nl, findings)
        res["hdmi"] = check_hdmi(nl, findings, specs)
        res["lvds"] = check_lvds(nl, findings, specs)
        res["memory"] = check_memory(nl, findings)
        res["wireless"] = check_wireless(nl, findings)
    if "power_conversion" in groups:
        res["chargers"] = check_chargers(nl, findings, specs)
        res["isolated_feedback"] = check_feedback_isolated(nl, findings)
        res["supercaps"] = check_supercaps(nl, findings)
        res["isolation"] = check_isolation(nl, findings)
    if "drives" in groups:
        res["motor_drivers"] = check_motor_drivers(nl, findings,
                                                   {r["ref"] for r in pt["regulators"]})
        res["temperature_sensing"] = check_temperature_sensing(nl, findings)
        res["addressable_leds"] = check_addressable_leds(nl, findings, volts)
    if "rf" in groups:
        res["rf"] = check_rf(nl, findings)
        res["certifications"] = certification_hints(nl, findings, res, pt)
