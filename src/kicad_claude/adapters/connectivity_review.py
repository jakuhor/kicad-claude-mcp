"""Netlist connectivity review.

Datasheet-free checks on the kicadsexpr netlist `power_review.parse_netlist`
reads:

  - IC power pins: every `power_in` pin of an active part, which rail it sits
    on, whether that rail has a source (regulator output, `power_out` pin,
    connector, or a series L / ferrite / R / diode path from one), supply
    pins tied to ground and ground pins on a non-ground net;
  - decoupling: capacitors from each IC supply net to ground (netlist only —
    physical proximity is the PCB tool `validate_decoupling_caps`);
  - open-drain / open-collector outputs without a pull-up (open-emitter:
    pull-down) and without another driver on the net;
  - I2C buses (pins named SDA / SCL, also prefixed: HPI_SDA, I2C1_SCL):
    pull-ups per line, pull-up rail mismatch, and address conflicts between
    identical parts with identical address-pin strapping.

What it does NOT do: resolve a 7-bit I2C address, check a pull-up value
against a bus spec, or say a pin is on the *wrong* rail — each needs the
part's datasheet (the spec cache). The per-IC pin -> rail map is reported so the
caller can compare it with the datasheet.

Rule ideas follow kicad-happy 3.0.1 `validation_detectors.py` /
`domain_detectors.py` (MIT License, Copyright (c) 2025 Andrew Klofas); its
built-in I2C address table is deliberately not copied (project rule: no
part constants from general knowledge).
"""

from __future__ import annotations

import re
from collections import defaultdict

from kicad_claude.adapters import power_review
from kicad_claude.adapters.power_review import (
    SUPPLY_PIN_RE,
    Netlist,
    Pin,
    detect_regulators,
    is_ground,
    is_unconnected,
    kind,
    parse_value,
)

PASSIVE_KINDS = {"R", "C", "L", "FB", "FL", "D", "TP", "J", "P", "CN", "SW",
                 "Y", "X", "F", "H", "MH", "JP", "LED", "RV", "VR", "T", "NT"}
DRIVER_TYPES = {"output", "power_out", "bidirectional", "tri_state"}
LINK_KINDS = {"L", "FB", "FL", "R", "D"}
GND_PIN_RE = re.compile(r"^(A|D|P|S|E)?GND\d*$|^EP$|^EPAD$|^PAD$|^THERMAL", re.I)
ADDR_PIN_RE = re.compile(r"^(A\d|ADDR\d*|ADD\d|AD\d|SA\d|ADR\d*|ADDR/\w+)$", re.I)
_I2C_SPLIT = re.compile(r"[_/\s.-]")


def base_type(p: Pin) -> str:
    return p.type.split("+")[0]


def _active(nl: Netlist, ref: str) -> bool:
    return kind(ref) not in PASSIVE_KINDS and not nl.components.get(ref, {}).get("dnp")


def _two_terminal_other(nl: Netlist, ref: str, net: str) -> str | None:
    pins = nl.pins.get(ref, [])
    if len(pins) != 2:
        return None
    a, b = pins[0].net, pins[1].net
    if a == b:
        return None
    return b if a == net else a if b == net else None


def _resistors(nl: Netlist, net: str) -> list[dict]:
    out = []
    for p in nl.nets.get(net, []):
        if kind(p.ref) != "R":
            continue
        other = _two_terminal_other(nl, p.ref, net)
        comp = nl.components.get(p.ref, {})
        if other is not None:
            out.append({"ref": p.ref, "value": comp.get("value", ""),
                        "ohms": parse_value(comp.get("value", "")), "to": other,
                        "dnp": comp.get("dnp", False)})
    return out


def _caps_to_ground(nl: Netlist, net: str) -> list[dict]:
    out = []
    for p in nl.nets.get(net, []):
        if kind(p.ref) != "C":
            continue
        other = _two_terminal_other(nl, p.ref, net)
        comp = nl.components.get(p.ref, {})
        if other is not None and is_ground(other):
            out.append({"ref": p.ref, "value": comp.get("value", ""),
                        "dnp": comp.get("dnp", False)})
    return out


# ---- rail sources --------------------------------------------------------- #


