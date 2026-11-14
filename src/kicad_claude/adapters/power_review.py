"""Power-tree and regulator review from a KiCAD netlist.

Input is the `kicadsexpr` netlist `kicad-cli sch export netlist` writes: it
covers the whole hierarchy, keeps KiCAD's own net names, and carries each
pin's function (name) and electrical type — the same connectivity ERC uses.

What it finds, without any datasheet constant:
  - regulators (pin-name driven: FB / SW / VOUT / VIN / EN / PG …), their
    topology (switching when a SW/PH/LX pin exists, else linear), and for a
    multi-channel part (LT8471: FB1/FB2, PG1/PG2 …) one record per channel;
  - input / output rail, EN state (tied to input, divider, driven by another
    part's pin — e.g. a PG output, which is sequencing —, tied to ground,
    floating), PG use (pull-up present or not), FB divider resistor values and
    ratio, input / output capacitors, switch-node inductor;
  - the rail tree: source rails → regulator → output rails, plus the number of
    power-input pins loading each rail.

What it does NOT do: compute Vout, EN thresholds, dropout or anything else
that needs a part constant. A Vout needs Vref from the datasheet; the caller
can pass it (`vref={"U1": 0.8}`) and the result says it came from the caller.
No value is taken from general knowledge (project rule).

Pin-name sets and the non-regulator exclusions follow kicad-happy 3.0.1
`signal_detectors.py` (MIT License, Copyright (c) 2025 Andrew Klofas).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sexpdata

# ---- pin-name vocabulary (kicad-happy signal_detectors.py, MIT) ----------- #
FB_NAMES = {"FB", "VFB", "ADJ", "VADJ", "SENSE"}
SW_NAMES = {"SW", "PH", "LX"}
EN_NAMES = {"EN", "ENABLE", "ON", "SHDN", "~{SHDN}", "~{EN}", "EN/UV", "UV", "RUN"}
VIN_NAMES = {"VIN", "VI", "IN", "PVIN", "AVIN", "INPUT"}
VOUT_NAMES = {"VOUT", "VO", "OUT", "OUTPUT"}
PG_NAMES = {"PG", "PGOOD", "POK", "PWRGD", "PGD", "~{PG}"}
SET_NAMES = {"SET", "ISET"}
REG_KEYWORDS = ("regulator", "ldo", "vreg", "buck", "boost", "converter",
                "dc-dc", "dc_dc", "pmic")
NON_REG = ("eeprom", "flash", "rtc", "uart", "buffer", "logic_", "encoder",
           "74lvc", "74hc", "esp32", "esp8266", "nucleo", "arduino", "raspberry",
           "lna", "mmic", "load_switch", "ideal_diode", "power_mux")

_GROUND_RE = re.compile(r"(^|/)(\w*GND\w*|VSS\w*|0V)$", re.I)


def is_ground(net: str) -> bool:
    return bool(_GROUND_RE.search(net))


def is_unconnected(net: str) -> bool:
    return net.split("/")[-1].startswith("unconnected-")


# ---- netlist model --------------------------------------------------------- #


@dataclass
class Pin:
    ref: str
    number: str
    name: str      # pin function with KiCAD's `_<number>` suffix removed
    type: str      # electrical type: input, output, power_in, passive, ...
    net: str


@dataclass
class Netlist:
    components: dict[str, dict] = field(default_factory=dict)
    nets: dict[str, list[Pin]] = field(default_factory=dict)
    pins: dict[str, list[Pin]] = field(default_factory=lambda: defaultdict(list))

    def others_on(self, net: str, ref: str) -> list[Pin]:
        return [p for p in self.nets.get(net, []) if p.ref != ref]


def _h(n: Any) -> str | None:
    return str(n[0]) if isinstance(n, list) and n and isinstance(n[0], sexpdata.Symbol) else None


def _c(n: list, k: str) -> list | None:
    return next((c for c in n[1:] if _h(c) == k), None)


def _v(n: list | None) -> str:
    return str(n[1]) if n and len(n) > 1 else ""


def parse_netlist(path: Path) -> Netlist:
    tree = sexpdata.loads(Path(path).read_text(encoding="utf-8"))
    nl = Netlist()
    for comp in (_c(tree, "components") or [None])[1:]:
        if _h(comp) != "comp":
            continue
        ref = _v(_c(comp, "ref"))
        ls = _c(comp, "libsource")
        fields = {}
        for f in (_c(comp, "fields") or [None])[1:]:
            if _h(f) == "field":
                fields[_v(_c(f, "name"))] = str(f[2]) if len(f) > 2 and not isinstance(f[2], list) else ""
        props = {_v(_c(p, "name")): _v(_c(p, "value")) for p in comp[1:] if _h(p) == "property"}
        nl.components[ref] = {
            "ref": ref,
            "value": _v(_c(comp, "value")),
            "footprint": _v(_c(comp, "footprint")),
            "description": _v(_c(comp, "description")),
            "datasheet": _v(_c(comp, "datasheet")),
            "lib_id": f"{_v(_c(ls, 'lib'))}:{_v(_c(ls, 'part'))}" if ls else "",
            "fields": fields,
            "dnp": "dnp" in props,
            "in_bom": "exclude_from_bom" not in props,
            "on_board": "exclude_from_board" not in props,
        }
    for net in (_c(tree, "nets") or [None])[1:]:
        if _h(net) != "net":
            continue
        name = _v(_c(net, "name"))
        pins = []
        for node in net[1:]:
            if _h(node) != "node":
                continue
            ref, num = _v(_c(node, "ref")), _v(_c(node, "pin"))
            func = _v(_c(node, "pinfunction"))
            if func.endswith(f"_{num}"):
                func = func[: -len(num) - 1]
            p = Pin(ref, num, func, _v(_c(node, "pintype")), name)
            pins.append(p)
            nl.pins[ref].append(p)
        nl.nets[name] = pins
    _infer_untyped_supply_pins(nl)
    return nl


# Supply pin names. VIN alone (or VIN_<word>) is a supply; VIN1, VINP, VINN,
# VIN+ are a converter's / amplifier's signal inputs and are left alone.
SUPPLY_PIN_RE = re.compile(
    r"^(A|D|P|IO|PV|AV|DV)?V(CC|DD)\w*$"
    r"|^(A|D|P|PV)?VIN(_(?![NPM]$)\w+)?$"
    r"|^V(BAT|IO|PP|M|S)(\d*|_\w+)?$|^VS?\+$", re.I)
# not VEE (an op-amp's V- may sit on GND) and not VBUS (a USB bridge's VBUS
# pin is a sense input, wired to a detect or EN net)
GROUND_PIN_NAME_RE = re.compile(r"^(A|D|P|S|E)?GND\w*$|^(A|D)?VSS\w*$", re.I)


def _infer_untyped_supply_pins(nl: Netlist) -> None:
    """A vendor symbol (SnapEDA, Ultra Librarian) often types every pin
    passive, so no supply check would see its VCC. For an IC whose pins are
    all passive and that has both a supply-named and a ground-named pin,
    those pins become power_in; the component records
    `pin_types: "inferred"`. Typed symbols are untouched."""
    for ref, pins in nl.pins.items():
        if not re.match(r"(U|IC)\d", ref) or not pins:
            continue
        if any(p.type not in ("passive", "") for p in pins):
            continue
        sup = [p for p in pins if SUPPLY_PIN_RE.match(p.name or "")]
        gnd = [p for p in pins if GROUND_PIN_NAME_RE.match(p.name or "")]
        if not sup or not gnd:
            continue
        for p in sup + gnd:
            p.type = "power_in"
        if ref in nl.components:
            nl.components[ref]["pin_types"] = "inferred"


# ---- value parsing --------------------------------------------------------- #

_MULT = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3,
         "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "R": 1.0, "r": 1.0}
_NUMBER = r"[+-]?\d+(?:\.\d+)?"
_VALUE = r"([+-]?\d+(?:\.\d+)?)([fpnumkKMGTRr]?)(\d*)"   # 4k7, 2.2u, 1R0, 10
# What may follow a number after a space and still belong to it: "10 µF", "4.7 k",
# "10 ohm" — a prefix or unit, not another field such as "50V" or "1%".
_UNIT_START = re.compile("(?:meg|ohms?|[fpnumkKMGTRr]?[FHfhΩΩ]?)(?:$)", re.I)


def parse_value(text: str) -> float | None:
    """A component value in SI units, as KiCAD's simulator reads it.

    `4k7` 4700, `10k` 1e4, `100n` 1e-7, `2.2uF` 2.2e-6, `0R` 0, `49.9` 49.9,
    `10 µF` / `10μF` 1e-5 (micro sign or Greek mu, unit after a space),
    `2.2Meg` 2.2e6, `4,7k` / `0,1uF` (decimal comma), `0.1uF, 50V` (first field
    of a list), `78 182` 78182 (digit groups). `1,000` is a decimal comma or a
    thousands separator, so it is not guessed: None, as for `DNP` or `abc`.
    """
    s = (text or "").strip().replace("µ", "u").replace("μ", "u")
    if not s:
        return None
    # A field list ends at the first comma that is not between two digits.
    for i, c in enumerate(s):
        if c in ",;" and not (c == "," and 0 < i < len(s) - 1
                              and s[i - 1].isdigit() and s[i + 1].isdigit()):
            s = s[:i].strip()
            break
    if re.search(r"\d,\d{3}(?!\d)", s):
        return None
    s = s.replace(",", ".")
    tokens = s.split()
    if len(tokens) > 1:
        groups = 1
        while (groups < len(tokens) and re.fullmatch(r"\d{3}", tokens[groups])
               and re.fullmatch(r"[+-]?\d{1,3}", tokens[0])):
            groups += 1
        if groups > 1:
            tokens = ["".join(tokens[:groups])] + tokens[groups:]
        elif re.fullmatch(_NUMBER, tokens[0]) and _UNIT_START.match(tokens[1]):
            tokens = [tokens[0] + tokens[1]] + tokens[2:]
    s = tokens[0] if tokens else ""
    s = re.sub(r"(?i)meg(?=\d*[a-zΩΩ]*$)", "M", s)
    s = re.sub("(?i)(?:ohms?|[ΩΩ])$", "", s)
    # One trailing farad / henry letter: `100nF`, `100nf`, `22uH`, `1fF`.
    if s and s[-1] in "FfHh" and re.fullmatch(_VALUE, s[:-1]):
        s = s[:-1]
    m = re.fullmatch(_VALUE, s)
    if not m:
        return None
    base, mult, frac = m.groups()
    num = float(f"{base}.{frac}") if frac and "." not in base else float(base)
    return num * _MULT.get(mult, 1.0)


def kind(ref: str) -> str:
    m = re.match(r"[A-Z]+", ref)
    return m.group(0) if m else ""


# ---- regulator detection --------------------------------------------------- #


def _base(name: str) -> tuple[str, str]:
    """`FB1` -> ("FB", "1"), `SENSE/FB` -> ("SENSE/FB", ""), `PG_2` -> ("PG", "2")."""
    m = re.fullmatch(r"(.*?)[_]?(\d+)", name)
    if m and m.group(1) and not name[0].isdigit():
        return m.group(1).upper(), m.group(2)
    return name.upper(), ""


def _parts(name: str) -> set[str]:
    return {p.strip() for p in name.split("/")} | {name}


def _role(name: str) -> str | None:
    parts = _parts(name)
    if parts & FB_NAMES:
        return "fb"
    if parts & SW_NAMES:
        return "sw"
    if name in EN_NAMES or parts & {"EN", "ENABLE", "SHDN", "RUN", "UV", "UVLO"}:
        return "en"
    if parts & PG_NAMES:
        return "pg"
    if parts & VIN_NAMES:
        return "vin"
    if parts & VOUT_NAMES:
        return "vout"
    if parts & SET_NAMES:
        return "set"
    return None


def detect_regulators(nl: Netlist) -> list[dict]:
    out = []
    for ref, comp in sorted(nl.components.items()):
        if kind(ref) not in ("U", "IC", "PS", "VR", "REG"):
            continue
        text = f"{comp['lib_id']} {comp['value']} {comp['description']}".lower()
        if any(k in text for k in NON_REG):
            continue
        channels: dict[str, dict[str, list[Pin]]] = defaultdict(lambda: defaultdict(list))
        for p in nl.pins.get(ref, []):
            name, ch = _base(p.name)
            role = _role(name)
            if role:
                channels[ch][role].append(p)
        shared = channels.pop("", {})
        if not channels:
            channels = {"": shared}
            shared = {}
        has_kw = any(k in text for k in REG_KEYWORDS)
        for ch, roles in sorted(channels.items()):
            merged = defaultdict(list, {k: list(v) for k, v in shared.items()})
            for k, v in roles.items():
                merged[k].extend(v)
            core = merged.get("fb") or merged.get("sw") or merged.get("vout") or merged.get("set")
            if not core or not (merged.get("vin") or has_kw):
                continue
            out.append({"ref": ref, "channel": ch, "comp": comp, "roles": merged})
    return out


# ---- per-regulator analysis ------------------------------------------------ #


def _first_net(pins: list[Pin], skip_unconnected: bool = True) -> str | None:
    for p in pins:
        if not (skip_unconnected and is_unconnected(p.net)):
            return p.net
    return None


def _two_terminal(nl: Netlist, ref: str) -> tuple[str, str] | None:
    pins = nl.pins.get(ref, [])
    if len(pins) != 2:
        return None
    return pins[0].net, pins[1].net


def _components_between(nl: Netlist, net: str, prefix: str) -> list[dict]:
    """Two-terminal parts of kind `prefix` on `net`: [{ref, value, other_net}]."""
    out = []
    for p in nl.nets.get(net, []):
        if kind(p.ref) != prefix:
            continue
        ends = _two_terminal(nl, p.ref)
        if not ends:
            continue
        other = ends[1] if ends[0] == net else ends[0]
        comp = nl.components.get(p.ref, {})
        out.append({"ref": p.ref, "value": comp.get("value", ""),
                    "ohms_or_f": parse_value(comp.get("value", "")),
                    "other_net": other, "dnp": comp.get("dnp", False)})
    return out


def _fb_divider(nl: Netlist, fb_net: str, out_hint: str | None) -> dict | None:
    rs = [r for r in _components_between(nl, fb_net, "R") if not r["dnp"]]
    bottom = [r for r in rs if is_ground(r["other_net"])]
    top = [r for r in rs if not is_ground(r["other_net"])]
    if not bottom or not top:
        return None
    if out_hint:
        top = sorted(top, key=lambda r: r["other_net"] != out_hint)
    t, b = top[0], bottom[0]
    ratio = None
    if t["ohms_or_f"] is not None and b["ohms_or_f"]:
        ratio = (t["ohms_or_f"] + b["ohms_or_f"]) / b["ohms_or_f"]
    return {"top": t["ref"], "top_value": t["value"], "bottom": b["ref"],
            "bottom_value": b["value"], "sense_net": t["other_net"],
            "ratio": round(ratio, 6) if ratio else None}


def _caps(nl: Netlist, net: str | None) -> list[dict]:
    if not net:
        return []
    return [{"ref": c["ref"], "value": c["value"], "dnp": c["dnp"]}
            for c in _components_between(nl, net, "C") if is_ground(c["other_net"])]


def _en_state(nl: Netlist, reg_ref: str, en_net: str | None, vin: str | None) -> dict:
    if not en_net or is_unconnected(en_net):
        return {"state": "floating", "net": en_net}
    if en_net == vin:
        return {"state": "tied_to_input", "net": en_net}
    if is_ground(en_net):
        return {"state": "tied_to_ground", "net": en_net}
    others = nl.others_on(en_net, reg_ref)
    drivers = [p for p in others
               if kind(p.ref) not in ("R", "C", "TP", "J", "D")
               and p.type in ("output", "open_collector", "open_emitter", "power_out",
                              "bidirectional", "tri_state")]
    if drivers:
        return {"state": "driven", "net": en_net,
                "by": [f"{p.ref}.{p.name or p.number}" for p in drivers]}
    rs = _components_between(nl, en_net, "R")
    if any(r["other_net"] == vin for r in rs) and any(is_ground(r["other_net"]) for r in rs):
        return {"state": "divider_from_input", "net": en_net,
                "resistors": [r["ref"] for r in rs]}
    if rs:
        return {"state": "resistor", "net": en_net,
                "to": sorted({r["other_net"] for r in rs})}
    if not others:
        return {"state": "floating", "net": en_net}
    if any(kind(p.ref) == "J" for p in others):
        return {"state": "external", "net": en_net}
    return {"state": "other", "net": en_net}


def _pg_state(nl: Netlist, reg_ref: str, pg_net: str | None) -> dict | None:
    if pg_net is None:
        return None
    if is_unconnected(pg_net):
        return {"state": "unused", "net": pg_net}
    others = nl.others_on(pg_net, reg_ref)
    pullups = [r for r in _components_between(nl, pg_net, "R") if not is_ground(r["other_net"])]
    users = [f"{p.ref}.{p.name or p.number}" for p in others
             if kind(p.ref) not in ("R", "C", "TP")]
    return {"state": "pulled_up" if pullups else "no_pullup", "net": pg_net,
            "pullup": [r["ref"] for r in pullups], "used_by": users}


def is_negative_rail(net: str | None) -> bool:
    seg = (net or "").split("/")[-1].upper()
    return seg.startswith("-") or "NEG" in seg


def analyze(nl: Netlist, vref: dict | None = None,
            vref_sources: dict | None = None,
            datasheet: dict | None = None,
            rail_voltages: dict | None = None,
            specs: dict | None = None) -> dict:
    """`vref` is keyed by regulator name (`U2/ch1`) or ref (`U1`);
    `vref_sources` says where each came from (default `caller`).

    `datasheet` (keyed the same way) holds values from extracted datasheet
    specs, each `(value, source)`: `vref`, `vref_negative`, `set_current`, and `anyout`
    `{"base": (v, src), "pins": {pin number: (v, src)}}`. Caller `vref`
    wins over it. `datasheet[name]["limits"]` holds spec triples
    `(min, typ, max, source)` for `abs_vin`, `rec_vin`, `abs_vout`,
    `rec_vout`, `abs_en`, `en_on`, `dropout`; with the rail voltages (computed
    Vouts, carried through series L / ferrite / 0 R links, plus the caller's
    `rail_voltages`) they drive the `PT-DS-*` checks.

    `specs` ({ref: spec record}) turns on the DC operating-point model
    (`dc_model`) for nets off the rails: multi-branch resistor networks, pin
    bias currents, internal pin clamps and zeners. The EN checks then use
    the clamped voltage, clamps add DC-* findings, and the solved nets are
    returned as `net_voltages`."""
    vref = vref or {}
    vref_sources = vref_sources or {}
    datasheet = datasheet or {}
    regs = []
    findings: list[dict] = []

    def finding(sev, rule, ref, msg, **kw):
        findings.append({"severity": sev, "rule": rule, "ref": ref, "message": msg, **kw})

    for cand in detect_regulators(nl):
        ref, ch, roles, comp = cand["ref"], cand["channel"], cand["roles"], cand["comp"]
        name = f"{ref}{'/ch' + ch if ch else ''}"
        vin = _first_net(roles.get("vin", []))
        ds_early = datasheet.get(name) or datasheet.get(ref) or {}
        vin_note = None
        if ds_early.get("input_pin"):
            pin_name, src_ip = ds_early["input_pin"]
            hit = next((p.net for p in nl.pins.get(ref, [])
                        if p.name.upper() == pin_name.upper() and not is_unconnected(p.net)), None)
            if hit and hit != vin:
                vin_note = (f"power input taken from pin {pin_name} ({hit}) per {src_ip}; "
                            f"the VIN-named pin is on {vin}")
                vin = hit
        vout_pin_net = _first_net(roles.get("vout", []))
        sw_net = _first_net(roles.get("sw", []))
        fb_net = _first_net(roles.get("fb", []))

        inductor = None
        if sw_net:
            ls = _components_between(nl, sw_net, "L")
            if ls:
                inductor = {"ref": ls[0]["ref"], "value": ls[0]["value"],
                            "other_net": ls[0]["other_net"]}
        # Switching when there is a SW/PH/LX pin, the part sits in a
        # switching-regulator library, or an inductor hangs off one of this
        # channel's pins other than VIN/VOUT (LT8471 names its switch C1/E1).
        ch_nets = {p.net for p in nl.pins.get(ref, [])
                   if _base(p.name)[1] in (ch, "")} - {vin, vout_pin_net}
        topology = "switching" if (
            sw_net or "switching" in comp["lib_id"].lower()
            or any(_components_between(nl, n, "L") for n in ch_nets
                   if n and not is_ground(n) and not is_unconnected(n))
        ) else "linear"

        divider = None
        fb_mode = None
        if fb_net:
            if vout_pin_net and fb_net == vout_pin_net:
                fb_mode = "tied_to_output"
            else:
                divider = _fb_divider(nl, fb_net, vout_pin_net
                                      or (inductor or {}).get("other_net"))
                fb_mode = "divider" if divider else "no_divider"

        out_net = vout_pin_net or (divider or {}).get("sense_net") \
            or (inductor or {}).get("other_net")

        set_info = None
        set_net = _first_net(roles.get("set", []))
        if set_net:
            rs = _components_between(nl, set_net, "R")
            set_info = {"net": set_net, "resistors": [
                {"ref": r["ref"], "value": r["value"], "to": r["other_net"]} for r in rs]}

        en = _en_state(nl, ref, _first_net(roles.get("en", []), False), vin) \
            if roles.get("en") else None
        pg = _pg_state(nl, ref, _first_net(roles.get("pg", []), False)) \
            if roles.get("pg") else None

        vkey = name if name in vref else ref if ref in vref else None
        ds = datasheet.get(name) or datasheet.get(ref) or {}
        vout = None
        if divider and divider["ratio"] and vkey:
            vout = {"value": round(float(vref[vkey]) * divider["ratio"], 4),
                    "vref": float(vref[vkey]),
                    "vref_source": vref_sources.get(vkey, "caller")}
        elif divider and divider["ratio"] and (ds.get("vref") or ds.get("vref_negative")):
            neg = is_negative_rail(divider["sense_net"])
            pick = ds.get("vref_negative") if neg else ds.get("vref")
            if pick:
                vout = {"value": round(pick[0] * divider["ratio"], 4), "vref": pick[0],
                        "vref_source": pick[1]}
            else:
                finding("info", "PT-VREF-POL", name, f"Sense rail {divider['sense_net']} is "
                        f"{'negative' if neg else 'positive'} but the datasheet specs give only a "
                        f"{'positive' if neg else 'negative'} Vref; Vout not computed.")
        elif set_info and ds.get("set_current"):
            rs = [parse_value(nl.components[r["ref"]]["value"]) for r in set_info["resistors"]
                  if is_ground(r["to"]) and not nl.components[r["ref"]]["dnp"]]
            if rs and all(rs):
                r_set = 1.0 / sum(1.0 / r for r in rs)
                vout = {"value": round(ds["set_current"][0] * r_set, 4), "method": "set_current",
                        "r_set": round(r_set, 3), "i_set": ds["set_current"][0],
                        "vref_source": ds["set_current"][1]}
        elif fb_mode == "tied_to_output" and ds.get("anyout"):
            ao = ds["anyout"]
            grounded = [p for p in nl.pins.get(ref, [])
                        if p.number in ao["pins"] and is_ground(p.net)]
            vout = {"value": round(ao["base"][0] + sum(ao["pins"][p.number][0] for p in grounded), 4),
                    "method": "anyout",
                    "pins_grounded": sorted(f"{p.name or p.number}({p.number})" for p in grounded),
                    "vref_source": ao["base"][1]}

        rec = {
            "name": name, "ref": ref, "channel": ch or None, "value": comp["value"],
            "lib_id": comp["lib_id"], "topology": topology,
            "input_net": vin, "output_net": out_net, "switch_net": sw_net,
            "inductor": inductor, "feedback": {"net": fb_net, "mode": fb_mode,
                                               "divider": divider} if fb_net else None,
            "set": set_info, "enable": en, "power_good": pg, "vout": vout,
            "input_caps": _caps(nl, vin), "output_caps": _caps(nl, out_net),
        }
        if vin_note:
            rec["input_note"] = vin_note
        regs.append(rec)

        # ---- findings (datasheet-free) ----
        if en and en["state"] == "floating":
            finding("error", "PT-EN-FLOAT", name, "EN is not connected; the regulator "
                    "state depends on an internal pull, if any — check the datasheet.")
        if en and en["state"] == "tied_to_ground":
            finding("error", "PT-EN-GND", name, "EN is tied to ground: the regulator never starts "
                    "(unless EN is active-low — check the datasheet).")
        if fb_mode == "no_divider":
            finding("warning", "PT-FB-NODIV", name, f"FB net {fb_net} has no resistor "
                    "divider to ground and is not tied to the output.")
        if divider and divider["ratio"] is None:
            finding("warning", "PT-FB-VAL", name, "FB divider value not parseable: "
                    f"{divider['top_value']} / {divider['bottom_value']}.")
        if divider and not vout and not any(f["rule"] == "PT-VREF-POL" and f["ref"] == name
                                            for f in findings):
            finding("info", "PT-VOUT-VREF", name, f"FB ratio {divider['ratio']}; Vout = "
                    "Vref x ratio needs Vref from the datasheet (pass vref={ref: V}).")
        if pg and pg["state"] == "no_pullup" and pg["used_by"]:
            finding("warning", "PT-PG-PULLUP", name, f"PG net {pg['net']} is used by "
                    f"{', '.join(pg['used_by'])} but has no pull-up resistor (PG is "
                    "usually open-drain — check the datasheet).")
        if not rec["input_caps"] and vin:
            finding("warning", "PT-CIN", name, f"No capacitor to ground on input {vin}.")
        if not rec["output_caps"] and out_net:
            finding("warning", "PT-COUT", name, f"No capacitor to ground on output {out_net}.")
        if set_info and not vout:
            finding("info", "PT-SET", name, "Output set by resistor(s) on "
                    f"{set_net}: "
                    + ", ".join(r["ref"] + " " + r["value"] for r in set_info["resistors"])
                    + "; Vout needs the SET current from the datasheet.")
        if topology == "switching" and not inductor and sw_net:
            finding("info", "PT-SW-L", name, f"No inductor on switch node {sw_net} "
                    "(fine for Ćuk/SEPIC/charge-pump topologies; otherwise check).")
        if not out_net:
            finding("warning", "PT-NOOUT", name, "Output rail could not be determined.")

    # ---- passive links: series L / ferrite / 0-ohm between two nets ----
    # A pi filter or a 0R bridge joins two rails without a regulator. DNP
    # parts are kept as links but flagged: as assembled that rail is open.
    reg_nets = {n for r in regs for n in (r["input_net"], r["output_net"]) if n}
    sw_nets = {r["switch_net"] for r in regs if r["switch_net"]}
    links: dict[str, list[dict]] = defaultdict(list)
    for lref, comp in nl.components.items():
        k = kind(lref)
        val = parse_value(comp["value"])
        if not (k in ("L", "FB", "FL") or (k == "R" and val == 0.0)):
            continue
        ends = _two_terminal(nl, lref)
        if not ends or ends[0] == ends[1]:
            continue
        a_net, b_net = ends
        if {a_net, b_net} & sw_nets or is_ground(a_net) or is_ground(b_net):
            continue
        for x, y in ((a_net, b_net), (b_net, a_net)):
            links[x].append({"ref": lref, "value": comp["value"], "to": y,
                             "dnp": comp["dnp"]})

    rail_set = set(reg_nets)
    frontier = list(reg_nets)
    while frontier:
        n = frontier.pop()
        for lk in links.get(n, []):
            if lk["to"] not in rail_set:
                rail_set.add(lk["to"])
                frontier.append(lk["to"])

    # Prune dead ends: a linked net with no consumer (no power-input pin, no
    # connector), not a regulator net, and no further link. This drops the
    # inductors inside a Ćuk/Zeta/SEPIC power stage and leaves real rails.
    def _has_consumer(n: str) -> bool:
        return any(p.type == "power_in" or kind(p.ref) in ("J", "P", "CN")
                   for p in nl.nets.get(n, []))

    changed = True
    while changed:
        changed = False
        for n in list(rail_set - reg_nets):
            live = [lk for lk in links.get(n, []) if lk["to"] in rail_set]
            if len(live) <= 1 and not _has_consumer(n):
                rail_set.discard(n)
                changed = True
    for n in list(links):
        links[n] = [lk for lk in links[n] if lk["to"] in rail_set]

    outputs: dict[str, list[str]] = defaultdict(list)
    for r in regs:
        if r["output_net"]:
            outputs[r["output_net"]].append(r["name"])
    for net, names in outputs.items():
        if len(names) > 1:
            finding("warning", "PT-MULTI-SRC", ",".join(names),
                    f"Rail {net} is the output of more than one regulator channel.")

    def consumers(net: str) -> dict:
        ps = nl.nets.get(net, [])
        return {"power_in_pins": sum(1 for p in ps if p.type == "power_in"),
                "connectors": sorted({p.ref for p in ps if kind(p.ref) in ("J", "P", "CN")})}

    rails = {n: {"net": n, **consumers(n), "sourced_by": outputs.get(n, []),
                 "feeds": [r["name"] for r in regs if r["input_net"] == n],
                 "links": links.get(n, [])} for n in sorted(rail_set)}

    # Rails downstream of a regulator output through links belong under it.
    downstream: set = set()
    for n in rails:
        if rails[n]["sourced_by"]:
            stack = [n]
            while stack:
                m = stack.pop()
                for lk in links.get(m, []):
                    t = lk["to"]
                    if t not in downstream and not rails[t]["sourced_by"]:
                        downstream.add(t)
                        stack.append(t)

    def subtree(net: str, seen: set, via: dict | None = None) -> dict:
        node = {"rail": net, **consumers(net), "regulators": [], "linked": []}
        if via:
            node["via"] = via
        seen.add(net)
        for rname in rails[net]["feeds"]:
            r = next(x for x in regs if x["name"] == rname)
            child = {"regulator": rname, "value": r["value"], "topology": r["topology"],
                     "enable": (r["enable"] or {}).get("state")}
            out = r["output_net"]
            if out and out in rails and out not in seen:
                child["output"] = subtree(out, seen)
            node["regulators"].append(child)
        for lk in rails[net]["links"]:
            if lk["to"] in seen or rails[lk["to"]]["sourced_by"]:
                continue
            node["linked"].append(subtree(lk["to"], seen, {
                "ref": lk["ref"], "value": lk["value"], "dnp": lk["dnp"]}))
        return node

    # Links carry no direction, so roots are taken from regulator inputs that
    # no regulator sources; every other rail hangs off a root via `linked`.
    seen: set = set()
    tree = []
    roots = sorted(n for n in rails
                   if rails[n]["feeds"] and not rails[n]["sourced_by"]
                   and n not in downstream)
    for n in roots + sorted(rails):
        if n not in seen and not rails[n]["sourced_by"] and n not in downstream:
            tree.append(subtree(n, seen))

    for r in regs:
        n = r["input_net"]
        if not n or rails.get(n, {}).get("sourced_by"):
            continue
        # Only a DNP part that is the sole path from a regulator-fed rail
        # matters; a DNP 0R out to a test SMA does not starve anything.
        fed = set(outputs) | downstream
        dnp = [lk for lk in links.get(n, []) if lk["dnp"] and lk["to"] in fed]
        live = [lk for lk in links.get(n, []) if not lk["dnp"] and lk["to"] in fed]
        if dnp and not live:
            finding("warning", "PT-DNP-LINK", r["name"],
                    f"Input {n} reaches {dnp[0]['to']} only through DNP "
                    f"{', '.join(lk['ref'] for lk in dnp)}: as assembled, {r['name']} "
                    "has no input unless that part is fitted or bridged.")

    volts = _rail_voltages(regs, links, rail_voltages or {})
    # pins a part drives at another pin's voltage + offset (Skyhook / bootstrap)
    for r in regs:
        ds = datasheet.get(r["name"]) or datasheet.get(r["ref"]) or {}
        for pin, (above, off, src) in (ds.get("internal_outputs") or {}).items():
            pn_ = {p.name.upper(): p.net for p in nl.pins.get(r["ref"], [])}
            n, a = pn_.get(pin.upper()), pn_.get(above.upper())
            if n and a and a in volts and n not in volts:
                volts[n] = (volts[a][0] + off, f"{r['ref']}.{pin} = {above} + {off:g} V ({src})")
    dc = None
    check_volts = volts
    if specs is not None:
        from kicad_claude.adapters import dc_model
        dc = dc_model.solve(nl, volts, specs)
        check_volts = {**dc["volts"], **volts}
        for f in dc_model.findings(dc):
            finding(f["severity"], f["rule"], f["ref"], f["message"])
    _datasheet_checks(nl, regs, check_volts, datasheet, finding)

    sequencing = [{"regulator": r["name"], "enabled_by": r["enable"]["by"]}
                  for r in regs if r["enable"] and r["enable"]["state"] == "driven"]

    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["ref"]))
    for rail in rails.values():
        rail["voltage"] = volts.get(rail["net"], (None,))[0]
    return {"regulators": regs, "rails": list(rails.values()), "tree": tree,
            "rail_voltages": {k: {"volts": v[0], "source": v[1]}
                              for k, v in sorted(volts.items())},
            "sequencing": sequencing, "findings": findings,
            **({"net_voltages": {n: {"volts": round(v, 4), "source": s}
                                 for n, (v, s) in sorted(dc["volts"].items()) if n not in volts},
                "dc_model": {"clamps": dc["clamps"], "notes": dc["notes"]}} if dc else {}),
            "counts": {k: sum(1 for f in findings if f["severity"] == k) for k in order}}


# ---- datasheet limit checks ------------------------------------------------ #


def _rail_voltages(regs: list[dict], links: dict, given: dict) -> dict[str, tuple[float, str]]:
    """Net -> (volts, source): caller values, computed regulator outputs, then
    carried across series links (same DC voltage, drop ignored). Fitted links
    first; a rail reachable only through a DNP link still gets its
    as-designed voltage, with `DNP <ref>` in the source, so the checks
    review the design intent (PT-DNP-LINK already flags the open path)."""
    volts: dict[str, tuple[float, str]] = {n: (float(v), "caller") for n, v in given.items()}
    for r in regs:
        if r["vout"] and r["output_net"] and r["output_net"] not in volts:
            volts[r["output_net"]] = (r["vout"]["value"], f"{r['name']} output")
    for allow_dnp in (False, True):
        frontier = list(volts)
        while frontier:
            n = frontier.pop()
            for lk in links.get(n, []):
                if (allow_dnp or not lk["dnp"]) and lk["to"] not in volts:
                    via = f"DNP {lk['ref']}" if lk["dnp"] else lk["ref"]
                    volts[lk["to"]] = (volts[n][0], f"{volts[n][1]} via {via}")
                    frontier.append(lk["to"])
    return volts


def _net_voltage(nl: Netlist, net: str, volts: dict) -> float | None:
    """DC voltage of a net set by resistors to known rails / ground
    (Thevenin, pin currents ignored), or the rail's own voltage."""
    if is_ground(net):
        return 0.0
    if net in volts:
        return volts[net][0]
    g = gv = 0.0
    feeds = 0
    for r in _components_between(nl, net, "R"):
        if r["dnp"] or not r["ohms_or_f"]:
            continue
        other = r["other_net"]
        v = 0.0 if is_ground(other) else volts.get(other, (None,))[0]
        if v is None:
            return None
        g += 1.0 / r["ohms_or_f"]
        gv += v / r["ohms_or_f"]
        feeds += not is_ground(other)
    return gv / g if g and feeds else None


