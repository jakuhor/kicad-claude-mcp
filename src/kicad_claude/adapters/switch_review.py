"""High-side load switches, the data lines that leave with their rail, and
fuse selectivity (`review_circuits` group `power`, rules `SW-*`).

A load switch here is a P-channel MOSFET whose source sits on a supply
rail and whose drain feeds a load. Around it the detector looks for the
parts such a stage is built from, by connectivity only:

  pull-up      resistor gate - source: off when nothing drives the gate
  zener        anode on the gate, cathode on the source: V_GS clamp
  driver       an N-FET / NPN pulling the gate down, directly or through a
               resistor, or an IC output doing the same; its gate is the
               switch's command net
  reset hold   a resistor command net - ground: the driver stays off while
               the MCU pin is high-impedance in reset / boot
  miller       a capacitor drain - gate, directly or through a steering
               diode (soft start)

Gate voltages come from the DC operating-point model (`dc_model.solve`) with
the driver on and off, so the zener clamp and the divider are those of the
schematic, not a rule of thumb. Ratings come from the spec cache:
absolute_max.VGS (V_GSS), electrical.RDS_on (its conditions name the V_GS it
is rated at, "VGS = -10 V"), zener VZ; without them the checks fall back to
a 4.5 V logic-level floor and a 20 V gate limit and say so.

Data lines that leave on the same connector as a switched rail must not
drive an unpowered load: the driving IC needs an enable that follows the
switch command and is inactive by default (SW-BACKPOWER, SW-OE,
SW-OE-DEFAULT, SW-DATA-PULL).

Fuse selectivity: a fuse whose series path to the supply connector passes
another fuse must open first on its own branch's fault — smaller melting
I²t and rating than the upstream fuse (SW-FUSE-SEL), from electrical.I2t /
I_rated in the spec cache.

Rules:
  SW-OFF-DEFAULT  error    no gate pull-up: the switch floats on when released
  SW-RESET        warning  command net with no pull-down: the driver can turn on
                           while the MCU pin is high-impedance
  SW-VGS-ON       warning  on-state |V_GS| below the R_DS(on) rating / 4.5 V
  SW-VGS-MAX      error / warning  |V_GS| above V_GSS / above 20 V (no spec)
  SW-ZENER        warning  gate zener VZ not below V_GSS
  SW-SOFTSTART    info     no Miller capacitor: turn-on dV/dt not limited
  SW-BACKPOWER    warning  an IC drives a pin of the switched connector with no
                           enable: it back-powers an unpowered load
  SW-OE           warning  that enable does not follow the switch command
  SW-OE-DEFAULT   warning  that enable has no resistor to its inactive level
  SW-DATA-PULL    info     the line has no resistor holding it while disabled
  SW-FUSE-SEL     warning / info  branch fuse not smaller than the upstream fuse
                           (I²t or rating) / values not in the spec cache
"""

from __future__ import annotations

import re

from kicad_claude.adapters import datasheet_specs as dss
from kicad_claude.adapters import dc_model
from kicad_claude.adapters.power_review import (
    Netlist,
    _two_terminal,
    is_ground,
    is_unconnected,
    kind,
)

LOGIC_LEVEL_V = 4.5     # V_GS a logic-level FET is rated at; floor without a spec
GATE_LIMIT_V = 20.0     # common V_GSS when the spec cache has none
DRIVER_OUT = {"output", "bidirectional", "tri_state", "open_collector"}
ENABLE_PIN = re.compile(r"^~?\{?(OE|G|EN|ENABLE)\}?(_?N|B|#)?\d*$", re.I)
ACTIVE_LOW = re.compile(r"^~|\{|(_N|B|#)\d*$|^nOE|^OEN", re.I)


def _f(sev: str, rule: str, ref: str, msg: str, **kw) -> dict:
    return {"severity": sev, "rule": rule, "ref": ref, "message": msg, **kw}


def _fitted(nl: Netlist, ref: str) -> bool:
    return not nl.components.get(ref, {}).get("dnp")


