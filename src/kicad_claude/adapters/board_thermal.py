"""Board thermal review: junction temperatures corrected by the layout.

Per part that dissipates (`part_ratings` `dissipation`: regulators, FETs,
diodes, CCRs, resistors):

  Tj = Ta + P x Rθ_eff, Rθ_eff = Rθ_JA x board correction.

Rθ_JA comes from the extracted datasheet (`thermal.rth_ja`) or, labelled as
an estimate, the package-typical table (`thermal_pkg`). The board correction
applies only to the table value (a still-air JEDEC figure without copper):
thermal vias under the exposed pad - good 0.50, adequate 0.65, insufficient
0.80, none 1.0. A datasheet Rθ_JA is already measured on a JEDEC board with
vias, so it is not reduced; the via check still runs.

Rules:
  TH-TJ        Tj above Tj max (error; warning when Rθ is the package
               estimate) / within the margin (warning);
  TH-VIAS      exposed pad of a part dissipating > 0.5 W with fewer than the
               recommended thermal vias (warning);
  TH-MLCC-HOT  class-II MLCC within 10 mm of a part rising >= 20 K
               (capacitance drift with temperature, warning);
  TH-ELCO-HOT  electrolytic within 10 mm of such a part (life halves per
               10 K, warning);
  TH-NODATA    a part that dissipates with no Rθ or no Tj max (info).

Ported in behaviour from kicad-happy 3.0.1 (MIT, © 2025 Andrew Klofas):
kicad/scripts/analyze_thermal.py:_get_pcb_thermal_correction,
_compute_junction_temps, _check_thermal_proximity; kicad/scripts/pcb/copper.py:
analyze_thermal_pad_vias (pad search box, drill weighting, recommended counts).
"""

from __future__ import annotations

import math
import re
import statistics

from kicad_claude.adapters import datasheet_specs as dss
from kicad_claude.adapters import derating as drt
from kicad_claude.adapters import pcb_editor, thermal_pkg
from kicad_claude.adapters.pcb_netlist import _footprint_rotation, pad_to_board_xy
from kicad_claude.adapters.sch_io import find_child, find_children, is_call, property_name_index

CORRECTION = {"good": 0.50, "adequate": 0.65, "insufficient": 0.80, "none": 1.0}
VIA_POWER_W = 0.5         # TH-VIAS only for parts above this dissipation
HOT_RISE_K = 20.0         # TH-MLCC-HOT / TH-ELCO-HOT source threshold
HOT_DIST_MM = 10.0
_CLASS2 = ("X5R", "X7R", "X6S", "X7S", "X8R", "Y5V", "Z5U")


def _f(sev: str, rule: str, ref: str, msg: str, **kw) -> dict:
    return {"severity": sev, "rule": rule, "ref": ref, "message": msg, **kw}


def _ref(fp: list) -> str:
    for prop in find_children(fp, "property"):
        i = property_name_index(prop)
        if len(prop) > i + 1 and str(prop[i]) == "Reference":
            return str(prop[i + 1])
    return ""


def footprints(tree: list) -> dict[str, dict]:
    """ref -> {lib, x, y, layer, pads: [{number, type, w, h, x, y, angle, net}]}."""
    out = {}
    for fp in tree[1:]:
        if not is_call(fp, "footprint"):
            continue
        ref = _ref(fp)
        if not ref:
            continue
        fx, fy, fa = _footprint_rotation(fp)
        layer = find_child(fp, "layer")
        pads = []
        for pad in find_children(fp, "pad"):
            at, size = find_child(pad, "at"), find_child(pad, "size")
            if not at or not size or len(size) < 3:
                continue
            px, py = float(at[1]), float(at[2])
            pt = pad_to_board_xy(fx, fy, fa, px, py)
            layers = find_child(pad, "layers")
            drill = find_child(pad, "drill")
            dval = next((float(x) for x in (drill or [])[1:]
                         if isinstance(x, (int, float))), None)
            pads.append({"number": str(pad[1]), "type": str(pad[2]) if len(pad) > 2 else "",
                         "drill": dval,
                         "w": float(size[1]), "h": float(size[2]), "x": pt[0], "y": pt[1],
                         # absolute orientation as KiCAD writes it; omitted = 0
                         "angle": float(at[3]) if len(at) > 3 else 0.0,
                         "net": pcb_editor.net_of(pad, tree) or "",
                         "layers": [str(x) for x in (layers[1:] if layers else [])]})
        out[ref] = {"lib": str(fp[1]) if len(fp) > 1 else "", "x": fx, "y": fy,
                    "layer": str(layer[1]) if layer and len(layer) > 1 else "F.Cu",
                    "pads": pads}
    return out


