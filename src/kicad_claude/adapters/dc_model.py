"""DC operating point of the nets the rail model does not cover.

`power_review` knows rail voltages (regulator outputs, series links,
caller values). This module adds, per scenario:

  - a nodal solve over every fitted resistor: dividers with several
    branches, gate networks, pull-ups feeding pull-downs (0 R, inductors and
    ferrites are shorts);
  - pin models from the datasheet spec cache (`pin_models` in the spec
    record, plus `regulator.en_pullup_current` on EN pins): bias current,
    input resistance, internal clamp to ground and the clamp current at
    which the part changes state (`i_trip`, e.g. LT8471 OVLO at 80 uA);
  - zener clamps (spec `VZ` between K and A, else the part's value "12V"),
    solved iteratively: a zener that would see more than VZ fixes its
    anode / cathode difference;
  - switches (`switches={"Q2": "on"}`): an "on" transistor joins drain and
    source (collector and emitter); an "off" one passes only its body diode
    (P: drain -> source, N: source -> drain; the diode drop is ignored, so
    the result is an upper bound) and leaves the far side at 0 V; the gate
    driver (an IC output, directly or through a series resistor) pulls low
    for a P-FET that is on, high (its supply) for an N-FET that is on, and
    releases (open drain) or drives the opposite level when off.

Everything is a static DC estimate: capacitors are open, pin currents not
in the spec cache are zero, and nothing switches dynamically.
"""

from __future__ import annotations

import re
from collections import defaultdict

from kicad_claude.adapters import datasheet_specs as dss
from kicad_claude.adapters.power_review import (
    Netlist,
    _base,
    _role,
    is_ground,
    is_unconnected,
    kind,
    parse_value,
)

ZENER_TEXT = re.compile(r"zener|BZX|BZT|MMSZ|MM3Z|BZV|1N47|1N52|D_Z\b|_Z$", re.I)
SWITCH_KINDS = ("Q", "M", "T")


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _fitted(nl, ref):
    return not nl.components.get(ref, {}).get("dnp")


def _pn(name: str) -> str:
    return re.sub(r"[~{}\s]", "", name or "").upper()


# ---- switches --------------------------------------------------------------- #


def polarity(nl: Netlist, ref: str, specs: dict) -> str | None:
    """'P' / 'N' from the symbol (PMOS / P-channel / PNP / Q_P...) or the
    spec record's VDS sign, else None."""
    c = nl.components.get(ref, {})
    text = f"{c.get('lib_id', '')} {c.get('value', '')} {c.get('description', '')}"
    if re.search(r"PMOS|P-?CH|Q_P|PNP|_P_|P-MOS", text, re.I):
        return "P"
    if re.search(r"NMOS|N-?CH|Q_N|NPN|_N_|N-MOS", text, re.I):
        return "N"
    vds = ((specs.get(ref) or {}).get("absolute_max") or {}).get("VDS") or {}
    if vds.get("min") is not None and vds["min"] < 0 and (vds.get("max") or 0) <= 0:
        return "P"
    if vds.get("max") is not None and vds["max"] > 0 and vds.get("min") is None:
        return "N"
    return None


def switch_dies(nl: Netlist, ref: str) -> list[dict]:
    """[{"main": (drain/collector, source/emitter), "gate": net}] per die,
    from pin names D/S/G or C/E/B with optional channel digit. Dual parts
    with generic names pair every drain net with every source net."""
    by = defaultdict(lambda: defaultdict(set))
    for p in nl.pins.get(ref, []):
        m = re.fullmatch(r"([DSGCEB])(\d?)", _pn(p.name))
        if m and not is_unconnected(p.net):
            role = {"C": "D", "E": "S", "B": "G"}.get(m.group(1), m.group(1))
            by[m.group(2)][role].add(p.net)
    out = []
    if set(by) == {""} and (len(by[""]["D"]) > 1 or len(by[""]["S"]) > 1):
        g = next(iter(by[""]["G"]), None)
        return [{"main": (d, s), "gate": g} for d in by[""]["D"] for s in by[""]["S"]]
    for ch, roles in by.items():
        g = next(iter(roles["G"]), None)
        out += [{"main": (d, s), "gate": g} for d in roles["D"] for s in roles["S"]]
    return out


