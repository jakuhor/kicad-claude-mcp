"""Part ratings against a per-rail current model.

The netlist knows voltages (power tree) but not currents. The caller gives
the load on each rail (`loads={"+5VD": 0.4}` amps, the downstream boards'
budget); this module carries it up the tree:

  - through series links (L / ferrite / 0 R) unchanged;
  - through a linear regulator unchanged (Iin = Iout, quiescent ignored);
  - through a switching regulator as power, Iin = |Vout| Iout / (eta Vin),
    with `efficiency` per regulator (caller) or a stated default;
  - from each root rail on up to its connector through the series chain
    (inductors, common-mode choke windings, low-ohm resistors, FET
    drain-source paths, series diodes).

With those currents and the datasheet spec cache it checks:

  RT-L-ISAT / RT-L-IRMS   inductor peak vs Isat, average vs Irms (buck
                          ripple from L, fsw, Vin, Vout; other topologies
                          average only);
  RT-CMC-I                common-mode choke current vs I_rated;
  RT-CONN-I               connector current per contact vs I_rated;
  RT-REG-IOUT             regulator output vs recommended IOUT / current
                          limit;
  RT-TJ                   Tj = Ta + rth_ja P vs tj_max: linear (Vin-Vout)
                          Iout, switcher Pout (1-eta)/eta, FET I^2 RDS(on)
                          per die, series diode VF I, CCR (V across) I;
  RT-FET-ID / RT-D-IF     FET and diode current vs rating;
  RT-LED                  LED current (series R and VF, or the CCR's
                          I_reg) vs IF max; CCR headroom vs V_min;
  RT-NODATA               a part that carries current but has no extracted
                          rating.

Every rating cites the spec record's page. No component value comes from
general knowledge: a part without an extraction is listed, not guessed.
Assumptions the caller can override (efficiency, ambient) are returned.
"""

from __future__ import annotations

import re
from collections import defaultdict

from kicad_claude.adapters import datasheet_specs as dss
from kicad_claude.adapters.power_review import (
    Netlist,
    _two_terminal,
    is_ground,
    is_unconnected,
    kind,
    parse_value,
)

LED_RE = re.compile(r"\bLED\b|LED_|_LED", re.I)


def _f(sev: str, rule: str, ref: str, msg: str, **kw) -> dict:
    return {"severity": sev, "rule": rule, "ref": ref, "message": msg, **kw}


def resolve(name: str, nets) -> str | None:
    """Net by exact name, else the one net whose last path part matches."""
    if name in nets:
        return name
    tail = name.lstrip("/")
    hits = [n for n in nets if n.split("/")[-1] == tail or n.lstrip("/") == tail]
    return hits[0] if len(hits) == 1 else None


def _src(rec: dict, page) -> str:
    return f"{rec.get('mpn')} p.{page}"


# ---- current model ------------------------------------------------------------ #


