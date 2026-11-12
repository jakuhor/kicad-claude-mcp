"""Automatic subcircuit simulation from the schematic netlist (OP-2).

Detects, from KiCAD's own netlist (`power_review.parse_netlist`):
  - resistor dividers into an active pin (FB, EN, PG, ADC …): ratio, the
    tapped voltage, and its worst-case / Monte Carlo spread from the
    resistors' `Tolerance` field and, for a regulator FB divider, the
    datasheet Vref min / max (spec cache) — so Vout comes with its range;
  - RC low-pass sections (series R into a node with a capacitor to ground
    and an active input): corner frequency with the source-side Thevenin R;
  - LC / ferrite / pi ladders: chains of series inductors between nodes
    that carry capacitors to ground (converter power-stage inductors are
    excluded), with R-C damping branches and bleeders, simulated with an
    SPICE AC sweep (ngspice or LTspice) from the ladder's source end: resonances,
    peaking, attenuation at the switching harmonics, optional Monte Carlo.

Component values come from the netlist `Value` field; tolerances only from a
`Tolerance` field (none means the nominal only, and the result says so).
Parts are ideal unless the datasheet spec cache gives ESR / DCR — nothing is
assumed from generic tables. The load is open and the source impedance zero:
a first-pass check of the passive network, not of the regulator loop.

Subcircuit ideas follow kicad-happy 3.0.1 `spice` (MIT License, Copyright
(c) 2025 Andrew Klofas); detection and testbenches are our own.
"""

from __future__ import annotations

import math
import random
import re
import tempfile
from collections import defaultdict
from pathlib import Path

from kicad_claude.adapters.power_review import Netlist, is_ground, is_unconnected, kind, parse_value

PASSIVE = {"R", "C", "L", "FB", "FL", "TP", "J", "P", "CN", "D", "H", "MH", "JP"}