def _links(nl: Netlist, net: str, include_dnp: bool) -> list[tuple[str, str]]:
    """(part ref, far net) for every series path out of `net`:
    two-terminal L / ferrite / R / diode; a transistor's D-S / C-E channel
    (a load switch passes supply, per channel on duals: D1-S1); every other
    winding net of a multi-pin choke or transformer."""
    out = []
    for p in nl.nets.get(net, []):
        k = kind(p.ref)
        if not include_dnp and nl.components.get(p.ref, {}).get("dnp"):
            continue
        if k in LINK_KINDS:
            other = _two_terminal_other(nl, p.ref, net)
            if other:
                out.append((p.ref, other))
                continue
        if k == "Q":
            m = re.fullmatch(r"([DSCE])(\d*)", (p.name or "").upper())
            if m:
                mate = {"D": "S", "S": "D", "C": "E", "E": "C"}[m.group(1)] + m.group(2)
                out += [(p.ref, q.net) for q in nl.pins.get(p.ref, [])
                        if (q.name or "").upper() == mate]
        elif k in ("L", "FL", "T") and len(nl.pins.get(p.ref, [])) > 2:
            out += [(p.ref, q.net) for q in nl.pins.get(p.ref, []) if q.net != net]
    return [(r, n) for r, n in out
            if n != net and not is_ground(n) and not is_unconnected(n)]


def sourced_nets(nl: Netlist, include_dnp: bool = False) -> dict[str, str]:
    """Nets that have a supply, each mapped to how: `regulator U1`,
    `power_out U3.VOUT`, `connector J1`, or `via L11 from +7V`.

    Seeds are regulator outputs, `power_out` pins and connector pins; the set
    then grows through the series paths `_links` follows (fitted parts only
    unless `include_dnp`). A resistor path counts (RC filters feed
    supplies), so a pull-up's far end also ends up "sourced" — this check
    finds rails with no source at all, not weak ones.
    """
    src: dict[str, str] = {}
    for reg in detect_regulators(nl):
        for p in reg["roles"].get("vout", []):
            src.setdefault(p.net, f"regulator {reg['ref']}")
    for net, pins in nl.nets.items():
        if is_ground(net) or is_unconnected(net):
            continue
        for p in pins:
            if base_type(p) == "power_out" and not nl.components.get(p.ref, {}).get("dnp"):
                src.setdefault(net, f"power_out {p.ref}.{p.name or p.number}")
            elif kind(p.ref) in ("J", "P", "CN"):
                src.setdefault(net, f"connector {p.ref}")
    # Regulator outputs without a VOUT pin (FB-divider / inductor side).
    for r in power_review.analyze(nl)["regulators"]:
        if r["output_net"]:
            src.setdefault(r["output_net"], f"regulator {r['name']}")
    frontier = list(src)
    while frontier:
        net = frontier.pop()
        for ref, other in _links(nl, net, include_dnp):
            if other not in src:
                src[other] = f"via {ref} from {net}"
                frontier.append(other)
    return src


# ---- checks --------------------------------------------------------------- #


_OUT_PIN_RE = re.compile(r"(OUT\d*|VREG)$", re.I)