def switches_in(nl: Netlist) -> list[str]:
    return sorted(r for r in nl.components if kind(r) in SWITCH_KINDS and _fitted(nl, r)
                  and switch_dies(nl, r))


# ---- pin models -------------------------------------------------------------- #


def pin_models(nl: Netlist, specs: dict) -> list[dict]:
    """[{ref, pin, net, i_bias, r_in, clamp, i_trip, i_sink, i_source, v_oh,
    v_out, src}] for pins with a model in the spec cache. Drive limits and
    output levels take the datasheet minimum (worst case for driving)."""
    out = []
    for ref, rec in specs.items():
        if ref not in nl.pins or not _fitted(nl, ref):
            continue
        ds_name = {str(p.get("number")): p.get("name", "") for p in rec.get("pins") or []}
        models = {_pn(k): v for k, v in (rec.get("pin_models") or {}).items()}
        reg = rec.get("regulator") or {}
        en_pu = dss.value(reg.get("en_pullup_current"))
        for p in nl.pins[ref]:
            if is_unconnected(p.net) or is_ground(p.net):
                continue
            names = {_pn(p.name), _pn(ds_name.get(p.number, ""))} - {""}
            m = next((models[n] for n in names if n in models), None)
            row = {}
            if m:
                for k in ("i_bias", "r_in", "clamp", "i_trip", "i_sink", "i_source", "v_oh",
                          "v_out"):
                    sv = m.get(k)
                    if sv:
                        v = dss.value(sv, {"i_bias": "max", "i_sink": "min", "i_source": "min",
                                           "v_oh": "min"}.get(k, "typ"))
                        if v is not None and k in ("i_sink", "i_source"):
                            v = abs(v)
                        if v is not None:
                            row[k] = v
                            row.setdefault("pages", {})[k] = sv.get("page")
                row["src"] = f"{rec.get('mpn')} pin_models"
            elif en_pu is not None and any(_role(_base(n)[0]) == "en" for n in names):
                # current OUT of the pin = negative bias into the pin
                row = {"i_bias": -en_pu, "src": f"{rec.get('mpn')} en_pullup_current p."
                       f"{reg['en_pullup_current'].get('page')}"}
            if row:
                out.append({"ref": ref, "pin": p.name or p.number, "net": p.net, **row})
    return out


def zeners(nl: Netlist, specs: dict) -> list[dict]:
    out = []
    for ref, c in nl.components.items():
        if kind(ref) != "D" or not _fitted(nl, ref):
            continue
        rec = specs.get(ref) or {}
        text = f"{c.get('lib_id', '')} {c.get('value', '')} {c.get('description', '')}"
        if rec.get("category") != "zener" and not ZENER_TEXT.search(text):
            continue
        pins = {_pn(p.name): p.net for p in nl.pins.get(ref, [])}
        a, k = pins.get("A"), pins.get("K")
        if not a or not k or is_unconnected(a) or is_unconnected(k):
            continue
        vz_sv = (rec.get("electrical") or {}).get("VZ") or (rec.get("electrical") or {}).get("VZ_typ")
        if vz_sv:
            vz, src = dss.value(vz_sv, "max"), f"{rec.get('mpn')} VZ max p.{vz_sv.get('page')}"
        else:
            m = re.match(r"(\d+(?:\.\d+)?)V(\d*)", c.get("value", ""), re.I)
            if not m:
                continue
            vz = float(f"{m.group(1)}.{m.group(2)}") if m.group(2) else float(m.group(1))
            src = "value field"
        ptot = dss.rated(rec, "absolute_max", "Ptot", "P_tot", "PD")
        out.append({"ref": ref, "anode": a, "cathode": k, "vz": vz, "src": src,
                    "ptot": ptot[0] if ptot else None})
    return out