def tolerance(comp: dict) -> float | None:
    """`1%` -> 0.01 from the Tolerance field; None when absent."""
    t = (comp.get("fields") or {}).get("Tolerance", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
    return float(m.group(1)) / 100 if m else None


def spec_tolerance(specs: dict, ref: str) -> float | None:
    """Half-range of the part's L or C spec value (`(max - min) / 2 / typ`),
    when a datasheet extraction gives it and no Tolerance field does."""
    el = ((specs or {}).get(ref) or {}).get("electrical") or {}
    for key in ("L", "C"):
        sv = el.get(key) or {}
        if sv.get("typ") and sv.get("min") is not None and sv.get("max") is not None:
            return (sv["max"] - sv["min"]) / 2 / sv["typ"]
    return None


def _two(nl: Netlist, ref: str) -> tuple[str, str] | None:
    ps = nl.pins.get(ref, [])
    if len(ps) != 2 or ps[0].net == ps[1].net:
        return None
    return ps[0].net, ps[1].net


def _active_on(nl: Netlist, net: str) -> list[str]:
    return [f"{p.ref}.{p.name or p.number}" for p in nl.nets.get(net, [])
            if kind(p.ref) not in PASSIVE and not nl.components.get(p.ref, {}).get("dnp")]


def _fitted(nl: Netlist, ref: str) -> bool:
    return not nl.components.get(ref, {}).get("dnp")


# ---- dividers ------------------------------------------------------------------ #


def find_dividers(nl: Netlist) -> list[dict]:
    out = []
    for mid, pins in nl.nets.items():
        if is_ground(mid) or is_unconnected(mid):
            continue
        rs = [p.ref for p in pins if kind(p.ref) == "R" and _fitted(nl, p.ref)]
        top = bot = None
        for r in rs:
            ends = _two(nl, r)
            if not ends:
                continue
            other = ends[1] if ends[0] == mid else ends[0]
            if is_ground(other):
                bot = bot or r
            else:
                top = top or (r, other)
        active = _active_on(nl, mid)
        if top and bot and active:
            out.append({"mid": mid, "top": top[0], "bottom": bot, "source": top[1],
                        "drives": active})
    return out


def analyze_divider(nl: Netlist, d: dict, volts: dict, vref: dict | None,
                    mc: int, rng: random.Random) -> dict:
    ct, cb = nl.components[d["top"]], nl.components[d["bottom"]]
    rt, rb = parse_value(ct["value"]), parse_value(cb["value"])
    rec = {**d, "top_value": ct["value"], "bottom_value": cb["value"],
           "tolerance": {d["top"]: tolerance(ct), d["bottom"]: tolerance(cb)}}
    if not rt or not rb:
        rec["error"] = "value not parseable"
        return rec
    ratio = rb / (rt + rb)
    rec["ratio"] = round(ratio, 6)
    tt, tb = tolerance(ct) or 0.0, tolerance(cb) or 0.0

    def r_range(nom, tol):
        return nom * (1 - tol), nom * (1 + tol)

    lo = r_range(rb, tb)[0] / (r_range(rt, tt)[1] + r_range(rb, tb)[0])
    hi = r_range(rb, tb)[1] / (r_range(rt, tt)[0] + r_range(rb, tb)[1])
    rec["ratio_worst"] = [round(lo, 6), round(hi, 6)]
    vs = volts.get(d["source"])
    if vs is not None:
        rec["v_mid"] = round(vs * ratio, 4)
        rec["v_mid_worst"] = sorted([round(vs * lo, 4), round(vs * hi, 4)])
    if vref:                                     # regulator FB: Vout = Vref / ratio
        vmin, vtyp, vmax, src = vref["min"], vref["typ"], vref["max"], vref["source"]
        g_lo, g_hi = 1 / hi, 1 / lo
        cand = [v * g for v in (vmin, vmax) if v is not None for g in (g_lo, g_hi)]
        rec["vout"] = {"typ": round(vtyp / ratio, 4) if vtyp is not None else None,
                       "worst": [round(min(cand), 4), round(max(cand), 4)] if cand else None,
                       "vref_source": src}
        if mc and cand:
            samples = []
            for _ in range(mc):
                a = rt * (1 + rng.uniform(-tt, tt))
                b = rb * (1 + rng.uniform(-tb, tb))
                v = rng.uniform(min(x for x in (vmin, vmax) if x is not None),
                                max(x for x in (vmin, vmax) if x is not None))
                samples.append(v * (a + b) / b)
            samples.sort()
            rec["vout"]["monte_carlo"] = {"n": mc, "p0_1": round(samples[int(0.001 * mc)], 4),
                                          "median": round(samples[mc // 2], 4),
                                          "p99_9": round(samples[int(0.999 * (mc - 1))], 4)}
    return rec


# ---- RC low-pass ------------------------------------------------------------------ #


def find_rc(nl: Netlist, ladder_nets: set) -> list[dict]:
    out = []
    for node, pins in nl.nets.items():
        if is_ground(node) or is_unconnected(node) or node in ladder_nets:
            continue
        caps = [p.ref for p in pins if kind(p.ref) == "C" and _fitted(nl, p.ref)
                and _two(nl, p.ref) and any(is_ground(n) for n in _two(nl, p.ref))]
        rs = []
        for p in pins:
            if kind(p.ref) != "R" or not _fitted(nl, p.ref):
                continue
            ends = _two(nl, p.ref)
            if ends:
                rs.append((p.ref, ends[1] if ends[0] == node else ends[0]))
        series = [(r, o) for r, o in rs if not is_ground(o)]
        active = _active_on(nl, node)
        if any(p.type.split("+")[0] in ("power_in", "power_out") for p in pins
               if kind(p.ref) not in PASSIVE):
            continue                       # a supply net, not a signal RC
        if caps and series and active:
            g = sum(1 / parse_value(nl.components[r]["value"]) for r, _ in rs
                    if parse_value(nl.components[r]["value"]))
            c = sum(parse_value(nl.components[x]["value"]) or 0 for x in caps)
            if g and c:
                r_th = 1 / g
                out.append({"node": node, "series": [r for r, _ in series],
                            "shunt_r": [r for r, o in rs if is_ground(o)], "caps": caps,
                            "drives": active, "r_thevenin_ohm": round(r_th, 3),
                            "c_total_f": float(f"{c:.4g}"),
                            "fc_hz": round(1 / (2 * math.pi * r_th * c), 3),
                            "tau_s": float(f"{r_th * c:.4g}")})
    return out


_FB_PIN = re.compile(r"^(FB|ADJ|VFB|NFB|FB\d|SET)$", re.I)


def find_rc_highpass(nl: Netlist, rails: set, skip: set = frozenset(),
                     loads: dict | None = None) -> list[dict]:
    """First-order high-pass: a series capacitor into a node whose only DC
    path is resistance to ground or to a rail (AC coupling with a bias /
    bleed resistor, an SMA DC block). fc = 1 / (2 pi R C), R the shunt
    resistors in parallel with the caller's load on the node (`loads`, ohm:
    a 50 ohm instrument on an SMA sets the corner, not the 10k bleed);
    without one the corner is the open-load value and says so."""
    out = []
    for node, pins in nl.nets.items():
        # a rail carried only through a DNP link is still a candidate: the DC
        # test below rejects any fitted path that holds the node
        if is_ground(node) or is_unconnected(node) or node in skip:
            continue
        if any(p.type.split("+")[0] in ("power_in", "power_out") for p in pins
               if kind(p.ref) not in PASSIVE):
            continue
        if any(_FB_PIN.match((p.name or "").strip()) for p in pins if kind(p.ref) not in PASSIVE):
            continue                           # a feed-forward cap on a feedback divider
        series, shunt, other = [], [], False
        for p in pins:
            if not _fitted(nl, p.ref):
                continue
            ends = _two(nl, p.ref)
            k = kind(p.ref)
            if k in ("C", "R") and ends:
                far = ends[1] if ends[0] == node else ends[0]
                if k == "C" and not is_ground(far):
                    series.append((p.ref, far))
                elif k == "R" and (is_ground(far) or far in rails):
                    shunt.append(p.ref)
                elif k == "C":
                    other = True               # a cap to ground: a band-pass, not this
                else:
                    other = True               # a series R carries DC in
            elif k in ("L", "FB", "D"):
                other = True
        if len(series) != 1 or not shunt or other:
            continue
        c = parse_value(nl.components[series[0][0]]["value"].split("@")[0])
        rv = [parse_value(nl.components[r]["value"]) for r in shunt]
        if not c or not all(rv):
            continue
        g = sum(1 / v for v in rv)
        load = (loads or {}).get(node)
        r_th = 1 / (g + (1 / load if load else 0.0))
        conn = sorted({p.ref for p in pins if kind(p.ref) in ("J", "P", "CN")})
        out.append({"node": node, "input": series[0][1], "series_cap": series[0][0],
                    "shunt_r": shunt, "r_ohm": round(r_th, 3), "c_f": float(f"{c:.4g}"),
                    "fc_hz": round(1 / (2 * math.pi * r_th * c), 3),
                    "load": f"{load:g} ohm (caller)" if load else
                    "open" + (f" - {', '.join(conn)} goes off-board: pass loads={{net: ohm}} "
                              "for what is plugged in" if conn else ""),
                    "drives": _active_on(nl, node)})
    return out


# ---- LC ladders ------------------------------------------------------------------ #


def _shunts(nl: Netlist, net: str) -> dict:
    """Capacitors to ground, R-C damping branches and bleeder resistors on `net`."""
    caps, damp, bleed = [], [], []
    for p in nl.nets.get(net, []):
        if not _fitted(nl, p.ref):
            continue
        ends = _two(nl, p.ref)
        if not ends:
            continue
        other = ends[1] if ends[0] == net else ends[0]
        k = kind(p.ref)
        if k == "C" and is_ground(other):
            caps.append(p.ref)
        elif k == "R" and is_ground(other):
            bleed.append(p.ref)
        elif k == "R":
            # damping: R to a private node that only a C to ground shares
            mid = [q for q in nl.nets.get(other, []) if q.ref != p.ref and _fitted(nl, q.ref)
                   and kind(q.ref) != "TP"]
            cs = [q.ref for q in mid if kind(q.ref) == "C" and _two(nl, q.ref)
                  and any(is_ground(n) for n in _two(nl, q.ref)) and _fitted(nl, q.ref)]
            if mid and len(cs) == len(mid):
                damp.append({"r": p.ref, "node": other, "caps": cs})
    return {"caps": caps, "damping": damp, "bleed": bleed}


def find_ladders(nl: Netlist, exclude_nets: set, sources: dict) -> list[dict]:
    """Components of nets joined by fitted two-terminal L / FB parts, where at
    least two nets carry capacitors to ground; switch nodes excluded."""
    adj = defaultdict(list)
    dnp_links = []
    for ref, comp in nl.components.items():
        if kind(ref) not in ("L", "FB", "FL"):
            continue
        ends = _two(nl, ref)
        if not ends or any(is_ground(n) or is_unconnected(n) or n in exclude_nets for n in ends):
            continue
        if comp.get("dnp"):
            dnp_links.append(ref)
            continue
        a, b = ends
        adj[a].append((ref, b))
        adj[b].append((ref, a))
    seen, out = set(), []
    for start in sorted(adj):
        if start in seen:
            continue
        comp_nets, stack, links = set(), [start], set()
        while stack:
            n = stack.pop()
            if n in comp_nets:
                continue
            comp_nets.add(n)
            for ref, m in adj[n]:
                links.add(ref)
                stack.append(m)
        seen |= comp_nets
        sh = {n: _shunts(nl, n) for n in comp_nets}
        if sum(1 for n in comp_nets if sh[n]["caps"]) < 2:
            continue
        # source end: a net whose supply comes from outside the ladder
        def src_rank(n):
            s = sources.get(n, "")
            outside = not any(f"via {x} " in s for x in links)
            return (not (s and outside), n)
        inp = min(comp_nets, key=src_rank)
        out.append({"input": inp, "nets": sorted(comp_nets), "links": sorted(links),
                    "shunts": sh, "dnp_links_nearby": sorted(
                        r for r in dnp_links if set(_two(nl, r)) & comp_nets)})
    return out


def ladder_deck(nl: Netlist, lad: dict, f_lo: float, f_hi: float, scale: dict | None = None,
                specs: dict | None = None, loads: dict | None = None,
                source_ohm: float = 0.0,
                mount_l: dict | None = None) -> tuple[str, dict, list[str]]:
    """SPICE deck for one ladder: 1 V AC at the input net through
    `source_ohm`, `loads` = {net: ohms} resistive loads (open otherwise),
    `mount_l` = {cap ref: henry} PCB mounting inductance in series with each
    capacitor (from `mounting_inductance`)."""
    mount_l = mount_l or {}
    scale = scale or {}
    specs = specs or {}
    loads = loads or {}
    node = {}
    for i, n in enumerate(lad["nets"]):
        node[n] = f"n{i}"
    if source_ohm:
        lines = ["* kicad-claude ladder", "VIN src 0 AC 1",
                 f"RSRC src {node[lad['input']]} {source_ohm:.6g}"]
    else:
        lines = ["* kicad-claude ladder", f"VIN {node[lad['input']]} 0 AC 1"]
    for n, r in loads.items():
        if n in node and r:
            lines.append(f"RLOAD_{node[n]} {node[n]} 0 {float(r):.6g}")
    notes = []

    def val(ref):
        v = parse_value(nl.components[ref]["value"].split("@")[0])
        return None if v is None else v * scale.get(ref, 1.0)

    def esr(ref, key):
        sv = ((specs.get(ref) or {}).get("electrical") or {}).get(key)
        return (sv or {}).get("typ") or (sv or {}).get("max")

    k = 0
    for ref in lad["links"]:
        a, b = _two(nl, ref)
        v = val(ref)
        if kind(ref) == "FB" or v is None or v >= 1:
            notes.append(f"{ref} ({nl.components[ref]['value']}): no inductance value - "
                         "modelled as 1 mOhm")
            lines.append(f"R{ref} {node[a]} {node[b]} 1m")
            continue
        dcr = esr(ref, "DCR")
        if dcr:
            k += 1
            lines.append(f"L{ref} {node[a]} x{k} {v:.6g}")
            lines.append(f"RDCR{ref} x{k} {node[b]} {dcr:.6g}")
        else:
            lines.append(f"L{ref} {node[a]} {node[b]} {v:.6g}")
    for n, sh in lad["shunts"].items():
        for c in sh["caps"]:
            v = val(c)
            if not v:
                notes.append(f"{c}: value not parseable, skipped")
                continue
            r = esr(c, "ESR")
            esl = (esr(c, "ESL") or 0.0) + mount_l.get(c, 0.0)
            chain = [("R", r), ("L", esl)]
            chain = [(t, x) for t, x in chain if x]
            prev = node[n]
            for t, x in chain:
                k += 1
                lines.append(f"{t}P{c}_{t} {prev} x{k} {x:.6g}")
                prev = f"x{k}"
            lines.append(f"C{c} {prev} 0 {v:.6g}")
        for r in sh["bleed"]:
            v = val(r)
            if v:
                lines.append(f"R{r} {node[n]} 0 {v:.6g}")
        for d in sh["damping"]:
            k += 1
            rv = val(d["r"])
            lines.append(f"R{d['r']} {node[n]} d{k} {max(rv or 0, 1e-6):.6g}")
            for c in d["caps"]:
                cv = val(c)
                if cv:
                    lines.append(f"C{c} d{k} 0 {cv:.6g}")
    for n in lad["nets"]:                      # keep every node referenced to ground
        lines.append(f"RLEAK_{node[n]} {node[n]} 0 1e12")
    lines.append(f".ac dec 100 {f_lo:g} {f_hi:g}")
    lines.append(".end")
    return "\n".join(lines) + "\n", node, notes


def find_ngspice() -> str | None:
    from kicad_claude.adapters import spice_backends
    return spice_backends.find_ngspice()


NOSIM = "no SPICE simulator found (ngspice on PATH, or LTspice)"


def run_ac(deck: str, nodes: list[str], sim, timeout: float = 60.0) -> dict:
    """AC sweep of `deck` on `sim` (a spice_backends.Backend or an ngspice
    path); returns {node: [(f, dB)]}."""
    from kicad_claude.adapters import spice_backends
    return spice_backends.as_backend(sim).run_ac(deck, nodes, timeout)


def _interp(curve: list, f: float) -> float | None:
    for (f1, v1), (f2, v2) in zip(curve, curve[1:]):
        if f1 <= f <= f2:
            t = (math.log10(f) - math.log10(f1)) / (math.log10(f2) - math.log10(f1))
            return v1 + t * (v2 - v1)
    return None


def summarize_curve(curve: list, freqs: list[float]) -> dict:
    peaks = []
    for (f0, a), (f1, b), (f2, c) in zip(curve, curve[1:], curve[2:]):
        if b > a and b >= c and b > 0.5:
            peaks.append({"f_hz": round(f1, 1), "gain_db": round(b, 2)})
    peak = max((p["gain_db"] for p in peaks), default=None)
    return {"peaks": peaks[:5], "max_peaking_db": peak,
            "at": {f"{f:g}": (round(v, 1) if (v := _interp(curve, f)) is not None else None)
                   for f in freqs}}


def analyze_ladder(nl: Netlist, lad: dict, freqs: list[float], sim,
                   mc: int, rng: random.Random, specs: dict, loads: dict | None = None,
                   source_ohm: dict | None = None, mount_l: dict | None = None,
                   bias: dict | None = None) -> dict:
    """`bias`: {cap ref: remaining capacitance fraction at its DC bias} from
    the datasheet C_bias curve; applied to every run (nominal and MC)."""
    bias = bias or {}
    # far end: the net farthest from the input in link hops
    hops = {lad["input"]: 0}
    frontier = [lad["input"]]
    while frontier:
        n = frontier.pop(0)
        for ref in lad["links"]:
            a, b = _two(nl, ref)
            for x, y in ((a, b), (b, a)):
                if x == n and y not in hops:
                    hops[y] = hops[n] + 1
                    frontier.append(y)
    out_net = max(hops, key=lambda n: (hops[n], len(lad["shunts"][n]["caps"])))
    rec = {"input": lad["input"], "output": out_net, "links": lad["links"],
           "nets": lad["nets"],
           "elements": {n: {"caps": [f"{c} {nl.components[c]['value']}" for c in s["caps"]],
                            "damping": [f"{d['r']} {nl.components[d['r']]['value']} + "
                                        + ", ".join(f"{c} {nl.components[c]['value']}"
                                                    for c in d["caps"]) for d in s["damping"]],
                            "bleed": s["bleed"]}
                        for n, s in lad["shunts"].items()},
           "dnp_links_nearby": lad["dnp_links_nearby"]}
    f_lo = 10.0
    f_hi = max([1e8] + [f * 10 for f in freqs])
    loads = {n: r for n, r in (loads or {}).items() if n in lad["nets"]}
    rs = (source_ohm or {}).get(lad["input"], 0.0)
    deck, node, notes = ladder_deck(nl, lad, f_lo, f_hi, dict(bias), specs=specs, loads=loads,
                                    source_ohm=rs, mount_l=mount_l)
    used_bias = {c: round(v, 3) for s in lad["shunts"].values() for c in s["caps"]
                 for v in [bias.get(c)] if v is not None}
    if used_bias:
        rec["dc_bias_ratio"] = used_bias
    used_mount = {c: mount_l[c] for s in lad["shunts"].values() for c in s["caps"]
                  if c in (mount_l or {})}
    if used_mount:
        rec["mounting_inductance_h"] = {c: float(f"{v:.3g}") for c, v in used_mount.items()}
    rec["loads"] = loads
    rec["source_ohm"] = rs
    rec["notes"] = notes + ["ideal parts except where the spec cache gives ESR / ESL / DCR"
                            + (" and the PCB gives mounting inductance" if mount_l else "")
                            + ("; capacitance derated by the datasheet DC-bias curve for "
                               + ", ".join(sorted(used_bias)) if used_bias else "") + "; "
                            + (f"loads {loads}" if loads else "open load (no damping from the "
                               "load: peaking is an upper bound, pass loads={net: ohm})")
                            + f", source {rs} ohm"]
    rec["deck"] = deck
    if not sim:
        rec["error"] = NOSIM
        return rec
    curves = run_ac(deck, [node[out_net]], sim)
    rec["response"] = summarize_curve(curves[node[out_net]], freqs)
    if mc:
        tol = {r: tolerance(nl.components[r]) or spec_tolerance(specs, r) for r in
               lad["links"] + [c for s in lad["shunts"].values() for c in s["caps"]]}
        known = {r: t for r, t in tol.items() if t}
        rec["monte_carlo"] = {"n": mc, "tolerances": tol}
        if known:
            peaks, atts = [], defaultdict(list)
            for _ in range(mc):
                scale = {r: (1 + rng.uniform(-t, t)) * bias.get(r, 1.0) for r, t in known.items()}
                scale.update({r: v for r, v in bias.items() if r not in scale})
                deck_i, node_i, _ = ladder_deck(nl, lad, f_lo, f_hi, scale, specs, loads, rs,
                                                mount_l)
                s = summarize_curve(run_ac(deck_i, [node_i[out_net]], sim)[node_i[out_net]],
                                    freqs)
                if s["max_peaking_db"] is not None:
                    peaks.append(s["max_peaking_db"])
                for f, v in s["at"].items():
                    if v is not None:
                        atts[f].append(v)
            rec["monte_carlo"]["peaking_db_max"] = round(max(peaks), 2) if peaks else None
            rec["monte_carlo"]["attenuation_db_worst"] = {f: round(max(v), 1)
                                                          for f, v in atts.items()}
        else:
            rec["monte_carlo"]["note"] = "no Tolerance field on the ladder parts: nominal only"
    return rec


# ---- op-amp stages ----------------------------------------------------------- #

_OPA_PIN = re.compile(r"^(?:(?P<pre>\d?)(?P<s1>[+-])?IN(?P<s2>[+-])?(?P<ch>[A-D\d]?)|"
                      r"(?P<pre2>\d?)OUT(?P<ch2>[A-D\d]?)|(?P<s3>[+-])(?P<ch3>[A-D]?))$", re.I)


def _opamp_pins(nl: Netlist, ref: str) -> dict[str, dict]:
    """channel -> {"+": net, "-": net, "out": net} from pin names
    (+IN / IN+ / IN1+ / 1IN- / -INA / OUTA / 1OUT / + / -)."""
    ch: dict[str, dict] = defaultdict(dict)
    for p in nl.pins.get(ref, []):
        name = (p.name or "").replace("~", "").replace("{", "").replace("}", "").upper()
        m = _OPA_PIN.match(name)
        if not m:
            continue
        if m.group("pre2") is not None and name.startswith(m.group("pre2") + "OUT"):
            key = m.group("pre2") or m.group("ch2") or ""
            ch[key]["out"] = p.net
            continue
        sign = m.group("s1") or m.group("s2") or m.group("s3")
        if not sign:
            continue
        key = m.group("pre") or m.group("ch") or m.group("ch3") or ""
        ch[key][sign] = p.net
    # a single-channel symbol whose output pin has no name (KiCAD "~"):
    # the one unnamed output-typed pin is OUT
    open_ = [k for k, v in ch.items() if {"+", "-"} <= set(v) and "out" not in v]
    unnamed = [p for p in nl.pins.get(ref, []) if p.type.split("+")[0] == "output"
               and (p.name or "").strip("~ ") == ""]
    if len(ch) == 1 and len(open_) == 1 and len(unnamed) == 1:
        ch[open_[0]]["out"] = unnamed[0].net
    return {k: v for k, v in ch.items() if {"+", "-", "out"} <= set(v)}


def _r_between(nl: Netlist, a: str, b: str, kinds=("R",)) -> list[str]:
    out = []
    for p in nl.nets.get(a, []):
        if kind(p.ref) in kinds and _fitted(nl, p.ref):
            ends = _two(nl, p.ref)
            if ends and set(ends) == {a, b}:
                out.append(p.ref)
    return out


def find_opamp_stages(nl: Netlist, specs: dict) -> list[dict]:
    out = []
    for ref in sorted(nl.components):
        if kind(ref) not in ("U", "IC") or not _fitted(nl, ref):
            continue
        for ch, pins in sorted(_opamp_pins(nl, ref).items()):
            inv, non, o = pins["-"], pins["+"], pins["out"]
            rec = {"ref": ref, "channel": ch or None, "in_plus": non, "in_minus": inv, "out": o}
            rf = _r_between(nl, o, inv)
            cf = _r_between(nl, o, inv, ("C",))
            if inv == o:
                rec.update(topology="follower", gain=1.0, noise_gain=1.0)
            elif rf:
                rfv = parse_value(nl.components[rf[0]]["value"])
                rg = []
                for p in nl.nets.get(inv, []):
                    if kind(p.ref) == "R" and p.ref not in rf and _fitted(nl, p.ref):
                        ends = _two(nl, p.ref)
                        if ends:
                            rg.append((p.ref, ends[1] if ends[0] == inv else ends[0]))
                rec["rf"] = rf[0]
                if not rg:
                    rec.update(topology="follower_with_rf", gain=1.0, noise_gain=1.0)
                else:
                    gref, gnet = rg[0]
                    rgv = parse_value(nl.components[gref]["value"])
                    rec["rg"] = gref
                    if rfv and rgv:
                        ng = 1 + rfv / rgv
                        if is_ground(gnet):
                            rec.update(topology="non_inverting", gain=round(ng, 4),
                                       noise_gain=round(ng, 4), input=non)
                        else:
                            rec.update(topology="inverting", gain=round(-rfv / rgv, 4),
                                       noise_gain=round(ng, 4), input=gnet)
                if cf and rfv:
                    cfv = parse_value(nl.components[cf[0]]["value"])
                    if cfv:
                        rec["feedback_pole_hz"] = round(1 / (2 * math.pi * rfv * cfv), 2)
                        rec["cf"] = cf[0]
            else:
                rec["topology"] = "unrecognised (open loop / comparator?)"
            gbw = (((specs.get(ref) or {}).get("electrical") or {}).get("GBW") or {})
            g = gbw.get("typ") or gbw.get("min")
            if g and rec.get("noise_gain"):
                rec["bandwidth_hz"] = round(g / rec["noise_gain"], 1)
                rec["gbw_source"] = f"datasheet p.{gbw.get('page')}"
            out.append(rec)
    return out


# ---- op-amp vendor models ----------------------------------------------------------- #

ROLE_RE = {
    "+IN": re.compile(r"^(\+IN|IN\+|INP|IN_P|NONINV\w*|VINP|\+)$", re.I),
    "-IN": re.compile(r"^(-IN|IN-|INN|IN_N|INV\w*|VINN|-)$", re.I),
    "OUT": re.compile(r"^(OUT\w*|VOUT|VO)$", re.I),
    "V+": re.compile(r"^(V\+|VCC|VDD|\+V\w*|VS\+|VPOS|V_POS|VP)$", re.I),
    "V-": re.compile(r"^(V-|VEE|VSS|-V\w*|VS-|VNEG|V_NEG|VN)$", re.I),
}


def parse_subckt(lib: str, name: str | None = None) -> tuple[str, list[str]]:
    """(name, node names) of `name` (or the first `.subckt`) in a SPICE lib,
    with `+` continuation lines joined."""
    text = Path(lib).read_text(encoding="utf-8", errors="replace")
    lines, cur = [], ""
    for raw in text.splitlines():
        if raw.lstrip().startswith("+"):
            cur += " " + raw.lstrip()[1:]
        else:
            if cur:
                lines.append(cur)
            cur = raw
    lines.append(cur)
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == ".subckt" and (
                name is None or parts[1].lower() == name.lower()):
            nodes = [x for x in parts[2:] if "=" not in x and x.lower() != "params:"]
            return parts[1], nodes
    raise ValueError(f"no .subckt {name or ''} in {lib}")


def model_roles(nodes: list[str], given: list[str] | None = None) -> list[str | None]:
    """Role per subckt node: the caller's list, else from node names."""
    if given:
        if len(given) != len(nodes):
            raise ValueError(f"pins lists {len(given)} roles for {len(nodes)} subckt nodes")
        return [g if g in ROLE_RE else None for g in given]
    out = []
    for n in nodes:
        out.append(next((r for r, rx in ROLE_RE.items() if rx.match(n)), None))
    return out


def model_from_fields(nl: Netlist, ref: str) -> dict | None:
    """KiCAD simulator fields: `Sim.Library` (+ `Sim.Name`). With `Sim.Pins`
    ("1=OUT 2=IN- 3=IN+ ...") the symbol pin -> model node map is used to
    give each model node its role from the symbol pin's net."""
    f = nl.components.get(ref, {}).get("fields") or {}
    lib = f.get("Sim.Library") or f.get("Spice_Lib_File")
    if not lib:
        return None
    rec = {"lib": lib, "subckt": f.get("Sim.Name") or f.get("Spice_Model") or None,
           "source": "Sim.* fields"}
    pins = f.get("Sim.Pins")
    if pins:
        rec["sim_pins"] = dict(x.split("=", 1) for x in pins.split() if "=" in x)
    return rec


def opamp_ac(nl: Netlist, stage: dict, model: dict, volts: dict, sim,
             supplies: dict | None = None) -> dict:
    """AC closed-loop response of one op-amp stage with a vendor subcircuit:
    1 V AC at the stage input, the stage's own Rf / Rg / Cf, DC supplies on
    the model's V+ / V- nodes. Returns low-frequency gain, -3 dB frequency
    and peaking."""
    lib = str(Path(model["lib"]).expanduser())
    name, nodes = parse_subckt(lib, model.get("subckt"))
    nets = {"+IN": stage["in_plus"], "-IN": stage["in_minus"], "OUT": stage["out"]}
    if model.get("sim_pins") and not model.get("pins"):
        # symbol pin number -> model node: the node's role is its pin's net
        pin_net = {p.number: p.net for p in nl.pins.get(stage["ref"], [])}
        sup_nets = sorted({(volts.get(pin_net[s], 0.0), pin_net[s]) for s in model["sim_pins"]
                           if s in pin_net and pin_net[s] not in nets.values()})
        by_node = {}
        for s, node_name in model["sim_pins"].items():
            net = pin_net.get(s)
            role = next((r for r, nn in nets.items() if nn == net), None)
            if role is None and sup_nets and net is not None:
                role = "V+" if net == sup_nets[-1][1] else "V-"
            by_node[node_name] = role
        roles = [by_node.get(n) for n in nodes]
    else:
        roles = model_roles(nodes, model.get("pins"))
    # supplies: caller > the IC's power pins with known rail voltages
    sup = dict((supplies or {}).get(stage["ref"]) or {})
    if not sup:
        rails = sorted({(volts[p.net], p.net) for p in nl.pins.get(stage["ref"], [])
                        if p.type.split("+")[0] == "power_in" and p.net in volts
                        and not is_ground(p.net)})
        if rails:
            sup["V+"] = rails[-1][0]
            sup["V-"] = rails[0][0] if len(rails) > 1 else 0.0
    if "V+" not in sup:
        raise ValueError(f"{stage['ref']}: supply voltages unknown - pass supplies="
                         f"{{'{stage['ref']}': {{'V+': 15, 'V-': -15}}}} or rail_voltages")
    sup.setdefault("V-", 0.0)
    node = {}

    def nd(net):
        if net is None or is_ground(net):
            return "0"
        return node.setdefault(net, f"m{len(node)}")
    lines = [f"* op-amp stage {stage['ref']} {stage.get('channel') or ''}", f".include \"{lib}\""]
    inp = stage.get("input") or stage["in_plus"]
    lines.append(f"VIN {nd(inp)} 0 AC 1")
    if stage.get("topology") == "inverting" and not is_ground(stage["in_plus"]):
        lines.append(f"VREF {nd(stage['in_plus'])} 0 0")
    for key in ("rf", "rg", "cf"):
        ref = stage.get(key)
        if ref:
            a, c = _two(nl, ref)
            v = parse_value(nl.components[ref]["value"])
            lines.append(f"{'C' if key == 'cf' else 'R'}{ref} {nd(a)} {nd(c)} {v:.6g}")
    xnodes = []
    for n, role in zip(nodes, roles):
        if role in nets:
            xnodes.append(nd(nets[role]))
        elif role in ("V+", "V-"):
            xnodes.append(f"s{role[1] == '+' and 'p' or 'n'}")
        else:
            xnodes.append(f"nc_{n}")
    if "V+" not in roles or "-IN" not in roles or "+IN" not in roles or "OUT" not in roles:
        raise ValueError(f"cannot map subckt {name} nodes {nodes} to +IN/-IN/OUT/V+/V- "
                         "(pass pins=[roles in node order])")
    lines.append(f"VSP sp 0 DC {sup['V+']}")
    lines.append(f"VSN sn 0 DC {sup['V-']}")
    lines.append(f"XU {' '.join(xnodes)} {name}")
    for n in node.values():
        lines.append(f"RLK_{n} {n} 0 1e12")
    lines += [".ac dec 50 1 1e9", ".end"]
    deck = "\n".join(lines) + "\n"
    curve = run_ac(deck, [nd(stage["out"])], sim)[nd(stage["out"])]
    lf = curve[0][1]
    peak = max(v for _, v in curve)
    f3 = next((f for f, v in curve if v < lf - 3), None)
    return {"model": name, "lib": lib, "supplies": sup, "gain_db_lf": round(lf, 2),
            "gain_lf": round(10 ** (lf / 20), 4), "f_3db_hz": round(f3, 1) if f3 else None,
            "peaking_db": round(peak - lf, 2), "roles": dict(zip(nodes, roles)), "deck": deck}


# ---- crystals ------------------------------------------------------------------- #


def find_crystals(nl: Netlist, specs: dict, c_stray_pf: float | None) -> list[dict]:
    out = []
    for ref in sorted(nl.components):
        if not re.fullmatch(r"(Y|X)\d+", ref) or not _fitted(nl, ref):
            continue
        sig = [p for p in nl.pins.get(ref, []) if not is_ground(p.net) and not is_unconnected(p.net)]
        if len(sig) != 2:
            continue
        loads = []
        for p in sig:
            cs = [q.ref for q in nl.nets.get(p.net, []) if kind(q.ref) == "C" and _fitted(nl, q.ref)
                  and _two(nl, q.ref) and any(is_ground(n) for n in _two(nl, q.ref))]
            loads.append(sum(parse_value(nl.components[c]["value"]) or 0 for c in cs) or None)
            loads.append(cs)
        c1, caps1, c2, caps2 = loads
        rec = {"ref": ref, "value": nl.components[ref]["value"], "pins": [sig[0].net, sig[1].net],
               "c1": caps1, "c2": caps2}
        if c1 and c2:
            series = c1 * c2 / (c1 + c2)
            rec["c_series_pf"] = round(series * 1e12, 3)
            rec["c_stray_pf"] = c_stray_pf
            rec["cl_effective_pf"] = round(series * 1e12 + (c_stray_pf or 0.0), 3)
            cl = (((specs.get(ref) or {}).get("electrical") or {}).get("CL") or {})
            if cl.get("typ"):
                rec["cl_spec_pf"] = round(cl["typ"] * 1e12, 3)
                rec["cl_source"] = f"datasheet p.{cl.get('page')}"
                rec["cl_error"] = round(rec["cl_effective_pf"] / rec["cl_spec_pf"] - 1, 3)
        out.append(rec)
    return out


# ---- PCB mounting inductance ------------------------------------------------------ #

MU0 = 4e-7 * math.pi


def via_inductance_h(height_mm: float, drill_mm: float) -> float:
    """Partial inductance of a via barrel, L = 0.2 h (ln(4h/d) + 1) nH
    (h, d in mm; Johnson & Graham)."""
    if height_mm <= 0 or drill_mm <= 0:
        return 0.0
    return 0.2e-9 * height_mm * (math.log(4 * height_mm / drill_mm) + 1)


def mounting_inductance(tree: list, caps: list[str]) -> dict[str, dict]:
    """For each capacitor ref: its ground pad's nearest ground via, the via
    barrel from the cap's layer to the nearest ground plane (stackup
    thicknesses) and its partial inductance, plus the pad-to-via connection
    as a strip over the plane (track width when a ground track joins them,
    else the pad width for a pour). A first-order mounting estimate; the
    cap's own ESL comes only from its datasheet."""
    from kicad_claude.adapters import emc_review, pcb_netlist
    from kicad_claude.adapters.pcb_geometry import is_ground_net

    b = emc_review.load_board(tree, 1.0)
    roles = {r["layer"]: r["role"] for r in emc_review.layer_roles(b)}
    gvias = []
    for node in tree[1:]:
        if isinstance(node, list) and node and str(node[0]) == "via":
            v = [x for x in node[1:] if isinstance(x, list)]
            at = next((x for x in v if str(x[0]) == "at"), None)
            dr = next((x for x in v if str(x[0]) == "drill"), None)
            from kicad_claude.adapters.pcb_editor import net_of
            if at and is_ground_net(net_of(node, tree)):
                gvias.append(((float(at[1]), float(at[2])), float(dr[1]) if dr else 0.3))
    pads = defaultdict(list)
    for p in pcb_netlist.list_pads(tree):
        pads[p["ref"]].append(p)
    tracks_by_layer = defaultdict(list)
    for t in pcb_netlist._tracks(tree):
        if is_ground_net(t["net_name"]):
            tracks_by_layer[t["layer"]].append(t)
    out = {}
    for c in caps:
        gp = [p for p in pads.get(c, []) if is_ground_net(p["net_name"])]
        if not gp or not gvias:
            continue
        pad = gp[0]
        (vx, vy), drill = min(gvias, key=lambda v: math.dist(v[0], pad["point"]))
        layer = pad["layers"][0] if pad["layers"] else b.layers[0]
        k = b.layers.index(layer) if layer in b.layers else 0
        # walk from the cap's layer into the board to the first ground plane;
        # dielectrics[j] sits between layers[j] and layers[j + 1]
        h, found = 0.0, None
        down = k < len(b.layers) - 1

        def diel(i):            # a board with no stackup has no dielectrics: no estimate
            return b.dielectrics[i] if 0 <= i < len(b.dielectrics) else {}
        j = k
        while 0 <= (j + 1 if down else j - 1) < len(b.layers):
            d = diel(j) if down else diel(j - 1)
            j = j + 1 if down else j - 1
            h += d.get("thickness_mm") or 0.0
            if roles.get(b.layers[j]) == "ground_plane":
                found = b.layers[j]
                break
        if not found or not h:
            continue
        dist = math.dist((vx, vy), pad["point"])
        # pad -> via: a ground track that starts at the pad and ends at the
        # via (single segment), else the pour, modelled as a strip of the
        # pad's width over the plane below the cap's layer
        h_strip = diel(k)["thickness_mm"] if down and diel(k).get("thickness_mm") \
            else (diel(k - 1).get("thickness_mm") if k else None) or h
        track = None
        for t in tracks_by_layer.get(layer, []):
            ends = (t["start"], t["end"])
            if any(math.dist(e, pad["point"]) <= pad["radius_mm"] for e in ends) and \
                    any(math.dist(e, (vx, vy)) <= 0.05 for e in ends):
                track = t
                break
        if track:
            w, method = track["width_mm"], "track"
            length = math.dist(track["start"], track["end"])
        else:
            w, method, length = pad["radius_mm"] * math.sqrt(2), "pour (pad-width strip)", dist
        l_trace = strip_inductance_h(length, w, h_strip)
        l_via = via_inductance_h(h, drill)
        out[c] = {"via_at": [vx, vy], "via_drill_mm": drill, "plane": found,
                  "height_mm": round(h, 4), "pad_to_via_mm": round(dist, 3),
                  "connection": method, "strip_width_mm": round(w, 3),
                  "l_via_h": l_via, "l_trace_h": l_trace, "l_h": l_via + l_trace}
    return out


def strip_inductance_h(length_mm: float, width_mm: float, height_mm: float) -> float:
    """Inductance of a strip of `length_mm` over a plane `height_mm` below:
    the per-length inductance implied by Hammerstad's microstrip Z0
    (L' = Z0 * sqrt(eps_eff) / c, independent of eps):
      w/h <= 1: L' = (mu0 / 2 pi) ln(8h/w + w/4h)
      w/h >  1: L' = mu0 / (w/h + 1.393 + 0.667 ln(w/h + 1.444))."""
    if length_mm <= 0 or width_mm <= 0 or height_mm <= 0:
        return 0.0
    u = width_mm / height_mm
    if u <= 1:
        lp = MU0 / (2 * math.pi) * math.log(8 / u + u / 4)
    else:
        lp = MU0 / (u + 1.393 + 0.667 * math.log(u + 1.444))
    return lp * length_mm * 1e-3


# ---- entry ------------------------------------------------------------------ #

TYPES = ("divider", "rc", "lc", "opamp", "crystal")


def _sim(sim):
    """`sim` as given; None -> the first installed simulator; "" -> none."""
    if sim is None:
        from kicad_claude.adapters import spice_backends
        return spice_backends.detect("auto")
    return sim


def analyze(nl: Netlist, types: tuple[str, ...] = TYPES, volts: dict | None = None,
            vref: dict | None = None, regulators: list | None = None, sources: dict | None = None,
            freqs: list[float] | None = None, mc: int = 0, seed: int = 1,
            specs: dict | None = None, sim=None,
            peaking_warn_db: float = 6.0, loads: dict | None = None,
            source_ohm: dict | None = None, mount_l: dict | None = None,
            c_stray_pf: float | None = None, cl_tolerance: float = 0.1,
            opamp_models: dict | None = None, supplies: dict | None = None) -> dict:
    """`vref`: {fb net: {min, typ, max, source}}; `regulators`: power-tree
    records (switch nets are excluded from ladders); `freqs`: frequencies to
    report attenuation at (e.g. switching harmonics)."""
    volts = volts or {}
    rng = random.Random(seed)
    findings: list[dict] = []
    result: dict = {}
    sw = set()
    for r in regulators or []:
        if r.get("switch_net"):
            sw.add(r["switch_net"])
    # converter-internal nets: nets of switching ICs' pins (other than in / out)
    rails = set(volts) | {n for r in regulators or []
                          for n in (r.get("input_net"), r.get("output_net")) if n}
    for r in regulators or []:
        if r.get("topology") != "switching":
            continue
        for p in nl.pins.get(r["ref"], []):
            if p.net not in rails and not is_ground(p.net):
                sw.add(p.net)
    ladders = find_ladders(nl, sw, sources or {}) if "lc" in types or "rc" in types else []
    ladder_nets = {n for lad in ladders for n in lad["nets"]}

    if "divider" in types:
        divs = []
        for d in find_dividers(nl):
            rec = analyze_divider(nl, d, volts, (vref or {}).get(d["mid"]), mc, rng)
            divs.append(rec)
            v = rec.get("vout")
            if v and v.get("worst"):
                findings.append({"severity": "info", "rule": "SP-DIV-VOUT", "ref": d["top"],
                                 "message": f"{d['mid']}: Vout {v['typ']} V, worst case "
                                 f"{v['worst'][0]}..{v['worst'][1]} V (Vref {v['vref_source']}, "
                                 f"tolerance {rec['tolerance']})."})
            if any(t is None for t in rec.get("tolerance", {}).values()):
                findings.append({"severity": "info", "rule": "SP-TOL", "ref": d["top"],
                                 "message": f"Divider on {d['mid']}: no Tolerance field on "
                                 + ", ".join(r for r, t in rec["tolerance"].items() if t is None)
                                 + " — worst case uses the nominal value."})
        result["dividers"] = divs
    if "rc" in types:
        result["rc_filters"] = find_rc(nl, ladder_nets | rails)
        result["rc_highpass"] = find_rc_highpass(nl, rails, ladder_nets, loads)
    if "lc" in types:
        ng = _sim(sim)
        lads = []
        for lad in ladders:
            from kicad_claude.adapters.datasheet_specs import c_bias_ratio
            bias = {}
            for n, sh in lad["shunts"].items():
                for c in sh["caps"]:
                    ratio = c_bias_ratio((specs or {}).get(c), volts.get(n, 0.0),
                                         parse_value(nl.components[c]["value"].split("@")[0]))
                    if ratio is not None and n in volts:
                        bias[c] = ratio
            rec = analyze_ladder(nl, lad, sorted(freqs or []), ng, mc, rng, specs or {},
                                 loads, source_ohm, mount_l, bias)
            lads.append(rec)
            resp = rec.get("response") or {}
            pk = (rec.get("monte_carlo") or {}).get("peaking_db_max") or resp.get("max_peaking_db")
            if pk is not None and pk > peaking_warn_db:
                findings.append({"severity": "warning" if rec["loads"] else "info",
                                 "rule": "SP-LC-PEAK", "ref": rec["links"][0],
                                 "message": f"Filter {rec['input']} -> {rec['output']} "
                                 f"({', '.join(rec['links'])}) peaks {pk} dB at "
                                 f"{resp['peaks'][0]['f_hz'] if resp.get('peaks') else '?'} Hz "
                                 f"(> {peaking_warn_db} dB): under-damped — add or check the "
                                 "damping branch."
                                 + ("" if rec["loads"] else " Open load: an upper bound; pass "
                                    "loads={net: ohm} for the real damping.")})
            if resp.get("at"):
                findings.append({"severity": "info", "rule": "SP-LC-ATT", "ref": rec["links"][0],
                                 "message": f"Filter {rec['input']} -> {rec['output']}: "
                                 + ", ".join(f"{float(f) / 1e3:g} kHz {v} dB"
                                             for f, v in resp["at"].items())
                                 + f"; peaking {resp.get('max_peaking_db')} dB ("
                                 + ("datasheet DCR / ESR / ESL" if any(
                                     (specs or {}).get(x) for x in rec["links"] + [
                                         c for e in lad["shunts"].values() for c in e["caps"]])
                                    else "ideal parts")
                                 + (", DC-bias derated caps" if rec.get("dc_bias_ratio") else "")
                                 + (f", loads {rec['loads']}" if rec["loads"] else ", open load")
                                 + ")."})
            if rec.get("error"):
                findings.append({"severity": "warning", "rule": "SP-NOSIM", "ref": rec["links"][0],
                                 "message": rec["error"]})
        result["ladders"] = lads
    if "opamp" in types:
        stages = find_opamp_stages(nl, specs or {})
        result["opamp_stages"] = stages
        ng = _sim(sim)
        tmp = tempfile.TemporaryDirectory()
        for s in stages:
            model = (opamp_models or {}).get(s["ref"]) or model_from_fields(nl, s["ref"])
            if not model and (specs or {}).get(s["ref"]) and not s.get(
                    "topology", "").startswith("unrecognised"):
                from kicad_claude.adapters import spice_models
                gen = spice_models.opamp(specs[s["ref"]])
                if gen.get("text"):
                    lib = Path(tmp.name) / f"{gen['subckt']}.lib"
                    lib.write_text(gen["text"], encoding="utf-8")
                    model = {"lib": str(lib), "subckt": gen["subckt"], "pins": gen["pins"],
                             "source": gen["source"]}
                else:
                    s["behavioural_missing"] = gen["missing"]
            if not model or s.get("topology", "").startswith("unrecognised"):
                if not s.get("topology", "").startswith("unrecognised"):
                    s["model"] = ("ideal (no vendor model: pass opamp_models or set Sim.Library"
                                  + (f"; behavioural model needs {', '.join(s['behavioural_missing'])}"
                                     if s.get("behavioural_missing") else "") + ")")
                continue
            if not ng:
                s["model"] = f"vendor model given, {NOSIM}"
                continue
            try:
                s["simulated"] = opamp_ac(nl, s, model, volts, ng, supplies)
                s["simulated"].pop("deck", None)
                s["model"] = (f"{model['source']}: {s['simulated']['model']}"
                              if str(model.get("source", "")).startswith("behavioural")
                              else f"vendor: {s['simulated']['model']}")
            except (ValueError, OSError, RuntimeError) as e:
                s["model"] = f"model not usable: {e}"
        tmp.cleanup()
        for s in stages:
            if s.get("topology", "").startswith("unrecognised"):
                continue
            findings.append({"severity": "info", "rule": "SP-OPAMP", "ref": s["ref"],
                             "message": f"{s['ref']}{('/' + s['channel']) if s['channel'] else ''}: "
                             f"{s['topology']}, gain {s.get('gain')}, noise gain "
                             f"{s.get('noise_gain')}"
                             + (f", bandwidth ~{s['bandwidth_hz']:.3g} Hz (GBW {s['gbw_source']})"
                                if s.get("bandwidth_hz") else ", bandwidth needs GBW from the "
                                "datasheet spec cache")
                             + (f", feedback pole {s['feedback_pole_hz']} Hz"
                                if s.get("feedback_pole_hz") else "")
                             + (f"; {s['model']}: gain {s['simulated']['gain_lf']}, -3 dB at "
                                f"{s['simulated']['f_3db_hz']} Hz, peaking "
                                f"{s['simulated']['peaking_db']} dB"
                                if s.get("simulated") else f"; {s.get('model', 'ideal')}") + "."})
    if "crystal" in types:
        xs = find_crystals(nl, specs or {}, c_stray_pf)
        result["crystals"] = xs
        for x in xs:
            if x.get("cl_error") is not None and abs(x["cl_error"]) > cl_tolerance:
                findings.append({"severity": "warning", "rule": "SP-XTAL-CL", "ref": x["ref"],
                                 "message": f"{x['ref']}: load {x['cl_effective_pf']} pF "
                                 f"(C1 {x['c1']}, C2 {x['c2']}, stray {x['c_stray_pf']}) vs "
                                 f"crystal CL {x['cl_spec_pf']} pF ({x['cl_source']}): "
                                 f"{x['cl_error']:+.0%} - the oscillator runs off frequency."})
            elif "cl_effective_pf" in x and x.get("cl_spec_pf") is None:
                findings.append({"severity": "info", "rule": "SP-XTAL-CL", "ref": x["ref"],
                                 "message": f"{x['ref']}: load {x['cl_effective_pf']} pF"
                                 + ("" if x["c_stray_pf"] is not None else
                                    " (no stray capacitance given: c_stray_pf)")
                                 + "; the crystal's CL is not in the spec cache."})
    if {"lc", "opamp"} & set(types):
        used = _sim(sim)
        result["simulator"] = (getattr(used, "name", "ngspice") if used else None)
    result["findings"] = findings
    result["counts"] = {k: sum(1 for f in findings if f["severity"] == k)
                        for k in ("error", "warning", "info")}
    return result
