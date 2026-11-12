"""Circuit-level detectors on KiCAD's netlist: power & protection, audio &
analog, digital buses & clocks, LEDs / displays / misc.

Pin names and pin electrical types drive everything (no part-number
keyword lists); datasheet values come only from the spec cache. Every
finding is a heuristic for a human to confirm. Detector scope follows
kicad-happy 3.0.1 `domain_detectors.py` / `validation_detectors.py` (MIT
License, Copyright (c) 2025 Andrew Klofas); the implementations here are
this server's own.

Groups and rules:

  power    PW-USBC-CC (Type-C CC: Rd 5.1 k sink / Rp source / controller;
           CC1+CC2 on one resistor), PW-USBC-DATA (D+ / D- of both
           orientations joined), PW-VBUS-TVS (no clamp on a connector's
           supply net), PW-SEQ-LOOP / PW-SEQ-SELF (enable chains), PW-RESET
           (reset nets: pull-up, filter cap, MR), PW-FB-IB (FB bias current
           x divider vs Vout, from the spec cache), PW-FB-Z (divider
           impedance), PW-CONN-GND (connector with supply pins, no ground
           pin), PW-ESD (external connector signal nets without a clamp),
           PW-REVPOL (connector supply reaching ICs with no series diode,
           drain-upstream FET or ideal-diode IC), PW-INRUSH (hot-plug peak
           into the input capacitance, USB VBUS > 10 uF, soft-start charge
           current);
  analog   AU-BIAS (op-amp input with no DC path), AU-HPF (coupling cap +
           bias resistor corner vs the audio band), AU-BLEED (output
           coupling cap to a connector with no bleed resistor), AU-UNUSED
           (unused op-amp channel left floating), AD-VREF (converter
           reference / VCOM pin without a cap), AD-SUPPLY (analog supply pin
           without a cap), AD-GND (analog and digital ground pins on
           different nets with no join), AD-AAF (ADC input with no RC), HP-JACK
           (headphone / line jack tip / ring driven with no series element),
           LS-* (level shifter: both sides on one rail, OE floating);
  digital  DG-FLOAT (input pin on a net with nothing that drives it),
           DG-BOOT (boot / mode strap pin without a resistor), DG-I2C-RPU
           (pull-up below the mode's sink limit of the I2C spec),
           DG-I2C-RISE (rise time 0.8473 R C vs the mode limit), DG-SPI-CS
           (chip select shared / no pull-up), DG-USB-ESD, DG-LEVEL (driver
           supply vs receiver supply), DG-UART-LEVEL (TX -> RX through series
           resistors across supply domains), DG-CLK-TERM (clock with several loads
           and no series resistor), DG-SWD (debug pins not brought out);
  misc     LE-NORES (LED with no current limit), LE-ISET (LED driver set
           pin open), DS-CAP (display charge-pump / VCOMH pins without a
           cap), RTC-VBAT (backup pin floating / on the main rail), KB-PULL
           (button to ground with no pull-up), KB-GHOST (switch matrix
           without diodes).
"""

from __future__ import annotations

import re
from collections import defaultdict

from kicad_claude.adapters import datasheet_specs as dss
from kicad_claude.adapters.power_review import (
    Netlist,
    Pin,
    _two_terminal,
    is_ground,
    is_unconnected,
    kind,
    parse_value,
)

GROUPS = ("power", "analog", "digital", "misc", "interfaces", "power_conversion", "drives",
          "rf")
CONN = ("J", "P", "CN", "X")
DRIVERS = {"output", "bidirectional", "tri_state", "power_out", "open_collector",
           "open_emitter"}
TVS_TEXT = re.compile(r"TVS|ESD|SMF\d|SMAJ|SMBJ|SMCJ|SP0\d|PESD|TPD\d|USBLC|PRTR|ESDA|RCLAMP|"
                      r"SRV05|IP4\d|CDSOT|Zener|D_TVS", re.I)
LED_TEXT = re.compile(r"\bLED\b|LED_|_LED", re.I)


def _f(sev: str, rule: str, ref: str, msg: str, **kw) -> dict:
    return {"severity": sev, "rule": rule, "ref": ref, "message": msg, **kw}


def pn(p: Pin) -> str:
    """Pin name, upper case, without KiCAD overbar braces."""
    return re.sub(r"[~{}]", "", (p.name or "")).upper()


def btype(p: Pin) -> str:
    return p.type.split("+")[0]


def fitted(nl: Netlist, ref: str) -> bool:
    return not nl.components.get(ref, {}).get("dnp")


def other_end(nl: Netlist, ref: str, net: str) -> str | None:
    ends = _two_terminal(nl, ref)
    if not ends or net not in ends or ends[0] == ends[1]:
        return None
    return ends[1] if ends[0] == net else ends[0]


def parts_on(nl: Netlist, net: str, k: str) -> list[tuple[str, str | None]]:
    """(ref, far net) of fitted two-terminal parts of kind `k` on `net`."""
    return [(p.ref, other_end(nl, p.ref, net)) for p in nl.nets.get(net, [])
            if kind(p.ref) == k and fitted(nl, p.ref) and other_end(nl, p.ref, net)]


def caps_to_ground(nl: Netlist, net: str) -> list[str]:
    return [r for r, o in parts_on(nl, net, "C") if is_ground(o)]


def is_clamp(nl: Netlist, ref: str, specs: dict) -> bool:
    c = nl.components.get(ref, {})
    cat = (specs.get(ref) or {}).get("category", "")
    return cat in ("tvs", "zener") or bool(TVS_TEXT.search(
        f"{c.get('value', '')} {c.get('lib_id', '')} {c.get('description', '')}"))


def clamped(nl: Netlist, net: str, specs: dict) -> list[str]:
    return sorted({p.ref for p in nl.nets.get(net, []) if fitted(nl, p.ref)
                   and kind(p.ref) in ("D", "U", "TVS", "ESD") and is_clamp(nl, p.ref, specs)})


PASSIVE_KINDS = {"R", "RN", "C", "L", "FB", "FL", "D", "LED", "J", "P", "CN", "X", "Y", "TP", "SW",
                 "S", "F", "FU", "Q", "T", "TR", "K", "BT", "H", "MH", "JP", "SJ", "NT", "FID",
                 "AE", "ANT", "E", "RV", "TH", "G", "LS", "BZ", "M", "BUS", "POT"}
XTAL_TEXT = re.compile(r"crystal|xtal|resonator|oscillator|\bOSC\b|\d\s*[kKmM]?HZ\b", re.I)


def is_active(nl: Netlist, ref: str) -> bool:
    """An IC or module: U / IC, or any part (Module301, A1 ...) that is not a
    passive / connector kind and has a supply pin."""
    k = kind(ref)
    if k in ("U", "IC"):
        return True
    prefix = re.match(r"[A-Za-z]*", ref).group(0).lower()
    if prefix in ("module", "mod", "a"):
        return True
    if k in PASSIVE_KINDS or is_connector(nl, ref):
        return False
    return any(btype(p) in ("power_in", "power_out") for p in nl.pins.get(ref, []))


def is_connector(nl: Netlist, ref: str) -> bool:
    c = nl.components.get(ref, {})
    text = f"{c.get('lib_id', '')} {c.get('value', '')} {c.get('description', '')}"
    if XTAL_TEXT.search(text) or kind(ref) in ("TP",) or re.search(r"TestPoint", text, re.I):
        return False
    return kind(ref) in CONN or c.get("lib_id", "").lower().startswith("connector") or bool(
        re.search(r"\bconnector\b|\bDB-?9\b|\bDB-?25\b|\bheader\b|receptacle|\bCONN_?\d|"
                  r"[:_]CONN_", text, re.I))


def ics(nl: Netlist) -> list[str]:
    return sorted(r for r in nl.components if fitted(nl, r) and is_active(nl, r))


def supply_v(nl: Netlist, ref: str, volts: dict) -> float | None:
    """Highest positive rail on the part's power-input pins."""
    vs = [volts[p.net] for p in nl.pins.get(ref, [])
          if btype(p) == "power_in" and p.net in volts and not is_ground(p.net)]
    vs = [v for v in vs if v > 0]
    return max(vs) if vs else None


def has_driver(nl: Netlist, net: str, exclude: str | None = None) -> bool:
    """Anything that sets the net's level: a driving pin, a resistor /
    inductor / diode / transistor, a connector, a crystal."""
    for p in nl.nets.get(net, []):
        if p.ref == exclude or not fitted(nl, p.ref):
            continue
        k = kind(p.ref)
        if btype(p) in DRIVERS or k in ("R", "RV", "POT", "L", "FB", "D", "Q", "Y", "X", "SW", "JP",
                                        "SJ", "NT", "RN", "V", "I", "VS", "IS") or is_connector(nl, p.ref):
            return True
        # an IC pin that is not a pure input (FPGA / MCU pins typed passive,
        # unspecified or free) can drive the net
        if is_active(nl, p.ref) and btype(p) not in ("input", "power_in", "no_connect"):
            return True
    return False


def _connectors(nl: Netlist) -> list[str]:
    return sorted(r for r in nl.components if fitted(nl, r) and is_connector(nl, r))


# ---- power & protection ------------------------------------------------------- #