def check_power_pins(nl: Netlist, findings: list, sources: dict[str, str]) -> list[dict]:
    reg_pins = {(r["ref"], p.number) for r in detect_regulators(nl)
                for role in ("vin", "vout") for p in r["roles"].get(role, [])}
    with_dnp = sourced_nets(nl, include_dnp=True)
    ics = []
    for ref in sorted(nl.components):
        if not _active(nl, ref):
            continue
        pins = [p for p in nl.pins.get(ref, []) if base_type(p) == "power_in"]
        if not pins:
            continue
        rails: dict[str, list[str]] = defaultdict(list)
        unsrc: dict[str, list[str]] = defaultdict(list)
        grounds: set[str] = set()
        for p in pins:
            label = p.name or p.number
            rails[p.net].append(label)
            if is_unconnected(p.net):
                findings.append(_f("error", "CR-PWR-NC", ref,
                                   f"Power-input pin {label} ({p.number}) is not connected."))
                continue
            gnd_name = bool(GND_PIN_RE.match(p.name or ""))
            if is_ground(p.net):
                grounds.add(p.net)
                if p.name and SUPPLY_PIN_RE.match(p.name) and (ref, p.number) not in reg_pins:
                    findings.append(_f("error", "CR-PWR-GND", ref,
                                       f"Supply pin {label} sits on ground net {p.net}."))
                continue
            if gnd_name:
                findings.append(_f("error", "CR-GND-RAIL", ref,
                                   f"Ground pin {label} sits on non-ground net {p.net}."))
                continue
            if p.net in sources:
                continue
            if p.net in with_dnp:
                findings.append(_f("warning", "CR-PWR-DNP", ref,
                                   f"Pin {label} is on {p.net}, reachable only through a "
                                   f"DNP part ({with_dnp[p.net]}): unpowered as assembled."))
            else:
                unsrc[p.net].append(label)
        for net, labels in unsrc.items():
            # an output-named pin typed power_in (a module's LDO_OUT) is the
            # usual cause: name it so the symbol gets fixed, not the board
            hint = sorted({f"{q.ref}.{q.name}" for q in nl.nets.get(net, [])
                           if base_type(q) == "power_in" and _OUT_PIN_RE.search(q.name or "")})
            findings.append(_f("warning", "CR-PWR-UNSRC", ref,
                               f"Pin{'s' if len(set(labels)) > 1 else ''} "
                               f"{', '.join(sorted(set(labels)))} on {net}, which has no source: "
                               "no regulator output, power_out pin, connector, or series "
                               "L / ferrite / R / diode / transistor path from one."
                               + (f" {', '.join(hint)} looks like the source but its symbol "
                                  "types it power_in: fix the pin type." if hint else "")))
        if len(grounds) > 1:
            findings.append(_f("info", "CR-GND-MIX", ref,
                               f"Ground pins on more than one ground net: {', '.join(sorted(grounds))} "
                               "— intended split? (check the join point)."))
        ics.append({"ref": ref, "value": nl.components[ref]["value"],
                    "rails": {n: sorted(set(v)) for n, v in sorted(rails.items())},
                    "rail_sources": {n: sources.get(n) for n in rails
                                     if not is_ground(n) and not is_unconnected(n)}})
    return ics


def check_decoupling(nl: Netlist, findings: list) -> list[dict]:
    """Caps from every IC supply net to ground. Regulator VIN / VOUT pins are
    left to `analyze_power_tree` (PT-CIN / PT-COUT)."""
    reg_pins = {(r["ref"], p.number) for r in detect_regulators(nl)
                for role in ("vin", "vout") for p in r["roles"].get(role, [])}
    out = []
    for ref in sorted(nl.components):
        if not _active(nl, ref):
            continue
        nets: dict[str, list[str]] = defaultdict(list)
        for p in nl.pins.get(ref, []):
            if base_type(p) != "power_in" or (ref, p.number) in reg_pins:
                continue
            if is_ground(p.net) or is_unconnected(p.net):
                continue
            nets[p.net].append(p.name or p.number)
        for net, pin_names in sorted(nets.items()):
            caps = _caps_to_ground(nl, net)
            fitted = [c for c in caps if not c["dnp"]]
            out.append({"ref": ref, "net": net, "pins": sorted(pin_names),
                        "caps": caps, "fitted": len(fitted)})
            if not fitted:
                findings.append(_f("warning", "CR-DEC-NONE", ref,
                                   f"Supply net {net} (pins {', '.join(sorted(pin_names))}) has "
                                   "no fitted capacitor to ground."
                                   + (" Only DNP: " + ", ".join(c["ref"] for c in caps) if caps else "")))
    return out


def check_open_drain(nl: Netlist, findings: list) -> list[dict]:
    out = []
    for net, pins in sorted(nl.nets.items()):
        if is_ground(net) or is_unconnected(net):
            continue
        od = [p for p in pins if base_type(p) in ("open_collector", "open_emitter")
              and _active(nl, p.ref)]
        if not od:
            continue
        emitter = all(base_type(p) == "open_emitter" for p in od)
        rs = [r for r in _resistors(nl, net) if not r["dnp"]]
        pulls = [r for r in rs if is_ground(r["to"]) == emitter]
        drivers = [p for p in pins if base_type(p) in DRIVER_TYPES and _active(nl, p.ref)
                   and p not in od]
        others = [p for p in pins if p not in od and kind(p.ref) not in ("TP",)]
        rec = {"net": net, "outputs": [f"{p.ref}.{p.name or p.number}" for p in od],
               "pull": [{"ref": r["ref"], "value": r["value"], "to": r["to"]} for r in pulls],
               "pull_kind": "pull-down" if emitter else "pull-up",
               "other_drivers": [f"{p.ref}.{p.name or p.number}" for p in drivers]}
        out.append(rec)
        if not pulls and not drivers and others:
            findings.append(_f("warning", "CR-OD-PULL", od[0].ref,
                               f"Open-{'emitter' if emitter else 'drain'} net {net} "
                               f"({', '.join(rec['outputs'])}) has no {rec['pull_kind']} resistor "
                               "and no other driver — confirm an internal one in the "
                               "receiving part's datasheet."))
    return out