def _between(nl: Netlist, a: str, b: str, kinds: tuple) -> list[str]:
    """Fitted two-terminal parts of `kinds` with one end on each net."""
    out = []
    for p in nl.nets.get(a, []):
        if kind(p.ref) in kinds and _fitted(nl, p.ref):
            ends = _two_terminal(nl, p.ref)
            if ends and set(ends) == {a, b}:
                out.append(p.ref)
    return sorted(set(out))


def _two_on(nl: Netlist, net: str, kinds: tuple) -> list[tuple[str, str]]:
    """(ref, other net) for fitted two-terminal parts of `kinds` on `net`."""
    out = []
    for p in nl.nets.get(net, []):
        if kind(p.ref) in kinds and _fitted(nl, p.ref):
            ends = _two_terminal(nl, p.ref)
            if ends and net in ends:
                o = ends[1] if ends[0] == net else ends[0]
                if o != net:
                    out.append((p.ref, o))
    return sorted(set(out))


def _ends(nl: Netlist, ref: str) -> tuple[str, str] | None:
    """(anode, cathode) by pin name A / K, else Device:D numbering."""
    pins = nl.pins.get(ref, [])
    by = {dc_model._pn(p.name): p.net for p in pins}
    if "A" in by and "K" in by:
        return by["A"], by["K"]
    num = {p.number: p.net for p in pins}
    return (num["2"], num["1"]) if {"1", "2"} <= set(num) else None


def _is_zener(nl: Netlist, ref: str, specs: dict) -> bool:
    c = nl.components.get(ref, {})
    text = f"{c.get('lib_id', '')} {c.get('value', '')} {c.get('description', '')}"
    return (specs.get(ref) or {}).get("category") == "zener" or bool(
        dc_model.ZENER_TEXT.search(text))


def _is_bjt(nl: Netlist, ref: str) -> bool:
    c = nl.components.get(ref, {})
    text = f"{c.get('lib_id', '')} {c.get('value', '')} {c.get('description', '')}"
    names = {dc_model._pn(p.name) for p in nl.pins.get(ref, [])}
    return bool(re.search(r"PNP|NPN|BJT|bipolar", text, re.I)) or bool(names & {"B", "C", "E"})


def _rds_rated_vgs(rec: dict | None) -> tuple[float, str] | None:
    """Largest |V_GS| named in the conditions of an RDS(on) spec value."""
    best = None
    for k, sv in ((rec or {}).get("electrical") or {}).items():
        if not k.upper().replace("(", "").replace(")", "").replace("_", "").startswith("RDSON"):
            continue
        m = re.search(r"V_?GS\s*=\s*(-?\d+(?:\.\d+)?)\s*V", str(sv.get("conditions", "")), re.I)
        if m and (best is None or abs(float(m.group(1))) > best[0]):
            best = (abs(float(m.group(1))), f"{rec.get('mpn')} {k} p.{sv.get('page')}")
    return best


def _command(nl: Netlist, gate: str, specs: dict) -> dict:
    """How the gate is pulled toward on: driver part, its command net."""
    hops = [(None, gate)] + [(r, o) for r, o in _two_on(nl, gate, ("R",))]
    for series, node in hops:
        for p in nl.nets.get(node, []):
            if p.ref == series:
                continue
            if kind(p.ref) == "Q" and dc_model.polarity(nl, p.ref, specs) == "N":
                for d in dc_model.switch_dies(nl, p.ref):
                    dn, sn = d["main"]
                    if dn == node and is_ground(sn) and d["gate"]:
                        return {"driver": p.ref, "series_r": series, "command": d["gate"],
                                "kind": "fet"}
            if kind(p.ref) in ("U", "IC") and p.type.split("+")[0] in DRIVER_OUT:
                return {"driver": f"{p.ref}.{p.name or p.number}", "series_r": series,
                        "command": node, "kind": "ic"}
    return {}


def _ic_driven(nl: Netlist, net: str) -> list[str]:
    """IC output pins on `net` or one series resistor away."""
    out = []
    for n in [net] + [o for _r, o in _two_on(nl, net, ("R",))]:
        out += [f"{p.ref}.{p.name or p.number}" for p in nl.nets.get(n, [])
                if kind(p.ref) in ("U", "IC") and p.type.split("+")[0] in DRIVER_OUT]
    return sorted(set(out))


