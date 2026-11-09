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
           pin), PW-ESD (external connector signal nets without a clamp);
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
           (pull-up below the 3 mA sink limit of the I2C spec), DG-SPI-CS
           (chip select shared / no pull-up), DG-USB-ESD, DG-LEVEL (driver
           supply vs receiver supply), DG-CLK-TERM (clock with several loads
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

GROUPS = ("power", "analog", "digital", "misc")
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


def ics(nl: Netlist) -> list[str]:
    return sorted(r for r in nl.components if kind(r) in ("U", "IC") and fitted(nl, r))


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
        if btype(p) in DRIVERS or k in ("R", "L", "FB", "D", "Q", "Y", "X", "SW", "JP", "SJ", "RN") \
                or k in CONN:
            return True
    return False


def _connectors(nl: Netlist) -> list[str]:
    return sorted(r for r in nl.components if kind(r) in CONN and fitted(nl, r))


# ---- power & protection ------------------------------------------------------- #

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
            ctrl = [p for p in nl.nets.get(net, []) if kind(p.ref) in ("U", "IC")
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


def check_supply_entry(nl: Netlist, findings: list, specs: dict) -> list[dict]:
    """Supply pins of connectors: a clamp on the net, and a ground pin."""
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
            if not clamped(nl, n, specs):
                findings.append(_f("warning", "PW-VBUS-TVS", j, f"Supply net {n} from {j} has "
                                   "no TVS / clamp: hot-plug and ESD transients reach the input."))
    return out


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

_OPA = re.compile(r"^(?P<pre>\d?)(?:(?P<s>[+-])IN|IN(?P<s2>[+-]))(?P<ch>[A-D\d]?)$|"
                  r"^(?P<opre>\d?)OUT(?P<och>[A-D\d]?)$")


def opamp_channels(nl: Netlist, specs: dict) -> dict[str, dict[str, dict[str, str]]]:
    """{ref: {channel: {"+": net, "-": net, "out": net}}} for parts with
    op-amp pin names (or category opamp)."""
    out = {}
    for ref in ics(nl):
        ch: dict[str, dict] = defaultdict(dict)
        for p in nl.pins.get(ref, []):
            m = _OPA.match(pn(p))
            if not m:
                continue
            if m.group("opre") is not None or m.group("och") is not None and "OUT" in pn(p):
                c = m.group("opre") or m.group("och") or ""
                ch[c]["out"] = p.net
            else:
                c = m.group("pre") or m.group("ch") or ""
                ch[c][m.group("s") or m.group("s2")] = p.net
        full = {c: v for c, v in ch.items() if {"+", "-", "out"} <= set(v)}
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
            if is_unconnected(p.net):
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
                findings.append(_f("warning", "DG-FLOAT", ref, f"Input {p.name} (pin {p.number}) "
                                   "is unconnected: a floating CMOS input draws current and "
                                   "toggles; tie it unless the datasheet gives an internal pull."))
            elif not is_ground(p.net) and not has_driver(nl, p.net):
                others = [q for q in nl.nets.get(p.net, []) if q.ref != ref]
                if not any(btype(q) == "power_in" for q in others) and not any(
                        kind(q.ref) == "C" for q in others):
                    out.append({"ref": ref, "pin": p.name, "net": p.net})
                    findings.append(_f("error", "DG-FLOAT", ref, f"Input {p.name} on {p.net} has "
                                       "no driver, pull resistor or strap: it floats."))
    return out


BOOT_RE = re.compile(r"^(BOOT\d?|BOOTSEL|MODE\d?|MD\d?|PROG|IO0|GPIO0|STRAP\w*|CFG\d?)$")


def check_boot(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for ref in ics(nl):
        for p in nl.pins.get(ref, []):
            if not BOOT_RE.match(pn(p)) or is_unconnected(p.net):
                continue
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


def check_i2c_pullup(nl: Netlist, findings: list, volts: dict) -> list[dict]:
    """Pull-up strength vs the I2C spec's 3 mA low-level sink (VOL 0.4 V)."""
    out = []
    for net in sorted(nl.nets):
        names = {pn(p) for p in nl.nets[net]}
        line = "SDA" if any(re.search(r"SDA", n) for n in names) else \
            "SCL" if any(re.search(r"SCL", n) for n in names) else None
        if not line:
            continue
        for r, o in parts_on(nl, net, "R"):
            v = volts.get(o)
            ohm = parse_value(nl.components[r]["value"])
            if v is None or not ohm or is_ground(o):
                continue
            i = (v - 0.4) / ohm
            out.append({"net": net, "ref": r, "ohms": ohm, "rail_v": v, "sink_ma": round(i * 1e3, 2)})
            if i > 0.003:
                findings.append(_f("error", "DG-I2C-RPU", r, f"{line} pull-up {r} "
                                   f"{nl.components[r]['value']} to {v:g} V needs {i * 1e3:.1f} mA "
                                   "to pull low: above the 3 mA standard / fast-mode sink."))
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
            fb_z_max: float = 100e3) -> dict:
    specs = specs or {}
    volts = {n: v["volts"] for n, v in pt["rail_voltages"].items()}
    findings: list[dict] = []
    res: dict = {}
    if "power" in groups:
        res["usb_c"] = check_usbc(nl, findings, specs, volts)
        res["supply_entry"] = check_supply_entry(nl, findings, specs)
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
        res["boot_pins"] = check_boot(nl, findings)
        res["i2c_pullups"] = check_i2c_pullup(nl, findings, volts)
        res["spi"] = check_spi(nl, findings)
        res["levels"] = check_levels(nl, findings, volts)
        res["clocks"] = check_clocks(nl, findings)
        res["debug"] = check_debug(nl, findings)
    if "misc" in groups:
        res["leds"] = check_leds(nl, findings, specs, volts)
        res["displays"] = check_display(nl, findings)
        res["rtc"] = check_rtc(nl, findings, set(volts))
        res["buttons"] = check_buttons(nl, findings)
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["rule"], f["ref"]))
    res["findings"] = findings
    res["counts"] = {k: sum(1 for f in findings if f["severity"] == k) for k in order}
    return res