def _i2c_line(name: str) -> str | None:
    tokens = {t.upper() for t in _I2C_SPLIT.split(name) if t}
    tokens |= {re.sub(r"\d+$", "", t) for t in tokens}
    tokens = {t.strip("~{}") for t in tokens}
    if "SDA" in tokens:
        return "SDA"
    if "SCL" in tokens:
        return "SCL"
    return None


def _strap(nl: Netlist, p: Pin, sda: str, scl: str) -> str:
    if is_unconnected(p.net):
        return "nc"
    if is_ground(p.net):
        return "gnd"
    if p.net == sda:
        return "sda"
    if p.net == scl:
        return "scl"
    rs = [r for r in _resistors(nl, p.net) if not r["dnp"]]
    others = [q for q in nl.nets.get(p.net, []) if q.ref != p.ref and kind(q.ref) not in ("R", "C", "TP")]
    if not others and len(rs) == 1:
        return f"R {rs[0]['value']} to {'gnd' if is_ground(rs[0]['to']) else rs[0]['to']}"
    return p.net


def _ds_address(rec: dict | None) -> tuple[str | None, str | None]:
    """A part's single fixed I2C address from its spec record, else None.
    Strap-dependent parts list several addresses; those stay unresolved."""
    i2c = (rec or {}).get("i2c") or {}
    addrs = i2c.get("addresses") or []
    if len(addrs) == 1:
        return addrs[0].lower(), f"datasheet p.{i2c.get('page')}"
    return None, None


def check_i2c(nl: Netlist, findings: list, specs: dict | None = None) -> list[dict]:
    specs = specs or {}
    per_ref: dict[str, dict[str, str]] = defaultdict(dict)
    for ref in nl.components:
        if not _active(nl, ref):
            continue
        for p in nl.pins.get(ref, []):
            line = _i2c_line(p.name or "")
            if line and not is_unconnected(p.net) and line not in per_ref[ref]:
                per_ref[ref][line] = p.net
    buses: dict[tuple[str, str], list[str]] = defaultdict(list)
    for ref, lines in per_ref.items():
        if "SDA" in lines and "SCL" in lines:
            buses[(lines["SDA"], lines["SCL"])].append(ref)
    out = []
    for (sda, scl), refs in sorted(buses.items()):
        refs.sort()
        bus = {"sda": sda, "scl": scl, "devices": [], "pullups": {}}
        rails = {}
        for line, net in (("SDA", sda), ("SCL", scl)):
            pu = [r for r in _resistors(nl, net) if not r["dnp"] and not is_ground(r["to"])]
            bus["pullups"][line] = [{"ref": r["ref"], "value": r["value"], "to": r["to"]} for r in pu]
            rails[line] = {r["to"] for r in pu}
            if not pu:
                ext = [q.ref for q in nl.nets.get(net, []) if kind(q.ref) in ("J", "P", "CN")]
                findings.append(_f("warning" if ext else "error", "CR-I2C-PULLUP", ",".join(refs),
                                   f"I2C {line} net {net} has no pull-up resistor"
                                   + (f" on this board (leaves via {', '.join(ext)})" if ext else "")
                                   + "."))
        if rails["SDA"] and rails["SCL"] and rails["SDA"] != rails["SCL"]:
            findings.append(_f("warning", "CR-I2C-RAIL", ",".join(refs),
                               f"SDA pulled to {sorted(rails['SDA'])}, SCL to {sorted(rails['SCL'])}."))
        sigs: dict[tuple, list[str]] = defaultdict(list)
        for ref in refs:
            comp = nl.components[ref]
            addr = {p.name: _strap(nl, p, sda, scl) for p in nl.pins.get(ref, [])
                    if p.name and ADDR_PIN_RE.match(p.name)}
            address, source = _ds_address(specs.get(ref))
            bus["devices"].append({"ref": ref, "value": comp["value"],
                                   "mpn": comp["fields"].get("MPN", ""), "address_pins": addr,
                                   "address": address, "address_source": source})
            part = (comp["fields"].get("MPN") or comp["value"]).upper()
            sigs[(part, tuple(sorted(addr.items())))].append(ref)
        for (part, _), same in sigs.items():
            if len(same) > 1:
                findings.append(_f("error", "CR-I2C-ADDR", ",".join(same),
                                   f"{len(same)}x {part} on {sda}/{scl} with identical address-pin "
                                   "strapping — they answer at the same address."))
        by_addr: dict[str, list[str]] = defaultdict(list)
        for d in bus["devices"]:
            if d["address"]:
                by_addr[d["address"]].append(d["ref"])
        for a, same in sorted(by_addr.items()):
            parts = {(nl.components[r]["fields"].get("MPN") or nl.components[r]["value"]).upper()
                     for r in same}
            if len(same) > 1 and len(parts) > 1:
                findings.append(_f("error", "CR-I2C-ADDR", ",".join(same),
                                   f"{', '.join(same)} all answer at {a} on {sda}/{scl} "
                                   "(fixed addresses from their datasheets)."))
        unres = [d["ref"] for d in bus["devices"] if not d["address"]]
        if len(refs) > 1 and unres and len({d["value"] for d in bus["devices"]}) > 1:
            findings.append(_f("info", "CR-I2C-UNRES", ",".join(unres),
                               f"Bus {sda}/{scl} mixes different parts; {', '.join(unres)} "
                               "have no single datasheet address in the spec cache "
                               "(extract them, or check the strapping by hand)."))
        out.append(bus)
    return out