def vias(tree: list) -> list[dict]:
    out = []
    for v in find_children(tree, "via"):
        at, drill = find_child(v, "at"), find_child(v, "drill")
        if not at or len(at) < 3:
            continue
        out.append({"x": float(at[1]), "y": float(at[2]),
                    "drill": float(drill[1]) if drill and len(drill) > 1 else 0.3,
                    "net": pcb_editor.net_of(v, tree) or ""})
    return out


def exposed_pads(fp: dict) -> list[dict]:
    """Thermal / exposed pads: SMD copper pads numbered EP / 0 / "", or at
    least 3x the median area of the footprint's other SMD pads."""
    smd = [p for p in fp["pads"] if p["type"] == "smd" and p["w"] * p["h"] > 0
           and any(x.endswith(".Cu") for x in p["layers"])]
    if len(smd) < 3:
        return []
    out = []
    for p in smd:
        others = [q["w"] * q["h"] for q in smd if q["number"] != p["number"]]
        if p["number"] in ("0", "EP", "") or (
                others and p["w"] * p["h"] >= 3.0 * statistics.median(others)):
            out.append(p)
    return out


def _recommended(area: float) -> tuple[int, int]:
    return ((5, 9) if area < 10 else (9, 16) if area < 25
            else (max(9, int(area * 0.5)), max(16, int(area * 0.8))))


def pad_vias(pad: dict, all_vias: list[dict]) -> dict:
    """Vias inside 1.5x the pad's (rotated) box on its net: count, drill-
    weighted count Σ(d / 0.3 mm)², and adequacy against the recommended
    count for the pad area (< 10 mm²: 5 / 9, < 25 mm²: 9 / 16, else 0.5 /
    0.8 per mm²)."""
    a = math.radians(pad["angle"])
    ca, sa = math.cos(a), math.sin(a)
    n, eff, ids = 0, 0.0, set()
    for i, v in enumerate(all_vias):
        if pad["net"] and v["net"] != pad["net"]:
            continue
        dx, dy = v["x"] - pad["x"], v["y"] - pad["y"]
        lx, ly = dx * ca - dy * sa, dx * sa + dy * ca
        if abs(lx) <= pad["w"] * 0.75 and abs(ly) <= pad["h"] * 0.75:
            n += 1
            eff += (v["drill"] / 0.3) ** 2
            ids.add((i, (v["drill"] / 0.3) ** 2))
    area = pad["w"] * pad["h"]
    lo, hi = _recommended(area)
    best = max(eff, n)        # drill size is secondary: the raw count may decide
    adequacy = ("good" if best >= hi else "adequate" if best >= lo
                else "insufficient" if n else "none")
    return {"pad": pad["number"], "net": pad["net"], "area_mm2": round(area, 2), "vias": n,
            "effective_vias": round(eff, 2), "recommended_min": lo, "recommended": hi,
            "adequacy": adequacy, "_via_ids": ids}


def _merge_split_pads(rows: list[dict]) -> list[dict]:
    """One row per pad number: an exposed pad drawn as several copper pads
    (same number) is one pad - areas add, and its vias are the union of the
    vias under each part (a via under two overlapping search boxes counts
    once)."""
    by: dict[str, dict] = {}
    for r in rows:
        m = by.get(r["pad"])
        if m is None:
            by[r["pad"]] = dict(r)
            continue
        m["area_mm2"] = round(m["area_mm2"] + r["area_mm2"], 2)
        m["_via_ids"] = m["_via_ids"] | r["_via_ids"]
        m["vias"] = len(m["_via_ids"])
        m["effective_vias"] = round(sum(w for _, w in m["_via_ids"]), 2)
        lo, hi = _recommended(m["area_mm2"])
        best = max(m["effective_vias"], m["vias"])
        m.update(recommended_min=lo, recommended=hi,
                 adequacy="good" if best >= hi else "adequate" if best >= lo
                 else "insufficient" if m["vias"] else "none")
    for m in by.values():
        m.pop("_via_ids", None)
    return list(by.values())


