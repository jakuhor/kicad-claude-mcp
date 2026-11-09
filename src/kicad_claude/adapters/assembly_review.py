"""Assembly review and schematic <-> PCB cross-verify (OP-3).

Assembly (from the `.kicad_pcb`):
  - courtyards: each footprint's courtyard (F.CrtYd / B.CrtYd lines, rects,
    polys, circles, arcs end points) as a convex hull on the board; hulls on
    the same side that overlap (AS-CRTYD-OVERLAP) or are closer than
    `min_spacing_mm` (AS-CRTYD-SPACING); courtyard beyond / within
    `edge_clearance_mm` of the board edge (AS-EDGE; info for connectors,
    which often overhang on purpose); footprints without a courtyard
    (AS-NO-CRTYD);
  - sides: fitted SMD parts on a side the assembler does not populate
    (`assembly_sides`, AS-SIDE), through-hole parts (AS-THT, info);
  - fiducials: fewer than `fiducials_min` on a side carrying SMD parts
    (AS-FIDUCIAL);
  - polarity: polarized parts (diodes, LEDs, polarized caps, ICs) with no
    silkscreen graphic of their own (AS-SILK-POLARITY, info).

Cross-verify (KiCAD netlist vs board): part on one side only (XV-MISSING /
XV-EXTRA), symbol pin numbers vs footprint pad numbers (XV-PADS), and
Value / Footprint / MPN differing between schematic and board (XV-FIELD).
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

from kicad_claude.adapters.pcb_editor import get_footprint_reference, iter_footprints
from kicad_claude.adapters.pcb_geometry import board_outline_loops
from kicad_claude.adapters.pcb_netlist import pad_to_board_xy
from kicad_claude.adapters.sch_io import find_child, find_children

POLARIZED_RE = re.compile(r"^(D|LED|U|IC|Q|BT)\d|CP|Polarized|Electrolytic|Tantal", re.I)
CONNECTOR_RE = re.compile(r"^(J|P|CN|X|USB)\d")


def _f(sev: str, rule: str, ref: str, msg: str) -> dict:
    return {"severity": sev, "rule": rule, "ref": ref, "message": msg}


def _hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set((round(x, 4), round(y, 4)) for x, y in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _xy(node, key):
    n = find_child(node, key)
    return (float(n[1]), float(n[2])) if n and len(n) > 2 else None


def footprints(tree: list) -> list[dict]:
    out = []
    for fp in iter_footprints(tree):
        at = find_child(fp, "at")
        fx, fy = (float(at[1]), float(at[2])) if at else (0.0, 0.0)
        rot = float(at[3]) if at and len(at) > 3 else 0.0
        layer = find_child(fp, "layer")
        side = "B" if layer and str(layer[1]).startswith("B.") else "F"
        attr = find_child(fp, "attr")
        attrs = {str(a) for a in (attr or [])[1:]}
        props = {str(p[1]): str(p[2]) for p in find_children(fp, "property") if len(p) >= 3}
        crt, silk, segs, silk_pts = [], 0, [], []
        for g in fp[1:]:
            if not isinstance(g, list) or not g or str(g[0]) not in (
                    "fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
                continue
            gl = find_child(g, "layer")
            lname = str(gl[1]) if gl else ""
            if not (lname.endswith("SilkS") or lname.endswith("CrtYd")):
                continue
            kind = str(g[0])
            local = []
            if kind == "fp_circle":
                c, e = _xy(g, "center"), _xy(g, "end")
                if c and e:
                    r = math.dist(c, e)
                    local = [(c[0] + r * math.cos(a), c[1] + r * math.sin(a))
                             for a in [k * math.pi / 8 for k in range(16)]]
            elif kind == "fp_poly":
                pts = find_child(g, "pts")
                local = [(float(p[1]), float(p[2])) for p in find_children(pts, "xy")] if pts else []
            elif kind == "fp_rect":
                s, e = _xy(g, "start"), _xy(g, "end")
                if s and e:
                    local = [s, (e[0], s[1]), e, (s[0], e[1])]
            else:
                local = [p for p in (_xy(g, "start"), _xy(g, "mid"), _xy(g, "end")) if p]
            pts = [pad_to_board_xy(fx, fy, rot, x, y) for x, y in local]
            if lname.endswith("SilkS"):
                silk += 1
                # sample along lines so a mark is found by its nearest point
                for a, c in zip(pts, pts[1:]) if kind in ("fp_line", "fp_arc") else \
                        zip(pts, pts[1:] + pts[:1]):
                    n = max(1, int(math.dist(a, c) / 0.2))
                    silk_pts += [(a[0] + (c[0] - a[0]) * t / n, a[1] + (c[1] - a[1]) * t / n)
                                 for t in range(n + 1)]
                if len(pts) == 1:
                    silk_pts += pts
                continue
            crt += pts
            if kind in ("fp_line", "fp_arc"):
                segs += list(zip(pts, pts[1:]))
            elif len(pts) >= 3:
                segs += list(zip(pts, pts[1:] + pts[:1]))
        pads = []
        for pad in find_children(fp, "pad"):
            if len(pad) > 2:
                pat = find_child(pad, "at")
                size = find_child(pad, "size")
                px, py = (float(pat[1]), float(pat[2])) if pat else (0.0, 0.0)
                pads.append({"number": str(pad[1]), "type": str(pad[2]),
                             "at": pad_to_board_xy(fx, fy, rot, px, py),
                             "r": (math.hypot(float(size[1]), float(size[2])) / 2
                                   if size and len(size) > 2 else 0.3)})
        models = []
        for m in find_children(fp, "model"):
            if find_child(m, "hide") is not None:
                continue

            def xyz(key, default, m=m):
                n = find_child(find_child(m, key) or [], "xyz")
                return tuple(float(v) for v in n[1:4]) if n and len(n) >= 4 else default
            models.append({"path": str(m[1]), "offset": xyz("offset", (0.0, 0.0, 0.0)),
                           "scale": xyz("scale", (1.0, 1.0, 1.0)),
                           "rotate": xyz("rotate", (0.0, 0.0, 0.0))})
        out.append({"ref": get_footprint_reference(fp) or "?", "lib": str(fp[1]) if len(fp) > 1 else "",
                    "x": fx, "y": fy, "side": side, "attrs": attrs, "props": props,
                    "hull": _hull(crt), "segments": segs, "silk_items": silk,
                    "silk_points": silk_pts, "pads": pads, "models": models})
    return out


def _seg_cross(p1, p2, q1, q2) -> bool:
    def o(a, b_, c):
        v = (b_[0] - a[0]) * (c[1] - a[1]) - (b_[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)
    d1, d2, d3, d4 = o(p1, p2, q1), o(p1, p2, q2), o(q1, q2, p1), o(q1, q2, p2)
    return d1 * d2 < 0 and d3 * d4 < 0


def _inside_soup(x: float, y: float, segs: list) -> bool:
    """Even-odd ray cast over a set of closed-outline segments."""
    inside = False
    for (x1, y1), (x2, y2) in segs:
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def courtyards_overlap(a: dict, b: dict) -> bool:
    """Real (possibly non-convex) courtyard outlines: edges cross, or one lies
    inside the other."""
    sa, sb = a["segments"], b["segments"]
    if any(_seg_cross(p1, p2, q1, q2) for p1, p2 in sa for q1, q2 in sb):
        return True
    return _probe_inside(sa, sb) or _probe_inside(sb, sa)


def _probe_inside(sa: list, sb: list) -> bool:
    """Any vertex or edge midpoint of outline `sa`, nudged 1 um toward its
    own centroid, strictly inside outline `sb`. Nudging keeps shapes that
    only touch (shared edge or corner) from counting, and edge midpoints
    catch aligned overlaps where no edges properly cross."""
    if not sa or not sb:
        return False
    pts = [p for seg in sa for p in seg]
    cx = sum(x for x, _ in pts) / len(pts)
    cy = sum(y for _, y in pts) / len(pts)
    probes = []
    for (x1, y1), (x2, y2) in sa:
        probes += [(x1, y1), ((x1 + x2) / 2, (y1 + y2) / 2)]
    for x, y in probes:
        d = math.hypot(cx - x, cy - y)
        if d < 1e-9:
            continue
        px, py = x + (cx - x) / d * 1e-3, y + (cy - y) / d * 1e-3
        if _inside_soup(px, py, sb):
            return True
    return False


def courtyard_distance(a: dict, b: dict) -> float:
    if courtyards_overlap(a, b):
        return 0.0
    best = math.inf
    for P, Q in ((a["segments"], b["segments"]), (b["segments"], a["segments"])):
        for p1, p2 in P:
            for q1, q2 in Q:
                best = min(best, _seg_dist(p1, q1, q2), _seg_dist(p2, q1, q2))
    return best


def _seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0 if not L2 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _inside(x: float, y: float, poly: list) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


_STEP_PT = re.compile(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.Ee+]+)\s*,\s*"
                      r"([-\d.Ee+]+)\s*,\s*([-\d.Ee+]+)\s*\)")
_BBOX_CACHE: dict[str, tuple | None] = {}


def step_bbox(path: str) -> tuple | None:
    """(xmin, ymin, zmin, xmax, ymax, zmax) in mm of a STEP file's points
    (all CARTESIAN_POINTs: a bounding box, not a solid model). Cached."""
    if path in _BBOX_CACHE:
        return _BBOX_CACHE[path]
    from pathlib import Path
    p = Path(path)
    if p.suffix.lower() == ".wrl" and p.with_suffix(".step").is_file():
        p = p.with_suffix(".step")
    bb = None
    if p.is_file() and p.suffix.lower() in (".step", ".stp"):
        xs, ys, zs = [], [], []
        for m in _STEP_PT.finditer(p.read_text(encoding="utf-8", errors="replace")):
            xs.append(float(m.group(1)))
            ys.append(float(m.group(2)))
            zs.append(float(m.group(3)))
        if zs:
            bb = (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
    _BBOX_CACHE[path] = bb
    return bb


def model_height(model: dict, variables: dict) -> tuple[float | None, str]:
    """Height above the board of one footprint 3D model (mm), and a status:
    `ok`, `missing` (path does not resolve to a file), `unsupported`
    (not STEP / no STEP next to the WRL), `rotated` (x / y rotation applied
    to the box corners — approximate)."""
    from kicad_claude.utils.kicad_config import expand_uri
    path = expand_uri(model["path"], variables) or model["path"]
    from pathlib import Path
    p = Path(path)
    if not p.is_file() and not (p.suffix.lower() == ".wrl" and p.with_suffix(".step").is_file()):
        return None, "missing"
    bb = step_bbox(str(p))
    if bb is None:
        return None, "unsupported"
    sx, sy, sz = model["scale"]
    ox, oy, oz = model["offset"]
    rx, ry, _ = (math.radians(a) for a in model["rotate"])
    corners = [(x * sx, y * sy, z * sz) for x in (bb[0], bb[3]) for y in (bb[1], bb[4])
               for z in (bb[2], bb[5])]
    zmax = -math.inf
    for x, y, z in corners:
        y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)
        x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
        zmax = max(zmax, z)
    status = "rotated" if (rx or ry) else "ok"
    return round(zmax + oz, 3), status


def pin1_marked(f: dict) -> bool:
    """A silkscreen point within reach of pad 1 (pad radius + 1.5 mm) whose
    point reflection through the pads' centre has no silk counterpart
    (0.15 mm): an asymmetric pin-1 dot, cathode bar or chamfer. A symmetric
    outline is not a mark, however close it runs to pad 1."""
    p1 = next((p for p in f["pads"] if p["number"] == "1"), None)
    named = [p for p in f["pads"] if p["number"]]
    if not p1 or len(named) < 2:
        return True                       # nothing to tell pin 1 from
    if not f["silk_points"]:
        return False
    cx = sum(p["at"][0] for p in named) / len(named)
    cy = sum(p["at"][1] for p in named) / len(named)
    pts = f["silk_points"]
    grid = defaultdict(list)
    for s in pts:
        grid[(round(s[0] / 0.15), round(s[1] / 0.15))].append(s)

    def has_near(q):
        gx, gy = round(q[0] / 0.15), round(q[1] / 0.15)
        return any(math.dist(q, s) <= 0.15 for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                   for s in grid.get((gx + dx, gy + dy), []))
    for s in pts:
        if math.dist(s, p1["at"]) > p1["r"] + 1.5:
            continue
        if not has_near((2 * cx - s[0], 2 * cy - s[1])):
            return True
    return False


def check_assembly(tree: list, assembly_sides: str = "top", min_spacing_mm: float = 0.0,
                   edge_clearance_mm: float = 0.3, fiducials_min: int = 2,
                   tall_mm: float = 5.0, tall_edge_mm: float = 3.0,
                   max_height_top_mm: float | None = None,
                   max_height_bottom_mm: float | None = None,
                   heights: bool = True) -> dict:
    fps = footprints(tree)
    findings: list[dict] = []
    fitted = [f for f in fps if "dnp" not in f["attrs"] and "exclude_from_pos_files" not in f["attrs"]]
    smd = [f for f in fitted if "smd" in f["attrs"]]
    tht = [f for f in fitted if "through_hole" in f["attrs"]]
    sides = {"top": {"F"}, "bottom": {"B"}, "both": {"F", "B"}}[assembly_sides]

    for f in smd:
        if f["side"] not in sides:
            findings.append(_f("error", "AS-SIDE", f["ref"],
                               f"SMD part on the {'bottom' if f['side'] == 'B' else 'top'} side, but "
                               f"assembly is {assembly_sides}-side only."))
    if tht:
        findings.append(_f("info", "AS-THT", ",".join(f["ref"] for f in tht[:20]),
                           f"{len(tht)} through-hole part(s): selective / hand soldering step."))

    fid = [f for f in fps if re.match(r"FID", f["ref"]) or "fiducial" in f["lib"].lower()]
    for s in ("F", "B"):
        if any(f["side"] == s for f in smd):
            n = sum(1 for f in fid if f["side"] == s)
            if n < fiducials_min:
                findings.append(_f("info", "AS-FIDUCIAL", "",
                                   f"{n} fiducial(s) on the {'top' if s == 'F' else 'bottom'} side "
                                   f"with SMD parts (assembler may want >= {fiducials_min}, or panel "
                                   "fiducials)."))

    hulls = [f for f in fitted if len(f["hull"]) >= 3]
    for f in fitted:
        if len(f["hull"]) < 3 and f["pads"] and not re.match(r"(TP|H|MH|FID)\d", f["ref"]):
            findings.append(_f("info", "AS-NO-CRTYD", f["ref"], "Footprint has no courtyard."))
    pairs = []
    for i, a in enumerate(hulls):
        ax = [p[0] for p in a["hull"]]
        ay = [p[1] for p in a["hull"]]
        for b in hulls[i + 1:]:
            if a["side"] != b["side"]:
                continue
            bx = [p[0] for p in b["hull"]]
            by = [p[1] for p in b["hull"]]
            if min(bx) > max(ax) + min_spacing_mm + 0.01 or min(ax) > max(bx) + min_spacing_mm + 0.01 \
                    or min(by) > max(ay) + min_spacing_mm + 0.01 or min(ay) > max(by) + min_spacing_mm + 0.01:
                continue
            if courtyards_overlap(a, b):
                pairs.append((a["ref"], b["ref"], 0.0))
                findings.append(_f("warning", "AS-CRTYD-OVERLAP", f"{a['ref']},{b['ref']}",
                                   f"Courtyards of {a['ref']} and {b['ref']} overlap."))
            elif min_spacing_mm > 0:
                d = courtyard_distance(a, b)
                if d < min_spacing_mm:
                    pairs.append((a["ref"], b["ref"], round(d, 3)))
                    findings.append(_f("warning", "AS-CRTYD-SPACING", f"{a['ref']},{b['ref']}",
                                       f"Courtyards of {a['ref']} and {b['ref']} are {d:.2f} mm apart "
                                       f"(< {min_spacing_mm} mm)."))

    loops = board_outline_loops(tree)
    outline = loops[0] if loops else None
    edge = []
    if outline:
        for f in hulls:
            out_pts = [p for p in f["hull"] if not _inside(p[0], p[1], outline)]
            d = min((_seg_dist(p, outline[i], outline[(i + 1) % len(outline)])
                     for p in f["hull"] for i in range(len(outline))), default=math.inf)
            if out_pts or d < edge_clearance_mm:
                conn = bool(CONNECTOR_RE.match(f["ref"]))
                edge.append({"ref": f["ref"], "beyond_edge": bool(out_pts),
                             "edge_distance_mm": 0.0 if out_pts else round(d, 3)})
                findings.append(_f("info" if conn else "warning", "AS-EDGE", f["ref"],
                                   f"{f['ref']} courtyard "
                                   + ("extends beyond the board edge" if out_pts
                                      else f"is {d:.2f} mm from the edge (< {edge_clearance_mm} mm)")
                                   + (" (connector: usually intended)." if conn else
                                      ": depanelling / handling stress.")))

    for f in fitted:
        text = f"{f['ref']} {f['lib']} {f['props'].get('Value', '')}"
        if not (POLARIZED_RE.search(text) and f["pads"]):
            continue
        if f["silk_items"] == 0:
            findings.append(_f("info", "AS-SILK-POLARITY", f["ref"],
                               "Polarized part with no silkscreen graphic of its own (no outline / "
                               "pin-1 or polarity mark for assembly inspection)."))
        elif not pin1_marked(f):
            findings.append(_f("info", "AS-PIN1-MARK", f["ref"],
                               "Polarized part whose silkscreen has no mark nearer pad 1 than the "
                               "other pads (no pin-1 dot / cathode bar / chamfer found)."))

    height_rows = []
    if heights:
        from kicad_claude.utils.kicad_config import path_vars
        variables = path_vars()
        missing = defaultdict(list)
        for f in fitted:
            if not f["models"]:
                if f["pads"] and not re.match(r"(TP|H|MH|FID)\d", f["ref"]):
                    findings.append(_f("info", "AS-3D-NONE", f["ref"], "Footprint has no 3D model."))
                continue
            hs = [model_height(m, variables) for m in f["models"]]
            for m, (_, st) in zip(f["models"], hs):
                if st == "missing":
                    missing[m["path"]].append(f["ref"])
            known = [h for h, _ in hs if h is not None]
            if not known:
                continue
            h = max(known)
            approx = any(st == "rotated" for _, st in hs)
            row = {"ref": f["ref"], "side": f["side"], "height_mm": h, "approximate": approx}
            height_rows.append(row)
            limit = max_height_top_mm if f["side"] == "F" else max_height_bottom_mm
            if limit is not None and h > limit:
                findings.append(_f("warning", "AS-HEIGHT", f["ref"],
                                   f"{h:.2f} mm tall on the {'top' if f['side'] == 'F' else 'bottom'} "
                                   f"side, above the {limit} mm limit (enclosure clearance)"
                                   + (" (rotated model, approximate)" if approx else "") + "."))
            if h >= tall_mm and outline and len(f["hull"]) >= 3:
                d = min((_seg_dist(p, outline[i], outline[(i + 1) % len(outline)])
                         for p in f["hull"] for i in range(len(outline))), default=math.inf)
                if d < tall_edge_mm:
                    conn = bool(CONNECTOR_RE.match(f["ref"]))
                    findings.append(_f("info" if conn else "warning", "AS-TALL-EDGE", f["ref"],
                                       f"{h:.1f} mm tall part {d:.1f} mm from the board edge: "
                                       "exposed to handling, depanelling and wave-solder shadowing."))
        for path, refs in sorted(missing.items()):
            findings.append(_f("info", "AS-3D-MISSING", ",".join(refs[:10]),
                               f"3D model {path} does not resolve to a file: no height, no render."))
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda x: (order[x["severity"]], x["rule"], x["ref"]))
    return {"footprints": len(fps), "fitted": len(fitted), "smd": len(smd), "tht": len(tht),
            "fiducials": [f["ref"] for f in fid], "courtyard_conflicts": pairs,
            "edge": edge, "heights": sorted(height_rows, key=lambda r: -r["height_mm"]),
            "findings": findings,
            "counts": {k: sum(1 for x in findings if x["severity"] == k) for k in order}}


def cross_verify(tree: list, nl) -> dict:
    """KiCAD netlist (`power_review.Netlist`) vs the board."""
    fps = {f["ref"]: f for f in footprints(tree)}
    findings = []
    sch = nl.components
    for ref in sorted(set(sch) - set(fps)):
        if not sch[ref].get("dnp") and not str(sch[ref].get("footprint", "")) == "":
            findings.append(_f("error", "XV-MISSING", ref, "In the schematic, not on the board."))
    for ref in sorted(set(fps) - set(sch)):
        if not re.match(r"(H|MH|FID|LOGO|G|REF\*\*)", ref) and ref != "?":
            findings.append(_f("warning", "XV-EXTRA", ref, "On the board, not in the schematic."))
    pad_rows = []
    for ref in sorted(set(fps) & set(sch)):
        pins = {p.number for p in nl.pins.get(ref, [])}
        pads = {p["number"] for p in fps[ref]["pads"]
                if p["number"] and p["type"] != "np_thru_hole"}
        missing = sorted(pins - pads)
        extra = sorted(pads - pins)
        if missing:
            pad_rows.append({"ref": ref, "pins_without_pad": missing, "pads_without_pin": extra})
            findings.append(_f("error", "XV-PADS", ref,
                               f"Symbol pins {', '.join(missing)} have no pad of that number in "
                               f"footprint {fps[ref]['lib']}"
                               + (f" (unmatched pads: {', '.join(extra)})" if extra else "") + "."))
        elif extra:
            pad_rows.append({"ref": ref, "pins_without_pad": [], "pads_without_pin": extra})
        s, b = sch[ref], fps[ref]
        checks = (("Value", s.get("value", ""), b["props"].get("Value", "")),
                  ("Footprint", s.get("footprint", ""), b["lib"]),
                  ("MPN", (s.get("fields") or {}).get("MPN", ""), b["props"].get("MPN", "")))
        for name, sv, bv in checks:
            if sv and bv and sv != bv:
                findings.append(_f("warning", "XV-FIELD", ref,
                                   f"{name}: schematic {sv!r} vs board {bv!r} "
                                   "(update_pcb_from_schematic scope='fields')."))
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda x: (order[x["severity"]], x["rule"], x["ref"]))
    return {"pad_mismatches": pad_rows, "findings": findings,
            "counts": {k: sum(1 for x in findings if x["severity"] == k) for k in order}}