def _f(sev: str, rule: str, ref: str, msg: str) -> dict:
    return {"severity": sev, "rule": rule, "ref": ref, "message": msg}


# ---- datasheet-backed pin checks ------------------------------------------- #

_EXPOSED = re.compile(r"^(EP|EPAD|PAD|THERMAL.*|TAB|GND_?PAD)$", re.I)
_NO_CONNECT = re.compile(r"leave (this pin )?(unconnected|open|floating)|do not connect|"
                         r"must not be connected", re.I)


def _pin_norm(name: str) -> str:
    s = re.sub(r"~\{([^}]*)\}", r"\1", name or "").upper()
    s = re.sub(r"(?<=\d)\.(?=\d)", "P", s)          # 0.4V -> 0P4V; n.c. -> NC
    return re.sub(r"[^A-Z0-9/+-]", "", s)


_GROUND_NAMES = re.compile(r"^(A|D|P|S|E)?(GND|VSS)\d*$")
_NC_NAMES = re.compile(r"^(NC|N/C|DNU|DNC|RSVD|RESERVED)\d*$")


def _names_match(sch: str, ds: str, ds_desc: str = "") -> bool:
    a, b = _pin_norm(sch), _pin_norm(ds)
    if not a or not b or a == b:
        return True
    if _GROUND_NAMES.match(a) and _GROUND_NAMES.match(b):
        return True                       # GND vs VSS: both ground
    if _NC_NAMES.match(a) and (_NC_NAMES.match(b) or _NO_CONNECT.search(ds_desc or "")):
        return True                       # symbol NC for a leave-unconnected pin
    ta, tb = set(a.split("/")), set(b.split("/"))
    if ta & tb:
        return True
    # KiCad library names often drop a channel digit or the `V` (6.4V vs 6P4V1)
    sa, sb = re.sub(r"V?\d*$", "", a), re.sub(r"V?\d*$", "", b)
    return bool(sa) and sa == sb




