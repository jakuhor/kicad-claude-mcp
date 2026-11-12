"""DFM check of a board against a fab preset (`project_settings.FAB_PRESETS`).

DRC enforces the project's own rules; this compares the copper with what a
fab can build (the preset), so a board whose rules are looser than the fab's
is caught. Checks (all mm):

  DFM-TRACK       track narrower than `min_track_width` (error);
  DFM-SPACING     copper of two nets on one layer closer than `min_clearance`
                  (tracks, pads, vias; error);
  DFM-DRILL       via drill below `min_via_drill`, plated pad hole below
                  `min_through_hole_diameter` (error);
  DFM-VIA         via diameter below `min_via_diameter` (error);
  DFM-RING        annular ring (size - drill) / 2 below the preset's
                  (min_via_diameter - min_via_drill) / 2 (error);
  DFM-EDGE        copper closer to the board edge than
                  `min_copper_edge_clearance` (error);
  DFM-VIA-IN-PAD  a via inside an SMD pad other than an exposed / thermal pad
                  (solder wicks into the hole unless it is filled and capped;
                  warning);
  DFM-SILK        silkscreen over a copper pad (clipped by the fab; warning);
  DFM-MASK        solder-mask web between two pads of one footprint below the
                  preset's `min_solder_mask_bridge` or the board's
                  `solder_mask_min_width` (warning; skipped when neither
                  gives one).

Score 0-100: 100 minus 10 per error and 3 per warning, at most 30 / 15 per
rule; the deductions are listed. Zones are not measured (DRC covers them).

Scope follows kicad-happy 3.0.1 (MIT, © 2025 Andrew Klofas):
kicad/scripts/pcb/dfm.py:analyze_dfm, pcb/copper.py:analyze_via_in_pad /
analyze_board_edge_via_clearance, pcb/assembly.py:
analyze_silkscreen_pad_overlaps; the implementation is this server's.
"""

from __future__ import annotations

import math
from collections import defaultdict

from kicad_claude.adapters import pcb_editor
from kicad_claude.adapters.board_thermal import exposed_pads
from kicad_claude.adapters.pcb_geometry import board_outline_loops
from kicad_claude.adapters.pcb_netlist import _footprint_rotation, pad_to_board_xy
from kicad_claude.adapters.sch_io import find_child, find_children, is_call, property_name_index

ERROR_POINTS, WARNING_POINTS = 10, 3
ERROR_CAP, WARNING_CAP = 30, 15
MAX_LISTED = 20
CELL = 2.0


def _f(sev: str, rule: str, ref: str, msg: str, **kw) -> dict:
    return {"severity": sev, "rule": rule, "ref": ref, "message": msg, **kw}


def _xy(node, key):
    n = find_child(node, key)
    return (float(n[1]), float(n[2])) if n and len(n) >= 3 else None


def _seg_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def _seg_seg(a, b, c, d) -> float:
    def ccw(p, q, r):
        return (r[1] - p[1]) * (q[0] - p[0]) - (q[1] - p[1]) * (r[0] - p[0])
    if (ccw(a, b, c) * ccw(a, b, d) < 0) and (ccw(c, d, a) * ccw(c, d, b) < 0):
        return 0.0
    return min(_seg_dist(a, c, d), _seg_dist(b, c, d), _seg_dist(c, a, b), _seg_dist(d, a, b))