def _gate_v(nl, known, specs, driver_state: dict, gate: str, src: str):
    res = dc_model.solve(nl, known, specs, driver_state)
    v = res["volts"]
    if gate in v and src in v:
        return v[gate][0] - v[src][0], res
    return None, res


def check_load_switches(nl: Netlist, findings: list, specs: dict, volts: dict) -> list[dict]:
    from kicad_claude.adapters.circuit_review import _supply_net

    known = {n: (float(v), "rail") for n, v in volts.items()}
    rows = []
    for q in dc_model.switches_in(nl):
        if dc_model.polarity(nl, q, specs) != "P" or _is_bjt(nl, q):
            continue  # a PNP's open base does not conduct: no pull-up rule for it
        for die in dc_model.switch_dies(nl, q):
            d, s = die["main"]
            g = die["gate"]
            if not g or is_ground(s) or is_ground(d) or is_unconnected(d):
                continue
            if s not in volts and not _supply_net(nl, s):
                continue
            rec = specs.get(q)
            pull_up = _between(nl, g, s, ("R", "RN"))
            zeners = [z for z in _between(nl, g, s, ("D",)) if _is_zener(nl, z, specs)
                      and _ends(nl, z) == (g, s)]
            cmd = _command(nl, g, specs)
            miller = _between(nl, d, g, ("C",))
            for c, mid in _two_on(nl, d, ("C",)):
                for dref in _between(nl, mid, g, ("D",)):
                    if _ends(nl, dref) == (mid, g):
                        miller.append(f"{c}+{dref}")
            row = {"ref": q, "rail": s, "load": d, "gate": g, "pull_up": pull_up,
                   "zener": zeners, "driver": cmd.get("driver"), "command": cmd.get("command"),
                   "miller": miller}
            rows.append(row)
            if not pull_up:
                findings.append(_f("error", "SW-OFF-DEFAULT", q,
                                   f"High-side {q} ({s} -> {d}): no resistor gate-source; "
                                   f"released, its gate {g} floats and the switch can conduct."))
            hold = []
            if cmd.get("kind") == "fet":
                hold = [r for r, o in _two_on(nl, cmd["command"], ("R", "RN")) if is_ground(o)]
                row["reset_hold"] = hold
                if not hold:
                    findings.append(_f("warning", "SW-RESET", cmd["driver"],
                                       f"{cmd['driver']} switches {q} on from {cmd['command']}, "
                                       "which has no resistor to ground: while the driving pin "
                                       f"is high-impedance (reset, boot) {q} can turn on."))
            if s not in volts:
                row["vgs"] = None
                row["note"] = f"{s} voltage unknown: pass rail_voltages to check V_GS"
                continue
            vgss = dss.rated(rec, "absolute_max", "VGS", "VGSS", prefer="max")
            on = off = None
            if cmd.get("kind") == "fet":
                on, res_on = _gate_v(nl, known, specs, {cmd["driver"]: "on"}, g, s)
                off, _ = _gate_v(nl, known, specs, {cmd["driver"]: "off"}, g, s)
                row["clamps_on"] = [c["ref"] for c in res_on["clamps"]]
            elif pull_up:
                off, _ = _gate_v(nl, known, specs, {}, g, s)
            row["vgs"] = {"on": None if on is None else round(on, 3),
                          "off": None if off is None else round(off, 3)}
            if off is not None and abs(off) > 1.0:
                findings.append(_f("error", "SW-OFF-DEFAULT", q,
                                   f"{q} V_GS {off:.3g} V with its driver off: not off."))
            if on is not None:
                need = _rds_rated_vgs(rec)
                if need and abs(on) < need[0]:
                    findings.append(_f("warning", "SW-VGS-ON", q,
                                       f"{q} on-state |V_GS| {abs(on):.3g} V at {s} = "
                                       f"{volts[s]:g} V, below the {need[0]:g} V its R_DS(on) is "
                                       f"rated at ({need[1]})."))
                elif not need and abs(on) < LOGIC_LEVEL_V:
                    findings.append(_f("warning", "SW-VGS-ON", q,
                                       f"{q} on-state |V_GS| {abs(on):.3g} V at {s} = "
                                       f"{volts[s]:g} V, below {LOGIC_LEVEL_V:g} V (no R_DS(on) "
                                       "rating in the spec cache)."))
                if vgss and abs(on) > vgss[0]:
                    findings.append(_f("error", "SW-VGS-MAX", q,
                                       f"{q} |V_GS| {abs(on):.3g} V above V_GSS {vgss[0]:g} V "
                                       f"({rec.get('mpn')} p.{vgss[1]})."))
                elif not vgss and abs(on) > GATE_LIMIT_V:
                    findings.append(_f("warning", "SW-VGS-MAX", q,
                                       f"{q} |V_GS| {abs(on):.3g} V above {GATE_LIMIT_V:g} V, the "
                                       "usual V_GSS (none in the spec cache)."))
            if vgss:
                for z in zeners:
                    zrec = next((x for x in dc_model.zeners(nl, specs) if x["ref"] == z), None)
                    if zrec and zrec["vz"] is not None and zrec["vz"] >= vgss[0]:
                        findings.append(_f("warning", "SW-ZENER", z,
                                           f"Gate zener {z} VZ {zrec['vz']:g} V is not below "
                                           f"{q}'s V_GSS {vgss[0]:g} V: it does not protect the gate."))
            if not miller:
                findings.append(_f("info", "SW-SOFTSTART", q,
                                   f"{q} has no drain-gate capacitor: turn-on dV/dt is set by "
                                   "the gate drive alone; a capacitive load's inrush is limited "
                                   "only by the source."))
    return rows