def check_datasheet_pins(nl: Netlist, findings: list, specs: dict) -> list[dict]:
    """Symbol pin numbers / names against the datasheet pin table, and pins
    the datasheet says to leave unconnected that share a net with others."""
    out = []
    for ref, rec in sorted(specs.items()):
        table = {str(p["number"]): p for p in rec.get("pins") or []}
        if not table or ref not in nl.pins:
            continue
        sch = {p.number: p for p in nl.pins[ref]}
        missing = sorted(set(table) - set(sch), key=_nat)
        top = max((int(n) for n in table if n.isdigit()), default=0)
        extra = sorted((n for n in set(sch) - set(table)
                        if not _EXPOSED.match(sch[n].name or "")
                        and not (n.isdigit() and int(n) > top and is_ground(sch[n].net)
                                 and _GROUND_NAMES.match(_pin_norm(sch[n].name or "GND")))),
                       key=_nat)               # pin N+1 on ground = exposed pad
        renamed = []
        for n in sorted(set(table) & set(sch), key=_nat):
            if not _names_match(sch[n].name, table[n].get("name", ""),
                                table[n].get("description", "")):
                renamed.append({"pin": n, "symbol": sch[n].name, "datasheet": table[n]["name"]})
            desc = table[n].get("description", "")
            if _NO_CONNECT.search(desc) and not is_unconnected(sch[n].net)                     and len(nl.nets.get(sch[n].net, [])) > 1:
                findings.append(_f("warning", "CR-PIN-NC", ref,
                                   f"Pin {n} {table[n]['name']}: datasheet says \"{desc}\" "
                                   f"but it is on net {sch[n].net}."))
        src = f"{rec['pdf']['file']}"
        if missing:
            findings.append(_f("warning", "CR-PIN-MISSING", ref,
                               f"Datasheet pins {', '.join(missing)} have no symbol pin ({src})."))
        if extra:
            findings.append(_f("warning", "CR-PIN-EXTRA", ref,
                               f"Symbol pins {', '.join(extra)} are not in the datasheet pin "
                               f"table ({src})."))
        for r in renamed:
            findings.append(_f("warning", "CR-PIN-NAME", ref,
                               f"Pin {r['pin']}: symbol {r['symbol']!r}, datasheet "
                               f"{r['datasheet']!r} ({src})."))
        out.append({"ref": ref, "mpn": rec.get("mpn"), "checked": len(set(table) & set(sch)),
                    "missing": missing, "extra": extra, "renamed": renamed})
    return out


_ALIASES = {"VIN": {"IN", "VI", "PVIN", "AVIN"}, "VOUT": {"OUT", "VO"},
            "EN": {"EN/UV", "OV/UV", "SHDN", "ENABLE", "UV", "RUN"}}


def _abs_keys_for(pin_name: str, keys: list[str]) -> list[str]:
    n = _pin_norm(pin_name)
    parts = set(n.split("/")) | {n}
    hits = []
    for k in keys:
        kn = _pin_norm(k)
        names = {kn} | _ALIASES.get(kn, set())
        if parts & names or any(re.fullmatch(re.escape(x) + r"\d+", p) for x in names for p in parts):
            hits.append(k)
    return hits


def check_pin_abs_max(nl: Netlist, findings: list, specs: dict, volts: dict,
                      ic_abs: float = 0.9) -> list[dict]:
    """Each pin whose name matches an `absolute_max` key (VIN <-> IN, EN <->
    EN/UV, CC <-> CC1 ...) against its net's DC voltage (rails, dividers);
    within the limit but beyond `ic_abs` x limit (derating profile) is
    CR-PIN-DERATE."""
    from kicad_claude.adapters.power_review import _net_voltage

    out = []
    for ref, rec in sorted(specs.items()):
        if ref in nl.pins:
            out += _pin_pairs(nl, findings, ref, rec, volts, _net_voltage)
        absmax = {k: v for k, v in (rec.get("absolute_max") or {}).items()
                  if not v.get("between")}
        if not absmax or ref not in nl.pins:
            continue
        for p in nl.pins[ref]:
            if is_unconnected(p.net) or not p.name:
                continue
            for k in _abs_keys_for(p.name, list(absmax)):
                sv = absmax[k]
                if sv.get("unit") != "V":
                    continue
                v = _net_voltage(nl, p.net, volts)
                if v is None:
                    continue
                lo, hi = sv.get("min"), sv.get("max")
                ok = (lo is None or v >= lo) and (hi is None or v <= hi)
                row = {"ref": ref, "pin": f"{p.name}({p.number})", "net": p.net,
                       "volts": round(v, 4), "limit": k, "min": lo, "max": hi,
                       "page": sv.get("page"), "ok": ok}
                out.append(row)
                if not ok:
                    findings.append(_f("error", "CR-PIN-ABS", ref,
                                       f"Pin {row['pin']} on {p.net} at {v:.4g} V exceeds "
                                       f"absolute maximum {k} {lo if lo is not None else '-'}.."
                                       f"{hi if hi is not None else '-'} V "
                                       f"({rec['pdf']['file']} p.{sv.get('page')})."))
                    continue
                lim = (hi if hi is not None and hi > 0 and v > ic_abs * hi else
                       lo if lo is not None and lo < 0 and v < ic_abs * lo else None)
                if lim is not None:
                    findings.append(_f("warning", "CR-PIN-DERATE", ref,
                                       f"Pin {row['pin']} on {p.net} at {v:.4g} V is "
                                       f"{v / lim:.0%} of absolute maximum {k} {lim:g} V "
                                       f"(derating limit {ic_abs:.0%}; "
                                       f"{rec['pdf']['file']} p.{sv.get('page')})."))
    return out