def board_items(tree: list) -> dict:
    """Tracks, vias, pads (with box corners) and footprint silkscreen of the board."""
    tracks, vias, pads, silk = [], [], [], []
    for n in tree[1:]:
        if is_call(n, "segment") or is_call(n, "arc"):
            s, e = _xy(n, "start"), _xy(n, "end")
            w, lay = find_child(n, "width"), find_child(n, "layer")
            if s and e:
                tracks.append({"a": s, "b": e, "w": float(w[1]) if w else 0.25,
                               "layer": str(lay[1]) if lay else "",
                               "net": pcb_editor.net_of(n, tree) or ""})
        elif is_call(n, "via"):
            at, size, drill = _xy(n, "at"), find_child(n, "size"), find_child(n, "drill")
            lays = find_child(n, "layers")
            if at:
                vias.append({"p": at, "size": float(size[1]) if size else 0.6,
                             "drill": float(drill[1]) if drill else 0.3,
                             "layers": [str(x) for x in (lays[1:] if lays else ["F.Cu", "B.Cu"])],
                             "net": pcb_editor.net_of(n, tree) or ""})
        elif is_call(n, "footprint"):
            ref = ""
            for prop in find_children(n, "property"):
                i = property_name_index(prop)
                if len(prop) > i + 1 and str(prop[i]) == "Reference":
                    ref = str(prop[i + 1])
            fx, fy, fa = _footprint_rotation(n)
            fpads = []
            for pad in find_children(n, "pad"):
                at, size = find_child(pad, "at"), find_child(pad, "size")
                if not at or not size or len(size) < 3:
                    continue
                c = pad_to_board_xy(fx, fy, fa, float(at[1]), float(at[2]))
                # a pad's (at x y angle) is its absolute orientation; omitted = 0
                ang = math.radians(float(at[3]) if len(at) > 3 else 0.0)
                w, h = float(size[1]), float(size[2])
                corners = [(c[0] + x * math.cos(ang) + y * math.sin(ang),
                            c[1] - x * math.sin(ang) + y * math.cos(ang))
                           for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2),
                                        (-w / 2, h / 2))]
                drill = find_child(pad, "drill")
                dval = None
                if drill and len(drill) > 1:
                    nums = [float(x) for x in drill[1:] if isinstance(x, (int, float))]
                    dval = min(nums) if nums else None
                lays = find_child(pad, "layers")
                rec = {"ref": ref, "number": str(pad[1]), "type": str(pad[2]) if len(pad) > 2 else "",
                       "shape": str(pad[3]) if len(pad) > 3 else "rect",
                       "c": c, "w": w, "h": h, "angle": ang, "corners": corners, "drill": dval,
                       "layers": [str(x) for x in (lays[1:] if lays else [])],
                       "net": pcb_editor.net_of(pad, tree) or ""}
                fpads.append(rec)
            pads += fpads
            for g in n[1:]:
                if not isinstance(g, list) or not g or str(g[0]) not in ("fp_line", "fp_rect"):
                    continue
                gl = find_child(g, "layer")
                if not gl or not str(gl[1]).endswith("SilkS"):
                    continue
                s, e = _xy(g, "start"), _xy(g, "end")
                if not s or not e:
                    continue
                pts = [s, e] if str(g[0]) == "fp_line" else [s, (e[0], s[1]), e, (s[0], e[1]), s]
                pts = [pad_to_board_xy(fx, fy, fa, x, y) for x, y in pts]
                side = str(gl[1])[0]
                silk += [{"ref": ref, "a": a, "b": b, "side": side} for a, b in zip(pts, pts[1:])]
    return {"tracks": tracks, "vias": vias, "pads": pads, "silk": silk}


def _in_pad(p, pad, margin=0.0) -> bool:
    dx, dy = p[0] - pad["c"][0], p[1] - pad["c"][1]
    ca, sa = math.cos(pad["angle"]), math.sin(pad["angle"])
    lx, ly = dx * ca - dy * sa, dx * sa + dy * ca
    return abs(lx) <= pad["w"] / 2 + margin and abs(ly) <= pad["h"] / 2 + margin


def _pad_edges(pad):
    c = pad["corners"]
    return list(zip(c, c[1:] + c[:1]))


def _on_layer(pad, layer) -> bool:
    return layer in pad["layers"] or ("*.Cu" in pad["layers"] and layer.endswith(".Cu")) or (
        "F&B.Cu" in pad["layers"] and layer in ("F.Cu", "B.Cu"))


def pad_geom(p: dict) -> tuple:
    """A pad as a distance primitive: circle -> ("pt", centre, r), oval ->
    ("seg", (a, b), r) along its long axis, anything else -> ("pad", pad, 0)
    (its rectangle; a roundrect's rounded corners are ignored, conservative)."""
    if p["shape"] == "circle":
        return "pt", p["c"], min(p["w"], p["h"]) / 2
    if p["shape"] == "oval" and abs(p["w"] - p["h"]) > 1e-9:
        r = min(p["w"], p["h"]) / 2
        half = (max(p["w"], p["h"]) - min(p["w"], p["h"])) / 2
        ca, sa = math.cos(p["angle"]), math.sin(p["angle"])
        dx, dy = (half * ca, -half * sa) if p["w"] > p["h"] else (half * sa, half * ca)
        c = p["c"]
        return "seg", ((c[0] - dx, c[1] - dy), (c[0] + dx, c[1] + dy)), r
    if p["shape"] == "oval":
        return "pt", p["c"], p["w"] / 2
    return "pad", p, 0.0