def rail_currents(nl: Netlist, pt: dict, loads: dict, efficiency: dict | None = None,
                  default_eta: float = 0.85) -> dict:
    """{"rails": {net: A}, "regulators": {name: {...}}, "links": {ref: A},
    "unattributed_roots": [...], "assumed_eta": [...]}."""
    efficiency = efficiency or {}
    regs = {r["name"]: r for r in pt["regulators"]}
    volts = {n: v["volts"] for n, v in pt["rail_voltages"].items()}
    load = defaultdict(float)
    unresolved = []
    for name, amps in (loads or {}).items():
        n = resolve(name, nl.nets)
        if n is None:
            unresolved.append(name)
        else:
            load[n] += float(amps)
    rails: dict[str, float] = {}
    links: dict[str, float] = {}
    rio: dict[str, dict] = {}
    assumed = []

    def eta_of(r):
        for k in (r["name"], r["ref"]):
            if k in efficiency:
                return float(efficiency[k]), "caller"
        assumed.append(r["name"])
        return default_eta, "assumed"

    def node_i(node: dict) -> float:
        n = node["rail"]
        i = load.get(n, 0.0)
        for ch in node["linked"]:
            ci = node_i(ch)
            links[ch["via"]["ref"]] = links.get(ch["via"]["ref"], 0.0) + ci
            i += ci
        for rr in node["regulators"]:
            r = regs[rr["regulator"]]
            iout = node_i(rr["output"]) if "output" in rr else (
                rails.get(r["output_net"]) or load.get(r["output_net"], 0.0))
            i += reg_in(r, iout)
        rails[n] = i
        return i

    def reg_in(r: dict, iout: float) -> float:
        vin, vout = volts.get(r["input_net"]), volts.get(r["output_net"])
        rec = {"iout": iout, "vin": vin, "vout": vout, "topology": r["topology"]}
        if r["topology"] == "linear":
            rec["iin"] = iout
            if vin is not None and vout is not None:
                rec["p_loss"] = abs(vin - vout) * iout
        else:
            eta, how = eta_of(r)
            rec.update(eta=eta, eta_source=how)
            if vin and vout is not None:
                pout = abs(vout) * iout
                rec["iin"] = pout / (eta * abs(vin))
                rec["p_loss"] = pout * (1 - eta) / eta
            else:
                rec["iin"] = None
        rio[r["name"]] = rec
        return rec["iin"] or 0.0

    roots = list(pt["tree"])
    for t in roots:
        node_i(t)
    # A root fed by a pin of a regulator IC whose VIN is on another root is
    # an internal stage of that IC (LT8471 Skyhook): carry its power up.
    for t in roots:
        n = t["rail"]
        on_root = {p.ref for p in nl.nets.get(n, [])}
        host = next((r for r in pt["regulators"] if r["ref"] in on_root
                     and r["input_net"] != n and r["input_net"] in rails), None)
        if host and rails.get(n):
            eta, how = eta_of(host)
            vin, vn = volts.get(host["input_net"]), volts.get(n)
            if vin and vn:
                extra = abs(vn) * rails[n] / (eta * abs(vin))
                rails[host["input_net"]] += extra
                rio.setdefault(f"{host['ref']}/internal:{n}", {
                    "iout": rails[n], "vin": vin, "vout": vn, "eta": eta, "eta_source": how,
                    "iin": extra, "p_loss": abs(vn) * rails[n] * (1 - eta) / eta,
                    "topology": "switching (internal stage)"})
    return {"rails": rails, "regulators": rio, "links": links, "loads": dict(load),
            "loads_unresolved": unresolved, "assumed_eta": sorted(set(assumed)),
            "default_eta": default_eta,
            "internal_roots": [k.split(":", 1)[1] for k in rio if ":" in k]}


def _fet_dies(nl: Netlist, ref: str) -> list[tuple[str, str]]:
    """(drain net, source net) per die from pin names D/S (D1/S1 ...)."""
    d, s = defaultdict(set), defaultdict(set)
    for p in nl.pins.get(ref, []):
        m = re.fullmatch(r"([DS])(\d?)", p.name.upper())
        if m:
            (d if m.group(1) == "D" else s)[m.group(2)].add(p.net)
    if set(d) == {""} and set(s) == {""} and (len(d[""]) > 1 or len(s[""]) > 1):
        # dual die with generic names: pair every drain net with every source net
        return [(a, b) for a in d[""] for b in s[""]]
    return [(a, b) for ch in d for a in d[ch] for b in s.get(ch, ())]


POWER_PIN_RE = re.compile(r"VBUS|VIN|VCC|VDD|PWR|POWER|^V\+|^\+|^\d+V", re.I)


def _power_entry(nl: Netlist, net: str) -> dict[str, list[str]]:
    """Connectors that bring power in on `net`: two or more contacts on it,
    or a contact named like a supply. A one-contact SMA / test header is a
    tap, not a source."""
    by = defaultdict(list)
    for p in nl.nets.get(net, []):
        if kind(p.ref) in ("J", "P", "CN"):
            by[p.ref].append(p)
    return {r: [p.number for p in ps] for r, ps in by.items()
            if len(ps) >= 2 or any(POWER_PIN_RE.search(p.name or "") for p in ps)}