def board_sourced(pt: dict) -> set:
    """Rails a regulator on this board drives, and rails linked below them."""
    out = set()

    def walk(node, under):
        if under:
            out.add(node["rail"])
        for rr in node.get("regulators", []):
            if "output" in rr:
                walk(rr["output"], True)
        for ch in node.get("linked", []):
            walk(ch, under)
    for t in pt.get("tree", []):
        walk(t, False)
    for r in pt.get("regulators", []):
        if r.get("output_net"):
            out.add(r["output_net"])
    return out


RP_TABLE = {5.0: ((56e3, "default USB"), (22e3, "1.5 A"), (10e3, "3.0 A")),
            3.3: ((36e3, "default USB"), (12e3, "1.5 A"), (4.7e3, "3.0 A"))}


def check_usbc(nl: Netlist, findings: list, specs: dict, volts: dict) -> list[dict]:
    out = []
    for j in _connectors(nl):
        pins = nl.pins.get(j, [])
        cc = {pn(p): p.net for p in pins if pn(p) in ("CC1", "CC2", "CC")}
        if not cc:
            continue
        rec = {"ref": j, "cc": {}}
        out.append(rec)
        for name, net in sorted(cc.items()):
            if is_unconnected(net):
                rec["cc"][name] = "unconnected"
                findings.append(_f("error", "PW-USBC-CC", j, f"{name} is not connected: a Type-C "
                                   "source never enables VBUS without Rd on each CC pin."))
                continue
            ctrl = [p for p in nl.nets.get(net, []) if is_active(nl, p.ref)
                    and fitted(nl, p.ref) and pn(p).startswith("CC")]
            rs = parts_on(nl, net, "R")
            desc = []
            for r, o in rs:
                ohm = parse_value(nl.components[r]["value"])
                if o in cc.values():
                    findings.append(_f("error", "PW-USBC-CC", r, f"{r} joins CC1 and CC2: each CC "
                                       "pin needs its own Rd / Rp, or the cable orientation and "
                                       "e-marker detection fail."))
                    continue
                if is_ground(o):
                    ok = ohm is not None and 4590 <= ohm <= 5610
                    desc.append(f"Rd {r} {nl.components[r]['value']}")
                    if not ok and not ctrl:
                        findings.append(_f("warning", "PW-USBC-CC", r, f"{r} {nl.components[r]['value']} "
                                           f"pull-down on {name}: a Type-C sink Rd is 5.1 k (+-10 %)."))
                else:
                    v = volts.get(o)
                    tab = next((t for vv, t in RP_TABLE.items() if v and abs(v - vv) < 0.4), None)
                    hit = next((t for t in tab or () if ohm and abs(ohm - t[0]) <= 0.1 * t[0]), None)
                    desc.append(f"Rp {r} {nl.components[r]['value']} to {o}"
                                + (f" ({hit[1]})" if hit else ""))
                    if tab and not hit:
                        findings.append(_f("warning", "PW-USBC-CC", r, f"{r} pull-up on {name} "
                                           f"to {v:g} V is not a Type-C Rp value."))
            if ctrl:
                desc.append("controller " + ", ".join(f"{p.ref}.{p.name}" for p in ctrl))
            if not desc:
                findings.append(_f("error", "PW-USBC-CC", j, f"{name} ({net}) has no Rd / Rp and "
                                   "no Type-C controller pin: the port cannot attach."))
            rec["cc"][name] = desc
        # data pairs of both orientations
        dp = {p.net for p in pins if pn(p) in ("D+", "DP", "DP1", "DP2")}
        dm = {p.net for p in pins if pn(p) in ("D-", "DN", "DN1", "DN2")}
        for lbl, nets in (("D+", dp), ("D-", dm)):
            used = {n for n in nets if not is_unconnected(n)}
            if used and (len(used) > 1 or len(used) < len(nets)):
                findings.append(_f("warning", "PW-USBC-DATA", j, f"{lbl} pins of the two plug "
                                   "orientations are not joined: USB 2.0 works in one "
                                   "orientation only."))
            if used:
                for n in used:
                    if not clamped(nl, n, specs):
                        findings.append(_f("warning", "DG-USB-ESD", j, f"{lbl} ({n}) has no ESD "
                                           "clamp on the connector net."))
    return out


def _outbound(nl: Netlist, net: str, board_sourced: set) -> bool:
    """A connector supply the board drives: a power_out pin on the net, or one
    series link (<= 1 Ohm, inductor, ferrite, transistor) from a net that
    has one or that a regulator here sources."""
    def driven(n):
        return n in board_sourced or any(btype(q) == "power_out" and not is_connector(nl, q.ref)
                                         for q in nl.nets.get(n, []))
    if driven(net):
        return True
    for p in nl.nets.get(net, []):
        k = kind(p.ref)
        if not fitted(nl, p.ref):
            continue
        if k == "R" and (parse_value(nl.components[p.ref]["value"]) or 1e9) > 1.0:
            continue
        if k in ("R", "L", "FB", "Q"):
            for q in nl.pins.get(p.ref, []):
                if q.net != net and not is_ground(q.net) and driven(q.net):
                    return True
    return False


def check_supply_entry(nl: Netlist, findings: list, specs: dict,
                       board_sourced: set | None = None) -> list[dict]:
    """Supply pins of connectors: a clamp on the net, and a ground pin.
    Nets the board itself sources (regulator outputs and what they feed)
    go OUT through the connector: no clamp is asked for."""
    board_sourced = board_sourced or set()
    out = []
    for j in _connectors(nl):
        pins = nl.pins.get(j, [])
        sup = {p.net for p in pins if re.fullmatch(r"VBUS|VIN|V\+|VCC|PWR|\+?\d+V\d*|VDD", pn(p))
               and not is_unconnected(p.net)}
        if not sup:
            continue
        gnd = [p for p in pins if is_ground(p.net)]
        out.append({"ref": j, "supply_nets": sorted(sup), "ground_pins": len(gnd)})
        if not gnd:
            findings.append(_f("warning", "PW-CONN-GND", j, "Connector brings in a supply but "
                               "has no ground pin: the return current takes another path."))
        for n in sorted(sup):
            if n in board_sourced or _outbound(nl, n, board_sourced):
                continue
            if not clamped(nl, n, specs):
                findings.append(_f("warning", "PW-VBUS-TVS", j, f"Supply net {n} from {j} has "
                                   "no TVS / clamp: hot-plug and ESD transients reach the input."))
    return out


IDEAL_DIODE_TEXT = re.compile(r"ideal.?diode|reverse.?(polarity|current|battery)|LM74\d0|"
                              r"LM5050|LTC435\d|LTC4376|MAX4020\d|LM66100|TPS2121|AP7400", re.I)


def _diode_ends(nl: Netlist, ref: str) -> tuple[str, str] | None:
    """(anode net, cathode net) from pin names A / K, else KiCAD's Device:D
    numbering (1 = K, 2 = A)."""
    pins = nl.pins.get(ref, [])
    if len(pins) != 2:
        return None
    by = {pn(p): p.net for p in pins}
    if "A" in by and "K" in by:
        return by["A"], by["K"]
    num = {p.number: p.net for p in pins}
    return (num["2"], num["1"]) if {"1", "2"} <= set(num) else None


def _revpol_path(nl: Netlist, net: str, specs: dict) -> tuple[dict | None, set]:
    """Walk downstream from a connector supply net through series parts
    (<= 1 Ohm R, L, ferrite, fuse, jumper) and return the first
    reverse-polarity element met - a series diode (anode upstream), a FET
    with its drain upstream (body diode blocks a reversed input), an
    ideal-diode / reverse-protection IC, or a shunt diode (cathode on the
    rail, anode on ground) - and the nets reached."""
    from kicad_claude.adapters.dc_model import switch_dies

    seen, frontier = {net}, [net]
    shunt = None
    for _ in range(6):
        nxt = []
        for n in frontier:
            for p in nl.nets.get(n, []):
                ref, k = p.ref, kind(p.ref)
                if not fitted(nl, ref):
                    continue
                c = nl.components.get(ref, {})
                text = f"{c.get('value', '')} {c.get('lib_id', '')} {c.get('description', '')}"
                if k == "D" and not is_clamp(nl, ref, specs) and not LED_TEXT.search(text):
                    ends = _diode_ends(nl, ref)
                    if not ends:
                        continue
                    a, kk = ends
                    if a == n and not is_ground(kk):
                        return {"type": "series diode", "ref": ref, "net": n}, seen | {kk}
                    if kk == n and is_ground(a):
                        shunt = shunt or {"type": "shunt diode (reverse input clamped; needs "
                                                  "a fuse upstream)", "ref": ref, "net": n}
                    continue
                if k in ("Q", "M"):
                    for d in switch_dies(nl, ref):
                        dn, sn = d["main"]
                        if dn == n and not is_ground(sn) and sn not in seen:
                            return {"type": "series FET (drain toward the input: its body diode "
                                            "blocks a reversed supply)", "ref": ref, "net": n}, \
                                seen | {sn}
                        if sn == n and not is_ground(dn) and dn not in seen:
                            seen.add(dn)
                            nxt.append(dn)          # load switch: body diode conducts reverse
                    continue
                cat = (specs.get(ref) or {}).get("category", "")
                if is_active(nl, ref) and (IDEAL_DIODE_TEXT.search(text) or IDEAL_DIODE_TEXT.search(
                        (specs.get(ref) or {}).get("description", ""))):
                    return {"type": "ideal-diode / reverse-protection IC", "ref": ref,
                            "net": n}, seen
                if cat == "load_switch":
                    continue
                if k in ("R", "L", "FB", "FL", "F", "FU", "JP", "SJ", "NT"):
                    if k == "R" and (parse_value(c.get("value", "")) or 1e9) > 1.0:
                        continue
                    o = other_end(nl, ref, n)
                    if o and o not in seen and not is_ground(o) and not is_unconnected(o):
                        seen.add(o)
                        nxt.append(o)
        frontier = nxt
    return shunt, seen