def _shapes(items):
    """Copper shapes per layer: (kind, geometry, half width, net, label)."""
    by = defaultdict(list)
    for t in items["tracks"]:
        by[t["layer"]].append(("seg", (t["a"], t["b"]), t["w"] / 2, t["net"], f"track {t['net']}"))
    layers = {t["layer"] for t in items["tracks"]} | {"F.Cu", "B.Cu"}
    for v in items["vias"]:
        for lay in layers:
            by[lay].append(("pt", v["p"], v["size"] / 2, v["net"], f"via {v['net']}"))
    for p in items["pads"]:
        k, g, r = pad_geom(p)
        for lay in layers:
            if _on_layer(p, lay):
                by[lay].append((k, g, r, p["net"], f"{p['ref']}.{p['number']}"))
    return by


def _gap(a, b) -> float:
    ka, ga, ra = a[0], a[1], a[2]
    kb, gb, rb = b[0], b[1], b[2]
    if ka == "pad" and kb == "pad":
        if any(_in_pad(c, gb) for c in ga["corners"]) or any(_in_pad(c, ga) for c in gb["corners"]):
            return 0.0
        return min(_seg_seg(e1[0], e1[1], e2[0], e2[1]) for e1 in _pad_edges(ga)
                   for e2 in _pad_edges(gb))
    if ka == "pad":
        a, b = b, a
        ka, ga, ra, kb, gb, rb = kb, gb, rb, ka, ga, ra
    if kb == "pad":
        segs = [ga] if ka == "seg" else [(ga, ga)]
        if any(_in_pad(s[0], gb) or _in_pad(s[1], gb) for s in segs):
            return 0.0
        d = min(_seg_seg(s[0], s[1], e[0], e[1]) for s in segs for e in _pad_edges(gb))
        return d - ra
    sa = ga if ka == "seg" else (ga, ga)
    sb = gb if kb == "seg" else (gb, gb)
    return _seg_seg(sa[0], sa[1], sb[0], sb[1]) - ra - rb


def _bbox(s):
    k, g, r = s[0], s[1], s[2]
    pts = g["corners"] if k == "pad" else (list(g) if k == "seg" else [g])
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r


def check_spacing(items, min_clear: float) -> list[tuple]:
    """Copper of two nets on one layer closer than `min_clear`: a grid of
    CELL mm over the bounding boxes, exact distance only for pairs whose
    boxes come within `min_clear`."""
    out = []
    for layer, shapes in _shapes(items).items():
        boxes = [_bbox(s_) for s_ in shapes]
        grid = defaultdict(list)
        for i, (x0, y0, x1, y1) in enumerate(boxes):
            if not shapes[i][3]:
                continue
            for gx in range(int(math.floor(x0 / CELL)), int(math.floor((x1 + min_clear) / CELL)) + 1):
                for gy in range(int(math.floor(y0 / CELL)),
                                int(math.floor((y1 + min_clear) / CELL)) + 1):
                    grid[(gx, gy)].append(i)
        seen = set()
        for cell in grid.values():
            for ii, i in enumerate(cell):
                a, ba = shapes[i], boxes[i]
                for j in cell[ii + 1:]:
                    b = shapes[j]
                    if a[3] == b[3]:
                        continue
                    bb = boxes[j]
                    if (ba[0] - bb[2] > min_clear or bb[0] - ba[2] > min_clear
                            or ba[1] - bb[3] > min_clear or bb[1] - ba[3] > min_clear):
                        continue
                    if (i, j) in seen:
                        continue
                    seen.add((i, j))
                    g = _gap(a, b)
                    if g < min_clear - 1e-6:
                        out.append((layer, a[4], b[4], max(0.0, g)))
    return out