def _hops(nl: Netlist, ref: str, specs: dict) -> tuple[str, list[tuple[str, str]]]:
    comp = nl.components.get(ref, {})
    k = kind(ref)
    cat = (specs.get(ref) or {}).get("category", "")
    if k in ("L", "FB", "FL"):
        ends = _two_terminal(nl, ref)
        if ends:
            return "inductor", [ends]
        ng = [q.net for q in nl.pins.get(ref, []) if not is_ground(q.net)]
        # CMC: the supply winding is the pair of non-ground pins
        return "cmc", [(ng[0], ng[1])] if len(ng) == 2 else []
    if k == "R":
        v = parse_value(comp.get("value", ""))
        ends = _two_terminal(nl, ref)
        return "resistor", [ends] if ends and v is not None and v <= 1.0 else []
    if k == "Q":
        return "fet", _fet_dies(nl, ref)
    if k == "D" and cat not in ("tvs", "zener") and not LED_RE.search(
            f"{comp.get('lib_id', '')} {comp.get('value', '')}"):
        ends = _two_terminal(nl, ref)
        return "diode", [ends] if ends else []
    return "", []


def series_chain(nl: Netlist, start: str, specs: dict) -> dict:
    """Shortest series path from `start` to a power-entry connector through
    inductors, CMC windings, <= 1 Ohm resistors, FET drain-source (a dual
    FET contributes one hop per die) and series diodes: {"parts": [{ref,
    kind, from, to, dnp}], "connectors": {ref: [pins]}}; empty when no
    connector is reachable that way."""
    here = _power_entry(nl, start)
    if here:
        return {"parts": [], "connectors": here}
    prev: dict[str, tuple[str, str, str] | None] = {start: None}
    frontier = [start]
    found = None
    while frontier and not found:
        nxt = []
        for n in frontier:
            for p in nl.nets.get(n, []):
                pk, hops = _hops(nl, p.ref, specs)
                for a, c in hops:
                    if n not in (a, c) or is_ground(a) or is_ground(c):
                        continue
                    o = c if a == n else a
                    if o in prev or is_unconnected(o):
                        continue
                    prev[o] = (n, p.ref, pk)
                    if _power_entry(nl, o):
                        found = o
                        break
                    nxt.append(o)
                if found:
                    break
            if found:
                break
        frontier = nxt
    if not found:
        return {"parts": [], "connectors": {}}
    parts, n = [], found
    while prev[n]:
        m, ref, pk = prev[n]
        parts.append({"ref": ref, "kind": pk, "from": m, "to": n,
                      "dnp": nl.components.get(ref, {}).get("dnp", False)})
        n = m
    return {"parts": parts[::-1], "connectors": _power_entry(nl, found)}


# ---- checks ------------------------------------------------------------------- #


def _regulator_inductors(nl: Netlist, pt: dict, rail_nets: set) -> dict[str, dict]:
    """Power-stage inductor -> {regulator, carries: iout|iin|max, buck}."""
    out = {}
    by_ref = defaultdict(list)
    for r in pt["regulators"]:
        by_ref[r["ref"]].append(r)
    for lref, comp in nl.components.items():
        if kind(lref) != "L" or comp.get("dnp"):
            continue
        ends = _two_terminal(nl, lref)
        if not ends:
            continue
        for ic, regs in by_ref.items():
            if not any(r["topology"] == "switching" for r in regs):
                continue
            ic_nets = {p.net for p in nl.pins.get(ic, [])}

            def near(net, ic_nets=ic_nets):   # on an IC pin, or one part away from one
                if net in ic_nets:
                    return True
                return any(set(q.net for q in nl.pins.get(p.ref, [])) & (ic_nets - {net})
                           for p in nl.nets.get(net, [])
                           if p.ref != lref and kind(p.ref) in ("C", "D"))
            for r in regs:
                if r["topology"] != "switching":
                    continue
                a, b = ends
                if r["output_net"] in ends:
                    other = b if a == r["output_net"] else a
                    if other not in rail_nets and near(other):
                        out[lref] = {"regulator": r["name"], "carries": "iout",
                                     "buck": r["switch_net"] == other}
                elif r["input_net"] in ends:
                    other = b if a == r["input_net"] else a
                    ch_pins = {p.net for p in nl.pins.get(ic, [])
                               if re.search(rf"{r['channel'] or ''}$", p.name)}
                    if other in (ch_pins if r["channel"] else ic_nets):
                        out.setdefault(lref, {"regulator": r["name"], "carries": "iin",
                                              "buck": False})
                elif any(is_ground(x) for x in ends):
                    other = b if is_ground(a) else a
                    ch_pins = {p.net for p in nl.pins.get(ic, [])
                               if r["channel"] and p.name.endswith(r["channel"])}
                    if other in ch_pins:
                        out.setdefault(lref, {"regulator": r["name"], "carries": "max",
                                              "buck": False})
    return out