# ---- solver ------------------------------------------------------------------- #


def _gauss(a: list[list[float]], b: list[float]) -> list[float] | None:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-18:
            return None
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c and m[r][c]:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def solve(nl: Netlist, known: dict, specs: dict | None = None,
          switches: dict[str, str] | None = None, max_clamps: int = 30) -> dict:
    """`known`: {net: (volts, source)}; `switches`: {ref: "on" | "off"} (refs
    not listed are left out of the model). Returns {"volts": {net: (v,
    src)}, "clamps": [...], "drivers": [...], "notes": [...]}."""
    specs = specs or {}
    switches = switches or {}
    notes: list[str] = []
    uf = _UF()
    for n in nl.nets:
        uf.find(n)
    gnd = "__GND__"
    for n in nl.nets:
        if is_ground(n):
            uf.union(gnd, n)
    # shorts
    for ref, c in nl.components.items():
        if not _fitted(nl, ref):
            continue
        k = kind(ref)
        pins = nl.pins.get(ref, [])
        if len(pins) == 2 and pins[0].net != pins[1].net and not any(
                is_unconnected(p.net) for p in pins):
            v = parse_value(c.get("value", ""))
            if (k == "R" and v == 0.0) or k in ("L", "FB", "FL") or (
                    k in ("JP", "SJ", "NT") and len(pins) == 2):
                uf.union(pins[0].net, pins[1].net)
    dies = {r: switch_dies(nl, r) for r in switches if r in nl.components}
    for r, st in switches.items():
        if st == "on":
            for d in dies.get(r, []):
                uf.union(*d["main"])

    val: dict[str, tuple[float, str]] = {}

    def setg(net, v, src):
        g = uf.find(net)
        if g not in val:
            val[g] = (float(v), src)
            return True
        return False
    val[uf.find(gnd)] = (0.0, "ground")
    for n, (v, src) in known.items():
        if n in nl.nets or n == gnd:
            g = uf.find(n)
            if g in val and abs(val[g][0] - v) > 1e-6 and val[g][1] != "ground":
                notes.append(f"{n}: {v} V conflicts with {val[g][0]} V on the same node "
                             f"({val[g][1]}); first value kept")
            setg(n, v, src)

    pms = pin_models(nl, specs)
    for pm in pms:      # pins the part drives at a fixed voltage (internal LDO output)
        if "v_out" in pm:
            setg(pm["net"], pm["v_out"], f"{pm['ref']}.{pm['pin']} v_out ({pm['src']})")
    pm_by_pin = {(pm["ref"], pm["pin"]): pm for pm in pms}

    # off switches: the body diode carries the anode side's voltage minus its
    # VSD from the spec cache (else the drop is ignored: an upper bound)
    pol = {r: polarity(nl, r, specs) for r in switches}
    changed = True
    while changed:
        changed = False
        for r, st in switches.items():
            if st != "off" or pol.get(r) is None:
                continue
            for d in dies.get(r, []):
                dn, sn = d["main"]
                an, ca = (dn, sn) if pol[r] == "P" else (sn, dn)
                ga, gc = uf.find(an), uf.find(ca)
                if ga in val and gc not in val:
                    vsd = dss.rated(specs.get(r), "electrical", "VSD", "VF_body", "VSD_body",
                                    prefer="typ")
                    if vsd:
                        val[gc] = (val[ga][0] - vsd[0], f"body diode of {r} (off) from {an}, "
                                   f"VSD {vsd[0]:g} V ({specs[r].get('mpn')} p.{vsd[1]})")
                    else:
                        val[gc] = (val[ga][0], f"body diode of {r} (off; drop ignored) from {an}")
                    changed = True
    for r, st in switches.items():
        if st != "off":
            continue
        for d in dies.get(r, []):
            for n in d["main"]:
                setg(n, 0.0, f"{r} off: unsourced side taken as 0 V")

    # gate drivers
    drivers = []
    for r, st in switches.items():
        p = pol.get(r)
        for d in dies.get(r, []):
            g = d["gate"]
            if not g:
                continue
            cand = []
            for q in nl.nets.get(g, []):
                if q.ref != r and kind(q.ref) in ("U", "IC") and q.type.split("+")[0] in (
                        "output", "open_collector", "tri_state", "bidirectional"):
                    cand.append((q, g))
            for q in nl.nets.get(g, []):
                if kind(q.ref) == "R" and _fitted(nl, q.ref):
                    other = [x.net for x in nl.pins.get(q.ref, []) if x.net != g]
                    for o in other:
                        for x in nl.nets.get(o, []):
                            if kind(x.ref) in ("U", "IC") and x.type.split("+")[0] in (
                                    "output", "open_collector", "tri_state", "bidirectional"):
                                cand.append((x, o))
            for q, node in cand:
                if uf.find(node) in val or any(x["node"] == node for x in drivers):
                    continue
                typ = q.type.split("+")[0]
                # the high level is the IC's supply only when it is unambiguous:
                # every non-ground supply pin on a known net, all at one voltage
                pm = pm_by_pin.get((q.ref, q.name or q.number)) or {}
                sup = [x for x in nl.pins.get(q.ref, [])
                       if x.type.split("+")[0] == "power_in" and not is_ground(x.net)]
                lv = {round(known[x.net][0], 3) for x in sup if x.net in known}
                ldo = [x for x in nl.pins.get(q.ref, []) if x.type.split("+")[0] == "power_out"
                       and not is_ground(x.net)]               # I/O from an internal LDO
                ldo_v = {round(val[uf.find(x.net)][0], 3) for x in ldo if uf.find(x.net) in val}
                if "v_oh" in pm:
                    vhi, hi_src = pm["v_oh"], f"v_oh ({pm['src']})"
                elif ldo:
                    vhi = ldo_v.pop() if len(ldo_v) == 1 and len(
                        [x for x in ldo if uf.find(x.net) in val]) == len(ldo) else None
                    hi_src = "its internal regulator output"
                else:
                    vhi = lv.pop() if sup and len(lv) == 1 and all(
                        x.net in known for x in sup) else None
                    hi_src = "its supply"
                limit = pm.get("i_sink") if (p == "P") == (st == "on") else pm.get("i_source")
                low = (p == "P") == (st == "on")
                if low:
                    setg(node, 0.0, f"{q.ref}.{q.name} drives low ({r} {st})")
                    drivers.append({"ref": q.ref, "pin": q.name, "node": node, "switch": r,
                                    "state": st, "level": "low", "limit_a": limit,
                                    "limit_src": pm.get("src")})
                elif typ != "open_collector" and vhi is not None:
                    setg(node, vhi, f"{q.ref}.{q.name} drives high at {vhi:g} V from {hi_src} "
                         f"({r} {st})")
                    drivers.append({"ref": q.ref, "pin": q.name, "node": node, "switch": r,
                                    "state": st, "level": "high", "v_high": vhi,
                                    "limit_a": limit, "limit_src": pm.get("src")})
                else:
                    drivers.append({"ref": q.ref, "pin": q.name, "node": node, "switch": r,
                                    "state": st, "level": "released"})

    # resistor network between groups
    edges = []
    for ref, c in nl.components.items():
        if kind(ref) not in ("R", "RN") or not _fitted(nl, ref):
            continue
        pins = nl.pins.get(ref, [])
        v = parse_value(c.get("value", ""))
        if len(pins) != 2 or not v or any(is_unconnected(p.net) for p in pins):
            continue
        a, b = uf.find(pins[0].net), uf.find(pins[1].net)
        if a != b:
            edges.append((a, b, v, ref))
    inj = defaultdict(float)
    for pm in pms:
        g = uf.find(pm["net"])
        if "i_bias" in pm:
            inj[g] -= pm["i_bias"]
        if pm.get("r_in"):
            edges.append((g, uf.find(gnd), pm["r_in"], f"{pm['ref']}.{pm['pin']} r_in"))
    zs = zeners(nl, specs)
    clamps_def = [{"kind": "pin", **pm} for pm in pms if "clamp" in pm] + \
                 [{"kind": "zener", **z} for z in zs]

    def run():
        fixed: dict[str, tuple[float, str]] = {}
        active: list[dict] = []
        sol: dict[str, float] = {}
        for _ in range(max_clamps + 1):
            res = _step(fixed, active)
            sol = res[0]
            if not res[1]:
                break
        return sol, fixed, active

    def _step(fixed, active):
        kv = {**{g: v for g, (v, _) in val.items()}, **{g: v for g, (v, _) in fixed.items()}}
        adj = defaultdict(list)
        for a, b, r, ref in edges:
            adj[a].append((b, r))
            adj[b].append((a, r))
        unknown = sorted({g for e in edges for g in e[:2] if g not in kv})
        # keep components that touch a known node
        comp, seen = {}, set()
        for u in unknown:
            if u in seen:
                continue
            stack, members, touches = [u], [], False
            seen.add(u)
            while stack:
                x = stack.pop()
                members.append(x)
                for y, _r in adj[x]:
                    if y in kv:
                        touches = True
                    elif y not in seen:
                        seen.add(y)
                        stack.append(y)
            if touches:
                for m in members:
                    comp[m] = True
        idx = {g: i for i, g in enumerate(sorted(comp))}
        sol = dict(kv)
        if idx:
            n = len(idx)
            A = [[0.0] * n for _ in range(n)]
            B = [0.0] * n
            for g, i in idx.items():
                for y, r in adj[g]:
                    A[i][i] += 1 / r
                    if y in idx:
                        A[i][idx[y]] -= 1 / r
                    else:
                        B[i] += kv[y] / r
                B[i] += inj.get(g, 0.0)
            x = _gauss(A, B)
            if x is None:
                notes.append("resistor network singular; part of it not solved")
            else:
                sol.update({g: x[i] for g, i in idx.items()})
        # most violated clamp
        worst, wv = None, 0.0
        for c in clamps_def:
            if c in active:
                continue
            if c["kind"] == "pin":
                g = uf.find(c["net"])
                if g in sol and g not in kv and sol[g] - c["clamp"] > wv:
                    worst, wv = c, sol[g] - c["clamp"]
            else:
                ga, gk = uf.find(c["anode"]), uf.find(c["cathode"])
                if ga in sol and gk in sol and (ga not in kv or gk not in kv):
                    over = (sol[gk] - sol[ga]) - c["vz"]
                    if over > wv:
                        worst, wv = c, over
        if worst is None:
            return sol, False
        active.append(worst)
        if worst["kind"] == "pin":
            fixed[uf.find(worst["net"])] = (worst["clamp"], f"{worst['ref']}.{worst['pin']} "
                                            f"internal clamp ({worst['src']})")
        else:
            ga, gk = uf.find(worst["anode"]), uf.find(worst["cathode"])
            if ga not in kv:
                fixed[ga] = (sol[gk] - worst["vz"], f"zener {worst['ref']} VZ {worst['vz']:g} V "
                             f"({worst['src']})")
            else:
                fixed[gk] = (sol[ga] + worst["vz"], f"zener {worst['ref']} VZ {worst['vz']:g} V "
                             f"({worst['src']})")
        return sol, True

    drv_inj: dict[str, float] = {}
    sol, fixed, active = run()

    def kcl_out(g):      # current flowing from node g into the network (A)
        tot = 0.0
        for a, b, r, _ in edges:
            if a == g and b in sol:
                tot += (sol[g] - sol[b]) / r
            elif b == g and a in sol:
                tot += (sol[g] - sol[a]) / r
        return tot - inj.get(g, 0.0) + drv_inj.get(g, 0.0)

    # a driver whose network wants more than its datasheet drive current
    # becomes a current source at that limit (minimum: worst case), re-solved
    for _ in range(3):
        over = []
        for d in drivers:
            g = uf.find(d["node"])
            if d["level"] in ("low", "high") and d.get("limit_a") and g in sol \
                    and not d.get("current_limited"):
                need = abs(kcl_out(g))
                if need > d["limit_a"]:
                    over.append((d, g, need))
        if not over:
            break
        for d, g, need in over:
            d["current_limited"] = True
            d["current_wanted_a"] = need
            val.pop(g, None)
            i_drv = -d["limit_a"] if d["level"] == "low" else d["limit_a"]
            inj[g] += i_drv
            drv_inj[g] = drv_inj.get(g, 0.0) + i_drv
        sol, fixed, active = run()
    for d in drivers:
        g = uf.find(d["node"])
        if d.get("current_limited"):
            d["current_a"] = -d["limit_a"] if d["level"] == "low" else d["limit_a"]
            d["node_v"] = sol.get(g)
        elif d["level"] != "released" and g in sol:
            d["current_a"] = kcl_out(g)        # + = sourced by the pin, - = sunk

    clamps = []
    for c in active:
        if c["kind"] == "pin":
            g = uf.find(c["net"])
            i_in = -kcl_out(g)       # current the network pushes into the clamp
            row = {"kind": "pin", "ref": c["ref"], "pin": c["pin"], "net": c["net"],
                   "clamp_v": c["clamp"], "current_a": i_in, "src": c["src"]}
            if "i_trip" in c:
                row["i_trip"] = c["i_trip"]
                row["tripped"] = i_in >= c["i_trip"]
                # source voltage at which the trip current is reached (one feeding rail)
                feeds = [(b if a == g else a, r) for a, b, r, _ in edges if g in (a, b)]
                live = [(y, r) for y, r in feeds if y in sol and abs(sol[y]) > 1e-9]
                if len(live) == 1 and all(y in val or y == uf.find(gnd) for y, _ in feeds):
                    ys, rs = live[0]
                    rest = sum((c["clamp"] - sol[y]) / r for y, r in feeds if y != ys)
                    members = [n for n in nl.nets if uf.find(n) == ys]
                    row["source_net_at_trip"] = next((n for n in members if n in known),
                                                     members[0] if members else ys)
                    row["source_v_at_trip"] = c["clamp"] + rs * (
                        c["i_trip"] + rest - inj.get(g, 0.0))
            clamps.append(row)
        else:
            ga, gk = uf.find(c["anode"]), uf.find(c["cathode"])
            g = ga if ga in fixed else gk
            i = abs(kcl_out(g))
            clamps.append({"kind": "zener", "ref": c["ref"], "anode": c["anode"],
                           "cathode": c["cathode"], "vz": c["vz"], "current_a": i,
                           "power_w": i * c["vz"], "ptot": c["ptot"], "src": c["src"]})
    srcs = {**{g: s for g, (_, s) in val.items()}, **{g: s for g, (_, s) in fixed.items()}}
    volts = {}
    for n in nl.nets:
        if is_unconnected(n):
            continue
        g = uf.find(n)
        if g in sol:
            src = srcs.get(g) or ("resistor network" + (", pin models" if any(
                uf.find(pm["net"]) == g for pm in pms) else ""))
            volts[n] = (sol[g], src)
    missing = {
        "drivers_without_rating": sorted({f"{d['ref']}.{d['pin']}" for d in drivers
                                          if d["level"] in ("low", "high") and not d.get("limit_a")}),
        "zeners_from_value_field": sorted(z["ref"] for z in zs if z["src"] == "value field"),
        "switches_without_polarity": sorted(r for r in switches if pol.get(r) is None),
        "switches_without_vsd": sorted(r for r, st in switches.items() if st == "off"
                                       and not dss.rated(specs.get(r), "electrical", "VSD",
                                                         "VF_body", "VSD_body")),
    }
    return {"volts": volts, "clamps": clamps, "drivers": drivers, "notes": notes,
            "missing_data": {k: v for k, v in missing.items() if v}}