def check_reverse_polarity(nl: Netlist, findings: list, specs: dict,
                           board_sourced: set | None = None,
                           internal: set | None = None) -> list[dict]:
    """PW-REVPOL: a supply that enters through a connector and reaches an
    active part with no reverse-polarity element in its path. USB
    connectors (keyed), board-to-board connectors (`internal`) and supplies
    the board drives out are skipped."""
    board_sourced = board_sourced or set()
    internal = internal or set()
    out = []
    for j in _connectors(nl):
        c = nl.components[j]
        if j in internal or re.search(r"USB", f"{c.get('lib_id', '')} {c.get('value', '')}",
                                      re.I):
            continue
        sup = sorted({p.net for p in nl.pins.get(j, [])
                      if re.fullmatch(r"VIN|V\+|VCC|PWR|\+?\d+V\d*|VDD|VBAT|BAT\+?|DC|DCIN|"
                                      r"VDC|V_?IN\d*", pn(p))
                      and not is_unconnected(p.net) and not is_ground(p.net)})
        for n in sup:
            if n in board_sourced or _outbound(nl, n, board_sourced) or re.search(
                    r"VBUS", n, re.I):
                continue
            prot, reached = _revpol_path(nl, n, specs)
            loads = sorted({p.ref for m in reached for p in nl.nets.get(m, [])
                            if p.ref != j and fitted(nl, p.ref) and is_active(nl, p.ref)})
            if not loads and not prot:
                continue
            out.append({"connector": j, "net": n, "protection": prot, "loads": loads})
            if prot is None:
                findings.append(_f("warning", "PW-REVPOL", j,
                                   f"Supply {n} from {j} reaches {', '.join(loads[:4])}"
                                   f"{' ...' if len(loads) > 4 else ''} with no series diode, "
                                   "P-FET or ideal-diode controller: a reversed input is "
                                   "applied to them."))
    return out


USB_VBUS_MAX_C = 10e-6   # USB 2.0 spec 7.2.4.1: at most 10 uF across VBUS at attach


def _downstream(nl: Netlist, net: str) -> tuple[set, list]:
    """Nets fed from `net` through series parts up to the first regulator:
    <= 1 Ohm R, L, ferrite, fuse, jumper, diode, FET channel. Returns (nets,
    fuse refs on the way)."""
    from kicad_claude.adapters.dc_model import switch_dies

    seen, frontier, fuses = {net}, [net], []
    for _ in range(6):
        nxt = []
        for n in frontier:
            for p in nl.nets.get(n, []):
                ref, k = p.ref, kind(p.ref)
                if not fitted(nl, ref):
                    continue
                outs = []
                if k in ("L", "FB", "FL", "F", "FU", "JP", "SJ", "NT", "D") or (
                        k == "R" and (parse_value(nl.components[ref]["value"]) or 1e9) <= 1.0):
                    if k == "D" and is_clamp(nl, ref, {}):
                        continue
                    outs = [other_end(nl, ref, n)]
                    if k in ("F", "FU"):
                        fuses.append(ref)
                elif k in ("Q", "M"):
                    outs = [b if a == n else a for d in switch_dies(nl, ref)
                            for a, b in [d["main"]] if n in (a, b)]
                for o in outs:
                    if o and o not in seen and not is_ground(o) and not is_unconnected(o):
                        seen.add(o)
                        nxt.append(o)
        frontier = nxt
    return seen, fuses


def _cap_sum(nl: Netlist, nets: set, specs: dict) -> tuple[float, list[str]]:
    total, refs = 0.0, []
    for n in nets:
        for r, o in parts_on(nl, n, "C"):
            if not is_ground(o) or not fitted(nl, r):
                continue
            c = dss.rated(specs.get(r), "electrical", "C", prefer="typ")
            v = c[0] if c else parse_value(nl.components[r]["value"])
            if v:
                total += v
                refs.append(r)
    return total, sorted(refs)


def check_inrush(nl: Netlist, findings: list, specs: dict, volts: dict, pt: dict,
                 board_sourced: set | None = None, source_ohm: float | None = None,
                 limit_a: float | None = None) -> dict:
    """PW-INRUSH. Per connector supply: the capacitance downstream up to the
    first regulator, and the hot-plug peak V / `source_ohm` (source + cable +
    contact resistance, from the caller) against `limit_a`, the connector's
    rating and a fuse's trip / rated current in the path; a USB VBUS with
    more than 10 uF (USB 2.0 7.2.4.1). Per regulator with a soft-start time
    in its spec record (`regulator.t_ss`): output capacitance C x Vout / t_ss
    against `limit_a`. Unknown source resistance -> info."""
    board_sourced = board_sourced or set()
    entries, regs_out = [], []
    for j in _connectors(nl):
        c = nl.components[j]
        usb = bool(re.search(r"USB", f"{c.get('lib_id', '')} {c.get('value', '')}", re.I))
        for n in sorted({p.net for p in nl.pins.get(j, []) if re.fullmatch(
                r"VBUS|VIN|V\+|VCC|PWR|\+?\d+V\d*|VDD|VBAT|BAT\+?|DC|DCIN|VDC|V_?IN\d*", pn(p))
                and not is_unconnected(p.net) and not is_ground(p.net)}):
            if n in board_sourced or _outbound(nl, n, board_sourced):
                continue
            nets, fuses = _downstream(nl, n)
            cap, refs = _cap_sum(nl, nets, specs)
            if not cap:
                continue
            v = volts.get(n) or next((volts[m] for m in nets if volts.get(m)), None)
            row = {"connector": j, "net": n, "capacitance_uf": round(cap * 1e6, 3),
                   "caps": refs, "volts": v, "fuses": fuses}
            entries.append(row)
            if usb and re.search(r"VBUS", f"{n} " + " ".join(
                    pn(p) for p in nl.pins.get(j, []) if p.net == n), re.I) and cap > USB_VBUS_MAX_C:
                findings.append(_f("warning", "PW-INRUSH", j, f"{cap * 1e6:.3g} uF on VBUS "
                                   f"({n}) before the first regulator: USB 2.0 (7.2.4.1) allows "
                                   "10 uF at attach - add a load switch / soft start."))
            limits = []
            if limit_a is not None:
                limits.append((float(limit_a), "inrush_limit_a"))
            rec = specs.get(j)
            pins_n = sum(1 for p in nl.pins.get(j, []) if p.net == n)
            tot = dss.rated(rec, "electrical", "I_rated_total", prefer="max")
            per = dss.rated(rec, "electrical", "I_rated", "I_contact", prefer="max")
            if tot or per:
                limits.append((tot[0] if tot else per[0] * pins_n,
                               f"{j} rating ({rec.get('mpn')} p.{(tot or per)[1]})"))
            for fz in fuses:
                fr = dss.rated(specs.get(fz), "electrical", "I_trip", "I_rated", prefer="max")
                if fr:
                    limits.append((fr[0], f"{fz} {fr[2]} ({specs[fz].get('mpn')} p.{fr[1]}; "
                                   "its I2t decides - screen only)"))
            if v is None or source_ohm is None:
                row["peak_a"] = None
                findings.append(_f("info", "PW-INRUSH", j, f"{cap * 1e6:.3g} uF on {n} before "
                                   "the first regulator: hot-plug peak not estimated - pass "
                                   "source_ohm (source + cable + contacts) and rail_voltages."))
                continue
            peak = abs(v) / float(source_ohm)
            row.update(peak_a=round(peak, 3), tau_us=round(float(source_ohm) * cap * 1e6, 2))
            over = [(lim, why) for lim, why in limits if peak > lim]
            if over:
                lim, why = min(over)
                findings.append(_f("warning", "PW-INRUSH", j, f"Hot-plug peak {peak:.3g} A "
                                   f"({abs(v):g} V / {source_ohm:g} Ohm into {cap * 1e6:.3g} uF, "
                                   f"tau {float(source_ohm) * cap * 1e6:.3g} us) above {lim:g} A "
                                   f"({why})."))
    for r in pt.get("regulators", []):
        rec = specs.get(r["ref"]) or {}
        tss = dss.rated(rec, "regulator", "t_ss", prefer="min") or dss.rated(
            rec, "electrical", "t_ss", "tSS", prefer="min")
        out_n = r.get("output_net")
        vout = volts.get(out_n) if out_n else None
        if not out_n or vout is None:
            continue
        cap, refs = _cap_sum(nl, {out_n}, specs)
        if not cap:
            continue
        row = {"regulator": r["name"], "output_net": out_n, "capacitance_uf": round(cap * 1e6, 3),
               "vout": vout, "t_ss_s": tss[0] if tss else None}
        regs_out.append(row)
        if not tss:
            continue
        i = cap * abs(vout) / tss[0]
        row["charge_current_a"] = round(i, 4)
        if limit_a is not None and i > limit_a:
            findings.append(_f("warning", "PW-INRUSH", r["name"], f"Soft-start charges "
                               f"{cap * 1e6:.3g} uF to {vout:g} V in {tss[0] * 1e3:g} ms "
                               f"(t_ss min, {rec.get('mpn')} p.{tss[1]}): {i:.3g} A, above "
                               f"inrush_limit_a {limit_a:g} A."))
    return {"entries": entries, "regulator_outputs": regs_out}