def analyze(tree: list, preset: str, rules: dict) -> dict:
    items = board_items(tree)
    findings: list[dict] = []
    metrics: dict = {}
    not_checked = []

    def add(sev, rule, ref, msg, **kw):
        findings.append(_f(sev, rule, ref, msg, **kw))

    tw = rules.get("min_track_width")
    widths = [t["w"] for t in items["tracks"]]
    if widths:
        metrics["min_track_width_mm"] = min(widths)
    if tw:
        for t in items["tracks"]:
            if t["w"] < tw - 1e-9:
                add("error", "DFM-TRACK", t["net"], f"Track {t['w']:g} mm on {t['layer']} "
                    f"({t['net']}) below {preset} min_track_width {tw:g} mm.")
    cl = rules.get("min_clearance")
    if cl:
        gaps = check_spacing(items, cl)
        if gaps:
            metrics["min_spacing_mm"] = round(min(g[3] for g in gaps), 4)
        for layer, a, b, g in gaps:
            add("error", "DFM-SPACING", a, f"{a} to {b} on {layer}: {g:.3f} mm below "
                f"{preset} min_clearance {cl:g} mm.")
    vd, vdia = rules.get("min_via_drill"), rules.get("min_via_diameter")
    ring_min = (vdia - vd) / 2 if vd and vdia else None
    if items["vias"]:
        metrics["min_via_drill_mm"] = min(v["drill"] for v in items["vias"])
        metrics["min_annular_ring_mm"] = round(min((v["size"] - v["drill"]) / 2
                                                   for v in items["vias"]), 4)
    for v in items["vias"]:
        at = f"via at ({v['p'][0]:g}, {v['p'][1]:g})"
        if vd and v["drill"] < vd - 1e-9:
            add("error", "DFM-DRILL", v["net"], f"{at} drill {v['drill']:g} mm below {preset} "
                f"min_via_drill {vd:g} mm.")
        if vdia and v["size"] < vdia - 1e-9:
            add("error", "DFM-VIA", v["net"], f"{at} diameter {v['size']:g} mm below {preset} "
                f"min_via_diameter {vdia:g} mm.")
        ring = (v["size"] - v["drill"]) / 2
        if ring_min is not None and ring < ring_min - 1e-9:
            add("error", "DFM-RING", v["net"], f"{at} annular ring {ring:.3f} mm below "
                f"{ring_min:.3f} mm ({preset}: (min_via_diameter - min_via_drill) / 2).")
    th = rules.get("min_through_hole_diameter")
    for p in items["pads"]:
        if th and p["type"] == "thru_hole" and p["drill"] and p["drill"] < th - 1e-9:
            add("error", "DFM-DRILL", p["ref"], f"Pad {p['ref']}.{p['number']} hole "
                f"{p['drill']:g} mm below {preset} min_through_hole_diameter {th:g} mm.")
    # copper to edge
    ed = rules.get("min_copper_edge_clearance")
    loops = board_outline_loops(tree)
    edges = [(a, b) for lp in loops for a, b in zip(lp, lp[1:] + lp[:1])]
    if ed and edges:
        for t in items["tracks"]:
            d = min(_seg_seg(t["a"], t["b"], a, b) for a, b in edges) - t["w"] / 2
            if d < ed - 1e-9:
                add("error", "DFM-EDGE", t["net"], f"Track on {t['layer']} ({t['net']}) "
                    f"{max(0.0, d):.3f} mm from the board edge, below {ed:g} mm.")
        for v in items["vias"]:
            d = min(_seg_dist(v["p"], a, b) for a, b in edges) - v["size"] / 2
            if d < ed - 1e-9:
                add("error", "DFM-EDGE", v["net"], f"Via at ({v['p'][0]:g}, {v['p'][1]:g}) "
                    f"{max(0.0, d):.3f} mm from the board edge, below {ed:g} mm.")
        for p in items["pads"]:
            if not any(x.endswith(".Cu") for x in p["layers"]):
                continue
            k, g, r = pad_geom(p)
            if k == "pad":
                d = min(_seg_seg(e[0], e[1], a, b) for e in _pad_edges(p) for a, b in edges)
            else:
                sg = g if k == "seg" else (g, g)
                d = min(_seg_seg(sg[0], sg[1], a, b) for a, b in edges) - r
            if d < ed - 1e-9:
                add("error", "DFM-EDGE", p["ref"], f"Pad {p['ref']}.{p['number']} "
                    f"{d:.3f} mm from the board edge, below {ed:g} mm.")
    elif ed:
        not_checked.append("copper to edge: no closed Edge.Cuts outline")
    # via in pad
    by_ref = defaultdict(list)
    for p in items["pads"]:
        by_ref[p["ref"]].append(p)
    eps = set()
    for ref, ps in by_ref.items():
        fake = {"pads": [{"number": p["number"], "type": p["type"], "w": p["w"], "h": p["h"],
                          "layers": p["layers"]} for p in ps]}
        eps |= {(ref, e["number"]) for e in exposed_pads(fake)}
    smd_grid = defaultdict(list)
    for p in items["pads"]:
        if p["type"] == "smd":
            smd_grid[(int(math.floor(p["c"][0] / CELL)), int(math.floor(p["c"][1] / CELL)))].append(p)
    for v in items["vias"]:
        gx, gy = int(math.floor(v["p"][0] / CELL)), int(math.floor(v["p"][1] / CELL))
        cands = [p for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)
                 for p in smd_grid.get((gx + dx, gy + dy), [])]
        for p in cands:
            if p["type"] == "smd" and (p["ref"], p["number"]) not in eps and _in_pad(v["p"], p) \
                    and any(x in v["layers"] for x in p["layers"]):
                add("warning", "DFM-VIA-IN-PAD", p["ref"], f"Via in SMD pad {p['ref']}."
                    f"{p['number']}: solder wicks into the hole unless it is filled and capped "
                    "(order via-in-pad / POFV or move the via).")
                break
    # silk on pads (pads bucketed on a grid by their box)
    pad_grid = defaultdict(list)
    for p in items["pads"]:
        xs, ys = [c[0] for c in p["corners"]], [c[1] for c in p["corners"]]
        p["box"] = (min(xs), min(ys), max(xs), max(ys))
        for gx in range(int(math.floor(p["box"][0] / CELL)), int(math.floor(p["box"][2] / CELL)) + 1):
            for gy in range(int(math.floor(p["box"][1] / CELL)),
                            int(math.floor(p["box"][3] / CELL)) + 1):
                pad_grid[(gx, gy)].append(p)
    hit = set()
    for s in items["silk"]:
        sx0, sx1 = sorted((s["a"][0], s["b"][0]))
        sy0, sy1 = sorted((s["a"][1], s["b"][1]))
        near = {id(p): p for gx in range(int(math.floor(sx0 / CELL)), int(math.floor(sx1 / CELL)) + 1)
                for gy in range(int(math.floor(sy0 / CELL)), int(math.floor(sy1 / CELL)) + 1)
                for p in pad_grid.get((gx, gy), [])}
        for p in near.values():
            b = p["box"]
            if b[0] > sx1 or b[2] < sx0 or b[1] > sy1 or b[3] < sy0:
                continue
            if (p["ref"], s["ref"]) in hit or not any(x.startswith(s["side"] + ".") or x == "*.Cu"
                                                       for x in p["layers"]):
                continue
            n = max(1, int(math.dist(s["a"], s["b"]) / 0.1))
            if any(_in_pad((s["a"][0] + (s["b"][0] - s["a"][0]) * k / n,
                            s["a"][1] + (s["b"][1] - s["a"][1]) * k / n), p) for k in range(n + 1)):
                hit.add((p["ref"], s["ref"]))
                add("warning", "DFM-SILK", s["ref"], f"Silkscreen of {s['ref']} over pad "
                    f"{p['ref']}.{p['number']}: the fab clips it.")
    # solder-mask web
    web = rules.get("min_solder_mask_bridge")
    if web is None:
        setup = find_child(tree, "setup")
        smw = find_child(setup, "solder_mask_min_width") if setup else None
        web = float(smw[1]) if smw and len(smw) > 1 and float(smw[1]) > 0 else None
    if web:
        for ref, ps in by_ref.items():
            smd = [p for p in ps if p["type"] == "smd"]
            for i, a in enumerate(smd):
                for b in smd[i + 1:]:
                    if a["net"] and a["net"] == b["net"]:
                        continue
                    g = _gap(("pad", a, 0.0), ("pad", b, 0.0))
                    if 0 < g < web - 1e-9:
                        add("warning", "DFM-MASK", ref, f"Solder-mask web between {ref}."
                            f"{a['number']} and {ref}.{b['number']} {g:.3f} mm, below {web:g} mm:"
                            " the fab gangs the opening (no mask dam).")
    else:
        not_checked.append("solder-mask web: no min_solder_mask_bridge in the preset and no "
                           "solder_mask_min_width in the board setup")
    # score
    per = defaultdict(lambda: [0, 0])
    for f in findings:
        per[f["rule"]][0 if f["severity"] == "error" else 1] += 1
    deductions = []
    for rule, (e, w) in sorted(per.items()):
        pts = min(ERROR_CAP, e * ERROR_POINTS) + min(WARNING_CAP, w * WARNING_POINTS)
        if pts:
            deductions.append({"rule": rule, "errors": e, "warnings": w, "points": pts})
    score = max(0, 100 - sum(d["points"] for d in deductions))
    counts = {"error": sum(1 for f in findings if f["severity"] == "error"),
              "warning": sum(1 for f in findings if f["severity"] == "warning")}
    listed = []
    by_rule = defaultdict(int)
    for f in findings:
        by_rule[f["rule"]] += 1
        if by_rule[f["rule"]] <= MAX_LISTED:
            listed.append(f)
    return {"preset": preset, "rules": rules, "dfm_score": score, "deductions": deductions,
            "metrics": metrics, "findings": listed, "counts": counts,
            "per_rule": dict(by_rule), "not_checked": not_checked}