def _pin_pairs(nl: Netlist, findings: list, ref: str, rec: dict, volts: dict,
               net_voltage) -> list[dict]:
    """Pin-to-pin limits (`between`): absolute_max -> error, recommended
    (e.g. a TVS standoff voltage) -> warning. Pin names come from the
    datasheet table by pin number when it exists (symbols often drop the
    channel digit: G/D/S on a dual FET)."""
    ds_names = {str(p["number"]): p.get("name", "") for p in rec.get("pins") or []}
    pins = [(ds_names.get(p.number) or p.name or "", p) for p in nl.pins[ref]]
    out, seen = [], set()
    for group, sev in (("absolute_max", "error"), ("recommended", "warning")):
        for key, sv in (rec.get(group) or {}).items():
            bt = sv.get("between")
            if not bt or sv.get("unit") != "V":
                continue
            pos, neg = _pin_norm(bt[0]), _pin_norm(bt[1])
            for name, p in pins:
                m = re.fullmatch(re.escape(pos) + r"(\d*)", _pin_norm(name))
                if not m:
                    continue
                qn, q = next(((qn, qp) for qn, qp in pins
                              if _pin_norm(qn) == neg + m.group(1)), ("", None))
                if q is None or is_unconnected(p.net) or is_unconnected(q.net):
                    continue
                k = (group, key, p.net, q.net)
                if k in seen:
                    continue
                seen.add(k)
                va, vb = net_voltage(nl, p.net, volts), net_voltage(nl, q.net, volts)
                if va is None or vb is None:
                    continue
                v = va - vb
                lo, hi = sv.get("min"), sv.get("max")
                ok = (lo is None or v >= lo) and (hi is None or v <= hi)
                row = {"ref": ref, "pin": f"{name}({p.number})-{qn}({q.number})",
                       "net": f"{p.net} - {q.net}", "volts": round(v, 4),
                       "limit": f"{group}.{key}", "min": lo, "max": hi,
                       "page": sv.get("page"), "ok": ok}
                out.append(row)
                if not ok:
                    findings.append(_f(sev, "CR-PIN-ABS" if sev == "error" else "CR-PIN-REC", ref,
                                       f"{key} {row['pin']} = {v:.4g} V ({p.net} - {q.net}) "
                                       f"outside {lo if lo is not None else '-'}.."
                                       f"{hi if hi is not None else '-'} V "
                                       f"({group}, {rec['pdf']['file']} p.{sv.get('page')})."))
    return out


def _rating(comp: dict, field: str) -> float | None:
    """Voltage rating from the `field` symbol field ("50V", "6.3 V", "6V3"),
    else a `/50V`-style suffix in Value."""
    for text in (comp["fields"].get(field, ""), comp.get("value", "")):
        m = re.search(r"(\d+(?:\.\d+)?)\s*V(\d*)\b", text or "", re.I)
        if m:
            whole, frac = m.group(1), m.group(2)
            return float(f"{whole}.{frac}") if frac and "." not in whole else float(whole)
    return None