def check_esd_audit(nl: Netlist, findings: list, specs: dict, internal: set) -> list[dict]:
    """Every external connector's signal nets (not supply, not ground)."""
    out = []
    bare_by = {}
    for j in _connectors(nl):
        if j in internal:
            continue
        sig = sorted({p.net for p in nl.pins.get(j, []) if not is_ground(p.net)
                      and not is_unconnected(p.net)
                      and not re.fullmatch(r"VBUS|VIN|V\+|VCC|PWR|SHIELD|\+?\d+V\d*", pn(p))})
        if not sig:
            continue
        bare = [n for n in sig if not clamped(nl, n, specs)]
        out.append({"ref": j, "signal_nets": sig, "unprotected": bare})
        if bare:
            bare_by[j] = bare
    if bare_by:
        findings.append(_f("info", "PW-ESD", ",".join(sorted(bare_by)),
                           "External connector signal nets without an ESD clamp: "
                           + "; ".join(f"{j}: {', '.join(n[:4])}{' ...' if len(n) > 4 else ''}"
                                       for j, n in sorted(bare_by.items()))
                           + ". Pass internal_connectors for board-to-board / test connectors."))
    return out


def check_sequencing(nl: Netlist, findings: list, pt: dict) -> dict:
    regs = pt["regulators"]
    by_out = defaultdict(list)
    for r in regs:
        if r["output_net"]:
            by_out[r["output_net"]].append(r["name"])
    edges = []
    for r in regs:
        en = r.get("enable") or {}
        if en.get("state") != "driven":
            continue
        for by in en.get("by") or []:
            dref = str(by).split(".")[0]
            src = [x["name"] for x in regs if x["ref"] == dref and x["name"] != r["name"]]
            for s in src:
                edges.append((s, r["name"]))
            # a part that is powered only from this regulator's own output
            if dref not in {x["ref"] for x in regs}:
                rails = {p.net for p in nl.pins.get(dref, []) if btype(p) == "power_in"}
                if rails and rails <= {r["output_net"]}:
                    findings.append(_f("error", "PW-SEQ-SELF", r["name"],
                                       f"EN is driven by {dref}, which is powered only from "
                                       f"{r['name']}'s own output {r['output_net']}: it never "
                                       "starts."))
    adj = defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
    seen_cycles = set()
    for start in list(adj):
        stack = [(start, [start])]
        while stack:
            n, path = stack.pop()
            for m in adj.get(n, []):
                if m == start:
                    key = frozenset(path)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        findings.append(_f("error", "PW-SEQ-LOOP", start, "Enable loop: "
                                           + " -> ".join(path + [start]) + "."))
                elif m not in path:
                    stack.append((m, path + [m]))
    return {"edges": edges}


RESET_RE = re.compile(r"^(N?RST|N?RESET|RST_?N|RESET_?N|MCLR|POR|NRST)$")