def findings(res: dict, scenario: str = "") -> list[dict]:
    """DC-CLAMP / DC-TRIP / DC-ZENER-P / DC-DRIVE findings of one solve."""
    tag = f" [{scenario}]" if scenario else ""
    out = []
    md = res.get("missing_data") or {}
    if md:
        what = {"drivers_without_rating": "gate-driver pins with no drive rating (i_sink / "
                "i_source: treated as ideal)",
                "zeners_from_value_field": "zeners with VZ from the value field only",
                "switches_without_polarity": "switches of unknown P / N type (gate drive and "
                "body diode not modelled)",
                "switches_without_vsd": "off switches with no body-diode VSD (drop ignored)"}
        out.append({"severity": "info", "rule": "DC-NODATA", "ref": "-",
                    "message": "DC model ran without datasheet data for: " + "; ".join(
                        f"{what[k]}: {', '.join(v[:8])}" for k, v in md.items())
                    + " - extract them (pin_models / electrical) for a better estimate." + tag})
    for c in res["clamps"]:
        ma = c["current_a"] * 1e3
        if c["kind"] == "pin":
            msg = (f"{c['ref']}.{c['pin']} ({c['net']}) is held at its internal clamp "
                   f"{c['clamp_v']:g} V, {ma:.3g} mA into the pin ({c['src']})")
            if c.get("tripped"):
                out.append({"severity": "warning", "rule": "DC-TRIP", "ref": c["ref"],
                            "message": msg + f": at or above the {c['i_trip'] * 1e6:.3g} uA "
                            "trip current - the part is in its tripped state (e.g. OVLO)."
                            + tag})
            else:
                extra = (f"; trip current {c['i_trip'] * 1e6:.3g} uA is reached at "
                         f"{c['source_net_at_trip']} = {c['source_v_at_trip']:.3g} V"
                         if c.get("source_v_at_trip") is not None else "")
                out.append({"severity": "info", "rule": "DC-CLAMP", "ref": c["ref"],
                            "message": msg + extra + "." + tag})
        else:
            p = c["power_w"]
            sev = "warning" if c["ptot"] and p > 0.5 * c["ptot"] else "info"
            out.append({"severity": "error" if c["ptot"] and p > c["ptot"] else sev,
                        "rule": "DC-ZENER-P" if c["ptot"] else "DC-CLAMP", "ref": c["ref"],
                        "message": f"Zener {c['ref']} conducts: {c['cathode']} - {c['anode']} "
                        f"held at {c['vz']:g} V ({c['src']}), {ma:.3g} mA, {p * 1e3:.3g} mW"
                        + (f" of Ptot {c['ptot'] * 1e3:g} mW" if c["ptot"] else "") + "." + tag})
    for d in res["drivers"]:
        if d.get("current_a") is None or abs(d["current_a"]) < 1e-6:
            continue
        verb = "sinks" if d["current_a"] < 0 else "sources"
        if d.get("current_limited"):
            out.append({"severity": "warning", "rule": "DC-DRIVE", "ref": d["ref"],
                        "message": f"{d['ref']}.{d['pin']} ({d['level']}, switch {d['switch']} "
                        f"{d['state']}): the network needs {d['current_wanted_a'] * 1e3:.3g} mA, "
                        f"the pin {verb} at most {d['limit_a'] * 1e3:.3g} mA (datasheet minimum, "
                        f"{d['limit_src']}); its node only reaches {d['node_v']:.3g} V - the "
                        "pin checks below use that level." + tag})
        else:
            rated = (f" (rating {d['limit_a'] * 1e3:.3g} mA)" if d.get("limit_a")
                     else " - no drive rating in the spec cache, check the datasheet")
            out.append({"severity": "info", "rule": "DC-DRIVE", "ref": d["ref"],
                        "message": f"{d['ref']}.{d['pin']} drives {d['level']} to switch "
                        f"{d['switch']} {d['state']}: {verb} {abs(d['current_a']) * 1e3:.3g} mA"
                        + rated + "." + tag})
    return out