def check(nl: Netlist, pt: dict, specs: dict, loads: dict, efficiency: dict | None = None,
          default_eta: float = 0.85, ambient_c: float | None = None,
          fsw: dict | None = None, tj_margin_c: float = 25.0) -> dict:
    findings: list[dict] = []
    ta = 40.0 if ambient_c is None else float(ambient_c)
    model = rail_currents(nl, pt, loads, efficiency, default_eta)
    rails, rio = model["rails"], model["regulators"]
    volts = {n: v["volts"] for n, v in pt["rail_voltages"].items()}
    regs = {r["name"]: r for r in pt["regulators"]}
    nodata: dict[str, str] = {}
    rows = []

    def therm(ref: str, p_w: float, what: str):
        rec = specs.get(ref)
        rth = dss.rated(rec, "thermal", "rth_ja", prefer="typ")
        tjm = dss.rated(rec, "thermal", "tj_max") or dss.rated(rec, "absolute_max", "TJ")
        row = {"ref": ref, "what": what, "p_w": round(p_w, 4)}
        if rth and tjm:
            tj = ta + rth[0] * p_w
            row.update(tj_c=round(tj, 1), rth_ja=rth[0], tj_max=tjm[0])
            src = f"rth_ja {rth[0]:g} C/W {_src(rec, rth[1])}, Tj max {tjm[0]:g} C"
            if tj > tjm[0]:
                findings.append(_f("error", "RT-TJ", ref, f"{what}: {p_w:.3g} W -> Tj {tj:.0f} C "
                                   f"at Ta {ta:g} C, above Tj max ({src})."))
            elif tj > tjm[0] - tj_margin_c:
                findings.append(_f("warning", "RT-TJ", ref, f"{what}: {p_w:.3g} W -> Tj {tj:.0f} C "
                                   f"at Ta {ta:g} C, within {tj_margin_c:g} C of Tj max ({src}). "
                                   "rth_ja is the datasheet's JEDEC board, not this layout."))
        elif p_w > 0.05:
            nodata[ref] = f"{what} {p_w:.3g} W, no rth_ja / Tj max extracted"
        rows.append(row)

    # regulators: output current and dissipation
    for name, io in rio.items():
        if io["iout"] and io.get("iin") is None:
            r = regs.get(name)
            findings.append(_f("info", "RT-MODEL", name,
                               f"{name}: input "
                               f"{(r or {}).get('input_net')} or output voltage unknown, so its "
                               "input current is not carried upstream; pass rail_voltages for it."))
    per_ic = defaultdict(list)
    for name, io in rio.items():
        if io.get("p_loss") is not None and io["iout"]:
            per_ic[name.split("/")[0]].append((name, io))
    for ref, items in per_ic.items():    # channels of one IC share its die
        therm(ref, sum(io["p_loss"] for _, io in items),
              " + ".join(f"{n} ({io['topology']}, Iout {io['iout']:.3g} A)" for n, io in items))
    for name, io in rio.items():
        ref = name.split("/")[0]
        rec = specs.get(ref)
        if not rec or not io["iout"]:
            continue
        imax = dss.rated(rec, "recommended", "IOUT")
        lim = dss.rated(rec, "electrical", "ICL", "ILIM", prefer="min")
        if lim and io["iout"] > lim[0]:
            findings.append(_f("error", "RT-REG-IOUT", name,
                               f"Iout {io['iout']:.3g} A above the current limit {lim[2]} "
                               f"{lim[0]:g} A (min, {_src(rec, lim[1])})."))
        elif imax and io["iout"] > imax[0]:
            findings.append(_f("warning", "RT-REG-IOUT", name,
                               f"Iout {io['iout']:.3g} A above the recommended IOUT "
                               f"{imax[0]:g} A ({_src(rec, imax[1])})."))

    # power-stage inductors
    rail_nets = set(volts) | set(rails)
    stage = _regulator_inductors(nl, pt, rail_nets)
    fsw = fsw or {}
    ind = {}
    for lref, st in stage.items():
        io = rio.get(st["regulator"])
        if not io or (io.get("iin") is None and st["carries"] != "iout"):
            continue
        iavg = {"iout": io["iout"], "iin": io["iin"],
                "max": max(io["iout"], io["iin"] or 0.0)}[st["carries"]]
        r = regs[st["regulator"]]
        ipk, ripple = iavg, None
        f = fsw.get(r["name"]) or fsw.get(r["ref"])
        if f is None:
            fs = ((specs.get(r["ref"]) or {}).get("regulator") or {}).get("fsw")
            f = dss.value(fs) if fs else None
        lval = parse_value(nl.components[lref]["value"])
        if st["buck"] and f and lval and io["vin"] and io["vout"] is not None and io["vin"] > io["vout"]:
            ripple = (io["vin"] - io["vout"]) * io["vout"] / (io["vin"] * lval * f)
            ipk = iavg + ripple / 2
        ind[lref] = {"regulator": r["name"], "carries": st["carries"], "i_avg": iavg,
                     "i_peak": ipk, "ripple_pp": ripple, "fsw": f}
        sw_lim = dss.rated(specs.get(r["ref"]), "electrical", "IHS_LIMIT", "ISW_LIMIT",
                           prefer="min")
        if st["buck"] and sw_lim and ipk > sw_lim[0]:
            findings.append(_f("error", "RT-REG-ILIM", r["name"],
                               f"{lref} peak {ipk:.3g} A above the switch current limit "
                               f"{sw_lim[2]} {sw_lim[0]:g} A (min, {_src(specs[r['ref']], sw_lim[1])})."))
    for lref, comp in nl.components.items():
        ends = _two_terminal(nl, lref) or ()
        if (kind(lref) == "L" and not comp.get("dnp") and lref not in ind
                and lref not in model["links"] and any(
                    n in {p.net for r in pt["regulators"] if r["topology"] == "switching"
                          for p in nl.pins.get(r["ref"], [])}
                    for n in ends if n not in rail_nets and not is_ground(n))):
            ind[lref] = {"regulator": None, "carries": "not modelled", "i_avg": 0.0,
                         "i_peak": 0.0, "ripple_pp": None,
                         "note": "power-stage inductor not attributed to a regulator channel "
                                 "(e.g. an internal boost stage): current not modelled"}
    for ref, amps in model["links"].items():
        if kind(ref) in ("L", "FB") and ref not in ind:
            ind[ref] = {"regulator": None, "carries": "series", "i_avg": amps, "i_peak": amps,
                        "ripple_pp": None}

    # root rails up to the connector
    chains = {}
    for t in pt["tree"]:
        n = t["rail"]
        i = rails.get(n, 0.0)
        if not i:
            continue
        ch = series_chain(nl, n, specs)
        chains[n] = {"current": i, **ch}
        for part in ch["parts"]:
            if part["kind"] in ("inductor", "cmc") and part["ref"] not in ind:
                ind[part["ref"]] = {"regulator": None, "carries": "series", "i_avg": i,
                                    "i_peak": i, "ripple_pp": None, "cmc": part["kind"] == "cmc"}
        dies = defaultdict(int)
        for part in ch["parts"]:
            if part["kind"] == "fet":
                dies[part["ref"]] += 1
        for ref, ndie in dies.items():
            rec = specs.get(ref)
            rds = None
            for k, sv in ((rec or {}).get("electrical") or {}).items():
                if k.upper().replace("(", "").replace(")", "").replace("_", "").startswith("RDSON"):
                    v = dss.value(sv, "max")
                    if v is not None and (rds is None or v > rds[0]):
                        rds = (v, sv.get("page"), k)
            idr = dss.rated(rec, "electrical", "ID_25C", "ID", prefer="max") or \
                dss.rated(rec, "absolute_max", "ID", prefer="max")
            if rds:
                therm(ref, ndie * i * i * rds[0],
                      f"{ndie} FET die(s) in the {n} path at {i:.3g} A, RDS(on) {rds[0]:g} Ohm "
                      f"max ({rds[2]}, {_src(rec, rds[1])})")
            else:
                nodata[ref] = f"FET in the {n} path at {i:.3g} A, no RDS(on) extracted"
            if idr and i > idr[0]:
                findings.append(_f("error", "RT-FET-ID", ref, f"{i:.3g} A above ID {idr[0]:g} A "
                                   f"({_src(rec, idr[1])})."))
        for part in ch["parts"]:
            if part["kind"] != "diode":
                continue
            rec = specs.get(part["ref"])
            ifr = dss.rated(rec, "electrical", "IF_AV", "IF") or dss.rated(rec, "absolute_max",
                                                                           "IF", "IF_AV")
            vf = dss.rated(rec, "electrical", "VF", prefer="max")
            if ifr and i > ifr[0]:
                findings.append(_f("error", "RT-D-IF", part["ref"], f"{i:.3g} A above IF "
                                   f"{ifr[0]:g} A ({_src(rec, ifr[1])})."))
            if vf:
                therm(part["ref"], vf[0] * i, f"series diode at {i:.3g} A, VF {vf[0]:g} V")
        for cref, pins in ch["connectors"].items():
            rec = specs.get(cref)
            ir = dss.rated(rec, "electrical", "I_rated", "I_contact", prefer="max")
            per = i / max(1, len(pins))
            row = {"ref": cref, "rail": n, "current": round(i, 4), "contacts": sorted(pins),
                   "per_contact": round(per, 4)}
            chains[n].setdefault("connector_load", []).append(row)
            tot = dss.rated(rec, "electrical", "I_rated_total", prefer="max")
            if tot:
                row["i_rated_total"] = tot[0]
                if i > tot[0]:
                    findings.append(_f("error", "RT-CONN-I", cref,
                                       f"{i:.3g} A over pins {', '.join(sorted(pins))} above the "
                                       f"collective rating {tot[0]:g} A ({_src(rec, tot[1])})."))
            if ir:
                row["i_rated"] = ir[0]
                if per > ir[0]:
                    findings.append(_f("error", "RT-CONN-I", cref,
                                       f"{per:.3g} A per contact ({i:.3g} A over {len(pins)} "
                                       f"pins {', '.join(sorted(pins))}) above I_rated {ir[0]:g} A "
                                       f"({_src(rec, ir[1])})."))
            if not (ir or tot):
                nodata[cref] = f"{i:.3g} A over {len(pins)} contacts, no I_rated extracted"

    for lref, x in sorted(ind.items()):
        rec = specs.get(lref)
        comp = nl.components[lref]
        if comp.get("dnp"):
            x["dnp"] = True
            continue
        cmc = x.get("cmc") or (rec or {}).get("category") == "common_mode_choke"
        if cmc:
            ir = dss.rated(rec, "electrical", "I_rated", "Irms", prefer="max")
            if ir:
                x["i_rated"] = ir[0]
                if x["i_avg"] > ir[0]:
                    findings.append(_f("error", "RT-CMC-I", lref,
                                       f"Common-mode choke at {x['i_avg']:.3g} A above I_rated "
                                       f"{ir[0]:g} A ({_src(rec, ir[1])})."))
            elif x["i_avg"]:
                nodata[lref] = f"choke at {x['i_avg']:.3g} A, no I_rated extracted"
            continue
        isat = dss.rated(rec, "electrical", "Isat", prefer="typ")
        irms = dss.rated(rec, "electrical", "Irms")
        if isat:
            x["isat"] = isat[0]
            if x["i_peak"] > isat[0]:
                findings.append(_f("error", "RT-L-ISAT", lref,
                                   f"Peak {x['i_peak']:.3g} A above Isat {isat[0]:g} A "
                                   f"({_src(rec, isat[1])})."))
            elif x["i_peak"] > 0.8 * isat[0]:
                findings.append(_f("warning", "RT-L-ISAT", lref,
                                   f"Peak {x['i_peak']:.3g} A is {x['i_peak'] / isat[0]:.0%} of Isat "
                                   f"{isat[0]:g} A ({_src(rec, isat[1])})."))
        if irms:
            x["irms"] = irms[0]
            if x["i_avg"] > irms[0]:
                findings.append(_f("warning", "RT-L-IRMS", lref,
                                   f"Average {x['i_avg']:.3g} A above {irms[2]} {irms[0]:g} A "
                                   f"({_src(rec, irms[1])})."))
        dcr = dss.rated(rec, "electrical", "DCR", prefer="max")
        if dcr:
            x["p_dcr_w"] = round(x["i_avg"] ** 2 * dcr[0], 4)
        if x["carries"] == "not modelled":
            continue
        if not (isat or irms) and x["i_avg"]:
            nodata[lref] = f"inductor at {x['i_avg']:.3g} A avg, no Isat / Irms extracted"
        if x["ripple_pp"] is None and x["regulator"]:
            x["note"] = "average only (ripple computed for bucks only)"

    leds = check_leds(nl, specs, volts, findings, therm)
    for ref, why in sorted(nodata.items()):
        findings.append(_f("info", "RT-NODATA", ref, f"{why}: extract its datasheet "
                           "(get_datasheet_extraction_task) to check it."))
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["rule"], f["ref"]))

    def rnd(d):
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}
    return {"assumptions": {"ambient_c": ta, "ambient_source": "caller" if ambient_c is not None
                            else "default 40 C (pass ambient_c)",
                            "efficiency_default": default_eta,
                            "efficiency_assumed_for": model["assumed_eta"],
                            "loads": model["loads"], "loads_unresolved": model["loads_unresolved"]},
            "rail_currents": {n: round(i, 4) for n, i in sorted(rails.items()) if i},
            "regulators": {k: rnd(v) for k, v in rio.items()},
            "inductors": {k: rnd(v) for k, v in sorted(ind.items())},
            "input_paths": chains, "thermal": rows, "leds": leds,
            "findings": findings,
            "counts": {k: sum(1 for f in findings if f["severity"] == k) for k in order}}