def _cap_kind(nl, ref: str, rec: dict | None) -> str | None:
    comp = nl.components.get(ref) or {}
    fam = drt.cap_family(comp, rec)
    if fam == "electrolytic":
        return "elco"
    text = " ".join([comp.get("value", ""), comp.get("description", ""),
                     " ".join((comp.get("fields") or {}).values()),
                     (rec or {}).get("description", "")]).upper()
    if fam == "ceramic" and not any(k in text for k in ("C0G", "NP0", "COG")):
        return "mlcc"         # class II unless the part says C0G / NP0
    return None


def analyze(tree: list, nl, specs: dict, ratings: dict, ambient_c: float,
            tj_margin_c: float) -> dict:
    fps = footprints(tree)
    all_vias = vias(tree)
    findings, parts, nodata = [], [], {}
    for d in ratings.get("dissipation", []):
        ref, p_w = d["ref"], d["p_w"]
        fp = fps.get(ref)
        if fp is None or p_w <= 0:
            continue
        rec = specs.get(ref)
        rth = dss.rated(rec, "thermal", "rth_ja", prefer="typ")
        tjm = dss.rated(rec, "thermal", "tj_max") or dss.rated(rec, "absolute_max", "TJ")
        pkg = thermal_pkg.classify(fp["lib"])
        eps = [] if ref[:2] in ("BT", "TP") or ref[:1] == "J" else exposed_pads(fp)
        # a "_ThermalVias" footprint carries its vias as plated holes of its own
        own = [{"x": p["x"], "y": p["y"], "drill": p["drill"] or 0.3, "net": p["net"]}
               for p in fp["pads"] if p["type"] == "thru_hole" and p.get("drill")]
        ep = sorted(_merge_split_pads([pad_vias(p, all_vias + own) for p in eps]),
                    key=lambda e: -e["area_mm2"])
        adequacy = ep[0]["adequacy"] if ep else None      # the main (largest) pad
        if rth:
            rth_v, src, corr = rth[0], f"datasheet {rec.get('mpn')} p.{rth[1]}", 1.0
        elif pkg:
            rth_v, src = pkg["rth_ja"], thermal_pkg.ASSUMED
            corr = CORRECTION[adequacy] if adequacy else 1.0
        else:
            rth_v, src, corr = None, None, 1.0
        row = {"ref": ref, "p_w": p_w, "method": d["method"], "package": (pkg or {}).get("package"),
               "x_mm": round(fp["x"], 3), "y_mm": round(fp["y"], 3), "exposed_pads": ep}
        if rth_v is not None:
            eff = rth_v * corr
            rise = p_w * eff
            row.update(rth_ja=rth_v, rth_source=src, board_correction=corr,
                       rth_effective=round(eff, 2), rise_k=round(rise, 2),
                       tj_c=round(ambient_c + rise, 1))
        if tjm:
            row["tj_max"] = tjm[0]
        parts.append(row)
        assumed = src == thermal_pkg.ASSUMED
        if rth_v is not None and tjm and ref[:1] != "R":
            tj = row["tj_c"]
            how = (f"Ta {ambient_c:g} C + {p_w:.3g} W x {row['rth_effective']:g} C/W ({src}"
                   + (f", x{corr:g} for {adequacy} thermal vias" if corr != 1.0 else "") + ")")
            if tj > tjm[0]:
                findings.append(_f("warning" if assumed else "error", "TH-TJ", ref,
                                   f"Tj {tj:.0f} C above Tj max {tjm[0]:g} C: {how}"
                                   + (" - Rθ is a package-typical estimate." if assumed else ".")))
            elif tj > tjm[0] - tj_margin_c:
                findings.append(_f("warning", "TH-TJ", ref,
                                   f"Tj {tj:.0f} C within {tj_margin_c:g} C of Tj max "
                                   f"{tjm[0]:g} C: {how}."))
        elif p_w > 0.05 and ref[:1] != "R":
            nodata[ref] = (f"{p_w:.3g} W, no " + ("Tj max" if rth_v is not None
                                                  else "rth_ja (unknown package)") + " extracted")
        if ep and p_w > VIA_POWER_W and adequacy in ("none", "insufficient"):
            e = ep[0]
            findings.append(_f("warning", "TH-VIAS", ref,
                               f"Exposed pad {e['pad']} ({e['area_mm2']:g} mm²) of a "
                               f"{p_w:.3g} W part has {e['vias']} thermal via(s) on {e['net'] or 'its net'}"
                               f"; at least {e['recommended_min']} (ideally {e['recommended']}) "
                               "recommended."))
    # exposed pads of parts below VIA_POWER_W (or with no load given): the
    # via count still sets how the part sheds heat once it does dissipate
    seen = {r["ref"] for r in parts if r["p_w"] > VIA_POWER_W}
    low_vias = []
    for ref, fp in sorted(fps.items()):
        if ref in seen or not re.match(r"(U|IC|Q)\d", ref) \
                or (nl.components.get(ref) or {}).get("dnp"):
            continue
        own = [{"x": p["x"], "y": p["y"], "drill": p["drill"] or 0.3, "net": p["net"]}
               for p in fp["pads"] if p["type"] == "thru_hole" and p.get("drill")]
        ep = sorted(_merge_split_pads([pad_vias(p, all_vias + own) for p in exposed_pads(fp)]),
                    key=lambda e: -e["area_mm2"])
        if ep and ep[0]["adequacy"] in ("none", "insufficient"):
            e = ep[0]
            low_vias.append({"ref": ref, **e})
            findings.append(_f("info", "TH-VIAS", ref,
                               f"Exposed pad {e['pad']} ({e['area_mm2']:g} mm²) has {e['vias']} "
                               f"thermal via(s) on {e['net'] or 'its net'}; at least "
                               f"{e['recommended_min']} (ideally {e['recommended']}) recommended "
                               f"(info: dissipation below {VIA_POWER_W:g} W or not known)."))
    # B4: hot-spot proximity
    hot = [r for r in parts if (r.get("rise_k") or 0) >= HOT_RISE_K]
    near = []
    for ref, fp in fps.items():
        if not ref.startswith("C") or (nl.components.get(ref) or {}).get("dnp"):
            continue
        k = _cap_kind(nl, ref, specs.get(ref))
        if not k:
            continue
        for h in hot:
            dist = math.hypot(fp["x"] - h["x_mm"], fp["y"] - h["y_mm"])
            if dist > HOT_DIST_MM:
                continue
            near.append({"cap": ref, "kind": k, "source": h["ref"], "distance_mm": round(dist, 2),
                         "source_rise_k": h["rise_k"]})
            if k == "mlcc":
                # info: a switcher's own input / output caps belong this close
                findings.append(_f("info", "TH-MLCC-HOT", ref,
                                   f"Class-II MLCC {ref} {dist:.1f} mm from {h['ref']} (junction "
                                   f"rise {h['rise_k']:.0f} K; the board at the cap runs cooler, "
                                   "falling with distance): X5R / X7R capacitance drifts with "
                                   "temperature - check the derated value (C0G / NP0 where "
                                   "it is critical)."))
            else:
                findings.append(_f("warning", "TH-ELCO-HOT", ref,
                                   f"Electrolytic {ref} {dist:.1f} mm from {h['ref']} (junction "
                                   f"rise {h['rise_k']:.0f} K; the board at the cap runs cooler): "
                                   "its life halves per 10 K - move it away or derate its "
                                   "lifetime at the measured board temperature."))
            break
    for ref, why in sorted(nodata.items()):
        findings.append(_f("info", "TH-NODATA", ref, f"{why}: extract its datasheet "
                           "(thermal.rth_ja, tj_max) to check it."))
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["rule"], f["ref"]))
    parts.sort(key=lambda r: -(r.get("tj_c") or r.get("rise_k") or 0))
    return {"assumptions": {"ambient_c": ambient_c, "tj_margin_c": tj_margin_c,
                            "board_correction": CORRECTION,
                            "note": "Board correction only on package-typical Rθ; datasheet "
                                    "Rθ_JA is the JEDEC test board."},
            "parts": parts, "low_power_vias": low_vias, "hot_spots": near, "findings": findings,
            "counts": {k: sum(1 for f in findings if f["severity"] == k) for k in order}}