def check_cap_voltage(nl: Netlist, findings: list, volts: dict, derating: float | None = None,
                      field: str = "Voltage", specs: dict | None = None,
                      profile: str = "commercial") -> dict:
    """DC voltage across each fitted capacitor (rails, dividers) against its
    rating: the fitted MPN's extracted datasheet (`absolute_max.V`) when
    there is one, else the symbol `field`. Error above the rating, warning
    above the derating fraction - `derating` when given, else the
    `profile`'s fraction for the capacitor's dielectric family; a field
    that disagrees with the datasheet is CR-CAP-FIELD."""
    from kicad_claude.adapters import datasheet_specs as dss
    from kicad_claude.adapters import derating as drt
    specs = specs or {}
    fractions = drt.profile(profile)["cap"]
    from kicad_claude.adapters.power_review import _net_voltage

    rows, unknown, no_rating = [], [], []
    for ref, comp in sorted(nl.components.items(), key=lambda kv: _nat(kv[0])):
        if kind(ref) != "C" or comp.get("dnp"):
            continue
        pins = nl.pins.get(ref, [])
        if len(pins) != 2:
            continue
        rating = _rating(comp, field)
        src = f"{field} field"
        ds = dss.rated(specs.get(ref), "absolute_max", "V", "V_rated") or             dss.rated(specs.get(ref), "electrical", "V_rated")
        if ds:
            mpn = specs[ref].get("mpn")
            if rating is not None and abs(rating - ds[0]) > 1e-6:
                findings.append(_f("warning", "CR-CAP-FIELD", ref,
                                   f"{field} field says {rating:g} V but {mpn} is rated "
                                   f"{ds[0]:g} V (datasheet p.{ds[1]}); the datasheet is used."))
            rating, src = ds[0], f"{mpn} datasheet p.{ds[1]}"
        if rating is None:
            no_rating.append(ref)
            continue
        va, vb = (_net_voltage(nl, p.net, volts) for p in pins)
        if va is None or vb is None:
            unknown.append(ref)
            continue
        v = abs(va - vb)
        use = v / rating
        family = drt.cap_family(comp, specs.get(ref))
        limit = derating if derating is not None else fractions[family]
        row = {"ref": ref, "value": comp["value"], "rating": rating, "rating_source": src,
               "volts": round(v, 3), "use": round(use, 3), "nets": [pins[0].net, pins[1].net],
               "family": family, "derating": limit}
        rows.append(row)
        if use > 1:
            findings.append(_f("error", "CR-CAP-V", ref,
                               f"{comp['value']} rated {rating:g} V ({src}) sees "
                               f"{v:.4g} V ({pins[0].net} / {pins[1].net})."))
        elif use > limit:
            how = "cap_derating" if derating is not None else f"{profile} profile, {family}"
            findings.append(_f("warning", "CR-CAP-DERATE", ref,
                               f"{comp['value']} rated {rating:g} V ({src}) at {v:.4g} V = {use:.0%} of "
                               f"rating (> {limit:.0%} derating, {how}). Ceramic X5R/X7R also lose "
                               "capacitance under DC bias - check the MPN's curve."))
    return {"checked": rows, "voltage_unknown": unknown, "no_rating": no_rating,
            "derating": derating if derating is not None else {"profile": profile, **fractions},
            "rating_field": field}


def _nat(s: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s))


CHECKS = ("power_pins", "decoupling", "open_drain", "i2c", "datasheet_pins", "pin_abs_max",
          "cap_voltage")


def analyze(nl: Netlist, checks: tuple[str, ...] = CHECKS, specs: dict | None = None,
            volts: dict | None = None, cap_derating: float | None = None,
            profile: str = "commercial") -> dict:
    """`specs`: {ref: spec record} from the datasheet cache; `volts`: {net:
    (volts, source)} from the power tree. The `datasheet_pins` and
    `pin_abs_max` checks need them; I2C uses `specs` for fixed addresses."""
    specs = specs or {}
    findings: list[dict] = []
    result: dict = {}
    if "power_pins" in checks:
        result["power_pins"] = check_power_pins(nl, findings, sourced_nets(nl))
    if "decoupling" in checks:
        result["decoupling"] = check_decoupling(nl, findings)
    if "open_drain" in checks:
        result["open_drain"] = check_open_drain(nl, findings)
    if "i2c" in checks:
        result["i2c_buses"] = check_i2c(nl, findings, specs)
    if "datasheet_pins" in checks:
        result["datasheet_pins"] = check_datasheet_pins(nl, findings, specs)
    if "pin_abs_max" in checks:
        from kicad_claude.adapters import derating as drt
        result["pin_abs_max"] = check_pin_abs_max(nl, findings, specs, volts or {},
                                                  drt.profile(profile)["ic_abs"])
    if "cap_voltage" in checks:
        result["cap_voltage"] = check_cap_voltage(nl, findings, volts or {}, cap_derating,
                                                  specs=specs, profile=profile)
    result["specs_used"] = sorted(f"{r}={s.get('mpn')}" for r, s in specs.items())
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["rule"], f["ref"]))
    result["findings"] = findings
    result["counts"] = {k: sum(1 for f in findings if f["severity"] == k) for k in order}
    return result