def check_reset(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for ref in ics(nl):
        for p in nl.pins.get(ref, []):
            name = pn(p)
            if name == "MR" and btype(p) == "input":
                if is_unconnected(p.net):
                    findings.append(_f("warning", "PW-RESET", ref, "Manual-reset input MR is "
                                       "unconnected: check the datasheet for an internal pull-up."))
                continue
            if not RESET_RE.match(name) or is_unconnected(p.net) or is_ground(p.net):
                continue
            pulls = [r for r, o in parts_on(nl, p.net, "R") if not is_ground(o)]
            caps = caps_to_ground(nl, p.net)
            drv = [q for q in nl.nets.get(p.net, []) if q.ref != ref and btype(q) in DRIVERS
                   and kind(q.ref) in ("U", "IC")]
            rec = {"ref": ref, "pin": p.name, "net": p.net, "pull_up": pulls, "caps": caps,
                   "driven_by": [f"{q.ref}.{q.name}" for q in drv]}
            out.append(rec)
            od = [q for q in drv if btype(q) in ("open_collector", "open_emitter")]
            if btype(p) in ("output", "open_collector") and not drv:
                continue      # a reset output of a supervisor: checked at its receivers
            if (od or not drv) and not pulls:
                findings.append(_f("warning" if od else "info", "PW-RESET", ref,
                                   f"Reset {p.name} ({p.net}) has no pull-up resistor"
                                   + (f" and is driven open-drain by {od[0].ref}" if od else
                                      "; an internal pull-up is assumed - check the datasheet")
                                   + "."))
            if not drv and not caps:
                findings.append(_f("info", "PW-RESET", ref, f"Reset {p.name} ({p.net}) has no "
                                   "filter cap to ground: noise on the line can reset the part."))
    return out


def check_feedback(nl: Netlist, findings: list, pt: dict, specs: dict,
                   fb_z_max: float) -> list[dict]:
    out = []
    for r in pt["regulators"]:
        fb = r.get("feedback") or {}
        div = fb.get("divider") or {}
        top, bot = parse_value(div.get("top_value") or ""), parse_value(div.get("bottom_value") or "")
        if not top or not bot:
            continue
        zpar = top * bot / (top + bot)
        rec = {"regulator": r["name"], "r_top": top, "r_bottom": bot, "z_parallel": round(zpar)}
        out.append(rec)
        if zpar > fb_z_max:
            findings.append(_f("info", "PW-FB-Z", r["name"], f"FB divider {zpar / 1e3:.0f} k "
                               f"parallel (> {fb_z_max / 1e3:.0f} k): high-impedance node, keep the "
                               "FB trace short and away from the switch node."))
        srec = specs.get(r["ref"]) or {}
        ib = dss.rated(srec, "electrical", "IFB", "I_FB", "IB_FB", prefer="max")
        vout = (r.get("vout") or {}).get("value")
        if ib and vout:
            err = ib[0] * top / abs(vout)
            rec["fb_bias_error"] = round(err, 5)
            if err > 0.005:
                findings.append(_f("warning", "PW-FB-IB", r["name"],
                                   f"FB bias current {ib[0]:g} A ({srec.get('mpn')} p.{ib[1]}) "
                                   f"through R_top {top:g} Ohm shifts Vout by up to {err:.2%}."))
    return out


# ---- audio & analog ---------------------------------------------------------- #

def opamp_channels(nl: Netlist, specs: dict) -> dict[str, dict[str, dict[str, str]]]:
    """{ref: {channel: {"+": net, "-": net, "out": net}}} for parts with
    op-amp pin names (or category opamp)."""
    # one pin matcher with the SPICE stages (bare "+" / "-", unnamed output)
    from kicad_claude.adapters.spice_auto import opamp_pins
    out = {}
    for ref in ics(nl):
        full = opamp_pins(nl, ref)
        if full and ((specs.get(ref) or {}).get("category") in ("opamp", None, "")
                     and not any(btype(p) == "power_out" for p in nl.pins.get(ref, []))):
            out[ref] = full
    return out


def dc_path(nl: Netlist, net: str, exclude: str) -> bool:
    if is_ground(net):
        return True
    for p in nl.nets.get(net, []):
        if p.ref == exclude or not fitted(nl, p.ref):
            continue
        k = kind(p.ref)
        if k in ("R", "L", "FB", "D") or k in CONN or btype(p) in DRIVERS:
            return True
    return False


def check_audio(nl: Netlist, findings: list, specs: dict, f_low_hz: float) -> list[dict]:
    out = []
    for ref, chans in opamp_channels(nl, specs).items():
        for c, n in sorted(chans.items()):
            rec = {"ref": ref, "channel": c or None, **n}
            out.append(rec)
            if is_unconnected(n["+"]) or (is_unconnected(n["-"]) and is_unconnected(n["out"])):
                findings.append(_f("warning", "AU-UNUSED", ref,
                                   f"Channel {c or '-'}: +IN unconnected. An unused op-amp "
                                   "channel should be a follower with +IN at a valid DC level "
                                   "(else it can oscillate and draw current)."))
                continue
            for side in ("+", "-"):
                net = n[side]
                if is_unconnected(net):
                    continue
                if not dc_path(nl, net, ref) and n["out"] != net:
                    findings.append(_f("error", "AU-BIAS", ref, f"Channel {c or '-'} {side}IN "
                                       f"({net}) has no DC path (only capacitors): the input "
                                       "floats to a rail."))
            # coupling cap into +IN with a bias resistor to ground / reference
            net = n["+"]
            for cref, far in parts_on(nl, net, "C"):
                if is_ground(far):
                    continue
                biases = [r for r, o in parts_on(nl, net, "R") if o != far]
                cval = parse_value(nl.components[cref]["value"])
                for r in biases:
                    rv = parse_value(nl.components[r]["value"])
                    if not (rv and cval):
                        continue
                    fc = 1 / (2 * 3.141592653589793 * rv * cval)
                    rec.setdefault("input_highpass", []).append(
                        {"cap": cref, "bias": r, "fc_hz": round(fc, 2)})
                    if fc > f_low_hz / 2:
                        findings.append(_f("warning", "AU-HPF", cref,
                                           f"{cref} {nl.components[cref]['value']} into {r} "
                                           f"{nl.components[r]['value']}: high-pass corner "
                                           f"{fc:.1f} Hz, rolls off the audio band (-3 dB at "
                                           f"{fc:.1f} Hz, aim below {f_low_hz / 2:g} Hz)."))
            # output coupling cap to a connector
            for cref, far in parts_on(nl, n["out"], "C"):
                if is_ground(far) or far in (n["-"], n["+"]):
                    continue
                to_conn = any(kind(p.ref) in CONN for p in nl.nets.get(far, []))
                bleed = [r for r, o in parts_on(nl, far, "R") if is_ground(o)]
                if to_conn and not bleed:
                    findings.append(_f("warning", "AU-BLEED", cref,
                                       f"Output coupling cap {cref} to the connector on {far} has "
                                       "no bleed resistor to ground: the output side charges up "
                                       "and pops on plug-in."))
    return out


CONV_REF = re.compile(r"^(VREF[PN+-]?|REF[PN+-]?|REFIN|REFOUT|VCOM|VMID|CAP[PN+-]?|VREFP|VREFN|"
                      r"FILT\w*|VQ)$")
ANA_SUP = re.compile(r"^(AVDD\w*|AVCC\w*|VA|VAA|AVD|VDDA\w*|VCCA)$")
DIG_SUP = re.compile(r"^(DVDD\w*|DVCC\w*|VD|VDDD|IOVDD|VIO|DVD)$")
AIN = re.compile(r"^(AIN\w*|VIN[LR]\w*|AIN[LR]\w*|IN[LR][PN+-]?|L?IN[+-]?[LR]|LIN\w*|RIN\w*)$")
I2S = re.compile(r"^(BCLK|BCK|SCLK|LRCK|LRCLK|WCLK|FSYNC|MCLK|SCKI|SDOUT|SDIN|DOUT|DIN|SDATA)$")


def check_converters(nl: Netlist, findings: list, specs: dict) -> list[dict]:
    out = []
    for ref in ics(nl):
        cat = (specs.get(ref) or {}).get("category", "")
        pins = nl.pins.get(ref, [])
        names = {pn(p) for p in pins}
        if not (cat in ("adc", "dac", "codec") or (names & {n for n in names if I2S.match(n)}
                                                    and names & {n for n in names
                                                                 if ANA_SUP.match(n)})):
            continue
        rec = {"ref": ref, "category": cat or "converter (pin names)"}
        out.append(rec)
        for p in pins:
            name = pn(p)
            if is_unconnected(p.net):
                continue
            if CONV_REF.match(name) and not is_ground(p.net):
                if not caps_to_ground(nl, p.net):
                    findings.append(_f("warning", "AD-VREF", ref, f"{p.name} ({p.net}) has no "
                                       "capacitor to ground: reference noise goes straight to the "
                                       "conversion (check the datasheet's recommended cap)."))
            elif ANA_SUP.match(name) and not caps_to_ground(nl, p.net):
                findings.append(_f("warning", "AD-SUPPLY", ref, f"Analog supply {p.name} "
                                   f"({p.net}) has no decoupling cap: supply noise reaches the "
                                   "converter's PSRR limit."))
            elif AIN.match(name) and cat in ("adc", "codec"):
                rs = parts_on(nl, p.net, "R")
                cs = caps_to_ground(nl, p.net) + [c for c, o in parts_on(nl, p.net, "C")
                                                  if o in {q.net for q in pins}]
                if not (rs and cs):
                    rec.setdefault("inputs_without_rc", []).append(p.name)
        if rec.get("inputs_without_rc"):
            findings.append(_f("info", "AD-AAF", ref, "Analog inputs without a series R + shunt C "
                               f"(anti-alias / kickback filter): {', '.join(rec['inputs_without_rc'])}."))
        gnds = {p.net for p in pins if re.fullmatch(r"(A|D|P)?GND\w*|(A|D)VSS\w*|VSS\w*", pn(p))}
        if len(gnds) > 1:
            joined = _grounds_joined(nl, gnds)
            rec["ground_nets"] = sorted(gnds)
            if not joined:
                findings.append(_f("warning", "AD-GND", ref, f"Ground pins on separate nets "
                                   f"{sorted(gnds)} with no join found (0 R / ferrite / net tie): "
                                   "the converter's grounds must meet at one point."))
    return out


def _grounds_joined(nl: Netlist, nets: set) -> bool:
    nets = list(nets)
    seen, frontier = {nets[0]}, [nets[0]]
    while frontier:
        n = frontier.pop()
        for p in nl.nets.get(n, []):
            k = kind(p.ref)
            if k in ("R", "L", "FB", "NT", "JP", "SJ") or "NetTie" in nl.components.get(
                    p.ref, {}).get("lib_id", ""):
                for q in nl.pins.get(p.ref, []):
                    if q.net not in seen:
                        seen.add(q.net)
                        frontier.append(q.net)
    return all(n in seen for n in nets)


JACK_TEXT = re.compile(r"jack|audio|TRS|PJ-|phone|3\.5|6\.35|SJ-\d", re.I)


def check_jacks(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for j in _connectors(nl):
        c = nl.components[j]
        pins = nl.pins.get(j, [])
        if not JACK_TEXT.search(f"{c['lib_id']} {c['value']} {c['description']}"):
            continue
        tr = [p for p in pins if re.match(r"^(T|R|R1|R2|TIP|RING\w*|L|LEFT|RIGHT)$", pn(p))]
        rec = {"ref": j, "contacts": [f"{p.name}:{p.net}" for p in tr]}
        out.append(rec)
        for p in tr:
            if is_unconnected(p.net):
                continue
            direct = [q for q in nl.nets.get(p.net, []) if kind(q.ref) in ("U", "IC")
                      and btype(q) in ("output", "power_out")]
            if direct:
                findings.append(_f("warning", "HP-JACK", j, f"{p.name} ({p.net}) is driven "
                                   f"directly by {direct[0].ref}.{direct[0].name}: no series "
                                   "resistor / coupling cap to isolate cable capacitance and "
                                   "shorts."))
    return out


def check_level_shifters(nl: Netlist, findings: list, volts: dict) -> list[dict]:
    out = []
    for ref in ics(nl):
        pins = {pn(p): p for p in nl.pins.get(ref, [])}
        a = pins.get("VCCA") or pins.get("VL") or pins.get("VCC1")
        b = pins.get("VCCB") or pins.get("VCC2") or (pins.get("VCC") if "VL" in pins else None)
        if not (a and b) or not any(re.fullmatch(r"[AB]\d+", n) for n in pins):
            continue
        rec = {"ref": ref, "side_a": a.net, "side_b": b.net,
               "v_a": volts.get(a.net), "v_b": volts.get(b.net)}
        out.append(rec)
        if a.net == b.net:
            findings.append(_f("info", "LS-SAME", ref, f"Both supplies on {a.net}: the level "
                               "shifter does not shift."))
        oe = pins.get("OE") or pins.get("EN")
        if oe and (is_unconnected(oe.net) or not has_driver(nl, oe.net, ref)):
            findings.append(_f("warning", "LS-OE", ref, f"{oe.name} ({oe.net}) is not driven or "
                               "strapped: outputs may stay disabled or float."))
    return out


# ---- digital buses & clocks ------------------------------------------------- #

NC_NAME = re.compile(r"^(NC|N\.C\.|DNC|DNU|RSVD|RESERVED)\d*$")


LEAVE_OPEN = re.compile(r"leave (this pin )?(unconnected|open|floating)|do not connect|"
                        r"may be left (open|floating|unconnected)|internal(ly)? pull", re.I)
OUT_DESC = re.compile(r"\binterrupt\b|\boutput\b|\bopen.drain\b", re.I)


def check_floating(nl: Netlist, findings: list, specs: dict | None = None) -> list[dict]:
    specs = specs or {}
    out = []
    open_inputs: dict[str, list[str]] = defaultdict(list)
    for ref in ics(nl):
        rec = specs.get(ref) or {}
        ds_pin = {str(p.get("number")): p for p in rec.get("pins", [])}
        reg = rec.get("regulator") or {}
        anyout = set((reg.get("anyout") or {}).get("pins", {})) | {
            n for ch in (reg.get("channels") or {}).values()
            for n in ((ch or {}).get("anyout") or {}).get("pins", {})}
        for p in nl.pins.get(ref, []):
            if btype(p) != "input" or NC_NAME.match(pn(p)):
                continue
            alone = not [q for q in nl.nets.get(p.net, []) if q.ref != p.ref or q.number != p.number]
            if is_unconnected(p.net) or alone:
                desc = (ds_pin.get(p.number) or {}).get("description", "")
                if p.number in anyout or LEAVE_OPEN.search(desc):
                    continue          # datasheet: open is a valid state
                if OUT_DESC.search(desc):
                    findings.append(_f("info", "DG-PINTYPE", ref, f"{p.name} (pin {p.number}) is "
                                       f"typed input in the symbol, but the datasheet says "
                                       f"\"{desc}\" ({rec.get('mpn')}): unconnected is fine for an "
                                       "unused output; fix the symbol pin type."))
                    continue
                out.append({"ref": ref, "pin": p.name, "net": None})
                open_inputs[ref].append(f"{p.name}({p.number})")
            elif not is_ground(p.net) and not has_driver(nl, p.net):
                others = [q for q in nl.nets.get(p.net, []) if q.ref != ref]
                if not any(btype(q) == "power_in" for q in others) and not any(
                        kind(q.ref) == "C" for q in others):
                    out.append({"ref": ref, "pin": p.name, "net": p.net})
                    dnp = sorted(q.ref for q in others if not fitted(nl, q.ref))
                    module = kind(ref) not in ("U", "IC")
                    if dnp:
                        findings.append(_f("info", "DG-FLOAT", ref, f"Input {p.name} on {p.net} is "
                                           f"strapped only by DNP {', '.join(dnp)}: it floats as "
                                           "assembled (fine if the datasheet defines a float "
                                           "level)."))
                    else:
                        findings.append(_f("info" if module else "error", "DG-FLOAT", ref,
                                           f"Input {p.name} on {p.net} has no driver, pull "
                                           "resistor or strap: it floats"
                                           + (" unless the module pulls it internally (check its "
                                              "datasheet)." if module else ".")))
    for ref, pins in sorted(open_inputs.items()):
        big = len(nl.pins.get(ref, [])) > 64 or kind(ref) not in ("U", "IC")
        findings.append(_f("info" if big else "warning", "DG-FLOAT", ref,
                           f"{len(pins)} input pin(s) unconnected: {', '.join(pins[:12])}"
                           + (" ..." if len(pins) > 12 else "")
                           + (" - large part / module: check its unused-pin guidance." if big else
                              " - a floating CMOS input draws current and toggles; tie them "
                              "unless the datasheet gives an internal pull or says to leave them "
                              "open.")))
    return out


BOOT_RE = re.compile(r"^(BOOT\d?|BOOTSEL|MODE\d?|MD\d?|PROG|IO0|GPIO0|STRAP\w*|CFG\d?)$")


def check_boot(nl: Netlist, findings: list, reg_refs: set | None = None) -> list[dict]:
    out = []
    for ref in ics(nl):
        if ref in (reg_refs or set()):
            continue
        names = {pn(q) for q in nl.pins.get(ref, [])}
        for p in nl.pins.get(ref, []):
            if not BOOT_RE.match(pn(p)) or is_unconnected(p.net):
                continue
            if parts_on(nl, p.net, "C") and not parts_on(nl, p.net, "R"):
                continue          # bootstrap capacitor pin (BOOT / BST of a gate driver)
            if pn(p) in ("IO0", "GPIO0") and not names & {"EN", "CHIP_PU", "CHIP_EN", "RST",
                                                          "NRST", "RESET"}:
                continue          # a plain GPIO0 is only a strap on SoCs with an enable pin
            rs = parts_on(nl, p.net, "R")
            tied = is_ground(p.net) or any(btype(q) == "power_out" or (q.ref != ref and btype(q) ==
                                                                          "power_in")
                                           for q in nl.nets.get(p.net, []))
            out.append({"ref": ref, "pin": p.name, "net": p.net, "resistors": [r for r, _ in rs],
                        "tied": tied})
            if not rs and not tied:
                findings.append(_f("warning", "DG-BOOT", ref, f"Boot / mode pin {p.name} "
                                   f"({p.net}) has no strap resistor: the boot mode depends on "
                                   "whatever drives it at reset."))
    return out


# I2C-bus specification, NXP UM10204 rev. 7: maximum rise time (30 % -> 70 %
# VDD) and the low-level sink current at VOL 0.4 V, per mode.
I2C_MODES = {"standard": {"tr_ns": 1000.0, "sink_a": 0.003},
             "fast": {"tr_ns": 300.0, "sink_a": 0.003},
             "fast_plus": {"tr_ns": 120.0, "sink_a": 0.020}}
I2C_CIN_KEYS = ("C_in", "C_I", "CI", "Cin", "C_IN", "C_io", "CIO", "C_pin")


def _i2c_cap(nl: Netlist, net: str, specs: dict, caller: dict) -> dict:
    """Bus capacitance on one line: each active part's input capacitance
    from its spec record plus the caller's `i2c_bus_capacitance_pf` for the
    net (wiring, connectors, off-board devices)."""
    total, parts, missing = 0.0, {}, []
    for ref in sorted({p.ref for p in nl.nets.get(net, []) if is_active(nl, p.ref)
                       and fitted(nl, p.ref)}):
        c = dss.rated(specs.get(ref), "electrical", *I2C_CIN_KEYS)
        if c:
            total += c[0]
            parts[ref] = {"pf": round(c[0] * 1e12, 2), "source": f"{specs[ref].get('mpn')} "
                          f"p.{c[1]}"}
        else:
            missing.append(ref)
    tail = net.split("/")[-1]
    extra = next((v for k, v in caller.items() if k in (net, tail) or k.lstrip("/") == tail),
                 None)
    if extra is not None:
        total += float(extra) * 1e-12
    return {"c_f": total, "parts": parts, "missing": missing, "caller_pf": extra}


def check_i2c_pullup(nl: Netlist, findings: list, volts: dict, specs: dict | None = None,
                     mode: str = "fast", bus_pf: dict | None = None) -> list[dict]:
    """Pull-up strength vs the mode's low-level sink (VOL 0.4 V: 3 mA, 20 mA
    for Fast-mode Plus) and rise time tr = 0.8473 R C (30 -> 70 % of VDD)
    against the mode's limit (UM10204)."""
    if mode not in I2C_MODES:
        raise ValueError(f"i2c_mode {mode!r} not in {list(I2C_MODES)}")
    lim = I2C_MODES[mode]
    specs, bus_pf = specs or {}, bus_pf or {}
    out = []
    for net in sorted(nl.nets):
        names = {pn(p) for p in nl.nets[net]}
        line = "SDA" if any(re.search(r"SDA", n) for n in names) else \
            "SCL" if any(re.search(r"SCL", n) for n in names) else None
        if not line:
            continue
        pullups = []
        for r, o in parts_on(nl, net, "R"):
            v = volts.get(o)
            ohm = parse_value(nl.components[r]["value"])
            if v is None or not ohm or is_ground(o) or not fitted(nl, r):
                continue
            pullups.append(ohm)
            i = (v - 0.4) / ohm
            out.append({"net": net, "ref": r, "ohms": ohm, "rail_v": v, "sink_ma": round(i * 1e3, 2)})
            if i > lim["sink_a"]:
                findings.append(_f("error", "DG-I2C-RPU", r, f"{line} pull-up {r} "
                                   f"{nl.components[r]['value']} to {v:g} V needs {i * 1e3:.1f} mA "
                                   f"to pull low: above the {lim['sink_a'] * 1e3:g} mA {mode} "
                                   "sink."))
        if not pullups:
            continue
        rp = 1 / sum(1 / x for x in pullups)
        cap = _i2c_cap(nl, net, specs, bus_pf)
        row = {"net": net, "line": line, "mode": mode, "r_pullup": round(rp, 1),
               "c_bus_pf": round(cap["c_f"] * 1e12, 2), "c_parts": cap["parts"],
               "c_caller_pf": cap["caller_pf"], "c_missing": cap["missing"],
               "tr_limit_ns": lim["tr_ns"]}
        out.append(row)
        if cap["c_f"] <= 0 or (cap["missing"] and cap["caller_pf"] is None):
            row["tr_ns"] = None
            findings.append(_f("info", "DG-I2C-RISE", net, f"{line} ({net}) rise time not "
                               "computed: no input capacitance extracted for "
                               f"{', '.join(cap['missing']) or 'any device'}; pass "
                               "i2c_bus_capacitance_pf={net: pF} for the bus."))
            continue
        tr = 0.8473 * rp * cap["c_f"] * 1e9
        row["tr_ns"] = round(tr, 1)
        if tr > lim["tr_ns"]:
            findings.append(_f("warning", "DG-I2C-RISE", net,
                               f"{line} ({net}) rise time 0.8473 x {rp:.0f} Ohm x "
                               f"{cap['c_f'] * 1e12:.0f} pF = {tr:.0f} ns, above the {mode} "
                               f"limit {lim['tr_ns']:g} ns (UM10204): use a lower pull-up "
                               "or a slower mode"
                               + (f"; no C_in for {', '.join(cap['missing'])} (lower bound)"
                                  if cap["missing"] else "") + "."))
    return out


def check_spi(nl: Netlist, findings: list) -> list[dict]:
    out = []
    cs = defaultdict(list)
    for ref in ics(nl):
        names = {pn(q) for q in nl.pins.get(ref, [])}
        if not names & {"SCK", "SCLK", "SDI", "MOSI", "DIN", "SI", "COPI"}:
            continue          # not an SPI device
        for p in nl.pins.get(ref, []):
            if re.fullmatch(r"N?CS\d?|CSN|N?SS|NSS|CE\d?|SYNC", pn(p)) and btype(p) == "input":
                cs[p.net].append(ref)
    for net, refs in sorted(cs.items()):
        if is_unconnected(net) or is_ground(net):
            continue
        rec = {"net": net, "devices": refs}
        out.append(rec)
        if len(set(refs)) > 1:
            findings.append(_f("error", "DG-SPI-CS", refs[0], f"Chip-select {net} is shared by "
                               f"{', '.join(sorted(set(refs)))}: both drive MISO at once."))
        if not any(not is_ground(o) for _, o in parts_on(nl, net, "R")):
            findings.append(_f("info", "DG-SPI-CS", refs[0], f"Chip-select {net} has no pull-up: "
                               "the device is selected while the controller pin floats at boot."))
    return out


def check_levels(nl: Netlist, findings: list, volts: dict) -> list[dict]:
    out = []
    for net, pins in sorted(nl.nets.items()):
        if is_ground(net) or is_unconnected(net):
            continue
        drv = [p for p in pins if btype(p) in ("output", "tri_state", "bidirectional")
               and kind(p.ref) in ("U", "IC") and fitted(nl, p.ref)]
        rcv = [p for p in pins if btype(p) in ("input", "bidirectional")
               and kind(p.ref) in ("U", "IC") and fitted(nl, p.ref)]
        for d in drv:
            vd = supply_v(nl, d.ref, volts)
            for r in rcv:
                if r.ref == d.ref:
                    continue
                vr = supply_v(nl, r.ref, volts)
                if vd is None or vr is None:
                    continue
                if vd > vr + 0.3:
                    out.append({"net": net, "driver": d.ref, "v_driver": vd, "receiver": r.ref,
                                "v_receiver": vr})
                    findings.append(_f("warning", "DG-LEVEL", r.ref,
                                       f"{net}: {d.ref}.{d.name} swings to {vd:g} V into "
                                       f"{r.ref}.{r.name} supplied at {vr:g} V - over its input "
                                       "range unless that pin is tolerant (check the datasheet)."))
                elif vd < 0.7 * vr:
                    out.append({"net": net, "driver": d.ref, "v_driver": vd, "receiver": r.ref,
                                "v_receiver": vr})
                    findings.append(_f("warning", "DG-LEVEL", r.ref,
                                       f"{net}: {d.ref} at {vd:g} V drives {r.ref} at {vr:g} V: "
                                       "a CMOS VIH of 0.7 VDD is not reached."))
    return out


UART_TX = re.compile(r"^(U\d?|UART\d?_?|USART\d?_?|SCI\d?_?)?TXD?\d?$")
UART_RX = re.compile(r"^(U\d?|UART\d?_?|USART\d?_?|SCI\d?_?)?RXD?\d?$")


def check_uart_levels(nl: Netlist, findings: list, volts: dict) -> list[dict]:
    """DG-UART-LEVEL: a TX pin reaching another part's RX pin through series
    resistors (direct connections are DG-LEVEL) across supply domains: the
    driver's high level above the receiver's supply (+0.3 V) or below its
    0.7 VDD. A series resistor into a clamp diode is a current limit, not
    a level shifter - check the receiver's injection-current rating."""
    out = []
    for net, pins in sorted(nl.nets.items()):
        for d in pins:
            if not (UART_TX.match(pn(d)) and is_active(nl, d.ref) and fitted(nl, d.ref)
                    and btype(d) in ("output", "bidirectional", "tri_state", "passive")):
                continue
            seen, frontier, via = {net}, [(net, [])], {}
            for _ in range(2):
                nxt = []
                for n, path in frontier:
                    for p in nl.nets.get(n, []):
                        if kind(p.ref) != "R" or not fitted(nl, p.ref):
                            continue
                        o = other_end(nl, p.ref, n)
                        if o and o not in seen and not is_ground(o) and not _is_rail(nl, o, volts):
                            seen.add(o)
                            via[o] = path + [p.ref]
                            nxt.append((o, path + [p.ref]))
                frontier = nxt
            for o, path in via.items():
                for r in nl.nets.get(o, []):
                    if r.ref == d.ref or not UART_RX.match(pn(r)) or not is_active(nl, r.ref):
                        continue
                    vd, vr = supply_v(nl, d.ref, volts), supply_v(nl, r.ref, volts)
                    if vd is None or vr is None:
                        continue
                    row = {"tx": f"{d.ref}.{d.name}", "rx": f"{r.ref}.{r.name}", "via": path,
                           "v_tx": vd, "v_rx": vr}
                    out.append(row)
                    if vd > vr + 0.3:
                        findings.append(_f("warning", "DG-UART-LEVEL", r.ref,
                                           f"UART {d.ref}.{d.name} ({vd:g} V) drives "
                                           f"{r.ref}.{r.name} ({vr:g} V) through "
                                           f"{', '.join(path)}: above the receiver's supply - "
                                           "a series resistor only limits the clamp current "
                                           "(check the input is tolerant or its injection "
                                           "rating)."))
                    elif vd < 0.7 * vr:
                        findings.append(_f("warning", "DG-UART-LEVEL", r.ref,
                                           f"UART {d.ref}.{d.name} at {vd:g} V drives "
                                           f"{r.ref}.{r.name} at {vr:g} V through "
                                           f"{', '.join(path)}: VIH 0.7 VDD is not reached."))
    return out


CLK_NET = re.compile(r"(^|[/_])(M?CLK\w*|BCLK|BCK|SCK|LRCK|XO|CLKOUT|OSC_?OUT|MCO\d?)$", re.I)


def check_clocks(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for net, pins in sorted(nl.nets.items()):
        osc_out = any(re.fullmatch(r"CLKOUT|OSC_?OUT|MCO\d?|OUT", pn(p))
                      and "oscillator" in nl.components.get(p.ref, {}).get("description", "").lower()
                      for p in pins)
        if not (CLK_NET.search(net) or osc_out) or is_unconnected(net):
            continue
        src = [p for p in pins if btype(p) in ("output", "tri_state") and fitted(nl, p.ref)]
        loads = [p for p in pins if btype(p) == "input" and fitted(nl, p.ref)]
        if not src:
            # source behind a series resistor
            for r, o in parts_on(nl, net, "R"):
                if any(btype(q) in ("output", "tri_state") for q in nl.nets.get(o, [])):
                    src = [q for q in nl.nets.get(o, []) if btype(q) in ("output", "tri_state")]
                    out.append({"net": net, "source": f"{src[0].ref}.{src[0].name}",
                                "series_r": r, "loads": [p.ref for p in loads]})
                    break
            continue
        rec = {"net": net, "source": f"{src[0].ref}.{src[0].name}", "series_r": None,
               "loads": [f"{p.ref}.{p.name}" for p in loads]}
        out.append(rec)
        if len(loads) >= 2:
            findings.append(_f("info", "DG-CLK-TERM", src[0].ref, f"Clock {net} from "
                               f"{src[0].ref}.{src[0].name} drives {len(loads)} loads with no "
                               "series resistor at the source: ringing and EMI on a branched "
                               "clock (add 22-33 Ohm at the source or a buffer per load)."))
    return out


def check_debug(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for ref in ics(nl):
        pins = {pn(p): p for p in nl.pins.get(ref, [])}
        swd = [pins[n] for n in ("SWDIO", "SWCLK", "TMS", "TCK", "PA13", "PA14") if n in pins]
        swd = [p for p in swd if pn(p) in ("SWDIO", "SWCLK") or "SWD" in (p.name or "").upper()]
        if not swd:
            continue
        reach = {}
        for p in swd:
            nets = {p.net} | {o for _, o in parts_on(nl, p.net, "R")}
            reach[p.name] = sorted({q.ref for n in nets for q in nl.nets.get(n, [])
                                    if kind(q.ref) in CONN + ("TP",)})
        out.append({"ref": ref, "reach": reach})
        if any(not v for v in reach.values()):
            findings.append(_f("warning", "DG-SWD", ref, "Debug pins not on a connector or test "
                               "point: " + ", ".join(k for k, v in reach.items() if not v)
                               + " - the part cannot be programmed in-circuit."))
    return out


# ---- LEDs, display, misc ----------------------------------------------------- #


def _is_rail(nl: Netlist, net: str, volts: dict) -> bool:
    return is_ground(net) or net in volts or any(
        btype(q) in ("power_in", "power_out") for q in nl.nets.get(net, []))


def _led_limited(nl: Netlist, ref: str, specs: dict, volts: dict) -> bool:
    """A resistor / CCR / driver on the LED's string node. A part on a
    rail the LED sits on does not count (it is not in series)."""
    for p in nl.pins.get(ref, []):
        net = p.net
        if _is_rail(nl, net, volts):
            continue
        for q in nl.nets.get(net, []):
            if q.ref == ref or not fitted(nl, q.ref):
                continue
            k = kind(q.ref)
            if k == "R" or (specs.get(q.ref) or {}).get("category") == "current_regulator":
                return True
            if k in ("U", "IC") and btype(q) in ("output", "open_collector", "power_out", "passive"):
                return True       # a driver IC / GPIO sets or limits the current
            if k == "Q":
                return True       # transistor stage: checked by RT-LED with values
            if k == "D" and q.ref != ref and other_end(nl, q.ref, net):
                # LEDs in series: follow the string one step
                far = other_end(nl, q.ref, net)
                if any(kind(x.ref) == "R" or (specs.get(x.ref) or {}).get("category") ==
                       "current_regulator" for x in nl.nets.get(far, [])):
                    return True
    return False


def check_leds(nl: Netlist, findings: list, specs: dict, volts: dict | None = None) -> list[dict]:
    out = []
    for ref, c in sorted(nl.components.items()):
        if kind(ref) != "D" or not fitted(nl, ref):
            continue
        if (specs.get(ref) or {}).get("category") != "led" and not LED_TEXT.search(
                f"{c['lib_id']} {c['value']} {c['description']}"):
            continue
        ok = _led_limited(nl, ref, specs, volts or {})
        out.append({"ref": ref, "limited": ok})
        if not ok:
            findings.append(_f("warning", "LE-NORES", ref, "LED with no series resistor, current "
                               "regulator or driver on either pin."))
    for ref in ics(nl):
        for p in nl.pins.get(ref, []):
            if re.fullmatch(r"I?SET|RSET|IREF|ISET\d|RISET", pn(p)) and (
                    is_unconnected(p.net) or not parts_on(nl, p.net, "R")):
                if (specs.get(ref) or {}).get("category", "").startswith("regulator"):
                    continue
                out.append({"ref": ref, "pin": p.name, "net": p.net})
                findings.append(_f("warning", "LE-ISET", ref, f"Current-set pin {p.name} has no "
                                   "resistor: the driver's output current is undefined."))
    return out


DISP_CAP = re.compile(r"^(VCOMH|VCOM|C\dP|C\dN|VLSS|VCL|VGH|VGL|VSH|VSL|VDDH|VPP|VREG\w*)$")


def check_display(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for ref in sorted(set(ics(nl)) | set(_connectors(nl))):
        pins = nl.pins.get(ref, [])
        names = {pn(p) for p in pins}
        if not ({"VCOMH", "C1P", "C1N", "BS0", "BS1", "D/C", "DC", "IREF"} & names
                and len(names & {"VCOMH", "C1P", "C1N", "IREF", "VCC", "BS0", "D/C"}) >= 2):
            continue
        rec = {"ref": ref, "missing_caps": []}
        out.append(rec)
        for p in pins:
            if DISP_CAP.match(pn(p)) and not is_unconnected(p.net):
                pair = re.fullmatch(r"C(\d)([PN])", pn(p))
                if pair:
                    mate = f"C{pair.group(1)}{'N' if pair.group(2) == 'P' else 'P'}"
                    mate_net = next((q.net for q in pins if pn(q) == mate), None)
                    ok = any(o == mate_net for _, o in parts_on(nl, p.net, "C"))
                else:
                    ok = bool(caps_to_ground(nl, p.net))
                if not ok:
                    rec["missing_caps"].append(p.name)
        if rec["missing_caps"]:
            findings.append(_f("warning", "DS-CAP", ref, "Display charge-pump / bias pins without "
                               f"their capacitor: {', '.join(sorted(set(rec['missing_caps'])))}."))
    return out


def check_rtc(nl: Netlist, findings: list, pt_rails: set) -> list[dict]:
    out = []
    for ref in ics(nl):
        for p in nl.pins.get(ref, []):
            if pn(p) not in ("VBAT", "VBACKUP", "VB", "VBK"):
                continue
            rec = {"ref": ref, "pin": p.name, "net": p.net}
            out.append(rec)
            if is_unconnected(p.net):
                findings.append(_f("error", "RTC-VBAT", ref, f"{p.name} is unconnected: tie it to "
                                   "the backup cell or to the supply (datasheet: no backup)."))
            elif p.net in pt_rails:
                bat = [q for q in nl.nets.get(p.net, []) if kind(q.ref) in ("BT", "B", "BAT")]
                if not bat:
                    findings.append(_f("info", "RTC-VBAT", ref, f"{p.name} sits on rail {p.net}: "
                                       "no backup source, the clock stops with the supply."))
    return out


def check_buttons(nl: Netlist, findings: list) -> list[dict]:
    out = []
    sw = [r for r in nl.components if kind(r) in ("SW", "S", "BTN", "K") and fitted(nl, r)]
    nets_by_sw = {}
    for s in sw:
        nets = [p.net for p in nl.pins.get(s, []) if not is_unconnected(p.net)]
        nets_by_sw[s] = set(nets)
        if len(set(nets)) != 2:
            continue
        g = [n for n in nets if is_ground(n)]
        if not g:
            continue
        sig = next(n for n in set(nets) if not is_ground(n))
        ics_on = [p for p in nl.nets.get(sig, []) if kind(p.ref) in ("U", "IC")]
        pulls = [r for r, o in parts_on(nl, sig, "R") if not is_ground(o)]
        caps = caps_to_ground(nl, sig)
        out.append({"ref": s, "net": sig, "pull_up": pulls, "debounce_caps": caps})
        if ics_on and not pulls:
            findings.append(_f("info", "KB-PULL", s, f"Button {s} pulls {sig} to ground with no "
                               "pull-up resistor: relies on the input's internal pull-up "
                               "(enable it in firmware)."))
    # switch matrix: >= 4 switches on shared row / column nets, no diodes
    shared = defaultdict(set)
    for s, nets in nets_by_sw.items():
        for n in nets:
            if not is_ground(n):
                shared[n].add(s)
    multi = {n: s for n, s in shared.items() if len(s) >= 2}
    in_matrix = {s for ss in multi.values() for s in ss}
    if len(in_matrix) >= 4 and len(multi) >= 4:
        diodes = [r for r in nl.components if kind(r) == "D" and fitted(nl, r)
                  and any(p.net in multi for p in nl.pins.get(r, []))]
        if not diodes:
            findings.append(_f("warning", "KB-GHOST", sorted(in_matrix)[0],
                               f"{len(in_matrix)} switches in a matrix on {len(multi)} shared nets "
                               "with no diodes: pressing three keys reads a fourth (ghosting)."))
    return out


# ---- driver ---------------------------------------------------------------- #


def analyze(nl: Netlist, pt: dict, specs: dict | None = None, groups: tuple = GROUPS,
            internal_connectors: set | None = None, audio_f_low_hz: float = 20.0,
            fb_z_max: float = 100e3, i2c_mode: str = "fast",
            i2c_bus_capacitance_pf: dict | None = None, source_ohm: float | None = None,
            inrush_limit_a: float | None = None) -> dict:
    specs = specs or {}
    volts = {n: v["volts"] for n, v in pt["rail_voltages"].items()}
    findings: list[dict] = []
    res: dict = {}
    if "power" in groups:
        res["usb_c"] = check_usbc(nl, findings, specs, volts)
        res["supply_entry"] = check_supply_entry(nl, findings, specs, board_sourced(pt))
        res["reverse_polarity"] = check_reverse_polarity(nl, findings, specs, board_sourced(pt),
                                                         internal_connectors)
        res["inrush"] = check_inrush(nl, findings, specs, volts, pt, board_sourced(pt),
                                     source_ohm, inrush_limit_a)
        res["esd"] = check_esd_audit(nl, findings, specs, internal_connectors or set())
        res["sequencing"] = check_sequencing(nl, findings, pt)
        res["reset"] = check_reset(nl, findings)
        res["feedback"] = check_feedback(nl, findings, pt, specs, fb_z_max)
    if "analog" in groups:
        res["opamps"] = check_audio(nl, findings, specs, audio_f_low_hz)
        res["converters"] = check_converters(nl, findings, specs)
        res["jacks"] = check_jacks(nl, findings)
        res["level_shifters"] = check_level_shifters(nl, findings, volts)
    if "digital" in groups:
        res["floating_inputs"] = check_floating(nl, findings, specs)
        res["boot_pins"] = check_boot(nl, findings, {r["ref"] for r in pt["regulators"]})
        res["i2c_pullups"] = check_i2c_pullup(nl, findings, volts, specs, i2c_mode,
                                              i2c_bus_capacitance_pf)
        res["spi"] = check_spi(nl, findings)
        res["levels"] = check_levels(nl, findings, volts)
        res["uart_levels"] = check_uart_levels(nl, findings, volts)
        res["clocks"] = check_clocks(nl, findings)
        res["debug"] = check_debug(nl, findings)
    if "misc" in groups:
        res["leds"] = check_leds(nl, findings, specs, volts)
        res["displays"] = check_display(nl, findings)
        res["rtc"] = check_rtc(nl, findings, set(volts))
        res["buttons"] = check_buttons(nl, findings)
    if set(groups) & {"interfaces", "power_conversion", "drives", "rf"}:
        from kicad_claude.adapters import domain_review
        domain_review.analyze(nl, pt, specs, groups, findings, res)
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["rule"], f["ref"]))
    res["findings"] = findings
    res["counts"] = {k: sum(1 for f in findings if f["severity"] == k) for k in order}
    return res