def _downstream_nets(nl: Netlist, net: str) -> set:
    """`net` and nets one fuse / ferrite / inductor / <= 1 Ohm away, twice."""
    seen = {net}
    frontier = [net]
    for _ in range(2):
        nxt = []
        for n in frontier:
            for ref, o in _two_on(nl, n, ("F", "FU", "FB", "L", "R")):
                if kind(ref) == "R":
                    from kicad_claude.adapters.power_review import parse_value
                    v = parse_value(nl.components[ref]["value"])
                    if v is None or v > 1.0:
                        continue
                if o not in seen and not is_ground(o):
                    seen.add(o)
                    nxt.append(o)
        frontier = nxt
    return seen


def _follows(nl: Netlist, en: str, command: str, specs: dict) -> bool:
    """`en` is the command net, a resistor away, or an N-FET inverter of it."""
    if en == command or command in {o for _r, o in _two_on(nl, en, ("R",))}:
        return True
    for p in nl.nets.get(en, []):
        if kind(p.ref) == "Q":
            for d in dc_model.switch_dies(nl, p.ref):
                if d["main"][0] == en and is_ground(d["main"][1]) and d["gate"] == command:
                    return True
    return False


# Single-gate 3-state buffers whose symbols leave the pins unnamed:
# 74xG125 OE (pin 1) active low, 74xG126 active high.
_ONE_GATE_OE = ((re.compile(r"1G125|1G240", re.I), "1", True),
                (re.compile(r"1G126|1G241", re.I), "1", False))


def _enable_pin(nl: Netlist, ic: str):
    """(pin, active_low) of an IC's output enable, or None."""
    for p in nl.pins.get(ic, []):
        if ENABLE_PIN.match(p.name or ""):
            return p, bool(ACTIVE_LOW.search(p.name or ""))
    c = nl.components.get(ic, {})
    text = f"{c.get('lib_id', '')} {c.get('value', '')}"
    for rx, num, low in _ONE_GATE_OE:
        if rx.search(text):
            p = next((x for x in nl.pins.get(ic, []) if x.number == num), None)
            if p is not None:
                return p, low
    return None