def _net_v(n: str, volts: dict) -> float | None:
    return 0.0 if is_ground(n) else volts.get(n)


def check_leds(nl: Netlist, specs: dict, volts: dict, findings: list, therm) -> list[dict]:
    """LED strings: walk both ways through two-terminal R / D / CCR parts and
    FET drain-source (assumed on) to nets of known voltage."""
    out = []
    for ref, comp in sorted(nl.components.items()):
        rec = specs.get(ref) or {}
        if kind(ref) != "D" or comp.get("dnp"):
            continue
        if rec.get("category") != "led" and not LED_RE.search(
                f"{comp['lib_id']} {comp['value']} {comp['description']}"):
            continue
        pins = {p.name.upper(): p.net for p in nl.pins.get(ref, [])}
        a, k = pins.get("A"), pins.get("K")
        if not a or not k:
            continue
        chain = [ref]
        r_total, ccr, others, switch = 0.0, None, [], []

        def walk(net, prev):
            nonlocal r_total, ccr
            seen = {prev}
            while _net_v(net, volts) is None:
                nxt = None
                for p in nl.nets.get(net, []):
                    if p.ref in seen or p.ref in chain:
                        continue
                    pk = kind(p.ref)
                    c = nl.components.get(p.ref, {})
                    if c.get("dnp"):
                        continue
                    ends = _two_terminal(nl, p.ref)
                    if pk == "Q":
                        dies = [d for d in _fet_dies(nl, p.ref) if net in d]
                        if dies:
                            ends = dies[0]
                            switch.append(p.ref)
                    elif pk == "R":
                        v = parse_value(c.get("value", ""))
                        if v is None:
                            return None
                        r_total += v
                    elif pk == "D":
                        if (specs.get(p.ref) or {}).get("category") == "current_regulator":
                            ccr = p.ref
                        else:
                            others.append(p.ref)
                    else:
                        continue
                    if not ends or net not in ends:
                        continue
                    nxt = ends[1] if ends[0] == net else ends[0]
                    chain.append(p.ref)
                    seen.add(p.ref)
                    break
                if nxt is None:
                    return None
                net = nxt
            return net
        top, bot = walk(a, ref), walk(k, ref)
        row = {"ref": ref, "chain": chain, "series_r": r_total or None, "ccr": ccr,
               "switch_assumed_on": switch}
        out.append(row)
        if top is None or bot is None:
            row["status"] = "string does not end on nets of known voltage (GPIO-driven?)"
            continue
        vs = _net_v(top, volts) - _net_v(bot, volts)
        row.update(top=top, bottom=bot, volts=round(vs, 3))
        vf = dss.rated(rec, "electrical", "VF", prefer="typ")
        ifm = dss.rated(rec, "absolute_max", "IF", prefer="max")
        if not vf:
            row["status"] = "no VF extracted"
            findings.append(_f("info", "RT-NODATA", ref, "LED without an extracted VF: current "
                               "not computed (extract its datasheet)."))
            continue
        vrest = vs - vf[0]
        if ccr:
            crec = specs[ccr]
            ireg = dss.rated(crec, "electrical", "I_reg", "Ireg", prefer="typ")
            vmin = dss.rated(crec, "electrical", "V_min", "Vak_min", prefer="max")
            if not ireg:
                row["status"] = f"CCR {ccr} without an extracted I_reg"
                continue
            i = ireg[0]
            v_ccr = vrest - i * r_total
            row.update(current=i, current_source=f"{ccr} I_reg {_src(crec, ireg[1])}",
                       v_ccr=round(v_ccr, 3))
            if vmin and v_ccr < vmin[0]:
                findings.append(_f("warning", "RT-LED", ccr,
                                   f"CCR has {v_ccr:.3g} V across it, below the {vmin[0]:g} V it "
                                   f"needs to regulate ({_src(crec, vmin[1])}): {ref} runs below "
                                   f"{i * 1e3:.3g} mA."))
            therm(ccr, max(0.0, v_ccr) * i, f"CCR for {ref} at {i * 1e3:.3g} mA, {v_ccr:.3g} V across")
        elif r_total:
            i = vrest / r_total
            row.update(current=round(i, 5), current_source=f"({vs:.3g} V - VF {vf[0]:g} V) / "
                       f"{r_total:g} Ohm, VF {_src(rec, vf[1])}")
        else:
            findings.append(_f("warning", "RT-LED", ref, f"LED across {vs:.3g} V with no series "
                               "resistor or current regulator."))
            continue
        if others:
            row["note"] = f"other series diodes {others} (their VF not subtracted)"
        if ifm and i > ifm[0]:
            findings.append(_f("error", "RT-LED", ref, f"{i * 1e3:.3g} mA above IF max "
                               f"{ifm[0] * 1e3:g} mA ({_src(rec, ifm[1])})."))
        elif i <= 0:
            findings.append(_f("warning", "RT-LED", ref, f"Supply {vs:.3g} V below VF "
                               f"{vf[0]:g} V: the LED does not light."))
    return out