def _fmt(v: float) -> str:
    return f"{v:.4g} V"


def _datasheet_checks(nl: Netlist, regs: list[dict], volts: dict,
                      datasheet: dict, finding) -> None:
    for r in regs:
        lim = (datasheet.get(r["name"]) or datasheet.get(r["ref"]) or {}).get("limits") or {}
        if not lim:
            continue
        name = r["name"]
        vin = volts.get(r["input_net"] or "", (None,))[0]
        vout = (r["vout"] or {}).get("value")
        if vout is None and r["output_net"] in volts:
            vout = volts[r["output_net"]][0]

        def outside(v, key, lim=lim):
            lo, _, hi, src = lim[key]
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                rng = f"{lo if lo is not None else '-'}..{hi if hi is not None else '-'} V"
                return f"{_fmt(v)} outside {rng} ({src})"
            return None

        if vin is not None:
            if "abs_vin" in lim and (m := outside(vin, "abs_vin")):
                finding("error", "PT-DS-VIN-ABS", name,
                        f"Input {r['input_net']} {m} - absolute maximum.")
            elif "rec_vin" in lim and (m := outside(vin, "rec_vin")):
                finding("warning", "PT-DS-VIN-REC", name,
                        f"Input {r['input_net']} {m} - recommended range.")
        if vout is not None:
            if "abs_vout" in lim and (m := outside(vout, "abs_vout")):
                finding("error", "PT-DS-VOUT-ABS", name, f"Output {m} - absolute maximum.")
            elif "rec_vout" in lim and (m := outside(vout, "rec_vout")):
                finding("warning", "PT-DS-VOUT-REC", name, f"Output {m} - recommended range.")
        if r["topology"] == "linear" and vin is not None and vout is not None and "dropout" in lim:
            _, typ, hi, src = lim["dropout"]
            need = hi if hi is not None else typ
            head = abs(vin) - abs(vout)
            if need is not None:
                sev = "warning" if head < need else "info"
                finding(sev, "PT-DS-HEADROOM", name,
                        f"Headroom {_fmt(head)} (|{_fmt(vin)}| - |{_fmt(vout)}|) vs dropout "
                        f"{_fmt(need)} ({'max' if hi is not None else 'typ'}, {src})"
                        + (": below dropout." if head < need else "."))
        en = r["enable"]
        if en and en.get("net") and en["state"] not in ("floating", "tied_to_ground"):
            v_en = _net_voltage(nl, en["net"], volts)
            if v_en is None:
                continue
            what = "released" if en["state"] == "driven" else "set"
            if "abs_en" in lim and (m := outside(v_en, "abs_en")):
                finding("error", "PT-DS-EN-ABS", name,
                        f"EN ({en['net']}) {what} at {m} - absolute maximum.")
            if "en_on" in lim:
                lo, typ, hi, src = lim["en_on"]
                sure = hi if hi is not None else typ
                mag = abs(v_en)
                if lo is not None and mag < lo:
                    finding("error", "PT-DS-EN-LOW", name,
                            f"EN {what} at {_fmt(v_en)}, below the minimum turn-on threshold "
                            f"{_fmt(lo)} ({src}): never enables.")
                elif sure is not None and mag < sure:
                    finding("warning", "PT-DS-EN-LOW", name,
                            f"EN {what} at {_fmt(v_en)}, below the guaranteed turn-on level "
                            f"{_fmt(sure)} ({src}).")
                elif sure is not None:
                    finding("info", "PT-DS-EN", name,
                            f"EN {what} at {_fmt(v_en)} vs turn-on {_fmt(sure)} ({src}).")