def check_data_enables(nl: Netlist, findings: list, specs: dict, switches: list) -> list[dict]:
    rows = []
    for sw in switches:
        rail_nets = _downstream_nets(nl, sw["load"])
        conns = {p.ref for n in rail_nets for p in nl.nets.get(n, [])
                 if kind(p.ref) in ("J", "P", "CN", "X")}
        for j in sorted(conns):
            for cp in nl.pins.get(j, []):
                n = cp.net
                if n in rail_nets or is_ground(n) or is_unconnected(n):
                    continue
                for drv in _ic_driven(nl, n):
                    ic = drv.split(".")[0]
                    row = {"switch": sw["ref"], "connector": j, "net": n, "driver": drv}
                    rows.append(row)
                    en_pin = _enable_pin(nl, ic)
                    if en_pin is None:
                        findings.append(_f("warning", "SW-BACKPOWER", ic,
                                           f"{drv} drives {j} ({n}) with no enable pin: with "
                                           f"{sw['ref']} off it can back-power the load through "
                                           "that line."))
                        continue
                    en, low = en_pin
                    row["enable"] = f"{ic}.{en.name or en.number}"
                    if sw.get("command") and not _follows(nl, en.net, sw["command"], specs):
                        findings.append(_f("warning", "SW-OE", ic,
                                           f"{ic}.{en.name} ({en.net}) does not follow "
                                           f"{sw['ref']}'s command {sw['command']}: the line can "
                                           f"be driven while {sw['load']} is off."))
                    pulls = _two_on(nl, en.net, ("R", "RN"))
                    to_inactive = [r for r, o in pulls if (not is_ground(o)) == low]
                    row["enable_default"] = to_inactive
                    if not to_inactive:
                        findings.append(_f("warning", "SW-OE-DEFAULT", ic,
                                           f"{ic}.{en.name} ({en.net}) has no resistor to its "
                                           f"{'high' if low else 'low'} (disabled) level."))
                    if not any(is_ground(o) for _r, o in _two_on(nl, n, ("R", "RN"))):
                        findings.append(_f("info", "SW-DATA-PULL", j,
                                           f"{n} on {j} has no resistor to ground: it floats "
                                           f"while {ic} is disabled."))
    return rows


def check_fuse_selectivity(nl: Netlist, findings: list, specs: dict) -> list[dict]:
    from kicad_claude.adapters import part_ratings

    fuses = sorted(r for r in nl.components if kind(r) in ("F", "FU") and _fitted(nl, r))
    rows = []
    for f in fuses:
        ends = _two_terminal(nl, f)
        if not ends:
            continue
        up = None
        for n in ends:
            # the end toward the supply: its path to the connector avoids f
            ch = part_ratings.series_chain(nl, n, specs)
            refs = [p["ref"] for p in ch["parts"]]
            if ch["connectors"] and f not in refs:
                up = next((r for r in refs if kind(r) in ("F", "FU")), None)
                break
        if not up:
            continue
        a, b = specs.get(f), specs.get(up)
        i2t_f = dss.rated(a, "electrical", "I2t", "I2t_melt", prefer="typ")
        i2t_u = dss.rated(b, "electrical", "I2t", "I2t_melt", prefer="typ")
        ir_f = dss.rated(a, "electrical", "I_rated", "I_hold", prefer="typ")
        ir_u = dss.rated(b, "electrical", "I_rated", "I_hold", prefer="typ")
        row = {"fuse": f, "upstream": up, "i2t": i2t_f and i2t_f[0],
               "i2t_upstream": i2t_u and i2t_u[0]}
        rows.append(row)
        if i2t_f and i2t_u:
            ratio = i2t_f[0] / i2t_u[0]
            row["i2t_ratio"] = round(ratio, 3)
            if ratio >= 1.0:
                findings.append(_f("warning", "SW-FUSE-SEL", f,
                                   f"{f} melting I²t {i2t_f[0]:g} A²s is not below upstream "
                                   f"{up}'s {i2t_u[0]:g} A²s: a fault behind {f} can open {up} "
                                   "and drop everything it feeds."))
        elif ir_f and ir_u and ir_f[0] > ir_u[0]:
            findings.append(_f("warning", "SW-FUSE-SEL", f,
                               f"{f} rating {ir_f[0]:g} A above upstream {up}'s {ir_u[0]:g} A."))
        else:
            findings.append(_f("info", "SW-FUSE-SEL", f,
                               f"{f} sits behind {up}: selectivity not checked - melting I²t "
                               "(electrical.I2t) of both is not in the spec cache."))
    return rows
